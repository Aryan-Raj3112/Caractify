import json
import uuid
from flask import Flask, redirect, request, render_template, Response, url_for, g, jsonify
from api_handler import stream_gemini_response
from dotenv import load_dotenv
import os
import logging
import markdown2
from markupsafe import Markup
from psycopg2 import pool
from chat_configs import chat_configs
import base64
from psycopg2.extras import DictCursor
from flask import make_response
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
from flask_login import LoginManager, login_user, current_user, logout_user, login_required
from models import User
import bcrypt

load_dotenv()
log_level = os.getenv("LOG_LEVEL", "INFO").upper()  # Get log level from env, default to INFO
logging.basicConfig(level=getattr(logging, log_level),  # Use getattr for dynamic level
                    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__) 

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY')

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

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

connection_pool = pool.ThreadedConnectionPool(
    minconn=3,
    maxconn=20,
    dsn=os.getenv('DATABASE_URL')
)

@app.teardown_appcontext
def close_conn(e):
    conn = g.pop('db_conn', None)
    if conn is not None:
        release_db_connection(conn)

def get_db_connection():
    conn = connection_pool.getconn()
    conn.cursor_factory = DictCursor
    return conn

def release_db_connection(conn):
    connection_pool.putconn(conn)

def generate_user_id():
    return str(uuid.uuid4())

def get_or_create_user_id():
    # Priority 1: Logged-in user
    if current_user.is_authenticated:
        return current_user.id
    
    # Priority 2: Existing cookie for anonymous user
    user_id = request.cookies.get('user_id')
    if user_id:
        return user_id
    
    # Priority 3: Generate new anonymous user ID
    new_user_id = generate_user_id()
    g.user_id = new_user_id  # Store in request context
    return new_user_id

def cleanup_sessions():
    with app.app_context():
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                # Delete empty sessions older than 1 hour
                cur.execute("""
                    DELETE FROM sessions 
                    WHERE last_updated < NOW() - INTERVAL '1 hour'
                    AND jsonb_array_length(chat_history) = 1
                """)
                conn.commit()
        finally:
            release_db_connection(conn)

scheduler = BackgroundScheduler()
scheduler.add_job(func=cleanup_sessions, trigger="interval", hours=1)
scheduler.start()

@app.route('/')
def home():
    user_id = get_or_create_user_id()
    response = make_response(render_template('home_page.html', chat_configs=chat_configs))
    if not request.cookies.get('user_id'):
        response.set_cookie('user_id', user_id, httponly=True, samesite='Strict', max_age=31536000, path='/')  # 1 year
    return response


@app.route('/chat/<chat_type>')
def chat(chat_type):
    user_id = get_or_create_user_id()
    conn = None  # Initialize conn outside try block
    try:
        conn = get_db_connection()  # Inside try block
        config = chat_configs.get(chat_type)
        if not config:
            return "Invalid chat type", 404

        # Check for existing ACTIVE session
        session_id = request.cookies.get('session_id')
        if session_id:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT session_id FROM sessions 
                    WHERE session_id = %s 
                    AND user_id = %s 
                    AND is_active = TRUE
                """, (session_id, user_id))
                if cur.fetchone():
                    return redirect(url_for('load_chat', session_id=session_id))

        # Create new session
        session_id = generate_user_id()
        initial_history = [{
            "role": "model",
            "parts": [{"type": "text", "content": config['welcome_message']}],
        }]
        
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sessions 
                (session_id, chat_history, chat_type, title, is_active, user_id)
                VALUES (%s, %s, %s, %s, TRUE, %s)
            """, (session_id, json.dumps(initial_history), chat_type, config['title'], user_id))
            conn.commit()

        # Fetch saved sessions (both active and inactive)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT session_id, title 
                FROM sessions 
                WHERE user_id = %s 
                AND chat_type = %s 
                ORDER BY last_updated DESC 
                LIMIT 5
            """, (user_id, chat_type))
            all_sessions = cur.fetchall() # Get all sessions

        response = make_response(render_template('index.html',  # Render template
                                                chat_history=initial_history,
                                                session_id=session_id,
                                                config=config,
                                                sessions=all_sessions)) # Pass sessions to template
        
        response.set_cookie(
            'user_id', 
            user_id, 
            httponly=True, 
            samesite='Strict', 
            max_age=31536000, 
            path='/'  # Explicit path
        )
        response.set_cookie(
            'session_id', 
            session_id, 
            httponly=True, 
            samesite='Strict', 
            path='/'  # Explicit path
        )
        
        return response

    except Exception as e:
        logger.error(f"Error in chat route: {str(e)}")
        return "Internal server error", 500
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
                                SET chat_history = %s, title = %s  -- Update session_id
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

            # Mark the session as active NOW, when it is loaded
            cur.execute("""
                UPDATE sessions 
                SET is_active = TRUE, last_updated = CURRENT_TIMESTAMP  -- Update last_updated as well
                WHERE session_id = %s
            """, (session_id,))
            conn.commit()

            # Handle JSONB/list conversion
            chat_history = result['chat_history']
            if isinstance(chat_history, str):  # For legacy string data
                chat_history = json.loads(chat_history)

            # Process image references (no changes needed here)
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
                SELECT session_id, title 
                FROM sessions 
                WHERE chat_type = %s 
                AND is_active = FALSE 
                ORDER BY last_updated DESC 
                LIMIT 5
            """, (result['chat_type'],))
            all_sessions = cur.fetchall()

            config = chat_configs.get(result['chat_type'], {})
            response = make_response(render_template('index.html', 
                chat_history=processed_history,
                session_id=session_id,
                config=config,
                sessions=all_sessions))
            # Set the session cookie here
            response.set_cookie('session_id', session_id, httponly=True, samesite='Strict')
            return response

    except Exception as e:
        logger.exception(f"Error loading chat: {str(e)}")
        return "Internal server error", 500
    finally:
        release_db_connection(conn)
        

