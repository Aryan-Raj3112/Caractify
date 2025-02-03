import google.generativeai as genai
from dotenv import load_dotenv
import os
from typing import Generator
import logging
import base64
from psycopg2 import pool
import os
import uuid
import psycopg2
import atexit

load_dotenv()
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, log_level),
                    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

genai.configure(api_key=os.getenv("API_KEY"))

connection_pool = pool.SimpleConnectionPool(
    minconn=1,
    maxconn=10,
    dsn=os.getenv('DATABASE_URL')
)

atexit.register(lambda: connection_pool.closeall())

def get_db_connection():
    return connection_pool.getconn()

def release_db_connection(conn):
    connection_pool.putconn(conn)

def generate_user_id():
    return str(uuid.uuid4())

def stream_gemini_response(message_parts: list, chat_history: list, system_prompt: str) -> Generator[str, None, None]:
    logger.debug("Entering stream_gemini_response function")
    logger.debug(f"Message parts received: {message_parts}")
    try:
        logger.debug("Starting Gemini API call")
        formatted_message = []
        for part in message_parts:
            if part.get('type') == 'text':
                # Ensure 'content' exists and is not empty
                content = part.get('content', '')
                if content:
                    formatted_message.append({'text': content})
            elif 'mime_type' in part and 'data' in part:
                formatted_message.append({
                    'inline_data': {
                        'mime_type': part['mime_type'],
                        'data': part['data']
                    }
                })
        logger.debug(f"Formatted message for Gemini: {formatted_message}")
        conn = get_db_connection()
        if not conn:
            logger.error("Failed to obtain database connection")
            yield "[ERROR: Database connection failed]"
            return
        # Convert chat history to Gemini format
        formatted_history = []
        for msg in chat_history:
            parts = []
            for part in msg.get('parts', []):
                if part.get('type') == 'text':
                    parts.append({'text': part['content']})
                elif part.get('type') == 'image_ref':
                    if conn:  # Check if a connection was successfully retrieved
                        try:
                            with conn.cursor() as cur:
                                cur.execute("SELECT image_data, mime_type FROM images WHERE image_id = %s", (part['image_id'],))
                                img_data, mime_type = cur.fetchone()
                                parts.append({
                                    'inline_data': {
                                        'mime_type': mime_type,
                                        'data': base64.b64encode(img_data).decode('utf-8')
                                    }
                                })
                        except psycopg2.Error as e:
                            logger.exception(f"Database error retrieving image: {e}")  # Log exception with traceback
                            yield f"[ERROR: Database error: {str(e)}]"
                        finally:
                            release_db_connection(conn)
            formatted_history.append({
                'role': 'user' if msg['role'] == 'user' else 'model',
                'parts': parts
            })
        logger.debug(f"Formatted history for Gemini: {formatted_history}")
        
        model = genai.GenerativeModel("gemini-1.5-flash", system_instruction=system_prompt)
        chat = model.start_chat(history=formatted_history)
        response = chat.send_message(
            {'role': 'user', 'parts': formatted_message}, 
            stream=True
        )

        for chunk in response:
            if chunk.text:
                cleaned_chunk = chunk.text.rstrip('\n')  # Remove trailing newlines
                logger.debug(f"Gemini chunk received: {cleaned_chunk[:100]}...")
                yield cleaned_chunk

    except psycopg2.Error as e:
        logger.exception(f"Database error retrieving image: {e}")
        yield f"[ERROR: Database error: {str(e)}]"