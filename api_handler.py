from dotenv import load_dotenv
import os
import google.generativeai as genai

load_dotenv()

# Configure the Gemini API with your API key
genai.configure(api_key=os.getenv("API_KEY"))

def get_gemini_response_with_history(user_message, history):
    """Generates a response based on the conversation history."""
    try:
        model = genai.GenerativeModel("gemini-1.5-flash", 
                                    system_instruction="You are a personal assistant. Your name is Orion.")
        chat = model.start_chat(history=history)
        response = chat.send_message(user_message)
        return response.text, history
    except Exception as e:
        return f"Sorry, I encountered an error: {e}", history
