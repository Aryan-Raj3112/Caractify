from dotenv import load_dotenv
import os
import google.generativeai as genai

load_dotenv()

# Configure the Gemini API with your API key
genai.configure(api_key=os.getenv("API_KEY"))

def get_gemini_response_with_history(user_message, history):
    try:
        model = genai.GenerativeModel("gemini-1.5-flash", 
                                    system_instruction="You are a personal assistant. Your name is Orion.")
        chat = model.start_chat(history=history)
        response = chat.send_message(user_message)
        return response.text, history
    except genai.types.BlockedPromptException:
        return "This request was blocked for safety reasons.", history
    except Exception as e:
        # Log the full error for debugging
        print(f"Gemini API Error: {str(e)}")  
        return "An error occurred while generating a response.", history
