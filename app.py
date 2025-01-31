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
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        local_dt = dt.astimezone()  # Convert to local timezone
        return local_dt.strftime("%H:%M · %b %d")
    except Exception as e:
        logger.error(f"Time format error: {str(e)}")
        return ""

app.jinja_env.filters['datetimeformat'] = format_datetime

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

load_dotenv()

# Connection pool
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


@app.route('/')
def home():
    return render_template('home_page.html', chat_configs=chat_configs)


@app.route('/chat/<chat_type>')
def chat(chat_type):
    conn = get_db_connection()
    try:
        config = chat_configs.get(chat_type)
        if not config:
            return "Invalid chat type", 404

        session_id = generate_user_id()
        initial_history = [{
            "role": "model",
            "parts": [{"type": "text", "content": config['welcome_message']}], 
            "timestamp": datetime.now(timezone.utc).isoformat()
        }]
        initial_history_json = json.dumps(initial_history)
        for message in initial_history:
            for part in message.get("parts", []):
                print(f"Debug: part.content type -> {type(part.get('content'))}, value -> {part.get('content')}")
        with conn.cursor() as cur:
            # Ensure sessions table has 'chat_type' column
            cur.execute(
                "INSERT INTO sessions (session_id, chat_history, chat_type) VALUES (%s, %s, %s)",
                (session_id, initial_history_json, chat_type)
            )
            conn.commit()
        
        return render_template('index.html', 
                            chat_history=initial_history,
                            session_id=session_id,
                            config=config)
    finally:
        release_db_connection(conn)


@app.route('/stream', methods=['POST'])
def stream():
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
            image_refs.append({
                'type': 'image_ref',
                'image_id': image_id,
                'mime_type': mime_type
            })

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

            chat_history_data, chat_type = result[0], result[1]
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
                            {
                                "role": "user",
                                "parts": message_parts,  # Use original message parts
                                "timestamp": datetime.now(timezone.utc).isoformat()
                            },
                            {
                                "role": "model",
                                "parts": [{"type": "text", "content": ''.join(complete_response)}],
                                "timestamp": datetime.now(timezone.utc).isoformat()
                            }
                        ]
                        updated_history[-2]['parts'] = [p for p in updated_history[-2]['parts'] if p]

                        with conn.cursor() as cur:
                            cur.execute("UPDATE sessions SET chat_history = %s WHERE session_id = %s", (json.dumps(updated_history), session_id))
                            conn.commit()

                    except Exception as e:
                        logger.error(f"Stream error: {str(e)}")
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

if __name__ == '__main__':
    app.run(debug=True)