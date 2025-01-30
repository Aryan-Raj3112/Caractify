import google.generativeai as genai
from dotenv import load_dotenv
import os
from typing import Generator

load_dotenv()
genai.configure(api_key=os.getenv("API_KEY"))

# In api_handler.py
def stream_gemini_response(user_input: str, chat_history: list) -> Generator[str, None, None]:
    try:
        if not user_input.strip():
            yield "[ERROR: Message cannot be empty]"
            return

        formatted_history = []
        for msg in chat_history:
            formatted_history.append({
                'role': msg['role'],
                'parts': [msg['parts']] if isinstance(msg['parts'], str) else msg['parts']
            })

        model = genai.GenerativeModel("gemini-1.5-flash")
        chat = model.start_chat(history=formatted_history)
        response = chat.send_message(user_input, stream=True)
        
        complete_response = []
        for chunk in response:
            if chunk.text:
                # Fix markdown formatting
                text = chunk.text.replace('**', '**') # Ensure proper bold syntax
                text = text.replace('* ', '* ')       # Fix list items
                complete_response.append(text)
                yield ' '.join(complete_response)
        
    except Exception as e:
        yield f"[ERROR: {str(e)}]"