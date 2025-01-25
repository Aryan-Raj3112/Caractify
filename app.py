from flask import Flask, request, render_template, redirect, url_for, session
from api_handler import get_gemini_response_with_history

app = Flask(__name__)
app.secret_key = 'your_secret_key'  # Required for Flask session

@app.route('/home')
def home():
    return render_template('home.html')

@app.route('/chat', methods=['POST'])
def chat():
    if 'history' not in session:
        session['history'] = [
            {"role": "model", "parts": "Hi! How can I help you today?"}
        ]
    
    user_message = request.form.get('message')
    if not user_message:
        return redirect(url_for('index'))

    # Get the AI's response and update history
    reply, updated_history = get_gemini_response_with_history(user_message, session['history'])
    
    # Update the session history
    session['history'] = updated_history

    return redirect(url_for('index'))

@app.route('/reset', methods=['POST'])
def reset():
    session['history'] = [
        {"role": "model", "parts": "Hi! How can I help you today?"}
    ]
    return redirect(url_for('index'))

@app.route('/')
def index():
    return render_template('index.html', chat_history=session.get('history', []))

if __name__ == '__main__':
    app.run(debug=True)




