import json
import uuid
from flask import Flask, request, render_template, Response
from api_handler import stream_gemini_response
from dotenv import load_dotenv
import os
import logging
import markdown2
from markupsafe import Markup
from psycopg2 import pool
from chat_configs import chat_configs
from datetime import datetime, timezone
import base64
from psycopg2.extras import DictCursor
from flask import make_response

load_dotenv()
log_level = os.getenv("LOG_LEVEL", "INFO").upper()  # Get log level from env, default to INFO
logging.basicConfig(level=getattr(logging, log_level),  # Use getattr for dynamic level
                    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__) 

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY')

def markdown_filter(text):
    if not text:  # Ensure text is not None or empty
        text = ""
    if not isinstance(text, str):
        text = str(text)  # Convert to string if possible
    return Markup(markdown2.markdown(text, extras=[
        'break-on-newline',
        'code-friendly',
        'fenced-code-blocks',
        'tables',
        'smarty-pants',
        'cuddled-lists'
    ]))

app.jinja_env.filters['markdown'] = markdown_filter

def format_datetime(value):
    try:
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        elif isinstance(value, datetime):
            dt = value.astimezone() if value.tzinfo else value.replace(tzinfo=timezone.utc).astimezone()
        else:
            return ""
        return dt.strftime("%H:%M · %b %d")
    except Exception as e:
        logger.error(f"Time format error: {str(e)}")
        return ""

app.jinja_env.filters['datetimeformat'] = format_datetime

# Connection pool
connection_pool = pool.SimpleConnectionPool(
    minconn=1,
    maxconn=10,
    dsn=os.getenv('DATABASE_URL')
)

def get_db_connection():
    conn = connection_pool.getconn()
    conn.cursor_factory = DictCursor
    return conn

def release_db_connection(conn):
    connection_pool.putconn(conn)

def generate_user_id():
    return str(uuid.uuid4())


@app.route('/')
def home():
    logger.debug("Home route accessed")
    return render_template('home_page.html', chat_configs=chat_configs)


@app.route('/chat/<chat_type>')
def chat(chat_type):
    logger.debug(f"Chat route accessed for type: {chat_type}")
    conn = get_db_connection()
    try:
        config = chat_configs.get(chat_type)
        if not config:
            logger.warning(f"Invalid chat type requested: {chat_type}")
            return "Invalid chat type", 404

        session_id = generate_user_id()
        initial_history = [{
            "role": "model",
            "parts": [{"type": "text", "content": config['welcome_message']}], 
        }]
        initial_history_json = json.dumps(initial_history)

        logger.debug(f"Initial history: {initial_history}")

        with conn.cursor() as cur:
            try:
                title = config['title']

                cur.execute(
                    "INSERT INTO sessions (session_id, chat_history, chat_type, title, updated_at) VALUES (%s, %s, %s, %s, NOW())",  # Add updated_at
                    (session_id, initial_history_json, chat_type, title)
                )
                conn.commit()
                logger.info(f"New session created: {session_id} for chat type: {chat_type}")
                
            except Exception as e:
                logger.exception(f"Failed to insert session: {str(e)}")
                conn.rollback() # Important: Rollback on error
                return "Internal server error", 500

            cur.execute("""
                SELECT session_id, chat_type, created_at, title
                FROM sessions 
                ORDER BY updated_at DESC
                LIMIT 5
            """)
            all_sessions = cur.fetchall()

        response = make_response(render_template('index.html', 
                                            chat_history=initial_history,
                                            session_id=session_id,
                                            config=config,
                                            sessions=all_sessions))
        response.set_cookie('session_id', session_id, httponly=True, samesite='Strict')
        return response

    finally:
        release_db_connection(conn)


