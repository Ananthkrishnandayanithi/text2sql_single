from flask import Flask, render_template, request, send_from_directory
import os
import subprocess
from langchain_experimental.sql import SQLDatabaseChain
from langchain.utilities import SQLDatabase
from langchain.embeddings import HuggingFaceEmbeddings
from langchain.chains.sql_database.prompt import PROMPT_SUFFIX
from langchain_community.vectorstores import Chroma
from langchain_google_genai import ChatGoogleGenerativeAI
from dotenv import load_dotenv
from langchain.prompts import PromptTemplate, SemanticSimilarityExampleSelector
from langchain.tools.sql_database.tool import QuerySQLDataBaseTool
from langchain_core.runnables import RunnablePassthrough
from langchain_core.output_parsers import StrOutputParser
from operator import itemgetter
import mysql.connector
import json
import pandas as pd
from datetime import datetime
import time
import shutil
from pathlib import Path
import importlib
from contextlib import contextmanager
from typing import Optional
import grpc
from sqlalchemy import Column, Integer, DateTime, Text, String, Float
from flask_sqlalchemy import SQLAlchemy
import pandas as pd
from werkzeug.utils import secure_filename
import os
from sqlalchemy import create_engine, inspect
import numpy as np
from flask import jsonify, flash
import traceback
from werkzeug.middleware.proxy_fix import ProxyFix
import logging
from logging.handlers import RotatingFileHandler
from waitress import serve
from sqlalchemy import create_engine, text

# Initialize Flask app and Database
if not os.path.exists('logs'):
    os.makedirs('logs')

logging.basicConfig(
    handlers=[RotatingFileHandler('logs/app.log', maxBytes=100000, backupCount=5)],
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s in %(module)s: %(message)s'
)
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Database Configuration
# Set default database URI for storing application data (query history, etc.)
app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+pymysql://aiuser:Entrans1@13.235.9.120:3306/salesdb'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
# Configure database bindings for different data sources
app.config['SQLALCHEMY_BINDS'] = {
    'default': 'mysql+pymysql://aiuser:Entrans1@13.235.9.120:3306/salesdb',
    'data_db': 'mysql+pymysql://aiuser:Entrans1@13.235.9.120:3306/salesdb',
    'query_history': 'mysql+pymysql://aiuser:Entrans1@13.235.9.120:3306/llm_outputs_db'
}

# Additional SQLAlchemy configurations
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_size': 10,
    'pool_recycle': 3600,
    'pool_pre_ping': True
}

# Initialize SQLAlchemy
db = SQLAlchemy(app)


