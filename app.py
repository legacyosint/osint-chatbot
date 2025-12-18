import os
import sqlite3
import base64
import io
from flask import Flask, request, jsonify
from flask_cors import CORS
import google.generativeai as genai
from PIL import Image

app = Flask(__name__)
CORS(app)

# --- CONFIGURATION ---
api_key = os.getenv("GEMINI_API_KEY") 
genai.configure(api_key=api_key)

# Safety & System Instructions
generation_config = {
    "temperature": 0.7,
    "max_output_tokens": 4096,
}
system_instruction = """
You are 'OSINT-MIND', a cyber-intelligence analyst. 
Format your responses using Markdown. Be concise, technical, and precise.
If an image is provided, analyze it for OSINT clues (metadata, landmarks, text).
"""
model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    generation_config=generation_config,
    system_instruction=system_instruction,
)

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect('chat_history.db')
    c = conn.cursor()
    # Create table for sessions
    c.execute('''CREATE TABLE IF NOT EXISTS sessions 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT)''')
    # Create table for messages
    c.execute('''CREATE TABLE IF NOT EXISTS messages 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, 
                  sender TEXT, content TEXT, has_image BOOLEAN)''')
    conn.commit()
    conn.close()

init_db()

# --- HELPER FUNCTIONS ---
def process_image(image_file):
    """Converts uploaded file to format Gemini accepts"""
    img = Image.open(image_file)
    return img

# --- ROUTES ---

@app.route('/sessions', methods=['GET', 'POST'])
def handle_sessions():
    conn = sqlite3.connect('chat_history.db')
    c = conn.cursor()
    
    if request.method == 'POST':
        # Create new session
        c.execute("INSERT INTO sessions (title) VALUES (?)", ("New Investigation",))
        session_id = c.lastrowid
        conn.commit()
        conn.close()
        return jsonify({"id": session_id, "title": "New Investigation"})
    
    else:
        # Get all sessions
        c.execute("SELECT * FROM sessions ORDER BY id DESC")
        sessions = [{"id": row[0], "title": row[1]} for row in c.fetchall()]
        conn.close()
        return jsonify(sessions)

@app.route('/history/<int:session_id>', methods=['GET'])
def get_history(session_id):
    conn = sqlite3.connect('chat_history.db')
    c = conn.cursor()
    c.execute("SELECT sender, content FROM messages WHERE session_id = ?", (session_id,))
    messages = [{"sender": row[0], "content": row[1]} for row in c.fetchall()]
    conn.close()
    return jsonify(messages)

@app.route('/chat', methods=['POST'])
def chat():
    session_id = request.form.get('session_id')
    user_text = request.form.get('message')
    image_file = request.files.get('image')

    # Prepare input for Gemini
    gemini_input = [user_text]
    has_image = False
    
    if image_file:
        img = process_image(image_file)
        gemini_input.append(img)
        has_image = True

    try:
        # Generate AI Response
        response = model.generate_content(gemini_input)
        ai_reply = response.text

        # Save to DB
        conn = sqlite3.connect('chat_history.db')
        c = conn.cursor()
        
        # Save User Message
        c.execute("INSERT INTO messages (session_id, sender, content, has_image) VALUES (?, ?, ?, ?)",
                  (session_id, "user", user_text, has_image))
        
        # Save AI Message
        c.execute("INSERT INTO messages (session_id, sender, content, has_image) VALUES (?, ?, ?, ?)",
                  (session_id, "bot", ai_reply, False))
        
        # Update Session Title if it's the first message
        c.execute("SELECT count(*) FROM messages WHERE session_id = ?", (session_id,))
        count = c.fetchone()[0]
        if count <= 2:
            # Generate a short title based on the query
            title_prompt = f"Summarize this into a 3-word title: {user_text}"
            title_resp = model.generate_content(title_prompt)
            clean_title = title_resp.text.strip().replace('"','')
            c.execute("UPDATE sessions SET title = ? WHERE id = ?", (clean_title, session_id))

        conn.commit()
        conn.close()

        return jsonify({"reply": ai_reply})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(port=5000, debug=True)