@app.route('/stream', methods=['POST'])
def stream():
    logger.debug("Stream route accessed")
    session_id = request.form.get('session_id')
    cookie_session_id = request.cookies.get('session_id')
    
    if session_id != cookie_session_id:
        return "Unauthorized", 403
    conn = get_db_connection()
    if conn is None:
        return "Could not obtain database connection", 500

    try:
        session_id = request.form.get('session_id', '').strip()
        user_message = request.form.get('message', '').strip()
        image_file = request.files.get('image')

        image_parts = []
        image_refs = []

        if image_file and image_file.filename:
            allowed_mimes = {'image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif'}
            if image_file.mimetype not in allowed_mimes:
                return "Unsupported image format", 400

            image_data = image_file.read()
            mime_type = image_file.mimetype

            image_id = str(uuid.uuid4())
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO images 
                    (image_id, session_id, image_data, mime_type) 
                    VALUES (%s, %s, %s, %s)
                """, (image_id, session_id, image_data, mime_type))
                conn.commit()

            encoded_image = base64.b64encode(image_data).decode('utf-8')
            image_parts.append({
                'mime_type': mime_type,
                'data': encoded_image
            })
            image_refs.append({'type': 'image_ref', 'image_id': image_id})

        message_parts = []
        if user_message:
            message_parts.append({"type": "text", "content": user_message})

        if image_parts:
            message_parts.extend(image_parts)

        if not message_parts:
            logger.error("Stream error: message_parts is empty")
            return "Error: No valid message parts", 400

        with conn.cursor() as cur:
            cur.execute("SELECT chat_history, chat_type FROM sessions WHERE session_id = %s", (session_id,))
            result = cur.fetchone()
            if not result:
                return "Session not found", 404

            # Correctly access columns by name
            chat_history_data = result['chat_history']
            chat_type = result['chat_type']
            config = chat_configs.get(chat_type, {})
            system_prompt = config.get('system_prompt', '')

            try:
                history = json.loads(chat_history_data) if isinstance(chat_history_data, str) else chat_history_data

                processed_history = []
                for message in history:
                    processed_parts = []
                    for part in message.get('parts', []):
                        if isinstance(part, dict) and part.get('type') == 'image_ref':
                            with conn.cursor() as img_cur:
                                img_cur.execute("SELECT image_data, mime_type FROM images WHERE image_id = %s", (part['image_id'],))
                                img_result = img_cur.fetchone()
                                if img_result:
                                    img_data, img_mime = img_result
                                    encoded_img = base64.b64encode(img_data).decode('utf-8')
                                    processed_parts.append({
                                        'mime_type': img_mime,
                                        'data': encoded_img
                                    })
                        else:
                            processed_parts.append(part)
                    processed_history.append({
                        'role': message['role'],
                        'parts': processed_parts,
                        'timestamp': message.get('timestamp')
                    })

                logger.debug(f"Sending to Gemini: {message_parts}")

                def event_stream():
                    try:
                        history = json.loads(chat_history_data) if isinstance(chat_history_data, str) and chat_history_data else []
                        complete_response = []
                        for chunk in stream_gemini_response(message_parts, processed_history, system_prompt):
                            if chunk.startswith('[ERROR'):
                                error_msg = chunk.replace('[ERROR', '').strip(']')
                                yield f"data: {json.dumps({'error': error_msg})}\n\n"
                                return

                            yield f"data: {json.dumps({'chunk': chunk})}\n\n"
                            complete_response.append(chunk)
                            
                        full_response = ''.join(complete_response)
                        
                        user_message_parts = []
                        if user_message:
                            user_message_parts.append({'type': 'text', 'content': user_message})
                        user_message_parts.extend(image_refs)

                        updated_history = history + [
                            {"role": "user", "parts": message_parts},
                            {"role": "model", "parts": [{"type": "text", "content": ''.join(complete_response)}]}
                        ]
                        updated_history[-2]['parts'] = [p for p in updated_history[-2]['parts'] if p]

                        first_user_message = "New Chat"
                        for msg in updated_history:
                            if msg['role'] == 'user':
                                for part in msg.get('parts', []):
                                    if isinstance(part, dict) and part.get('type') == 'text' and part.get('content'):
                                        first_user_message = part['content'][:50]
                                        break
                                if first_user_message != "New Chat":
                                    break
                        title = first_user_message

                        with conn.cursor() as cur:
                            cur.execute("""
                                UPDATE sessions 
                                SET chat_history = %s, title = %s, updated_at = NOW()  -- Update updated_at
                                WHERE session_id = %s
                            """, (json.dumps(updated_history), title, session_id))
                            conn.commit()
                            logger.debug(f"Session {session_id} updated with new history and title.")
                    except Exception as e:
                        logger.exception(f"Stream error: {str(e)}")
                        yield f"data: {json.dumps({'error': str(e)})}\n\n"

                return Response(event_stream(), mimetype="text/event-stream")

            except json.JSONDecodeError:
                logger.error(f"Invalid JSON in history for session {session_id}")
                return "Invalid chat history", 500

    finally:
        if conn:
            release_db_connection(conn)


@app.route('/image/<image_id>')
def get_image(image_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT image_data, mime_type 
                FROM images 
                WHERE image_id = %s
            """, (image_id,))
            result = cur.fetchone()
            if not result:
                return "Image not found", 404
            
            return Response(
                result[0],
                mimetype=result[1],
                headers={'Content-Disposition': 'inline'}
            )
    finally:
        release_db_connection(conn)
        

@app.route('/chat/session/<session_id>')
def load_chat(session_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Fetch target session
            cur.execute("""
                SELECT chat_history, chat_type 
                FROM sessions 
                WHERE session_id = %s
            """, (session_id,))
            result = cur.fetchone()
            if not result:
                return "Session not found", 404

            # Handle JSONB/list conversion
            chat_history = result['chat_history']
            if isinstance(chat_history, str):  # For legacy string data
                chat_history = json.loads(chat_history)

            # Process image references
            processed_history = []
            for msg in chat_history:
                parts = []
                for part in msg.get('parts', []):
                    if isinstance(part, dict) and part.get('type') == 'image_ref':
                        with conn.cursor() as img_cur:
                            img_cur.execute("""
                                SELECT image_data, mime_type 
                                FROM images 
                                WHERE image_id = %s
                            """, (part['image_id'],))
                            img_result = img_cur.fetchone()
                            if img_result:
                                parts.append({
                                    'type': 'image',
                                    'mime_type': img_result['mime_type'],
                                    'data': base64.b64encode(img_result['image_data']).decode('utf-8')
                                })
                    else:
                        parts.append(part)
                processed_history.append({
                    'role': msg['role'],
                    'parts': parts
                })

            # Fetch sessions (limit to 5)
            cur.execute("""
                SELECT session_id, chat_type, title, updated_at 
                FROM sessions 
                ORDER BY updated_at DESC 
                LIMIT 5
            """)
            all_sessions = cur.fetchall()

        config = chat_configs.get(result['chat_type'], {})
        return render_template('index.html', 
                            chat_history=processed_history,
                            session_id=session_id,
                            config=config,
                            sessions=all_sessions)
    except Exception as e:
        logger.exception(f"Error loading chat: {str(e)}")
        return "Internal server error", 500
    finally:
        release_db_connection(conn)

if __name__ == '__main__':
    app.run(debug=os.getenv("DEBUG_MODE", True))