# Add these configurations after existing app configurations
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'csv', 'xlsx'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
def allowed_file(filename):
    """Check if the file has an allowed extension"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'csv', 'xlsx', 'xls'}

import re
def clean_column_names(df):
    """Clean column names to be SQL-friendly"""
    # Replace spaces with underscores and remove special characters
    df.columns = [re.sub(r'[^\w]', '_', col).strip('_').lower() for col in df.columns]
    # Ensure no duplicate column names by adding numbering
    seen = {}
    for i, col in enumerate(df.columns):
        if col in seen:
            df.columns.values[i] = f"{col}_{seen[col]}"
            seen[col] += 1
        else:
            seen[col] = 1
    return df

def get_sql_type(dtype):
    if pd.api.types.is_integer_dtype(dtype):
        return 'INTEGER'
    elif pd.api.types.is_float_dtype(dtype):
        return 'FLOAT'
    elif pd.api.types.is_datetime64_any_dtype(dtype):
        return 'DATETIME'
    else:
        return 'TEXT'

def csv_to_sql(file_path, base_table_name, db_uri):
    """Convert CSV or Excel sheets to SQL tables"""
    excel_file = None  # Initialize variable to hold ExcelFile object
    try:
        engine = create_engine(db_uri)
        inspector = inspect(engine)
        
        file_ext = os.path.splitext(file_path)[1].lower()
        
        # Detect file type and load sheets
        if file_ext == '.csv':
            # For CSV files, treat as a single sheet
            df = pd.read_csv(file_path)
            sheets = {'main': df}  # Use 'main' as the sheet name for CSV
        elif file_ext in ['.xlsx', '.xls']:
            # For Excel files, load all sheets
            excel_file = pd.ExcelFile(file_path)
            sheet_names = excel_file.sheet_names
            
            if not sheet_names:
                # Close file if open
                if excel_file is not None:
                    excel_file.close()
                return False, "Excel file contains no sheets"
                
            sheets = {}
            for sheet_name in sheet_names:
                sheets[sheet_name] = excel_file.parse(sheet_name)
        else:
            return False, "Unsupported file format. Only .csv, .xlsx, and .xls are allowed."

        created_tables = []
        
        # Process each sheet
        for sheet_name, df in sheets.items():
            # Skip empty sheets
            if df.empty:
                continue
                
            # Create unique table name for each sheet
            if file_ext == '.csv' or len(sheets) == 1:
                # For CSV or Excel with single sheet, use the base table name
                table_name = base_table_name
            else:
                # For Excel with multiple sheets, append sheet name
                safe_sheet_name = re.sub(r'[^\w]', '_', sheet_name).strip('_').lower()
                table_name = f"{base_table_name}_{safe_sheet_name}"
                
                # Ensure table name doesn't exceed MySQL's limit (64 chars)
                if len(table_name) > 64:
                    table_name = table_name[:60] + "_" + str(hash(sheet_name) % 1000)
            
            # Clean column names
            df = clean_column_names(df)
            
            # Check if table already exists
            if table_name in inspector.get_table_names():
                # Option 2: Drop existing table and recreate
                with engine.connect() as conn:
                    conn.execute(text(f"DROP TABLE IF EXISTS `{table_name}`"))
                    conn.commit()
            
            # Create column definitions based on DataFrame data types
            column_definitions = []
            for col, dtype in df.dtypes.items():
                sql_type = get_sql_type(dtype)
                column_definitions.append(f"`{col}` {sql_type}")
            
            # Create table
            create_table_sql = f"""
            CREATE TABLE `{table_name}` (
                `id` INT AUTO_INCREMENT PRIMARY KEY,
                {', '.join(column_definitions)}
            );
            """
            
            with engine.connect() as conn:
                conn.execute(text(create_table_sql))
                conn.commit()
            
            # Insert data
            df.to_sql(table_name, engine, if_exists='append', index=False)
            created_tables.append(table_name)
        
        if not created_tables:
            return False, "No tables were created. All sheets may be empty."
        
        # Make sure to close Excel file before returning
        if excel_file is not None:
            excel_file.close()
            
        return True, f"Successfully created {len(created_tables)} tables: {', '.join(created_tables)}"
        
    except pd.errors.EmptyDataError:
        return False, "The file is empty"
    except pd.errors.ParserError:
        return False, "Error parsing the file. Please check the file format."
    except Exception as e:
        return False, f"Error processing file: {str(e)}"
    finally:
        # Ensure excel_file is closed even if an exception occurs
        if excel_file is not None:
            try:
                excel_file.close()
            except:
                pass


def get_sql_type(pandas_dtype):
    """Map pandas dtypes to SQL data types"""
    dtype_str = str(pandas_dtype)
    if 'int' in dtype_str:
        return 'INT'
    elif 'float' in dtype_str:
        return 'FLOAT'
    elif 'datetime' in dtype_str:
        return 'DATETIME'
    elif 'bool' in dtype_str:
        return 'BOOLEAN'
    else:
        return 'TEXT'


# Add this helper function to validate table names
def validate_table_name(name):
    """Validate table name contains only letters, numbers, and underscores"""
    return bool(re.match(r'^[a-zA-Z0-9_]+$', name))

# LLMQuery Model Definition
class LLMQuery(db.Model):
    __bind_key__ = 'query_history'  # Specify the database binding
    __tablename__ = 'llm_queries'

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    user_question = Column(Text, nullable=False)
    generated_sql_query = Column(Text)
    llm_answer = Column(Text)
    visualization_recommendation = Column(Text)
    plot_file_path = Column(String(255))
    output_type = Column(String(50))
    database_type = Column(String(50))
    
    # New columns
    generated_plot_code = Column(Text)  # Store the full Python plot generation code
    total_execution_time = Column(Float)  # Store total execution time in seconds

    def __repr__(self):
        return f'<LLMQuery {self.id}: {self.user_question[:50]}>'

# Load environment variables
load_dotenv()

# Database connection config'mysql+pymysql://aiuser:Entrans1@13.235.9.120:3306/salesdb'
db_user = "aiuser"
db_password = "Entrans1"
db_host = "13.235.9.120"
db_name = "salesdb"

@contextmanager
def timeout_context(timeout_seconds: int = 30):
    """Context manager to handle query timeouts"""
    start_time = time.time()
    yield
    if time.time() - start_time > timeout_seconds:
        raise TimeoutError("Query execution timed out")

def ensure_static_directory():
    """Ensure static directory exists and is clean"""
    static_dir = Path("static")
    if static_dir.exists():
        # Clean the directory
        shutil.rmtree(static_dir)
    static_dir.mkdir(exist_ok=True)

def ensure_sql_server_active():
    """Check if SQL Server is running and attempt to start it if not."""
    try:
        # Try connecting to the database
       conn = mysql.connector.connect(
            host="13.235.9.120",
            port=3306,
            user="aiuser",
            password="Entrans1",
        )
       conn.close()
       print("SQL Server is active.")
    except mysql.connector.Error as e:
        print("SQL Server is not active. Attempting to start it...")
        # Command to start SQL Server (adjust based on your system's configuration)
        start_command = "sudo systemctl start mysql"  # For Linux systems
        try:
            subprocess.run(start_command, shell=True, check=True)
            time.sleep(5)  # Give the server time to start
            print("SQL Server started successfully.")
        except subprocess.CalledProcessError as start_error:
            print(f"Failed to start SQL Server: {start_error}")
            raise RuntimeError("SQL Server could not be started. Please start it manually.")

# Ensure SQL Server is active before initializing the Flask app
ensure_sql_server_active()

# Initialize SQL database
db_connection = SQLDatabase.from_uri(
    f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}",
    sample_rows_in_table_info=3,
)

def print_schema_info(database):
    schema_info = database.get_table_info()
    print("Database Schema Information:")
    print(schema_info)

# Call the function to print the schema info
print_schema_info(db_connection)
def refresh_database_connections():
    """Refresh database connections and embeddings only when necessary"""
    global db_connection, vectorstore
    
    try:
        print("Refreshing database connections...")
        
        # Refresh main database connection
        db_connection = SQLDatabase.from_uri(
            'mysql+pymysql://aiuser:Entrans1@13.235.9.120:3306/salesdb',
            sample_rows_in_table_info=3,
            include_tables=None
        )
        
        # Force regeneration of embeddings only if a new CSV was uploaded
        vectorstore = initialize_schema_embeddings(db_connection, force_refresh=True)
        
        # Verify the refresh was successful
        if not verify_database_connection():
            raise Exception("Failed to verify database connection after refresh")
        
        # Force garbage collection
        import gc
        gc.collect()
        
        return True, "Database connections refreshed successfully"
        
    except Exception as e:
        error_msg = f"Error refreshing connections: {str(e)}"
        print(error_msg)
        return False, error_msg

# Initialize HuggingFace embeddings
model_name = "BAAI/bge-small-en-v1.5"

embeddings = HuggingFaceEmbeddings(
    model_name=model_name
)
def generate_table_summary(llm, table_name: str, columns_info: str) -> str:
    """Generate a semantic summary of the table using LLM with error handling."""
    summary_prompt = PromptTemplate.from_template(
        """
        As a database expert, analyze this table schema and generate a detailed semantic summary.
        
        Table Name: {table_name}
        Column Information: {columns_info}
        
        ## Instructions for Handling Errors:
        - If the `Column Information` contains an error message (e.g., "Error: table_names..."), it means the table schema could not be retrieved. 
        - In such cases, **instead of repeating the error message**, provide a general summary stating that the table information is unavailable and further investigation is needed.
        - **Do not include any part of the error message in your summary.**
        
        ## Desired Summary Format:
        1. The main purpose and domain of this table
        2. Key relationships and data types
        3. Important business metrics or KPIs it might contain
        4. Potential use cases for analysis
        
        Provide the summary in 3-4 concise sentences.
        """
    )

    try:
        summary = llm.invoke(
            summary_prompt.format(
                table_name=table_name,
                columns_info=columns_info
            )
        ).content
        print(f"\nGenerated summary for table {table_name}:")
        print(summary)
        return summary
    except Exception as e:
        print(f"Error generating summary for table {table_name}: {str(e)}")
        # Fallback if LLM call fails
        return f"Table '{table_name}' summary could not be generated due to an error. Please check the LLM configuration and input."
# Modify the initialize_schema_embeddings function for better logging and error handling
import mysql.connector

def get_column_info(table_name):
    """Retrieve column information using database-agnostic SQL."""
    try:
        # Determine the database type from the connection URI
        db_uri = app.config['SQLALCHEMY_DATABASE_URI']
        if 'mysql' in db_uri:
            # MySQL-specific query
            query = f"SHOW COLUMNS FROM `{table_name}`"
        elif 'postgresql' in db_uri:
            # PostgreSQL-specific query
            query = f"""
                SELECT column_name, data_type 
                FROM information_schema.columns 
                WHERE table_name = '{table_name}'
            """
        else:
            raise ValueError("Unsupported database type")

        # Execute the query
        with db.engine.connect() as conn:
            result = conn.execute(text(query))
            columns_info = result.fetchall()

        # Format the columns information
        if 'mysql' in db_uri:
            columns_str = "\n".join([f"- {col[0]}: {col[1]}" for col in columns_info])
        elif 'postgresql' in db_uri:
            columns_str = "\n".join([f"- {col[0]}: {col[1]}" for col in columns_info])
        else:
            columns_str = "Unsupported database type"

        return columns_str

    except Exception as e:
        print(f"Error getting column info for {table_name}: {e}")
        return f"Error: Could not retrieve column information for {table_name}"

from typing import List, Optional
import torch
from transformers import AutoTokenizer, AutoModel
import numpy as np
from langchain.embeddings.base import Embeddings

class ColBERTEmbeddings(Embeddings):
    def __init__(
        self,
        model_name: str = "colbert-ir/colbertv2.0",
        max_length: int = 512,
        device: Optional[str] = None,
        target_dimension: int = 768  # Change to 768 
    ):
        self.model_name = model_name
        self.max_length = max_length
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.target_dimension = target_dimension
        
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(model_name)
            self.model = AutoModel.from_pretrained(model_name)
            self.model.to(self.device)
            self.model.eval()
            
            # Add dimension reduction layer
            self.dimension_reduction = torch.nn.Linear(
                768,  # ColBERT's output dimension
                target_dimension  # Target dimension for Chroma
            ).to(self.device)
            
        except Exception as e:
            raise ValueError(f"Error loading ColBERT model: {str(e)}")

    def _tokenize_and_embed(self, text: str) -> np.ndarray:
        try:
            # Tokenize the text
            inputs = self.tokenizer(
                text,
                max_length=self.max_length,
                padding=True,
                truncation=True,
                return_tensors="pt"
            )
            inputs = {k: v.to(self.device) for k, v in inputs.items()}

            # Generate embeddings
            with torch.no_grad():
                outputs = self.model(**inputs)
                # Use the last hidden state
                embeddings = outputs.last_hidden_state.mean(dim=1)
                # Reduce dimension
                reduced_embeddings = self.dimension_reduction(embeddings)
                
            return reduced_embeddings.cpu().numpy()[0]

        except Exception as e:
            raise ValueError(f"Error generating embeddings: {str(e)}")

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        embeddings = []
        for text in texts:
            try:
                embedding = self._tokenize_and_embed(text)
                embeddings.append(embedding.tolist())
            except Exception as e:
                print(f"Error embedding document: {str(e)}")
                # Provide a zero embedding as fallback
                embeddings.append([0.0] * self.target_dimension)
        return embeddings

    def embed_query(self, text: str) -> List[float]:
        try:
            embedding = self._tokenize_and_embed(text)
            return embedding.tolist()
        except Exception as e:
            print(f"Error embedding query: {str(e)}")
            return [0.0] * self.target_dimension
 
def initialize_schema_embeddings(database, force_refresh=False):
    """Initialize schema embeddings with ColBERT only if necessary."""
    try:
        print("Checking schema embeddings status...")
        
        # Check if embeddings already exist
        schema_store_path = Path("./schema_store")
        if schema_store_path.exists() and not force_refresh:
            print("Loading existing schema embeddings...")
            # Initialize ColBERT embeddings
            embeddings = ColBERTEmbeddings(
                model_name="colbert-ir/colbertv2.0",
                max_length=512
            )
            # Load existing vectorstore
            return Chroma(
                persist_directory="./schema_store",
                embedding_function=embeddings,
                collection_metadata={"hnsw:space": "cosine", "dimension": 384}
            )

        print("Initializing new schema embeddings with ColBERT...")
        
        # Initialize ColBERT embeddings
        embeddings = ColBERTEmbeddings(
            model_name="colbert-ir/colbertv2.0",
            max_length=512
        )

        tables = database.get_usable_table_names()
        print(f"Found tables: {tables}")

        if not tables:
            raise ValueError("No tables found in database")

        schema_texts = []
        metadata = []

        # Initialize LLM
        api_key = os.getenv("GOOGLE_GENERATIVE_AI_KEY")
        llm = ChatGoogleGenerativeAI(
            model="gemini-1.5-pro-002",
            temperature=0.2,
            api_key=api_key,
        )

        for table in tables:
            try:
                columns_str = get_column_info(table)
                table_summary = generate_table_summary(llm, table, columns_str)
                
                schema_text = f"""
                Table: {table}
                Summary: {table_summary}
                Columns and Types: {columns_str}
                Sample Data Summary: Table contains data about {table.replace('_', ' ').lower()} 
                """

                schema_texts.append(schema_text)
                metadata.append({"table": table, "summary": table_summary})

            except Exception as e:
                print(f"Error processing table {table}: {e}")
                table_summary = f"Table '{table}' summary could not be generated due to an error."
                schema_texts.append(f"Table: {table}\nSummary: {table_summary}")
                metadata.append({"table": table, "summary": table_summary})

        # Create new vector store with ColBERT embeddings
        vectorstore = Chroma.from_texts(
            texts=schema_texts,
            embedding=embeddings,
            metadatas=metadata,
            persist_directory="./schema_store",
            collection_metadata={"hnsw:space": "cosine", "dimension": 384}
        )

        print("Schema embeddings initialized successfully with ColBERT")
        return vectorstore

    except Exception as e:
        print(f"Error initializing schema embeddings: {str(e)}")
        raise
        
schema_constraints = {}
def get_relevant_tables(question, vectorstore, k=2):
    """
    Find relevant tables with improved selection based on multiple factors.
    """
    try:
        print(f"\nSearching for tables relevant to question: {question}")

        all_tables = db_connection.get_usable_table_names()
        print(f"Available tables: {all_tables}")

        # Perform similarity search
        results = vectorstore.similarity_search_with_relevance_scores(question, k=k)
        
        table_scores = {}
        for doc, score in results:
            table_name = doc.metadata["table"]
            table_summary = doc.metadata.get("summary", "No summary available")
            
            # Assign weight based on relevance score
            table_scores[table_name] = score * 0.6  # 60% weight for semantic match

            # Additional weight for direct keyword matches
            for word in question.split():
                if word.lower() in table_name.lower():
                    table_scores[table_name] += 0.3  # 30% weight for direct match

        # Sort tables based on scores
        sorted_tables = sorted(table_scores.items(), key=lambda x: x[1], reverse=True)
        relevant_tables = [t[0] for t in sorted_tables[:k]]

        print(f"Selected tables: {relevant_tables}")
        return relevant_tables

    except Exception as e:
        print(f"Error in get_relevant_tables: {str(e)}")
        return []


def verify_database_connection():
    """Verify database connection and schema initialization"""
    try:
        # Test database connection
        tables = db_connection.get_usable_table_names()
        if not tables:
            print("No tables found in database")
            return False
            
        # Test vectorstore
        test_question = "show me all tables"
        try:
            results = vectorstore.similarity_search_with_relevance_scores(test_question, k=1)
            if not results:
                print("No results from vectorstore")
                return False
        except Exception as e:
            print(f"Vectorstore test failed: {e}")
            return False
            
        return True
        
    except Exception as e:
        print(f"Database verification failed: {str(e)}")
        return False
# Initialize schema embeddings
vectorstore = initialize_schema_embeddings(db_connection)
api_key = os.getenv("GOOGLE_GENERATIVE_AI_KEY")
# Set up the LLM model (Google Gemini)
llm = ChatGoogleGenerativeAI(
    model="gemini-1.5-pro-002",
    temperature=0.2,
    api_key=api_key,
)

# Prompt templates (schema_aware_query_prompt, answer_prompt, viz_prompt, plot_prompt remain the same as in previous implementation)
schema_aware_query_prompt = PromptTemplate.from_template(
    """
    As a database expert, your task is to generate a precise, optimized, and schema-compliant SQL query to answer the given question. Ensure the output directly aligns with the expected structure and logic.  

    **Inputs:**  
    1. **Question:** {question}  
    2. **Relevant Tables:** {relevant_tables}  
    3. **Full Database Schema:** {schema}  

    **Key Directives:**  

    1. **Strict Schema Adherence:**  
    - Use **only** the columns and tables specified in the `"Relevant Tables"` input.  
    - Do **not** infer or rename table/column names; use them **exactly as provided**.  
    - Strictly do not make any assumptions, modifications, or renaming of table or column names. Use them exactly as they appear in the database. Verify twice before generating any response (e.g., do not replace newsales1 with sales).
    
    2. **JOIN Accuracy & Optimization:**  
    - Ensure that all `JOIN` conditions correctly use **primary-foreign key relationships**.  
    - Use `INNER JOIN` unless an `OUTER JOIN` is **explicitly required**.  
    - Avoid unnecessary joins and **only retrieve the required fields**.  

    3. **Time-Series Analysis & Aggregation:**  
    - If the question involves **trend analysis over time**, use **time-based window functions** (e.g., `AVG() OVER`, `SUM() OVER`).  
    - Ensure **date/time columns** are used correctly with `ORDER BY` for sequential calculations.  
    - When computing moving averages, use **time-based windowing** (e.g., `RANGE INTERVAL 'N' HOUR PRECEDING`).  

    4. **Handling Date & Time in MySQL:**  
    - **Use `DATE_FORMAT()` instead of `STRFTIME()`** for formatting dates/times in MySQL.  
    - Example:
      ```sql
      DATE_FORMAT(Time, '%H') AS Sale_Hour
      ```
    - Ensure proper parsing when working with timestamps (e.g., `STR_TO_DATE()` for correct conversions).  

    5. **Sorting, Ranking, and Limits:**  
    - If ranking is required, use `ORDER BY <column> DESC`.  
    - If only the **top N results** are needed, include a `LIMIT` clause.  

    6. **Field Completeness in Output:**  
    - Ensure **all necessary columns** appear in the final output (e.g., **include `Item_Name` when using `Item_Code`**).  
    - Do **not omit** key fields that impact result interpretation.  

    7. **Alias Matching & Readability:**  
    - Use **descriptive aliases** for calculated fields (e.g., `AS Moving_Average_Revenue`).  
    - Maintain consistent formatting and indentation to enhance readability.  

    8. **Performance Optimization:**  
    - **Avoid unnecessary subqueries** unless required for filtering or ranking.  
    - **Prefer joins over subqueries** when fetching related data.  
    - Use **WHERE conditions** efficiently to filter data at an early stage.  

    9. **Formatting Best Practices:**  
    - Format the SQL query with **consistent capitalization, indentation, and spacing** for enhanced readability and best practices.  

    **Example Query for Time-Series Revenue Analysis:**  
    ```sql
    SELECT 
        DATE(s2.`Date`) AS Sale_Date,
        DATE_FORMAT(s2.`Time`, '%H') AS Sale_Hour,
        SUM(s2.Quantity_Sold * s2.Unit_Selling_Price) AS Total_Revenue
    FROM newsale2 AS s2
    GROUP BY Sale_Date, Sale_Hour
    ORDER BY Sale_Date, Sale_Hour
    LIMIT 1000;
    ```

    **Example Query for Aggregated Sales Data:**  
    ```sql
    SELECT 
        s1.Item_Name, 
        SUM(s2.Quantity_Sold) AS Total_Quantity_Sold, 
        SUM(s2.Quantity_Sold * s2.Unit_Selling_Price) AS Total_Revenue 
    FROM 
        newsale1 s1
    JOIN 
        newsale2 s2 ON s1.Item_Code = s2.Item_Code
    GROUP BY 
        s1.Item_Name
    ORDER BY 
        Total_Quantity_Sold DESC
    LIMIT 10;
    ```

    Generate the SQL query directly, ensuring it adheres to MySQL syntax, best practices, and the example structure. Avoid redundancy, and make the output directly usable without additional modification.
    """
)
# Add this after the existing prompt templates
sql_validation_prompt = PromptTemplate.from_template(
    """
    You are a SQL Query Validator. Your task is to analyze the generated SQL query for potential issues and validate its correctness.
    
    Generated Query:
    {query}
    
    Original Question:
    {question}
    
    Database Schema:
    {schema}
    
    Validation Rules:
    1. Syntax Check:
       - Verify SQL keywords and clauses are used correctly
       - Check for proper table and column name references
       - Validate parentheses and operator usage
    
    2. Schema Compliance:
       - Confirm all referenced tables exist in the schema
       - Verify all column names exist in their respective tables
       - Check JOIN conditions use correct column relationships
    
    3. Logic Validation:
       - Ensure the query logic matches the original question
       - Verify aggregations and groupings are appropriate
       - Check if WHERE conditions are logical
    
    4. Performance Considerations:
       - Flag potential performance issues
       - Check for unnecessary JOINs or subqueries
       - Verify proper indexing opportunities
    
    5. Common Pitfalls:
       - Check for proper handling of NULL values
       - Verify date/time operations
       - Validate numeric operations and type conversions
    
    Return a JSON response with:
    {
        "is_valid": boolean,
        "issues": [list of identified issues],
        "suggestions": [list of improvements],
        "modified_query": "corrected query if issues found, otherwise original"
    }
    
    If the query is valid and optimal, return empty lists for issues and suggestions.
    """
)
# Answer prompt template
answer_prompt = PromptTemplate.from_template(
    """
    You are a highly intelligent AI assistant tasked with analyzing SQL query results to provide precise and insightful answers to user questions.
    Format your response with clear table structure and analysis.

    Question: {question}
    SQL Query: {query}
    SQL Result: {result}

    Format your response in this exact structure:
    1. Create a markdown table with the results
    2. Provide analysis after the table
    3. Ensure the table and analysis are separated by blank lines
    
    Important:
    - Always start with the table header using | Column1 | Column2 | format
    - Use proper table formatting with | separator
    - Include a row of |---| separators after the header
    - Present all numerical data aligned right in the table
    - After the table, provide concise analysis of the key findings
    
    Response Format Example:
    | Department | Count |
    |------------|-------|
    | HR | 10 |
    | IT | 20 |

    Analysis shows that IT has twice as many employees as HR...

    Answer:
    """
)

# Visualization prompt template
viz_prompt = PromptTemplate.from_template(
    """
    You are a Data Visualization Assistant. Your task is to analyze the given data analysis question and result, recommend the most suitable visualization type, and explain your choice briefly.

    Instructions:
    - Based on the provided question and result, recommend a single most appropriate visualization type.
    - Provide a concise explanation (2–3 sentences) for why this graph type is ideal.
    - Ensure your response follows this exact format:
      Recommended Graph: [graph type]
      Reasoning: [your explanation]
    - Your response must be a plain string with no special characters, code blocks, or message objects.

    Example:
    Question: "How has sales revenue changed over the past year?"
    Result: "Monthly revenue data for the past year"
    Recommended Graph: Line Chart
    Reasoning: A line chart is ideal for showing trends over time, making it easy to visualize changes in revenue across months.

    Now, based on the input provided:
    Question: {question}
    Result: {result}
    """
)

# Plot prompt template
plot_prompt = PromptTemplate.from_template(
    """You are an **Intelligent Plotting Code Generator**, specialized in creating robust and error-free Python code using pandas and Plotly.
    
    Your task is to generate Python plotting code based on the provided data and user query. Follow these guidelines carefully:

    ### **General Requirements**:
    1. **Executable Python Code**:
        - The generated code must be **runnable as-is**, without requiring additional encoding declarations.
        - Avoid encoding-related errors such as `SyntaxError: Non-UTF-8 code...` by ensuring all strings and data are strictly UTF-8 compliant.
        - Do **not** include explanations, markdown, or unnecessary formatting—return only the Python code.

    2. **Input Data Structure**:
        - The data is provided as a list of tuples, where each tuple represents a row. Example:  
          `[('Category 1', 10), ('Category 2', 20), ('Category 3', 30)]`
        - Tuples may contain multiple elements, including **categories, timestamps, and numerical values**.

    3. **Plot Type Selection**:
        Generate one of the following plot types based on the query:
        - **Bar Plot**: Use for comparing discrete categories. The x-axis represents categories, and the y-axis represents values. Sort bars in ascending order by values.
        - **Pie Chart**: Use for showing proportions. Generate a percentage-based pie chart where categories are labels and values represent the sizes. Display percentages on the chart.
        - **Histogram**: Use for displaying the frequency distribution of a continuous variable. Bins should be created from the data, with counts represented as bars.
        - **Line Plot**: Use for showing trends over time. The x-axis represents the sequential variable (e.g., years), and the y-axis represents the metric (e.g., average salary). Include markers for each data point, with a connecting line.
        - **Scatter Plot**: Use for analyzing relationships between two continuous variables. Map one variable to the x-axis and another to the y-axis. Optionally, encode a third variable using marker size or color.
        - **Time Series Graph**: Use for data that is based on or related to time. The x-axis should represent the time or date (e.g., daily, monthly, yearly,hours), and the y-axis should represent the metric or value over time.
    4. **Data Processing & Validation**:
        - Convert necessary fields (e.g., timestamps) to the correct data type before plotting.
        - Ensure input data does not contain missing, empty, or malformed tuples.
        - If duplicate values exist, **aggregate or count occurrences** unless the user specifies otherwise.

    5. **Error Handling & Logging**:
        - Wrap the entire logic in a `try-except` block.
        - Use Python’s `logging` module to log meaningful error messages for any parsing or plotting issues.

    6. **Edge Cases**:
        - Handle cases where the dataset is **empty** or contains only one entry.
        - If timestamps are present, **combine date and time fields correctly** to ensure proper chronological ordering.

    7. **Plot Saving Mechanism**:
        - Save the generated plot as `static/plot.png` **without using `fig.write_image('static/plot.png')`**.
        - Use an appropriate Plotly method to ensure the image is stored correctly.

    ---
    ### **Input:**
    - **User Query**: {question}
    - **Data**: {result}
    - **Columns**: {columns}

    ### **Expected Output:**
    Return only **valid Python code** that:
    - **Imports** necessary libraries (`pandas`, `plotly.graph_objects`, `logging`).
    - **Processes** the data correctly (e.g., converts timestamps, calculates moving averages).
    - **Generates** the required plot with proper axes, labels, and formatting.
    - **Handles** errors, logging meaningful messages.
    - **Saves** the output image to `static/plot.png`.

    Ensure the final code meets these criteria without deviation.
    """
)


def validate_sql_query(query: str, question: str, schema: str) -> dict:
    """
    Validate the generated SQL query using the validation prompt.
    Returns a dictionary with validation results.
    """
    try:
        validation_response = llm.invoke(
            sql_validation_prompt.format(
                query=query,
                question=question,
                schema=schema
            )
        ).content
        
        # Parse the response into a dictionary
        import json
        validation_result = json.loads(validation_response)
        
        return validation_result
    except Exception as e:
        print(f"Query validation error: {str(e)}")
        return {
            "is_valid": False,
            "issues": [f"Validation error: {str(e)}"],
            "suggestions": [],
            "modified_query": query
        }


def safe_execute_query(query: str, db: SQLDatabase) -> Optional[str]:
    """Execute SQL query safely with performance optimization."""
    try:
        # Check if query is optimized before execution
        explain_query = f"EXPLAIN {query}"
        with timeout_context(30):  
            explain_result = db.run(explain_query)
            print(f"EXPLAIN result:\n{explain_result}")

        # Execute the actual query
        result = db.run(query)
        return result
    except Exception as e:
        print(f"Query execution error: {str(e)}")
        return None


def generate_query_with_schema(inputs):
    relevant_tables = get_relevant_tables(inputs["question"], vectorstore)

    # If multiple equally relevant tables, request clarification
    if len(relevant_tables) > 1:
        return {
            "clarification_needed": True,
            "message": f"Multiple tables seem relevant: {', '.join(relevant_tables)}. Can you specify which table you need?"
        }

    schema_info = db_connection.get_table_info()
    query = llm.invoke(
        schema_aware_query_prompt.format(
            question=inputs["question"],
            relevant_tables=relevant_tables,
            schema=schema_info
        )
    ).content

    return query


def clean_query(query):
    query = remove_limit_from_query(query)
    cleaned_query = query.replace("```sql", "").replace("```", "").strip()

    if os.path.exists("generated_sql_queries.sql"):
        os.remove("generated_sql_queries.sql")

    with open("generated_sql_queries.sql", "a") as f:
        f.write(f"-- Generated Query:\n{cleaned_query}\n\n")
    
    return cleaned_query

def remove_limit_from_query(query):
    if "LIMIT" in query.upper():
        query = query.rsplit("LIMIT", 1)[0]
    return query

def get_relevant_columns(db_config, question):
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor()
    
    cursor.execute("""SELECT TABLE_NAME, COLUMN_NAME 
                      FROM INFORMATION_SCHEMA.COLUMNS 
                      WHERE TABLE_SCHEMA = %s""", (db_config['database'],))
    
    columns = cursor.fetchall()
    cursor.close()
    conn.close()
    
    return json.dumps(dict(columns))

def generate_plot_code(llm_response):
    """Generate plot code from LLM response with proper error handling"""
    try:
        # Ensure static directory is ready
        ensure_static_directory()

        # Extract the content if it's an AIMessage object
        if hasattr(llm_response, 'content'):
            plot_code = llm_response.content
        else:
            plot_code = str(llm_response)

        print("LLM Response for Plot Code:", plot_code)

        # Clean the code
        cleaned_response = plot_code.replace("```python", "").replace("```", "").strip()
        cleaned_response = cleaned_response.replace(
            "fig.write_html(",
            "# fig.write_html("
        ).replace(
            "fig.show()",
            "# fig.show()"
        ).replace(
            "offline.init_notebook_mode()",
            "# offline.init_notebook_mode()"
        )


        # Remove old generate_plot.py if it exists
        if os.path.exists('generate_plot.py'):
            os.remove('generate_plot.py')

        # Write the complete code to file
        with open('generate_plot.py', 'w', encoding='utf-8') as f:
            f.write("# -*- coding: utf-8 -*-\n")  # Add the UTF-8 encoding declaration
            f.write("import plotly.express as px\n")
            f.write("import pandas as pd\n")
            f.write("import datetime\n")
            f.write("import plotly.io as pio\n\n")  # Add extra newline for clarity
            f.write(cleaned_response + "\n")  # Ensure the cleaned_response content ends with a newline

        return cleaned_response
    except Exception as e:
        print(f"Error in generate_plot_code: {str(e)}")
        return None

def save_to_csv(question, answer):
    filename = "llm_answers.csv"
    if os.path.isfile(filename):
        os.remove(filename)
    
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data = {"timestamp": [timestamp], "question": [question], "answer": [answer]}
    df = pd.DataFrame(data)
    
    df.to_csv(filename, index=False)

def save_log(question, answer):
    """Save question and answer log to a CSV file"""
    filename = "question_answer_log.csv"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    if os.path.isfile(filename):
        df = pd.read_csv(filename)
    else:
        df = pd.DataFrame(columns=["timestamp", "question", "answer"])
    
    new_data = pd.DataFrame({"timestamp": [timestamp], "question": [question], "answer": [answer]})
    df = pd.concat([df, new_data], ignore_index=True)
    df.to_csv(filename, index=False)
def get_db_connection():
    try:
        with app.app_context():
            db.engine.connect()
            return True
    except Exception as e:
        app.logger.error(f"Database connection error: {e}")
        return False
def generate_plot_file(plot_code):
    """Generate plot file with proper module reloading"""
    try:
        # Remove existing plot file
        plot_path = 'static/plot.png'
        if os.path.exists(plot_path):
            os.remove(plot_path)

        # Force Python to reload the generate_plot module
        import generate_plot
        importlib.reload(generate_plot)
        
        # Ensure the plot was generated
        if not os.path.exists(plot_path):
            raise Exception("Plot file was not generated")
            
        return plot_path
    except Exception as e:
        print(f"Error generating plot: {str(e)}")
        return None

# Database config
db_config = {
    'host': "13.235.9.120",
    'port': 3306,
    'user': "aiuser",
    'password': "Entrans1",
    'database': "salesdb"
}
# First, add this new function for SQL retry logic
def retry_sql_generation(question: str, schema: str, max_retries: int = 3) -> dict:
    """
    Retry SQL query generation with validation feedback loop.
    Returns a dictionary containing the final validated query and validation status.
    """
    attempts = 0
    previous_queries = set()  # Track previous attempts to avoid repetition
    
    while attempts < max_retries:
        try:
            # Generate initial query
            relevant_tables = get_relevant_tables(question, vectorstore)
            query = llm.invoke(
                schema_aware_query_prompt.format(
                    question=question,
                    relevant_tables=relevant_tables,
                    schema=schema
                )
            ).content
            
            # Clean the query
            cleaned_query = clean_query(query)
            
            # Skip if we've seen this query before
            if cleaned_query in previous_queries:
                attempts += 1
                continue
                
            previous_queries.add(cleaned_query)
            
            # Validate the query
            validation_result = validate_sql_query(cleaned_query, question, schema)
            
            if validation_result["is_valid"]:
                return {
                    "query": validation_result["modified_query"],
                    "validation_status": "success",
                    "attempts": attempts + 1
                }
            
            # If invalid, create a new prompt incorporating the validation feedback
            retry_prompt = PromptTemplate.from_template(
                """
                The previous SQL query attempt had these issues:
                Issues: {issues}
                
                Suggestions for improvement:
                {suggestions}
                
                Please generate a corrected SQL query for the original question:
                Question: {question}
                Available Tables: {relevant_tables}
                Schema: {schema}
                
                Generate only the corrected SQL query, ensuring it addresses all identified issues.
                """
            )
            
            # Generate new query with feedback
            retry_response = llm.invoke(
                retry_prompt.format(
                    issues="\n".join(validation_result["issues"]),
                    suggestions="\n".join(validation_result["suggestions"]),
                    question=question,
                    relevant_tables=relevant_tables,
                    schema=schema
                )
            ).content
            
            attempts += 1
            
        except Exception as e:
            logging.error(f"Error in retry attempt {attempts + 1}: {str(e)}")
            attempts += 1
            
    # If we've exhausted retries, return the best query we have
    return {
        "query": cleaned_query,
        "validation_status": "failed_after_retries",
        "attempts": attempts
    }

# Then, update your main chain definition
def initialize_chain():
    """Initialize the processing chain with retry mechanism"""
    return (
        RunnablePassthrough()
        .assign(
            sql_generation=lambda x: retry_sql_generation(
                x["question"],
                db_connection.get_table_info()
            )
        )
        .assign(
            query=lambda x: x["sql_generation"]["query"]
        )
        .assign(
            columns=lambda x: get_relevant_columns(db_config, x["question"])
        )
        .assign(
            result=lambda x: safe_execute_query(x["query"], db_connection)
        )
        .assign(
            answer=lambda x: llm.invoke(
                answer_prompt.format(
                    question=x["question"],
                    query=x["query"],
                    result=x["result"]
                )
            ).content
        )
        .assign(
            visualization=lambda x: llm.invoke(
                viz_prompt.format(
                    question=x["question"],
                    result=x["result"]
                )
            ).content
        )
        .assign(
            plot_code=lambda x: generate_plot_code(
                llm.invoke(
                    plot_prompt.format(
                        question=x["question"],
                        result=x["result"],
                        columns=x["columns"]
                    )
                )
            )
        )
    )

# Initialize the chain globally
chain = initialize_chain()
@app.errorhandler(500)
def handle_500_error(error):
    app.logger.error(f'Internal Server Error: {error}')
    return jsonify({
        'error': 'Internal Server Error',
        'message': 'An unexpected error occurred. Please try again later.'
    }), 500

@app.errorhandler(404)
def handle_404_error(error):
    return jsonify({
        'error': 'Not Found',
        'message': 'The requested resource was not found.'
    }), 404
# Flask routes
@app.route('/')
def index():
    return render_template('index.html')
# Modify the generate_answer_and_plot route to include better error handling and debugging
@app.route('/generate', methods=['POST'])
def generate_answer_and_plot():
    start_total_time = time.time()
    
    try:
        question = request.form.get('question')
        if not question:
            return jsonify({'error': 'No question provided'}), 400
            
        output_type = request.form.get('output', 'both')
        database_type = request.form.get('database_type')
        
        # Execute chain with current database connection
        result = chain.invoke({"question": question})
        
        # Log retry attempts if any occurred
        if result.get('sql_generation', {}).get('attempts', 1) > 1:
            logging.info(f"Query required {result['sql_generation']['attempts']} attempts")

        # Process results
        answer = result.get('answer') if output_type in ['answer', 'both'] else None
        plot_code = result.get('plot_code') if output_type in ['plot', 'both'] else None

        # Generate plot if needed
        plot_path = None
        if plot_code:
            plot_path = generate_plot_file(plot_code)

        # Calculate total execution time
        total_execution_time = time.time() - start_total_time

        # Save to database
        try:
            new_query = LLMQuery(
                user_question=question,
                generated_sql_query=result.get('query', ''),
                llm_answer=answer,
                visualization_recommendation=result.get('visualization', ''),
                plot_file_path=plot_path,
                output_type=output_type,
                generated_plot_code=plot_code,
                total_execution_time=total_execution_time,
                database_type=database_type
            )
            db.session.add(new_query)
            db.session.commit()
        except Exception as e:
            print(f"Failed to save to database: {str(e)}")
            db.session.rollback()

        return render_template('result.html',
            answer=answer,
            plot_path=plot_path,
            show_answer=(output_type in ['answer', 'both']),
            show_plot=(output_type in ['plot', 'both']))

    except Exception as e:
        print(f"Error in generate_answer_and_plot: {str(e)}")
        traceback.print_exc()
        return render_template('result.html',
            error=f"An error occurred while processing your request: {str(e)}",
            show_answer=False,
            show_plot=False)

    except Exception as e:
        print(f"Error in generate_answer_and_plot: {str(e)}")
        traceback.print_exc()
        return render_template('result.html',
            error=f"An error occurred while processing your request: {str(e)}",
            show_answer=False,
            show_plot=False)
# Modify the upload_file route
@app.route('/upload', methods=['GET', 'POST'])
def upload_file():
    if request.method == 'POST':
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            file_path = None
            try:
                if 'file' not in request.files:
                    return jsonify({'error': 'No file selected'}), 400

                file = request.files['file']
                table_name = request.form.get('table_name', '').strip()

                if file.filename == '':
                    return jsonify({'error': 'No file selected'}), 400
                if not table_name:
                    return jsonify({'error': 'Table name is required'}), 400
                if not validate_table_name(table_name):
                    return jsonify({'error': 'Table name can only contain letters, numbers, and underscores'}), 400
                
                # Check file extension
                file_ext = os.path.splitext(file.filename)[1].lower()
                if file_ext not in ['.csv', '.xlsx', '.xls']:
                    return jsonify({'error': 'Only CSV and Excel files are allowed'}), 400

                filename = secure_filename(file.filename)
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(file_path)

                success, message = csv_to_sql(
                    file_path,
                    table_name,
                    'mysql+pymysql://aiuser:Entrans1@13.235.9.120:3306/salesdb'
                )

                # Delay file removal to ensure all handles are closed
                import time
                time.sleep(1)  # Wait a second to ensure file handles are released
                
                # Try to remove the file with error handling
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                except Exception as file_error:
                    print(f"Warning: Could not remove temporary file {file_path}: {str(file_error)}")
                    # Continue processing - this is not a critical error

                if success:
                    refresh_success, refresh_message = refresh_database_connections()
                    if not refresh_success:
                        return jsonify({'error': f"Table created but failed to refresh connections: {refresh_message}"}), 400
                    return jsonify({'success': message})
                else:
                    return jsonify({'error': message}), 400

            except Exception as e:
                print(f"Upload error: {str(e)}")
                traceback.print_exc()
                
                # Try to clean up file if it exists and hasn't been removed
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except:
                        pass  # If we can't remove it, just continue
                        
                return jsonify({'error': str(e)}), 500
        else:
            file_path = None
            try:
                if 'file' not in request.files:
                    return render_template('upload.html', error='No file selected')

                file = request.files['file']
                table_name = request.form.get('table_name', '').strip()

                if file.filename == '':
                    return render_template('upload.html', error='No file selected')
                if not table_name:
                    return render_template('upload.html', error='Table name is required')
                if not validate_table_name(table_name):
                    return render_template('upload.html', 
                        error='Table name can only contain letters, numbers, and underscores')
                
                # Check file extension
                file_ext = os.path.splitext(file.filename)[1].lower()
                if file_ext not in ['.csv', '.xlsx', '.xls']:
                    return render_template('upload.html', error='Only CSV and Excel files are allowed')

                filename = secure_filename(file.filename)
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(file_path)

                success, message = csv_to_sql(
                    file_path,
                    table_name,
                    'mysql+pymysql://aiuser:Entrans1@13.235.9.120:3306/salesdb'
                )

                # Delay file removal to ensure all handles are closed
                import time
                time.sleep(1)  # Wait a second to ensure file handles are released
                
                # Try to remove the file with error handling
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                except Exception as file_error:
                    print(f"Warning: Could not remove temporary file {file_path}: {str(file_error)}")
                    # Continue processing - this is not a critical error

                if success:
                    refresh_success, refresh_message = refresh_database_connections()
                    if not refresh_success:
                        return render_template('upload.html', 
                            error=f"Table created but failed to refresh connections: {refresh_message}")
                    
                    return render_template('upload.html', 
                        success=f"File processed successfully: {message}")
                else:
                    return render_template('upload.html', error=message)

            except Exception as e:
                print(f"Upload error: {str(e)}")
                traceback.print_exc()
                
                # Try to clean up file if it exists and hasn't been removed
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except:
                        pass  # If we can't remove it, just continue
                        
                return render_template('upload.html', error=str(e))

    return render_template('upload.html')

@app.route('/history')
def view_database_history():
    """Render the database query history"""
    with app.app_context():
        queries = LLMQuery.query.order_by(LLMQuery.timestamp.desc()).limit(100).all()
    return render_template('history.html', queries=queries)


@app.route('/schema')
def view_schemas():
    """Route to display database schemas."""
    if not db_connection:
        return render_template('schema.html', 
                             error="Please select a database to view schema")
    
    schema_info = db_connection.get_table_info()
    return render_template('schema.html', schema_info=schema_info)

@app.route('/static/<filename>')
def serve_plot(filename):
    return send_from_directory('static', filename)
# Global variables for database connections
db_connection = None 
vectorstore = None
chain = None

def initialize_database_connection(database_type=None):
    """Initialize database connection based on selected type"""
    global db_connection, vectorstore, chain
    
    if database_type is None:
        return False, "No database selected"
        
    try:
        if database_type == 'mysql':
            db_uri = 'mysql+pymysql://aiuser:Entrans1@13.235.9.120:3306/salesdb'
            app.config['SQLALCHEMY_DATABASE_URI'] = db_uri
            app.config['SQLALCHEMY_BINDS'] = {
                'data_db': db_uri
            }
        elif database_type == 'postgresql':
            db_uri = 'postgresql://avnadmin:AVNS_7JbnT3uvNxLgCFOl-Jb@postdb-anantdayanithi.l.aivencloud.com:20704/defaultdb'
            app.config['SQLALCHEMY_DATABASE_URI'] = db_uri
            app.config['SQLALCHEMY_BINDS'] = {
                'data_db': db_uri
            }
        else:
            return False, "Invalid database type"

        # Initialize database connection
        db_connection = SQLDatabase.from_uri(
            db_uri,
            sample_rows_in_table_info=3,
        )
        
        # Initialize schema embeddings
        vectorstore = initialize_schema_embeddings(db_connection)
        
        # Initialize chain
        chain = initialize_chain()
        
        # Initialize tables
        with app.app_context():
            db.create_all()
            
        return True, f"Successfully initialized {database_type} database"
        
    except Exception as e:
        return False, f"Database initialization error: {str(e)}"
@app.route('/download_csv')
def download_csv():
    """Generate and serve CSV file from the displayed table."""
    try:
        # Fetch the latest query result
        latest_query = LLMQuery.query.order_by(LLMQuery.timestamp.desc()).first()
        
        if not latest_query or not latest_query.llm_answer:
            return jsonify({'error': 'No data available for download'}), 400

        # Convert the markdown table format to CSV
        table_data = latest_query.llm_answer.split("\n")
        processed_data = [row.replace("|", ",").strip(",") for row in table_data if "|" in row]

        # Save to a CSV file
        csv_file = "static/downloaded_data.csv"
        with open(csv_file, "w", encoding="utf-8") as f:
            f.write("\n".join(processed_data))

        return send_from_directory('static', 'downloaded_data.csv', as_attachment=True)

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/switch_database', methods=['POST'])
def switch_database():
    """Route to handle database switching"""
    try:
        database_type = request.json.get('database_type')
        if not database_type:
            return jsonify({'error': 'No database type provided'}), 400

        success, message = initialize_database_connection(database_type)
        
        if success:
            return jsonify({
                'success': True, 
                'message': f'Switched to {database_type} database'
            }), 200
        else:
            return jsonify({'error': message}), 500

    except Exception as e:
        app.logger.error(f"Database switch error: {str(e)}")
        return jsonify({'error': str(e)}), 500
# Optional method to retrieve query history with execution details
def get_query_history_with_execution_time():
    """
    Retrieve query history including the new execution time column
    """
    try:
        # Query all LLMQuery records, ordered by timestamp (most recent first)
        query_history = LLMQuery.query.order_by(LLMQuery.timestamp.desc()).all()
        
        # Convert to a list of dictionaries for easier handling
        history_data = []
        for query in query_history:
            history_data.append({
                'id': query.id,
                'timestamp': query.timestamp,
                'user_question': query.user_question,
                'generated_sql_query': query.generated_sql_query,
                'llm_answer': query.llm_answer,
                'visualization_recommendation': query.visualization_recommendation,
                'plot_file_path': query.plot_file_path,
                'output_type': query.output_type,
                'generated_plot_code': query.generated_plot_code,
                'total_execution_time': query.total_execution_time,
                'database_type': query.database_type
            })
        
        return history_data
    except Exception as e:
        print(f"Error retrieving query history: {e}")
        return []
@app.route('/health')
def health_check():
    if get_db_connection():
        return jsonify({'status': 'healthy', 'database': 'connected'}), 200
    return jsonify({'status': 'unhealthy', 'database': 'disconnected'}), 503
def init_db():
    with app.app_context():
        db.create_all()
def init_query_history_db():
    """Initialize the query history database and create necessary tables"""
    try:
        engine = create_engine('mysql+pymysql://aiuser:Entrans1@13.235.9.120:3306/llm_outputs_db')
        
        # Create the database if it doesn't exist
        with engine.connect() as conn:
            conn.execute(text("CREATE DATABASE IF NOT EXISTS llm_outputs_db"))
        
        # Create tables in the query history database
        with app.app_context():
            db.create_all(bind='query_history')
            
        return True, "Query history database initialized successfully"
    except Exception as e:
        return False, f"Error initializing query history database: {str(e)}"
if __name__ == '__main__':
    init_db()
    init_query_history_db()
    
    # Initialize database connection without forcing embedding refresh
    db_connection = SQLDatabase.from_uri(
        'mysql+pymysql://aiuser:Entrans1@13.235.9.120:3306/salesdb',
        sample_rows_in_table_info=3,
    )
    
    # Initialize schema embeddings only if they don't exist
    vectorstore = initialize_schema_embeddings(db_connection)
    
    # Create database tables if they don't exist
    with app.app_context():
        db.create_all()
    
    # Start the server
    if os.getenv('FLASK_ENV') == 'development':
        app.run(debug=True, host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
    else:
        serve(app, host='0.0.0.0', port=int(os.getenv('PORT', 5000)), threads=4)
