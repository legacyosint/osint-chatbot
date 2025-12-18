import os
from flask import Flask, request, jsonify
import google.generativeai as genai
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # Allows your frontend to talk to this backend

# 1. SETUP: Load the API Key from a safe place (Environment Variable)
# NEVER hardcode the key here like "AIzaSy..."
api_key = os.getenv("GEMINI_API_KEY") 

genai.configure(api_key=api_key)

# 2. CONFIG: OSINT Specific System Instructions
# This tells the AI how to behave.
generation_config = {
    "temperature": 0.7,
    "top_p": 0.95,
    "top_k": 64,
    "max_output_tokens": 8192,
}

system_instruction = """
You are 'OSINT-MIND', a senior intelligence analyst mentor. 
Your goal is to help students learn Open Source Intelligence techniques ethically and legally.

GUIDELINES:
1. METHODOLOGY: When asked how to find info, do not just give the answer. Explain the *method* (e.g., "Use reverse image search," "Check WHOIS records").
2. ETHICS: If a user asks for illegal actions (doxing private citizens, hacking databases), firmly refuse and explain *why* it is unethical/illegal.
3. TOOLS: Recommend standard OSINT tools (Maltego, Shodan, TheHarvester, Wayback Machine) where appropriate.
4. SAFETY: Warn users about Operational Security (OPSEC) (e.g., "Remember to use a VPN/Sockpuppet account when researching this").
"""

model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    generation_config=generation_config,
    system_instruction=system_instruction,
)

# 3. THE ENDPOINT: This is the door your frontend knocks on
@app.route('/chat', methods=['POST'])
def chat():
    user_input = request.json.get('message')
    
    if not user_input:
        return jsonify({"error": "No message provided"}), 400

    try:
        # Send message to Gemini
        response = model.generate_content(user_input)
        return jsonify({"reply": response.text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(port=5000)
