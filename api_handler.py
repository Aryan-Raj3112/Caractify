import google.generativeai as genai
from dotenv import load_dotenv
import os
from typing import Generator
import json
import logging

load_dotenv()
genai.configure(api_key=os.getenv("API_KEY"))

# In api_handler.py
def stream_gemini_response(user_input: str, chat_history: list, system_prompt: str) -> Generator[str, None, None]:
    try:
        if not user_input.strip():
            yield "[ERROR: Message cannot be empty]"
            return

        # Ensure chat_history is a list
        if isinstance(chat_history, str):
            chat_history = json.loads(chat_history)

        formatted_history = []
        for msg in chat_history:
            formatted_history.append({
                'role': msg['role'],
                'parts': [msg['parts']] if isinstance(msg['parts'], str) else msg['parts']
            })

        model = genai.GenerativeModel(
            "gemini-1.5-flash",
            system_instruction=system_prompt
        )
        chat = model.start_chat(history=formatted_history)
        response = chat.send_message(user_input, stream=True)
        
        full_response = ""
        for chunk in response:
            if chunk.text:
                # Accumulate the full response
                full_response += chunk.text
                # Yield the complete accumulated response
                yield full_response

    except Exception as e:
        logging.error(f"Stream error: {str(e)}")
        yield f"[ERROR: {str(e)}]"