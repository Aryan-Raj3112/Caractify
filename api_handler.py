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

load_dotenv()
genai.configure(api_key=os.getenv("API_KEY"))

connection_pool = pool.SimpleConnectionPool(
    minconn=1,
    maxconn=10,
    dsn=os.getenv('DATABASE_URL')
)
def get_db_connection():
    return connection_pool.getconn()

def release_db_connection(conn):
    connection_pool.putconn(conn)

def generate_user_id():
    return str(uuid.uuid4())

def stream_gemini_response(message_parts: list, chat_history: list, system_prompt: str) -> Generator[str, None, None]:
    try:
        # Convert incoming message parts to Gemini format
        formatted_message = []
        for part in message_parts:
            if part.get('type') == 'text':
                formatted_message.append({'text': part['content']})
            elif 'mime_type' in part and 'data' in part:  # Image part
                formatted_message.append({
                    'inline_data': {
                        'mime_type': part['mime_type'],
                        'data': part['data']
                    }
                })
        conn = get_db_connection()
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
                        except psycopg2.Error as e: # Handle potential database errors
                            print(f"Database error: {e}")
                            yield f"[ERROR: Database error: {str(e)}]" # Yield an error message
                        finally:
                            release_db_connection(conn)
            formatted_history.append({
                'role': 'user' if msg['role'] == 'user' else 'model',
                'parts': parts
            })

        model = genai.GenerativeModel("gemini-1.5-flash", system_instruction=system_prompt)
        chat = model.start_chat(history=formatted_history)
        response = chat.send_message({"parts": formatted_message}, stream=True)

        for chunk in response:
            if chunk.text:
                yield chunk.text

    except Exception as e:
        yield f"[ERROR: {str(e)}]"