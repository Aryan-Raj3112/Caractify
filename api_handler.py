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
        conn = get_db_connection()  # Get a single connection for processing message parts

        # Process current message parts (including image_ref)
        for part in message_parts:
            if part.get('type') == 'text':
                content = part.get('content', '')
                if content:
                    formatted_message.append({'text': content})
            elif part.get('type') == 'image_ref':
                image_id = part.get('image_id')
                if conn and image_id:
                    try:
                        with conn.cursor() as cur:
                            cur.execute("SELECT image_data, mime_type FROM images WHERE image_id = %s", (image_id,))
                            img_data, mime_type = cur.fetchone()
                            formatted_message.append({
                                'inline_data': {
                                    'mime_type': mime_type,
                                    'data': base64.b64encode(img_data).decode('utf-8')
                                }
                            })
                    except Exception as e:
                        logger.error(f"Error fetching image {image_id}: {e}")
                        yield f"[ERROR: Failed to load image: {str(e)}]"
                        return
            elif isinstance(part, dict) and 'mime_type' in part and 'data' in part:
                # Direct image data (if somehow included)
                formatted_message.append({
                    'inline_data': {
                        'mime_type': part['mime_type'],
                        'data': part['data']
                    }
                })

        # Process chat history (existing logic)
        formatted_history = []
        for msg in chat_history:
            parts = []
            for part in msg.get('parts', []):
                if part.get('type') == 'text':
                    parts.append({'text': part['content']})
                elif part.get('type') == 'image_ref':
                    image_id = part.get('image_id')
                    if conn and image_id:
                        try:
                            with conn.cursor() as cur:
                                cur.execute("SELECT image_data, mime_type FROM images WHERE image_id = %s", (image_id,))
                                img_data, mime_type = cur.fetchone()
                                parts.append({
                                    'inline_data': {
                                        'mime_type': mime_type,
                                        'data': base64.b64encode(img_data).decode('utf-8')
                                    }
                                })
                        except Exception as e:
                            logger.error(f"Error fetching image {image_id} from history: {e}")
                            yield f"[ERROR: Failed to load image from history: {str(e)}]"
                            return
            formatted_history.append({
                'role': 'user' if msg['role'] == 'user' else 'model',
                'parts': parts
            })

        logger.debug(f"Formatted message for Gemini: {formatted_message}")
        logger.debug(f"Formatted history for Gemini: {formatted_history}")
        
        release_db_connection(conn)  # Release the connection after processing

        model = genai.GenerativeModel("gemini-1.5-flash", system_instruction=system_prompt)
        chat = model.start_chat(history=formatted_history)
        response = chat.send_message(
            formatted_message,
            stream=True
        )

        for chunk in response:
            if chunk.text:
                cleaned_chunk = chunk.text.rstrip('\n')  # Remove trailing newlines
                logger.debug(f"Gemini chunk received: {cleaned_chunk[:100]}...")
                yield cleaned_chunk
                
    except Exception as e:
        logger.exception(f"Gemini API error: {e}")
        yield f"[ERROR: {str(e)}]"
    finally:
        if conn:
            release_db_connection(conn)