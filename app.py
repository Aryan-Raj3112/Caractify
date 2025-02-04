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
from flask_login import LoginManager, login_user, current_user, logout_user, login_required
from models import User
import bcrypt
from flask_wtf.csrf import CSRFProtect
import copy
from flask import has_request_context, copy_current_request_context

load_dotenv()
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, log_level),
                    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__) 

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY')

csrf = CSRFProtect(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

def markdown_filter(text):
    if text is None:
        text = ""
    text = str(text).strip()
    if not text:
        return Markup("")
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

def get_or_create_user_id():
    if current_user.is_authenticated:
        return current_user.id
    
    return None

def validate_chat_history(history):
    validated = []
    for msg in history:
        if not isinstance(msg, dict):
            continue
        validated.append({
            'role': msg.get('role', 'user'),
            'parts': [p for p in msg.get('parts', []) if isinstance(p, dict)]
        })
    return validated

def cleanup_sessions():
    with app.app_context():
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                # Delete empty sessions older than 1 hour AND anonymous sessions older than 24h
                cur.execute("""
                    DELETE FROM sessions 
                    WHERE (
                        last_updated < NOW() - INTERVAL '1 hour' 
                        AND jsonb_array_length(chat_history) = 1
                    ) OR (
                        user_id IS NULL 
                        AND last_updated < NOW() - INTERVAL '24 hours'
                    )
                """)
                conn.commit()
        finally:
            release_db_connection(conn)

scheduler = BackgroundScheduler()
scheduler.add_job(func=cleanup_sessions, trigger="interval", hours=1)
scheduler.start()

@app.route('/')
def home():
    response = make_response(render_template('home_page.html', chat_configs=chat_configs))
    
    if not request.cookies.get('session_id'):
        new_session_id = str(uuid.uuid4())
        response.set_cookie('session_id', new_session_id, httponly=True, samesite='Strict', path='/')
    
    return response

@app.before_request
def validate_chat_type():
    if request.endpoint == 'chat':
        chat_type = request.view_args.get('chat_type')
        if chat_type not in chat_configs:
            return "Invalid chat type", 404

@app.route('/chat/<chat_type>')
def chat(chat_type):
    conn = None
    try:
        conn = get_db_connection()
        config = chat_configs.get(chat_type)
        if not config:
            return "Invalid chat type", 404

        session_id = request.cookies.get('session_id')
        all_sessions = []
        new_session = False

        if current_user.is_authenticated:
            # Always create new session for logged-in users accessing the chat endpoint
            new_session_id = generate_user_id()
            with conn.cursor() as cur:
                # Deactivate any existing active sessions
                cur.execute("""
                    UPDATE sessions 
                    SET is_active = FALSE 
                    WHERE user_id = %s 
                    AND chat_type = %s
                """, (current_user.id, chat_type))
                
                # Create fresh session
                cur.execute("""
                    INSERT INTO sessions 
                    (session_id, chat_history, chat_type, title, user_id, is_active)
                    VALUES (%s, %s, %s, %s, %s, TRUE)
                """, (
                    new_session_id,
                    json.dumps([]),
                    chat_type,
                    config.get('title', 'New Chat'),
                    current_user.id
                ))
                conn.commit()
                session_id = new_session_id
                new_session = True

            # Fetch recent inactive sessions
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT session_id, title 
                    FROM sessions 
                    WHERE user_id = %s 
                    AND chat_type = %s
                    AND is_active = FALSE
                    ORDER BY last_updated DESC 
                    LIMIT 5
                """, (current_user.id, chat_type))
                all_sessions = cur.fetchall()
        else:
            # Existing anonymous user logic
            session_id = session_id or generate_user_id()

        response = make_response(render_template(
            'index.html',
            chat_history=[],
            session_id=session_id,
            config=config,
            sessions=all_sessions
        ))
        
        if new_session or not request.cookies.get('session_id'):
            response.set_cookie('session_id', session_id, httponly=True, samesite='Strict', path='/')
        return response

    except Exception as e:
        logger.error(f"Error in chat route: {str(e)}")
        return "Internal server error", 500
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/stream', methods=['POST'])
def stream():
    logger.debug("Stream route accessed")
    session_id = request.form.get('session_id')
    cookie_session_id = request.cookies.get('session_id')

    if session_id != cookie_session_id:
        return "Unauthorized", 403

    # Capture context-dependent data here
    user_id = current_user.id if current_user.is_authenticated else None
    user_message = request.form.get('message', '').strip()
    chat_type = request.form.get('chat_type')
    image_file = request.files.get('image')
    image_data = None
    mime_type = None
    image_refs = []

    # Process image upload in request context
    if image_file and image_file.filename:
        allowed_mimes = {'image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif'}
        if image_file.mimetype in allowed_mimes:
            image_data = image_file.read()
            mime_type = image_file.mimetype

    def event_stream():
        nonlocal image_data, mime_type, image_refs
        conn = None
        try:
            conn = get_db_connection()
            config = chat_configs.get(chat_type)
            if not config:
                yield f"data: {json.dumps({'error': 'Invalid chat type'})}\n\n"
                return

            image_parts = []
            # Handle image insertion if data captured
            if image_data and mime_type:
                image_id = str(uuid.uuid4())
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO images (image_id, session_id, image_data, mime_type)
                        VALUES (%s, %s, %s, %s)
                    """, (image_id, session_id, image_data, mime_type))
                    conn.commit()

                encoded_image = base64.b64encode(image_data).decode('utf-8')
                image_parts.append({'mime_type': mime_type, 'data': encoded_image})
                image_refs.append({'type': 'image_ref', 'image_id': image_id})

            # Session creation/update
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO sessions (session_id, chat_history, chat_type, title, user_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (session_id) DO UPDATE SET
                        last_updated = CURRENT_TIMESTAMP,
                        is_active = TRUE
                """, (
                    session_id,
                    json.dumps([]),
                    chat_type,
                    config.get('title', 'New Chat'),
                    user_id  # Use captured user_id
                ))
                conn.commit()

                # Fetch chat history
                cur.execute("""
                    SELECT chat_history FROM sessions
                    WHERE session_id = %s AND user_id IS NOT DISTINCT FROM %s
                """, (session_id, user_id))
                result = cur.fetchone()

                if not result:
                    yield f"data: {json.dumps({'error': 'Session not found'})}\n\n"
                    return

                chat_history_data = result['chat_history']
                system_prompt = config.get('system_prompt', '')

            # Process message parts
            message_parts = []
            if user_message:
                message_parts.append({"type": "text", "content": user_message})
            message_parts.extend(image_refs)

            # Stream response
            complete_response = []
            for chunk in stream_gemini_response(message_parts, 
                                              chat_history_data, 
                                              system_prompt):
                if chunk.startswith('[ERROR'):
                    error_msg = chunk.replace('[ERROR', '').strip(']')
                    yield f"data: {json.dumps({'error': error_msg})}\n\n"
                    return
                yield f"data: {json.dumps({'chunk': chunk})}\n\n"
                complete_response.append(chunk)

            # Update session history
            updated_history = chat_history_data + [
                {"role": "user", "parts": message_parts},
                {"role": "model", "parts": [{"type": "text", "content": ''.join(complete_response)}]}
            ]

            # Update database
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE sessions 
                    SET chat_history = %s,
                        last_updated = CURRENT_TIMESTAMP,
                        is_active = TRUE
                    WHERE session_id = %s
                """, (json.dumps(updated_history), session_id))
                conn.commit()
                
            if len(updated_history) >= 2:
                # Get first user message for title
                first_user_message = next(
                    (part['content'] for msg in updated_history 
                    if msg['role'] == 'user' for part in msg['parts'] 
                    if part.get('type') == 'text'),
                    None
                )
                
                if first_user_message:
                    # Generate title from first message
                    title = first_user_message[:50].strip()
                    if len(first_user_message) > 50:
                        title += "..."
                        
                    with conn.cursor() as update_cur:
                        update_cur.execute("""
                            UPDATE sessions 
                            SET title = %s 
                            WHERE session_id = %s
                        """, (title, session_id))
                        conn.commit()

        except Exception as e:
            logger.error(f"Stream error: {str(e)}")
            yield f"data: {json.dumps({'error': 'Internal server error'})}\n\n"
        finally:
            if conn:
                release_db_connection(conn)

    return Response(event_stream(), mimetype="text/event-stream")

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
            # Deactivate all other sessions first
            cur.execute("""
                SELECT chat_history, chat_type 
                FROM sessions 
                WHERE session_id = %s
                AND user_id = %s
            """, (session_id, get_or_create_user_id()))
            result = cur.fetchone()
            if not result:
                return "Session not found", 404

            cur.execute("""
                UPDATE sessions 
                SET is_active = FALSE 
                WHERE user_id = %s 
                AND chat_type = (
                    SELECT chat_type FROM sessions WHERE session_id = %s
                )
            """, (get_or_create_user_id(), session_id))
            
            # Mark the session as active NOW, when it is loaded
            cur.execute("""
                UPDATE sessions 
                SET is_active = TRUE, last_updated = CURRENT_TIMESTAMP
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
        return jsonify({"status": "ignored"}), 200

    session_id = request.json.get('session_id')
    user_id = current_user.id
    conn = get_db_connection()
    
    try:
        with conn.cursor() as cur:
            # Check if session has actual content
            cur.execute("""
                SELECT chat_history FROM sessions 
                WHERE session_id = %s AND user_id = %s
            """, (session_id, user_id))
            
            result = cur.fetchone()
            if result:
                chat_history = json.loads(result['chat_history'])
                # Count actual user messages (not including empty messages)
                user_messages = [
                    m for m in chat_history 
                    if m['role'] == 'user' 
                    and any(part.get('content') for part in m['parts'])
                ]
                
                if len(user_messages) == 0:
                    # Delete completely empty sessions
                    cur.execute("""
                        DELETE FROM sessions 
                        WHERE session_id = %s AND user_id = %s
                    """, (session_id, user_id))
                else:
                    # Update title from first message if not set
                    first_message = next(
                        (part['content'] for msg in chat_history 
                         if msg['role'] == 'user' for part in msg['parts'] 
                         if part.get('type') == 'text'),
                        None
                    )
                    
                    if first_message:
                        title = first_message[:50].strip()
                        if len(first_message) > 50:
                            title += "..."
                            
                        cur.execute("""
                            UPDATE sessions 
                            SET is_active = FALSE, 
                                title = COALESCE(NULLIF(title, ''), %s),
                                last_updated = CURRENT_TIMESTAMP
                            WHERE session_id = %s
                        """, (title, session_id))
                conn.commit()
        
        return jsonify({"status": "success"}), 200
    except Exception as e:
        logger.error(f"Error saving session: {str(e)}")
        return jsonify({"error": str(e)}), 500
    finally:
        release_db_connection(conn)
            
            
@app.before_request
def validate_sessions():
    if request.endpoint in ['chat', 'load_chat']:
        session_id = request.cookies.get('session_id')
        if session_id:
            conn = get_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE sessions 
                        SET last_updated = NOW() 
                        WHERE session_id = %s 
                        AND user_id = %s
                    """, (session_id, get_or_create_user_id()))
                    conn.commit()
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
        return User(user_data['id'])  # Properly initialize User with ID
    return None

