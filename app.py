import json
import uuid
import psycopg2
from flask import Flask, request, render_template, redirect, url_for, session
from api_handler import get_gemini_response_with_history
from dotenv import load_dotenv
import os
import logging
import markdown2
from markupsafe import Markup

app = Flask(__name__)
app.secret_key = 'your_secret_key'

def markdown_filter(text):
    return Markup(markdown2.markdown(text))

app.jinja_env.filters['markdown'] = markdown_filter

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

load_dotenv()

# Database connection
def get_db_connection():
    return psycopg2.connect(os.getenv('DATABASE_URL'))

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
            user_id = generate_user_id()
            initial_history = [{"role": "model", "parts": "Hi! How can I help you today?"}]
            initial_history_json = json.dumps(initial_history)
            
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sessions (session_id, user_id, chat_history) VALUES (%s, %s, %s)",
                    (session_id, user_id, initial_history_json)
                )
                conn.commit()
                logger.debug(f"New session created - session_id: {session_id}")
            
            return render_template('index.html', 
                                 chat_history=initial_history,
                                 session_id=session_id)

        if request.method == 'POST':
            user_message = request.form.get('message')
            session_id = request.form.get('session_id')
            logger.debug(f"POST received - session_id: {session_id}, message: {user_message}")
            
            if not user_message or not session_id:
                return redirect(url_for('chat'))

            with conn.cursor() as cur:
                cur.execute("SELECT chat_history FROM sessions WHERE session_id = %s", (session_id,))
                result = cur.fetchone()
                
                if not result:
                    logger.error(f"Session not found: {session_id}")
                    return redirect(url_for('chat'))

                try:
                    if isinstance(result[0], str):
                        history = json.loads(result[0])
                    else:
                        history = result[0]
                        
                    # Add user message to history
                    history.append({"role": "user", "parts": user_message})
                    
                    # Get AI response and append it
                    reply, _ = get_gemini_response_with_history(user_message, history)
                    history.append({"role": "model", "parts": reply})
                    
                    # Convert updated history to JSON
                    history_json = json.dumps(history)
                    
                    # Update database
                    cur.execute(
                        """UPDATE sessions 
                        SET chat_history = %s, 
                            updated_at = CURRENT_TIMESTAMP 
                        WHERE session_id = %s""",
                        (history_json, session_id)
                    )
                    conn.commit()
                    logger.debug(f"History updated for session: {session_id}")
                    
                    return render_template('index.html', 
                                        chat_history=history,
                                        session_id=session_id)
                except json.JSONDecodeError as e:
                    logger.error(f"JSON decode error: {e}")
                    return "Invalid chat history format", 500

    except Exception as e:
        logger.exception(f"Error in chat route: {e}")
        return "An error occurred", 500
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    app.run(debug=True)