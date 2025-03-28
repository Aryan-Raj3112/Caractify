import base64
import logging
import os
import random
import time
import uuid
from typing import Generator
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
from PIL import Image
import io
from google import genai
from google.genai import types
import psycopg2
from dotenv import load_dotenv
from psycopg2 import pool
from psycopg2.extras import DictCursor
from queue import Queue, Empty
from threading import Thread
import datetime

load_dotenv()
# Load environment variables and set log level.
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
JPEG_QUALITY = int(os.getenv('JPEG_QUALITY', '70'))

# Configure basic logging with timestamp, level, name, and message.
logging.basicConfig(level=getattr(logging, log_level),
                    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv('DATABASE_URL')

MODEL_NAMES = ["gemini-2.0-flash-lite", "gemini-2.0-flash"]
MODEL_WEIGHTS = {
    "gemini-2.0-flash-lite": 0.5,
    "gemini-2.0-flash": 0.5,
}

# Ensure that the database url uses postgres and enforces ssl
if DATABASE_URL:
    parsed = urlparse(DATABASE_URL)
    if parsed.scheme != 'postgresql':
        parsed = parsed._replace(scheme='postgresql')

    if 'sslmode' not in parsed.query:
        new_query = 'sslmode=require' + ('&' + parsed.query if parsed.query else '')
        parsed = parsed._replace(query=new_query)

    DATABASE_URL = urlunparse(parsed)


def create_connection_pool():
    """
    Creates a pool of database connections.

    Establishes a connection pool with the database specified by DATABASE_URL.
    Handles connection failures with retries and exponential backoff.

    Returns:
        psycopg2.pool.ThreadedConnectionPool: The established connection pool.

    Raises:
        psycopg2.OperationalError: If the database connection fails after max_retries.
        Exception: If any unexpected error occurs during pool creation.
    """
    max_retries = 5
    retry_delay = 2  # seconds

    for attempt in range(max_retries):
        try:
            logger.info(
                f"Attempting to establish database connection pool (attempt {attempt + 1}/{max_retries})..."
            )
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
            # Immediate connection test: ensure at least one connection is working.
            test_conn = pool.getconn()
            with test_conn.cursor() as cur:
                cur.execute("SELECT 1")
            pool.putconn(test_conn)
            logger.info("Database connection pool established successfully.")
            return pool

        except psycopg2.OperationalError as e:
            logger.warning(
                f"Database connection failed (attempt {attempt + 1}/{max_retries}): {e}. "
                f"Retrying in {retry_delay * (attempt + 1)} seconds..."
            )
            if attempt < max_retries - 1:
                time.sleep(retry_delay * (attempt + 1))
            else:
                logger.error(
                    f"Max database connection retries ({max_retries}) reached. "
                    f"Failed to establish connection pool. Error: {e}"
                )
                raise

        except Exception as e:
            logger.error(f"An unexpected error occurred during database connection pool creation: {e}")
            raise


# Initialize the database connection pool.
connection_pool = create_connection_pool()


def get_db_connection():
    """
    Retrieves a database connection from the pool with retries.
    
    Attempts to get a validated connection from the pool.
    Handles connection failures with retries and exponential backoff.
    
    Returns:
        psycopg2.extensions.connection: A valid database connection.
    
    Raises:
        psycopg2.OperationalError: If the connection fails after max_retries.
        Exception: If any unexpected error occurs during connection retrieval.
    """
    max_retries = 3
    for attempt in range(max_retries):
        conn = None
        try:
            logger.debug(f"Attempting to get connection from pool (attempt {attempt + 1}/{max_retries})...")
            conn = connection_pool.getconn()
            conn.cursor_factory = DictCursor

            # Test connection validity
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            logger.debug("Got valid connection from pool")
            return conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            logger.warning(f"Connection attempt {attempt + 1}/{max_retries} failed: {e}")
            # Check for specific SSL errors to trigger pool reinitialization
            error_message = str(e)
            if any(err in error_message for err in ["EOF detected", "bad record mac", "decryption failed"]):
                logger.error("SSL connection error detected. Reinitializing connection pool.")
                create_connection_pool()
            if conn:
                try:
                    conn.close()
                    logger.warning("Closed a broken connection")
                except Exception as close_e:
                    logger.error(f"Failed to close broken connection: {close_e}")
            if attempt < max_retries - 1:
                time.sleep(1 * (attempt + 1))  # Exponential backoff
                continue
            else:
                logger.error(f"Max connection retries ({max_retries}) reached. Error: {e}")
                raise
        except Exception as e:
            logger.error(f"Unexpected error getting connection: {e}")
            if conn:
                try:
                    connection_pool.putconn(conn)
                except Exception:
                    pass
            raise


def release_db_connection(conn):
    """
    Releases a database connection back to the pool.

    Validates the connection before returning it to the pool.
    Closes the connection if it is found to be invalid.

    Args:
        conn (psycopg2.extensions.connection): The database connection to release.
    """
    if conn:
        if conn.closed:
            logger.warning("Attempted to release a closed connection")
            return
        try:
            # Validate connection before returning to pool
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            try:
                connection_pool.putconn(conn)
                logger.debug("Returned valid connection to pool")
            except KeyError as e:
                logger.warning(f"Key error releasing connection: {e}. Closing connection instead.")
                conn.close()
            except Exception as put_e:
                logger.error(f"Error in putconn: {put_e}. Closing connection.")
                conn.close()
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            logger.warning(f"Closing dead connection instead of returning to pool: {e}")
            try:
                conn.close()
            except Exception as close_e:
                logger.error(f"Failed to close dead connection: {close_e}")


def generate_user_id():
    """
    Generates a unique user ID using UUID.

    Returns:
        str: A UUID string.
    """
    return str(uuid.uuid4())


def get_next_model():
    """
    Selects a model based on predefined weights.

    Returns:
        str: The name of the selected model.
    """
    return random.choices(
        list(MODEL_WEIGHTS.keys()),
        weights=list(MODEL_WEIGHTS.values()),
        k=1
    )[0]
    
def check_api_limit(conn):
    """
    Checks and updates the API call counter, enforcing a limit of 2000 calls per 24 hours.
    
    Args:
        conn: A psycopg2 database connection object.
    
    Returns:
        bool: True if the API can be called (count < 2000), False otherwise.
    """
    try:
        with conn.cursor() as cur:
            # Lock the row to prevent race conditions
            cur.execute("SELECT count, last_reset FROM api_call_counter WHERE id = 1 FOR UPDATE")
            row = cur.fetchone()
            if row is None:
                # Insert the row if it doesn't exist (fallback, though initial insert should handle this)
                now = datetime.datetime.now(datetime.timezone.utc)
                cur.execute("INSERT INTO api_call_counter (id, count, last_reset) VALUES (1, 0, %s)", (now,))
                count = 0
                last_reset = now
            else:
                count = row['count']
                last_reset = row['last_reset']
                now = datetime.datetime.now(datetime.timezone.utc)
                # Check if 24 hours have passed since the last reset
                if now > last_reset + datetime.timedelta(hours=24):
                    count = 0
                    last_reset = now
            
            # Check if limit is reached
            if count < 2000:
                count += 1
                cur.execute("UPDATE api_call_counter SET count = %s, last_reset = %s WHERE id = 1", 
                           (count, last_reset))
                conn.commit()
                return True
            else:
                conn.commit()
                return False
    except Exception as e:
        conn.rollback()
        logger.error(f"Error checking API limit: {e}")
        return False


def stream_gemini_response(message_parts: list, chat_history: list, system_prompt: str) -> Generator[str, None, None]:
    """
    Streams responses from the Gemini API based on message parts and chat history.
    
    Handles text and image parts, database interaction for image retrieval, and
    manages the streaming of the API response using the new google.genai module.

    Args:
        message_parts (list): List of message parts from the current request.
        chat_history (list): List of previous messages in the chat history.
        system_prompt (str): The system prompt to guide the Gemini model.

    Yields:
        str: Text chunks from the Gemini API response.
    """
    conn = None
    try:
        logger.info("Starting Gemini API call")
        formatted_message = []
        conn = get_db_connection()

        # Process current message parts
        logger.info("Processing message parts")
        for part in message_parts:
            if part.get('type') == 'text':
                content = part.get('content', '')
                if content:
                    formatted_message.append({'text': content})
            elif part.get('type') == 'image_ref':
                try:
                    image_id = part.get('image_id')
                    with conn.cursor() as cur:
                        logger.info(f"Fetching image data for image_id: {image_id}")
                        cur.execute("SELECT image_data, mime_type FROM images WHERE image_id = %s", (image_id,))
                        result = cur.fetchone()
                        if result:
                            img_data, mime_type = result
                            logger.info(f"Successfully fetched image data for image_id: {image_id}")
                            formatted_message.append({
                                'inline_data': {
                                    'mime_type': mime_type,
                                    'data': base64.b64encode(img_data).decode('utf-8')
                                }
                            })
                        else:
                            logger.warning(f"No image data found for image_id: {image_id}")
                            yield f"[WARNING: No image data found for image_id: {image_id}]"
                            return
                except Exception as e:
                    logger.error(f"Error processing image for image_id {image_id}: {e}")
                    yield f"[ERROR: Failed to process image: {e}]"
                    return

        # Process chat history
        logger.info("Processing chat history")
        formatted_history = []
        for msg in chat_history:
            parts = []
            for part in msg.get('parts', []):
                if part.get('type') == 'text':
                    parts.append({'text': part['content']})
                elif part.get('type') == 'image_ref':
                    try:
                        image_id = part.get('image_id')
                        with conn.cursor() as cur:
                            logger.info(f"Fetching history image data for image_id: {image_id}")
                            cur.execute("SELECT image_data, mime_type FROM images WHERE image_id = %s", (image_id,))
                            result = cur.fetchone()
                            if result:
                                img_data, mime_type = result
                                logger.info(f"Successfully fetched history image data for image_id: {image_id}")
                                parts.append({
                                    'inline_data': {
                                        'mime_type': mime_type,
                                        'data': base64.b64encode(img_data).decode('utf-8')
                                    }
                                })
                            else:
                                logger.warning(f"No history image data found for image_id: {image_id}")
                                yield f"[WARNING: No history image data found for image_id: {image_id}]"
                                return
                    except Exception as e:
                        logger.error(f"Error processing history image for image_id {image_id}: {e}")
                        yield f"[ERROR: Failed to process history image: {e}]"
                        return
            
            formatted_history.append({
                'role': 'user' if msg['role'] == 'user' else 'model',
                'parts': parts
            })
        
        if not check_api_limit(conn):
            yield "Try again later."
            release_db_connection(conn)
            return

        # Release connection before API call
        if conn:
            release_db_connection(conn)
            conn = None

        # Initialize the client
        client = genai.Client(api_key=os.getenv("API_KEY"))
        selected_model = get_next_model()
        logger.info(f"Using model: {selected_model}")

        # Convert formatted_history to types.Content
        history_contents = []
        for msg in formatted_history:
            parts = []
            for p in msg['parts']:
                if 'text' in p:
                    parts.append(types.Part(text=p['text']))
                elif 'inline_data' in p:
                    mime_type = p['inline_data']['mime_type']
                    data_base64 = p['inline_data']['data']
                    data_bytes = base64.b64decode(data_base64)
                    parts.append(types.Part(inline_data={'mime_type': mime_type, 'data': data_bytes}))
            history_contents.append(types.Content(role=msg['role'], parts=parts))

        # Convert formatted_message to types.Content for current user message
        current_parts = []
        for p in formatted_message:
            if 'text' in p:
                current_parts.append(types.Part(text=p['text']))
            elif 'inline_data' in p:
                mime_type = p['inline_data']['mime_type']
                data_base64 = p['inline_data']['data']
                data_bytes = base64.b64decode(data_base64)
                current_parts.append(types.Part(inline_data={'mime_type': mime_type, 'data': data_bytes}))
        current_content = types.Content(role="user", parts=current_parts)

        # Combine history and current message into contents
        contents = history_contents + [current_content]

        # Configure generation parameters
        generate_content_config = types.GenerateContentConfig(
            temperature=0.6,
            top_p=0.95,
            top_k=40,
            max_output_tokens=8192,
            response_mime_type="text/plain",
            system_instruction=system_prompt,
        )

        # Use background thread to stream response
        result_queue = Queue()

        def produce_response():
            try:
                response_iter = client.models.generate_content_stream(
                    model=selected_model,
                    contents=contents,
                    config=generate_content_config,
                )
                for chunk in response_iter:
                    result_queue.put(chunk)
                result_queue.put(None)  # Signal end of stream
            except Exception as e:
                logger.error(f"Error in background stream thread: {e}")
                result_queue.put(e)

        thread = Thread(target=produce_response, daemon=True)
        thread.start()

        yield ""  # Initial empty yield

        while True:
            try:
                item = result_queue.get(timeout=1)
            except Empty:
                yield ": heartbeat\n\n"
                continue

            if item is None:
                break
            if isinstance(item, Exception):
                yield f"[ERROR PROCESSING RESPONSE CHUNK: {item}]"
                break

            try:
                if item.text:
                    yield item.text
            except Exception as e:
                logger.error(f"Error processing response chunk: {e}")
                yield f"[ERROR PROCESSING RESPONSE CHUNK: {e}]"
    except Exception as e:
        logger.exception(f"Gemini API error: {e}")
        yield f"[ERROR: {e}]"
    finally:
        if conn:
            try:
                release_db_connection(conn)
            except Exception as e:
                logger.error(f"Error releasing connection in finally block: {e}")

COMPRESS_IMAGES = True

def generate_image_response(message_parts: list, chat_history: list, session_id: str) -> dict:
    conn = None
    try:
        logger.info(f"Starting Gemini image generation for session {session_id}")
        formatted_message = []
        conn = get_db_connection()

        # Process current message parts
        logger.info("Processing message parts")
        for part in message_parts:
            if part.get('type') == 'text':
                content = part.get('content', '')
                if content:
                    formatted_message.append({'text': content})
            elif part.get('type') == 'image_ref':
                try:
                    image_id = part.get('image_id')
                    with conn.cursor() as cur:
                        logger.info(f"Processing current message image reference: {image_id}")
                        cur.execute("SELECT image_data, mime_type FROM images WHERE image_id = %s", (image_id,))
                        result = cur.fetchone()
                        if result:
                            img_data, mime_type = result
                            formatted_message.append({
                                'inline_data': {
                                    'mime_type': mime_type,
                                    'data': base64.b64encode(img_data).decode('utf-8')
                                }
                            })
                        else:
                            logger.error(f"Current message image not found: {image_id}")
                            return {'error': f"Image {image_id} not found in current message"}
                except Exception as e:
                    logger.error(f"Image processing failed for {image_id}: {str(e)}")
                    return {'error': f"Current image processing failed: {str(e)}"}

        logger.info(f"Processed {len(formatted_message)} parts from current message")

        # Process chat history
        logger.info("Processing chat history")
        formatted_history = []
        for msg_idx, msg in enumerate(chat_history):
            logger.debug(f"Processing history message #{msg_idx+1} (role: {msg.get('role')})")
            parts = []
            for part_idx, part in enumerate(msg.get('parts', [])):
                if part.get('type') == 'text':
                    logger.debug(f"History text part #{part_idx+1}: {part['content'][:50]}...")
                    parts.append({'text': part['content']})
                elif part.get('type') == 'image_ref':
                    image_id = part.get('image_id')
                    logger.info(f"Processing history image reference: {image_id}")
                    try:
                        with conn.cursor() as cur:
                            cur.execute("SELECT image_data, mime_type FROM images WHERE image_id = %s", (image_id,))
                            result = cur.fetchone()
                            if result:
                                img_data, mime_type = result
                                logger.debug(f"Retrieved image {image_id} ({len(img_data)} bytes, {mime_type})")
                                parts.append({
                                    'inline_data': {
                                        'mime_type': mime_type,
                                        'data': base64.b64encode(img_data).decode('utf-8')
                                    }
                                })
                            else:
                                logger.error(f"History image not found: {image_id}")
                                return {'error': f"Image {image_id} not found in history"}
                    except Exception as e:
                        logger.error(f"Image retrieval failed for {image_id}: {str(e)}")
                        return {'error': f"History image processing failed: {str(e)}"}
            formatted_history.append({
                'role': 'user' if msg['role'] == 'user' else 'model',
                'parts': parts
            })

        logger.info(f"Processed {len(formatted_history)} history messages")
        
        if not check_api_limit(conn):
            release_db_connection(conn)
            return {'text': "Try again later."}

        # Release connection before API call
        if conn:
            release_db_connection(conn)
            conn = None
            logger.debug("Database connection released before API call")

        # Initialize the client
        client = genai.Client(api_key=os.getenv("API_KEY"))
        model = "gemini-2.0-flash-exp-image-generation"
        logger.info(f"Initializing model {model} for generation")

        # Convert chat history to SDK types
        logger.debug("Converting history to SDK content types")
        history_contents = []
        for msg in formatted_history:
            parts = []
            for p in msg['parts']:
                if 'text' in p:
                    parts.append(types.Part(text=p['text']))
                elif 'inline_data' in p:
                    data_bytes = base64.b64decode(p['inline_data']['data'])
                    parts.append(types.Part(
                        inline_data={'mime_type': p['inline_data']['mime_type'], 'data': data_bytes}
                    ))
            history_contents.append(types.Content(role=msg['role'], parts=parts))
            logger.debug(f"Converted {len(parts)} parts for {msg['role']} role")

        # Convert formatted_message to SDK types for current user message
        current_parts = []
        for p in formatted_message:
            if 'text' in p:
                current_parts.append(types.Part(text=p['text']))
            elif 'inline_data' in p:
                data_bytes = base64.b64decode(p['inline_data']['data'])
                current_parts.append(types.Part(
                    inline_data={'mime_type': p['inline_data']['mime_type'], 'data': data_bytes}
                ))
        current_content = types.Content(role="user", parts=current_parts)

        # Combine history and current message contents
        contents = history_contents + [current_content]
        logger.info(f"Total content payload: {len(contents)} messages")

        # Configure generation parameters
        generate_content_config = types.GenerateContentConfig(
            temperature=1,
            top_p=0.95,
            top_k=40,
            max_output_tokens=8192,
            response_modalities=["image", "text"],
            safety_settings=[types.SafetySetting(category="HARM_CATEGORY_CIVIC_INTEGRITY", threshold="OFF")],
            response_mime_type="text/plain",
        )
        logger.debug(f"Generation config: {generate_content_config}")

        # Generate content
        logger.info("Initiating API content generation request")
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=generate_content_config,
        )

        # Process response
        response_text = ""
        image_id = None
        if response.candidates:
            logger.debug(f"Processing {len(response.candidates)} candidates")
            candidate = response.candidates[0]
            if candidate.content:
                logger.info(f"Processing response with {len(candidate.content.parts)} parts")
                for part_idx, part in enumerate(candidate.content.parts):
                    if part.text:
                        response_text += part.text
                        logger.debug(f"Text part #{part_idx+1}: {part.text[:50]}...")
                    elif part.inline_data:
                        logger.info(f"Processing image part #{part_idx+1} ({part.inline_data.mime_type})")
                        try:
                            if COMPRESS_IMAGES:
                                # Original compression code
                                img = Image.open(io.BytesIO(part.inline_data.data))
                                output = io.BytesIO()
                                img.save(output, format='JPEG', quality=JPEG_QUALITY)
                                compressed_data = output.getvalue()
                                mime_type = 'image/jpeg'
                                original_size = len(part.inline_data.data)
                                compressed_size = len(compressed_data)
                                logger.info(f"Image compressed from {original_size} bytes to {compressed_size} bytes")
                            else:
                                # Bypass compression and use original data
                                compressed_data = part.inline_data.data
                                mime_type = part.inline_data.mime_type
                                logger.info(f"Using original image data ({mime_type}, {len(compressed_data)} bytes)")
                        except Exception as e:
                            logger.error(f"Image compression failed: {str(e)}")
                            return {'error': f"Image compression failed: {str(e)}"}

                        # Save the compressed image to the database
                        conn = get_db_connection()
                        try:
                            image_id = str(uuid.uuid4())
                            with conn.cursor() as cur:
                                cur.execute("""
                                    INSERT INTO images (image_id, session_id, image_data, mime_type)
                                    VALUES (%s, %s, %s, %s)
                                """, (image_id, session_id, compressed_data, mime_type))
                                conn.commit()
                                logger.info(f"Image stored successfully. ID: {image_id}")
                        except Exception as e:
                            logger.error(f"Image storage failed: {str(e)}")
                            return {'error': f"Image storage failed: {str(e)}"}
                        finally:
                            release_db_connection(conn)
                            logger.debug("Database connection released after image storage")
        else:
            logger.warning("Empty response received from API with no candidates")

        # Prepare result
        result = {}
        if response_text:
            logger.debug(f"Final text response length: {len(response_text)} characters")
            result['text'] = response_text
        if image_id:
            logger.info(f"Image generation successful. ID: {image_id}")
            result['image_id'] = image_id

        if not result:
            logger.warning("No content generated in response")
            return {'text': "Image generation completed, but no output was produced."}

        logger.info(f"Returning result with keys: {list(result.keys())}")
        return result

    except Exception as e:
        logger.exception(f"Critical error in session {session_id}: {str(e)}")
        return {'error': str(e)}
    finally:
        if conn:
            logger.debug("Cleaning up database connection")
            release_db_connection(conn)