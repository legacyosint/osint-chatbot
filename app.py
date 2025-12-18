import os
import psycopg2
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import google.generativeai as genai
from PIL import Image

app = Flask(__name__)
CORS(app)

# --- CONFIGURATION ---
# Get keys from Environment Variables (set these in Render)
api_key = os.getenv("GEMINI_API_KEY") 
db_url = os.getenv("DATABASE_URL") 

genai.configure(api_key=api_key)

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

# --- DATABASE CONNECTION ---
def get_db_connection():
    conn = psycopg2.connect(db_url)
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    # PostgreSQL syntax: SERIAL is used for auto-increment
    c.execute('''CREATE TABLE IF NOT EXISTS sessions 
                 (id SERIAL PRIMARY KEY, title TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages 
                 (id SERIAL PRIMARY KEY, session_id INTEGER, 
                  sender TEXT, content TEXT, has_image BOOLEAN)''')
    conn.commit()
    c.close()
    conn.close()

# Initialize DB on startup (safely)
try:
    init_db()
except Exception as e:
    print(f"DB Init Error (Ignore if running locally without DB setup): {e}")

# --- HELPER FUNCTIONS ---
def process_image(image_file):
    img = Image.open(image_file)
    return img

# --- ROUTES ---

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/sessions', methods=['GET', 'POST'])
def handle_sessions():
    conn = get_db_connection()
    c = conn.cursor()
    
    if request.method == 'POST':
        # Create new session
        c.execute("INSERT INTO sessions (title) VALUES (%s) RETURNING id", ("New Investigation",))
        session_id = c.fetchone()[0]
        conn.commit()
        c.close()
        conn.close()
        return jsonify({"id": session_id, "title": "New Investigation"})
    
    else:
        # Get all sessions
        c.execute("SELECT * FROM sessions ORDER BY id DESC")
        sessions = [{"id": row[0], "title": row[1]} for row in c.fetchall()]
        c.close()
        conn.close()
        return jsonify(sessions)

@app.route('/history/<int:session_id>', methods=['GET'])
def get_history(session_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT sender, content FROM messages WHERE session_id = %s ORDER BY id ASC", (session_id,))
    messages = [{"sender": row[0], "content": row[1]} for row in c.fetchall()]
    c.close()
    conn.close()
    return jsonify(messages)

@app.route('/chat', methods=['POST'])
def chat():
    session_id = request.form.get('session_id')
    user_text = request.form.get('message')
    image_file = request.files.get('image')

    gemini_input = [user_text]
    has_image = False
    
    if image_file:
        img = process_image(image_file)
        gemini_input.append(img)
        has_image = True

    try:
        response = model.generate_content(gemini_input)
        ai_reply = response.text

        conn = get_db_connection()
        c = conn.cursor()
        
        # Save User Message (Use %s for Postgres)
        c.execute("INSERT INTO messages (session_id, sender, content, has_image) VALUES (%s, %s, %s, %s)",
                  (session_id, "user", user_text, has_image))
        
        # Save AI Message
        c.execute("INSERT INTO messages (session_id, sender, content, has_image) VALUES (%s, %s, %s, %s)",
                  (session_id, "bot", ai_reply, False))
        
        # Update Title Logic
        c.execute("SELECT count(*) FROM messages WHERE session_id = %s", (session_id,))
        count = c.fetchone()[0]
        if count <= 2:
            title_prompt = f"Summarize this into a 3-word title: {user_text}"
            title_resp = model.generate_content(title_prompt)
            clean_title = title_resp.text.strip().replace('"','')
            c.execute("UPDATE sessions SET title = %s WHERE id = %s", (clean_title, session_id))

        conn.commit()
        c.close()
        conn.close()

        return jsonify({"reply": ai_reply})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(port=5000, debug=True)