@app.route('/save_session', methods=['POST'])
def save_session():
    if not current_user.is_authenticated:
        return jsonify({"status": "ignored"}), 200  # Do nothing for anonymous users

    session_id = request.json.get('session_id')
    user_id = current_user.id  # Use logged-in user's ID
    conn = get_db_connection()
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT chat_history FROM sessions 
                WHERE session_id = %s AND user_id = %s
            """, (session_id, user_id))
            
            result = cur.fetchone()
            if result:
                chat_history = json.loads(result['chat_history'])
                user_messages = [m for m in chat_history if m['role'] == 'user']
                
                if not user_messages:
                    cur.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))
                else:
                    cur.execute("""
                        UPDATE sessions 
                        SET is_active = FALSE 
                        WHERE session_id = %s
                    """, (session_id,))
                conn.commit()
        
        # Add return statement
        return jsonify({"status": "success"}), 200
    except Exception as e:
        logger.error(f"Error saving session: {str(e)}")
        return jsonify({"error": str(e)}), 500
    finally:
        release_db_connection(conn)
            
            
@app.before_request
def validate_session():
    if request.endpoint in ['chat', 'load_chat', 'stream']:
        session_id = request.cookies.get('session_id')
        user_id = get_or_create_user_id()  # Get current user ID (logged-in or cookie)
        
        if not session_id:
            return  # Allow new sessions to be created

        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                # Match session to the CURRENT user_id (never NULL)
                cur.execute("""
                    SELECT 1 FROM sessions 
                    WHERE session_id = %s 
                    AND user_id = %s  -- Remove "OR user_id IS NULL"
                """, (session_id, user_id))
                if not cur.fetchone():
                    # Invalid session: clear cookie and redirect
                    response = make_response(redirect(url_for('home')))
                    response.delete_cookie('session_id', path='/')
                    return response
        finally:
            release_db_connection(conn)

@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = 'http://localhost:5000'  # Your origin
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

@login_manager.user_loader
def load_user(user_id):
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        user_data = cur.fetchone()
    release_db_connection(conn)
    if user_data:
        user = User()
        user.id = user_data['id']
        return user
    return None

# Add new routes
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password'].encode('utf-8')
        
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE email = %s", (email,))
            user_data = cur.fetchone()
        release_db_connection(conn)
        
        if user_data and bcrypt.checkpw(password, user_data['password'].encode('utf-8')):
            user = User()
            user.id = user_data['id']
            login_user(user)
            return redirect(url_for('home'))
        
        return "Invalid credentials", 401
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form['email']
        password = bcrypt.hashpw(request.form['password'].encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        user_id = generate_user_id()
        
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO users (id, email, password)
                    VALUES (%s, %s, %s)
                """, (user_id, email, password))
                conn.commit()
            return redirect(url_for('login'))
        except Exception as e:
            conn.rollback()
            return "Registration failed", 400
        finally:
            release_db_connection(conn)
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('home'))

if __name__ == '__main__':
    app.run(debug=os.getenv("DEBUG_MODE", True))