import markdown
from flask import Flask, request, render_template, redirect, url_for, session
from api_handler import get_gemini_response_with_history

app = Flask(__name__)
app.secret_key = 'your_secret_key'

# Initialize session history on app start or refresh
@app.before_request
def initialize_session():
    if 'history' not in session:
        session['history'] = [
            {"role": "model", "parts": "Hi! How can I help you today?"}
        ]

@app.route('/')
def home():
    """Renders the home page with a button to start the chat."""
    return render_template('home_page.html')

@app.route('/chat', methods=['GET', 'POST'])
def chat():
    if request.method == 'GET':  # Reset on refresh
        return redirect(url_for('reset'))

    if request.method == 'POST':
        user_message = request.form.get('message')
        if not user_message:
            return redirect(url_for('reset'))
        reply, updated_history = get_gemini_response_with_history(user_message, session['history'])
        session['history'] = updated_history
    
    for message in session['history']:
        message['parts'] = markdown.markdown(message['parts'], output_format='html')
    return render_template('index.html', chat_history=session.get('history', []))

@app.route('/reset', methods=['GET'])
def reset():
    session.clear()
    session['history'] = [
        {"role": "model", "parts": "Hi! How can I help you today?"}
    ]
    return render_template('index.html', chat_history=session['history'])

if __name__ == '__main__':
    app.run(debug=True)
