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
    conn = None
    try:
        conn = get_db_connection()
        session_id = request.args.get('session_id', '').strip()
        user_message = request.args.get('message', '').strip()

        if not user_message or not session_id:
            return "Invalid request", 400

        with conn.cursor() as cur:
            cur.execute("SELECT chat_history FROM sessions WHERE session_id = %s", (session_id,))
            result = cur.fetchone()
            if not result:
                return "Session not found", 404

            try:
                history = json.loads(result[0]) if isinstance(result[0], str) else result[0]

                def event_stream():
                    stream_conn = get_db_connection()
                    try:
                        complete_response = []
                        for chunk in stream_gemini_response(user_message, history):
                            if chunk.startswith('[ERROR'):
                                yield f"event: error\ndata: {json.dumps({'error': chunk})}\n\n"
                                return
                            complete_response = [chunk]  # Replace instead of append
                            yield f"data: {json.dumps({'chunk': chunk})}\n\n"

                        # Update history only once at the end
                        updated_history = history + [
                            {"role": "user", "parts": user_message},
                            {"role": "model", "parts": chunk}  # Use final chunk
                        ]

                        with stream_conn.cursor() as cur:
                            cur.execute(
                                "UPDATE sessions SET chat_history = %s WHERE session_id = %s",
                                (json.dumps(updated_history), session_id)
                            )
                            stream_conn.commit()

                    except Exception as e:
                        logger.error(f"Stream error: {str(e)}")
                        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
                    finally:
                        release_db_connection(stream_conn)

                return Response(event_stream(), mimetype="text/event-stream")

            except json.JSONDecodeError:
                logger.error(f"Invalid JSON in history for session {session_id}")
                return "Invalid chat history", 500

    except Exception as e:
        logger.error(f"Stream setup error: {str(e)}")
        return "Internal server error", 500
    finally:
        if conn:
            release_db_connection(conn)

if __name__ == '__main__':
    app.run(debug=True)