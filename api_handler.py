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
import psycopg2
import random
import time

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

def create_connection_pool():
    max_retries = 5
    retry_delay = 2  # seconds
    
    for attempt in range(max_retries):
        try:
            pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=2,
                maxconn=15,
                dsn=DATABASE_URL,
                keepalives=1,
                keepalives_idle=15,
                keepalives_interval=5,
                keepalives_count=2,
                connect_timeout=3
            )
            # Immediate connection test
            test_conn = pool.getconn()
            with test_conn.cursor() as cur:
                cur.execute("SELECT 1")
            pool.putconn(test_conn)
            logger.info("Database connection pool established")
            return pool
        except psycopg2.OperationalError as e:
            logger.warning(f"DB connection failed (attempt {attempt+1}): {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay * (attempt + 1))
            else:
                logger.error("Max DB connection retries reached")
                raise

connection_pool = create_connection_pool()

# app.py - Update get_db_connection and release_db_connection

def get_db_connection():
    """Get a validated connection from the pool with retries"""
    max_retries = 3
    for attempt in range(max_retries):
        conn = None
        try:
            conn = connection_pool.getconn()
            conn.cursor_factory = DictCursor
            
            # Test connection validity
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            logger.debug("Got valid connection from pool")
            return conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            logger.warning(f"Connection failed (attempt {attempt+1}): {str(e)}")
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            if attempt < max_retries - 1:
                time.sleep(1 * (attempt + 1))  # Exponential backoff
                continue
            else:
                logger.error("Max connection retries reached")
                raise
        except Exception as e:
            logger.error(f"Unexpected error getting connection: {str(e)}")
            if conn:
                try:
                    connection_pool.putconn(conn)
                except Exception:
                    pass
            raise

def release_db_connection(conn):
    """Safely release connection back to pool"""
    try:
        if conn and not conn.closed:
            # Validate connection before returning to pool
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                connection_pool.putconn(conn)
                logger.debug("Returned valid connection to pool")
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning("Closing dead connection instead of returning to pool")
                conn.close()
    except Exception as e:
        logger.error(f"Error releasing connection: {str(e)}")

def generate_user_id():
    return str(uuid.uuid4())

MODEL_WEIGHTS = {}
models_str = os.getenv("GEMINI_MODELS")

if models_str:
    for pair in models_str.split(','):
        try:
            model, weight = pair.split(':')
            MODEL_WEIGHTS[model.strip()] = float(weight.strip())
        except (ValueError, AttributeError) as e:
            logger.error(f"Invalid model/weight pair: {pair} - {str(e)}")
else:
    # Fallback defaults if environment variable not set
    MODEL_WEIGHTS = {
        "gemini-2.0-flash-lite-preview-02-05": 0.5,
        "gemini-2.0-flash-exp": 0.5,
    }
    logger.warning("Using default model weights as GEMINI_MODELS environment variable is not set")

logger.info(f"Loaded model weights: {MODEL_WEIGHTS}")

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
        
        last_ping = time.time()
        yield ""

        for chunk in response:
            current_time = time.time()
            if current_time - last_ping > 4:
                yield ""
                last_ping = current_time
                
            try:
                # Collect all text parts from the chunk
                text_parts = []
                for part in chunk.parts:
                    if part.text:
                        text_parts.append(part.text)
                
                # Only process if we have text content
                if text_parts:
                    combined_text = ''.join(text_parts).rstrip('\n')
                    if combined_text:
                        yield combined_text
            except Exception as e:
                logger.error(f"Error processing response chunk: {e}")
                yield f"[ERROR PROCESSING RESPONSE CHUNK: {str(e)}]"

    except Exception as e:
        logger.exception(f"Gemini API error: {e}")
        yield f"[ERROR: {str(e)}]"
    finally:
        if conn:
            try:
                release_db_connection(conn)
            except Exception as e:
                logger.error(f"Error releasing connection in finally block: {e}")