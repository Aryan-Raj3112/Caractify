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

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY')

def markdown_filter(text):
    # Configure markdown with specific extensions
    return Markup(markdown2.markdown(text, extras=[
        'break-on-newline',
        'code-friendly',
        'fenced-code-blocks',
        'tables',
        'smarty-pants',
        'cuddled-lists'
    ]))

app.jinja_env.filters['markdown'] = markdown_filter

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
    return render_template('home_page.html')

@app.route('/chat', methods=['GET'])
def chat():
    conn = get_db_connection()
    try:
        session_id = generate_user_id()
        initial_history = [{"role": "model", "parts": "Hi! How can I help you today?"}]
        initial_history_json = json.dumps(initial_history)
        
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (session_id, chat_history) VALUES (%s, %s)",
                (session_id, initial_history_json)
            )
            conn.commit()
        
        return render_template('index.html', 
                            chat_history=initial_history,
                            session_id=session_id)
    finally:
        release_db_connection(conn)

@app.route('/stream', methods=['GET'])
def stream():
    conn = get_db_connection()
    try:
        # Safely get parameters with defaults
        session_id = request.args.get('session_id', '').strip()
        raw_message = request.args.get('message', '')
        user_message = raw_message.strip()

        # Validate before proceeding
        if not user_message or not session_id:
            logger.error(f"Invalid request - Message: '{raw_message}' (session: {session_id})")
            return "Invalid request", 400

        # Add debug logging here
        logger.debug(f"Processing message: '{user_message}' (session: {session_id})")

        with conn.cursor() as cur:
            cur.execute("SELECT chat_history FROM sessions WHERE session_id = %s", (session_id,))
            result = cur.fetchone()
            if not result:  # Handle missing session
                logger.error(f"Session not found: {session_id}")
                return "Session not found", 404
            history = result[0]
            logger.debug(f"Chat history length: {len(history)}")

            def event_stream():
                conn = get_db_connection()
                try:
                    full_response = []
                    response_generator = stream_gemini_response(user_message, history)
                    
                    try:
                        for chunk in response_generator:
                            yield f"data: {json.dumps({'chunk': chunk})}\n\n"
                            full_response.append(chunk)
                            
                        # Successful completion
                        updated_history = history + [
                            {"role": "user", "parts": user_message},
                            {"role": "model", "parts": "".join(full_response)}
                        ]
                        
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE sessions SET chat_history = %s WHERE session_id = %s",
                                (json.dumps(updated_history), session_id)
                            )
                            conn.commit()
                        
                        yield "event: end\ndata: done\n\n"
                        
                    except Exception as e:
                        logger.error(f"Stream error: {str(e)}")
                        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
                        
                finally:
                    release_db_connection(conn)

        return Response(event_stream(), mimetype="text/event-stream")
    
    except Exception as e:
        logger.error(f"Stream setup failed: {str(e)}")
        return "Internal error", 500

if __name__ == '__main__':
    app.run(debug=True)