# Add new routes
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password'].encode('utf-8')

        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE email = %s", (email,))
                user_data = cur.fetchone()
        finally:
            release_db_connection(conn)

        if user_data and bcrypt.checkpw(password, user_data['password'].encode('utf-8')):
            user = User(user_data['id'])
            login_user(user)

            anonymous_id = request.cookies.get('anonymous_id')
            if anonymous_id:
                conn = get_db_connection()
                try:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE sessions 
                            SET user_id = %s 
                            WHERE user_id = %s
                        """, (user.id, anonymous_id))
                        conn.commit()
                        resp = make_response(redirect(url_for('home')))
                        resp.delete_cookie('anonymous_id')
                        return resp
                finally:
                    release_db_connection(conn)

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


@app.route('/api/sessions/<chat_type>')
def get_sessions(chat_type):
    conn = get_db_connection()
    try:
        if current_user.is_authenticated:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT session_id, title 
                    FROM sessions 
                    WHERE user_id = %s 
                    AND chat_type = %s
                    AND is_active = FALSE
                    ORDER BY last_updated DESC 
                    LIMIT 5
                """, (current_user.id, chat_type))
                sessions = cur.fetchall()
        else:
            # Anonymous users: get sessions by session_id cookie
            session_id = request.cookies.get('session_id')
            if not session_id:
                return jsonify([])
                
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT session_id, title 
                    FROM sessions 
                    WHERE session_id = %s
                    AND chat_type = %s
                    AND jsonb_array_length(chat_history) > 1
                    ORDER BY last_updated DESC 
                    LIMIT 5
                """, (session_id, chat_type))
                sessions = cur.fetchall()
        
        return jsonify([dict(session) for session in sessions])
    except Exception as e:
        logger.error(f"Error fetching sessions: {e}")
        return jsonify([])
    finally:
        release_db_connection(conn)
        

@app.route('/new_chat/<chat_type>', methods=['POST'])
@login_required
def new_chat(chat_type):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Ensure current session is saved first
            cur.execute("""
                UPDATE sessions 
                SET is_active = FALSE 
                WHERE user_id = %s 
                AND chat_type = %s
                AND is_active = TRUE
            """, (current_user.id, chat_type))
            
            # Create new session with empty history
            new_session_id = generate_user_id()
            cur.execute("""
                INSERT INTO sessions 
                (session_id, chat_history, chat_type, title, user_id, is_active)
                VALUES (%s, %s, %s, %s, %s, TRUE)
            """, (
                new_session_id,
                json.dumps([]),
                chat_type,
                'New Chat',  # Temporary title
                current_user.id
            ))
            conn.commit()
            
        return jsonify({
            "status": "success",
            "new_session_id": new_session_id
        })
    except Exception as e:
        logger.error(f"Error creating new chat: {str(e)}")
        return jsonify({"error": str(e)}), 500
    finally:
        release_db_connection(conn)
        
        
def cleanup_sessions():
    with app.app_context():
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM sessions 
                    WHERE (
                        last_updated < NOW() - INTERVAL '1 hour' 
                        AND jsonb_array_length(chat_history) <= 1
                    ) OR (
                        user_id IS NULL 
                        AND last_updated < NOW() - INTERVAL '24 hours'
                    )
                """)
                conn.commit()
        finally:
            release_db_connection(conn)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('home'))

if __name__ == '__main__':
    app.run(debug=os.getenv("DEBUG_MODE", True))