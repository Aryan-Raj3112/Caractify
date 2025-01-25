from flask import Flask, request, render_template, redirect, url_for, session
from api_handler import get_gemini_response_with_history

app = Flask(__name__)
app.secret_key = 'your_secret_key'  # Required for Flask session

@app.route('/')
def home():
    """Renders the home page with a button to start the chat."""
    return render_template('home_page.html')

@app.route('/chat', methods=['GET', 'POST'])
def chat():
    """Handles the chat page and conversation logic."""
    if 'history' not in session:
        session['history'] = [
            {"role": "model", "parts": "Hi! How can I help you today?"}
        ]
    
    if request.method == 'POST':
        user_message = request.form.get('message')
        if not user_message:
            return redirect(url_for('chat'))

        # Get the AI's response and update history
        reply, updated_history = get_gemini_response_with_history(user_message, session['history'])
        
        # Update the session history
        session['history'] = updated_history

    return render_template('index.html', chat_history=session.get('history', []))

@app.route('/reset', methods=['POST'])
def reset():
    """Resets the chat history."""
    session['history'] = [
        {"role": "model", "parts": "Hi! How can I help you today?"}
    ]
    return redirect(url_for('chat'))

if __name__ == '__main__':
    app.run(debug=True)