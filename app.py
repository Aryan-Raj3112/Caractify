import json
import uuid
import psycopg2
from flask import Flask, request, render_template, redirect, url_for
from api_handler import get_gemini_response_with_history
import json
import uuid
import psycopg2
from flask import Flask, request, render_template, redirect, url_for  # Removed 'session'
from api_handler import get_gemini_response_with_history
from dotenv import load_dotenv
import os
import logging
import markdown2
from markupsafe import Markup
from psycopg2 import pool
import bleach



app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY')

def markdown_filter(text):
    return Markup(markdown2.markdown(text))

app.jinja_env.filters['bleach_clean'] = bleach.clean

app.jinja_env.filters['markdown'] = markdown_filter

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

load_dotenv()

# Connection pool
connection_pool = psycopg2.pool.SimpleConnectionPool(
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

@app.route('/chat', methods=['GET', 'POST'])
def chat():
    conn = get_db_connection()
    try:
        if request.method == 'GET':
            session_id = generate_user_id()
            initial_history = [{"role": "model", "parts": "Hi! How can I help you today?"}]
            initial_history_json = json.dumps(initial_history)
            
            with conn:  # ✅ Context manager handles transactions
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO sessions (session_id, chat_history) VALUES (%s, %s)",
                        (session_id, initial_history_json)
                    )
                    logger.debug(f"New session created: {session_id}")
            
            return render_template('index.html', 
                                 chat_history=initial_history,
                                 session_id=session_id)

        elif request.method == 'POST':
            user_message = request.form.get('message', '').strip()
            session_id = request.form.get('session_id')
            
            if not user_message or not session_id:
                return redirect(url_for('chat'))

            with conn:  # ✅ Context manager handles transactions
                with conn.cursor() as cur:
                    cur.execute("SELECT chat_history FROM sessions WHERE session_id = %s", (session_id,))
                    result = cur.fetchone()
                    
                    if not result:
                        logger.error(f"Session not found: {session_id}")
                        return redirect(url_for('chat'))

                    history = result[0]
                    history.append({"role": "user", "parts": user_message})
                    
                    reply, _ = get_gemini_response_with_history(user_message, history)
                    history.append({"role": "model", "parts": reply})
                    
                    cur.execute(
                        """UPDATE sessions 
                        SET chat_history = %s, updated_at = CURRENT_TIMESTAMP 
                        WHERE session_id = %s""",
                        (json.dumps(history), session_id)
                    )
                
            return render_template('index.html', 
                                chat_history=history,
                                session_id=session_id)

    except Exception as e:
        logger.exception(f"Error in chat route: {e}")
        return "An error occurred", 500
    finally:
        if conn:
            release_db_connection(conn)

if __name__ == '__main__':
    app.run(debug=True)