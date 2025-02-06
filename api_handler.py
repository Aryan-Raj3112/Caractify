import google.generativeai as genai
from dotenv import load_dotenv
import os
from typing import Generator
import logging
import base64
from psycopg2 import pool
import os
import uuid
from psycopg2.extras import DictCursor
import atexit
import psycopg2
import random

load_dotenv()
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, log_level),
                    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

genai.configure(api_key=os.getenv("API_KEY"))

DATABASE_URL = os.getenv('DATABASE_URL')
if DATABASE_URL:
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(DATABASE_URL)
    if parsed.scheme != 'postgresql':
        parsed = parsed._replace(scheme='postgresql')
    
    if 'sslmode' not in parsed.query:
        new_query = 'sslmode=require' + ('&' + parsed.query if parsed.query else '')
        parsed = parsed._replace(query=new_query)
    
    DATABASE_URL = urlunparse(parsed)

try:
    connection_pool = pool.ThreadedConnectionPool(
        minconn=3,
        maxconn=20,
        dsn=DATABASE_URL
    )
    logger.info("Database connection pool created successfully")
except psycopg2.OperationalError as e:
    logger.error(f"Failed to create database connection pool: {e}")
    raise  # Consider proper error handling for your application
except Exception as e:
    logger.critical(f"Unexpected error initializing database pool: {e}")
    raise

atexit.register(lambda: connection_pool.closeall())

def get_db_connection():
    """Get a connection from the pool with proper error handling"""
    try:
        conn = connection_pool.getconn()
        conn.cursor_factory = DictCursor
        return conn
    except Exception as e:
        logger.error(f"Failed to get DB connection: {str(e)}")
        raise

def release_db_connection(conn):
    """Release connection back to pool safely"""
    try:
        if conn and not conn.closed:
            connection_pool.putconn(conn)
    except Exception as e:
        logger.error(f"Error releasing connection: {str(e)}")

def generate_user_id():
    return str(uuid.uuid4())

MODEL_WEIGHTS = {
    "gemini-2.0-flash-lite-preview-02-05": 0.5,
    "gemini-2.0-flash-exp": 0.5,
}

def get_next_model():
    return random.choices(
        list(MODEL_WEIGHTS.keys()),
        weights=list(MODEL_WEIGHTS.values()),
        k=1
    )[0]

def stream_gemini_response(message_parts: list, chat_history: list, system_prompt: str) -> Generator[str, None, None]:
    conn = None
    try:
        logger.debug("Starting Gemini API call")
        formatted_message = []
        conn = get_db_connection()

        # Process current message parts
        for part in message_parts:
            if part.get('type') == 'text':
                content = part.get('content', '')
                if content:
                    formatted_message.append({'text': content})
            elif part.get('type') == 'image_ref':
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT image_data, mime_type FROM images WHERE image_id = %s", (part.get('image_id'),))
                        result = cur.fetchone()
                        if result:
                            img_data, mime_type = result
                            formatted_message.append({
                                'inline_data': {
                                    'mime_type': mime_type,
                                    'data': base64.b64encode(img_data).decode('utf-8')
                                }
                            })
                except Exception as e:
                    logger.error(f"Error processing image: {e}")
                    yield f"[ERROR: Failed to process image: {str(e)}]"
                    return

        # Process chat history
        formatted_history = []
        for msg in chat_history:
            parts = []
            for part in msg.get('parts', []):
                if part.get('type') == 'text':
                    parts.append({'text': part['content']})
                elif part.get('type') == 'image_ref':
                    try:
                        with conn.cursor() as cur:
                            cur.execute("SELECT image_data, mime_type FROM images WHERE image_id = %s", (part.get('image_id'),))
                            result = cur.fetchone()
                            if result:
                                img_data, mime_type = result
                                parts.append({
                                    'inline_data': {
                                        'mime_type': mime_type,
                                        'data': base64.b64encode(img_data).decode('utf-8')
                                    }
                                })
                    except Exception as e:
                        logger.error(f"Error processing history image: {e}")
                        yield f"[ERROR: Failed to process history image: {str(e)}]"
                        return
            
            formatted_history.append({
                'role': 'user' if msg['role'] == 'user' else 'model',
                'parts': parts
            })

        # Release connection before API call
        if conn:
            release_db_connection(conn)
            conn = None
            
        selected_model = get_next_model()
        logger.debug(f"Using model: {selected_model}")
        
        model = genai.GenerativeModel(selected_model, system_instruction=system_prompt)
        chat = model.start_chat(history=formatted_history)
        response = chat.send_message(formatted_message, stream=True)

        for chunk in response:
            if chunk.text:
                cleaned_chunk = chunk.text.rstrip('\n')
                yield cleaned_chunk

    except Exception as e:
        logger.exception(f"Gemini API error: {e}")
        yield f"[ERROR: {str(e)}]"
    finally:
        if conn:
            try:
                release_db_connection(conn)
            except Exception as e:
                logger.error(f"Error releasing connection in finally block: {e}")