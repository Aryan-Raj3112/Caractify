import base64
import logging
import os
import random
import time
import uuid
from typing import Generator
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

import google.generativeai as genai
import psycopg2
from dotenv import load_dotenv
from psycopg2 import pool
from psycopg2.extras import DictCursor

load_dotenv()
# Load environment variables, set log level, and debug mode.
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
debug_mode = os.getenv("DEBUG_MODE", "0").upper() == "1"

# Configure basic logging with timestamp, level, name, and message.
logging.basicConfig(level=getattr(logging, log_level),
                    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

# Configure the Gemini API key.
genai.configure(api_key=os.getenv("API_KEY"))

DATABASE_URL = os.getenv('DATABASE_URL')

# Get model names from environment or use defaults
MODEL_NAMES_ENV = os.getenv("MODEL_NAMES")
if MODEL_NAMES_ENV:
    MODEL_NAMES = [model.strip() for model in MODEL_NAMES_ENV.split(",")]
else:
    logger.warning(
        "MODEL_NAMES not found in .env file. "
        "Using default model names: gemini-2.0-flash-lite, gemini-2.0-flash"
    )
    # Default model names
    MODEL_NAMES = ["gemini-2.0-flash-lite", "gemini-2.0-flash"]

# Load model weights from environment or set defaults
MODEL_WEIGHTS_ENV = os.getenv("MODEL_WEIGHTS")
MODEL_WEIGHTS = {}  # Initialize as an empty dictionary
if MODEL_WEIGHTS_ENV:
    try:
        # Parse weights from env string "model1:0.5,model2:0.3,model3:0.2"
        weights_list = MODEL_WEIGHTS_ENV.split(",")
        total_weight = 0.0
        for item in weights_list:
            model, weight = item.split(":")
            model = model.strip()
            weight = float(weight.strip())
            if model in MODEL_NAMES:
                MODEL_WEIGHTS[model] = weight
                total_weight += weight
        if total_weight != 1.0:
            logger.warning(f"Weights in MODEL_WEIGHTS don't add up to 1.0. using equal weights.")
            MODEL_WEIGHTS = {}
    except (ValueError, TypeError) as e:
        logger.warning(f"Invalid MODEL_WEIGHTS format: {e}. Using default equal weights.")
else:
    logger.warning(
        "MODEL_WEIGHTS not found in .env file. "
        "Using default equal weights for the models."
    )
# Ensure all models have a weight; default to equal if not in environment
if not MODEL_WEIGHTS:
    for model_name in MODEL_NAMES:
        MODEL_WEIGHTS[model_name] = 1.0 / len(MODEL_NAMES)
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
    retry_delay = 2

    for attempt in range(max_retries):
        try:
            logger.info(
                f"Attempting to establish database connection pool (attempt {attempt + 1}/{max_retries})..."
            )
            # Parse and enforce SSL parameters
            parsed = urlparse(DATABASE_URL)
            query_params = parse_qs(parsed.query)
            if 'sslmode' not in query_params:
                query_params['sslmode'] = ['require']
            new_query = urlencode(query_params, doseq=True)
            secure_db_url = parsed._replace(query=new_query).geturl()

            pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=2,
                maxconn=15,
                dsn=secure_db_url,
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
            if debug_mode:
                logger.debug("Got valid connection from pool")
            return conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            logger.warning(f"Connection attempt {attempt + 1}/{max_retries} failed: {e}")
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
                connection_pool.putconn(conn)
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
            connection_pool.putconn(conn)
            if debug_mode:
                logger.debug("Returned valid connection to pool")
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


def stream_gemini_response(message_parts: list, chat_history: list, system_prompt: str) -> Generator[str, None, None]:
    """
    Streams responses from the Gemini API based on message parts and chat history.

    Handles text and image parts, database interaction for image retrieval, and
    manages the streaming of the API response.

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

        # Release connection before API call to avoid long usage
        if conn:
            release_db_connection(conn)
            conn = None
            
        selected_model = get_next_model()
        logger.info(f"Using model: {selected_model}")
        
        # Set model, and starts the chat history.
        model = genai.GenerativeModel(selected_model, system_instruction=system_prompt)
        chat = model.start_chat(history=formatted_history)
        # Gets a stream of response.
        response = chat.send_message(formatted_message, stream=True)
        
        last_ping = time.time()
        yield ""

        for chunk in response:
            current_time = time.time()
            if current_time - last_ping > 4:
                yield ""
                last_ping = current_time
                
            try:
                text_parts = [part.text for part in chunk.parts if part.text]
                if text_parts:
                    combined_text = ''.join(text_parts).rstrip('\n')
                    if combined_text:
                        yield combined_text
            except Exception as e:
                logger.error(f"Error processing response chunk: {e}")
                yield f"[ERROR PROCESSING RESPONSE CHUNK: {e}]"

    except Exception as e:
        logger.exception(f"Gemini API error: {e}")
        yield f"[ERROR: {e}]"
    finally:
        #Ensures the release of the connection.
        if conn:
            try:
                release_db_connection(conn)
            except Exception as e:
                logger.error(f"Error releasing connection in finally block: {e}")
