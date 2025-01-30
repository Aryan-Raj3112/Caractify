import os

script_dir = os.path.dirname(os.path.abspath(__file__)) 

chat_configs = {
    'assistant': {
        'system_prompt': "You are a helpful assistant. Provide clear and concise answers.",
        'title': "Orion",
        'image': os.path.join(script_dir, 'static', 'gemini.webp'),
        'welcome_message': "Hi! How can I help you today?"
    },
    'chef': {
        'system_prompt': "You are a professional chef. Provide detailed recipes and cooking tips.",
        'title': "Chef Bot",
        'image': os.path.join(script_dir, 'static', 'chef.webp'),
        'welcome_message': "Hello! Ready to cook something delicious?"
    }
}