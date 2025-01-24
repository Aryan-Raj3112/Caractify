from dotenv import load_dotenv
import os
import google.generativeai as genai

load_dotenv()

# Configure the Gemini API with your API key
genai.configure(api_key=os.getenv("API_KEY"))

def get_gemini_response_with_history(user_message, history):
    """
    Generates a response based on the conversation history.

    Args:
        user_message (str): The user's current message.
        history (list): The list of previous messages in the conversation history.

    Returns:
        str: The generated response.
    """
    try:
        # Start a new chat with history or continue with existing history
        model = genai.GenerativeModel("gemini-1.5-flash")
        chat = model.start_chat(history=history)

        # Send the user's message to the chat model
        response = chat.send_message(user_message)
        
        # Update history with the user's message and model's reply
        history.append({"role": "user", "parts": user_message})
        history.append({"role": "model", "parts": response.text})

        return response.text, history
    
    except Exception as e:
        return f"Sorry, I encountered an error while generating a response: {e}", history
