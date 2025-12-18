import os
import psycopg2
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from google import genai # NEW IMPORT
from google.genai import types # NEW IMPORT
from PIL import Image
import io

app = Flask(__name__)
CORS(app)

# --- CONFIGURATION ---
api_key = os.getenv("GEMINI_API_KEY") 
db_url = os.getenv("DATABASE_URL")

# Fix for Render Database URL
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

# NEW CLIENT SETUP
client = genai.Client(api_key=api_key)

system_instruction = """
You are 'OSINT-MIND', a cyber-intelligence analyst. 
Format your responses using Markdown. Be concise, technical, and precise.
If an image is provided, analyze it for OSINT clues.
"""

# --- DATABASE CONNECTION ---
def get_db_connection():
    try:
        conn = psycopg2.connect(db_url)
        return conn
    except Exception as e:
        print(f"DB Connection Error: {e}")
        return None

def init_db():
    conn = get_db_connection()
    if not conn: return
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sessions 
                 (id SERIAL PRIMARY KEY, title TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages 
                 (id SERIAL PRIMARY KEY, session_id INTEGER, 
                  sender TEXT, content TEXT, has_image BOOLEAN)''')
    conn.commit()
    c.close()
    conn.close()

# Initialize DB
init_db()

# --- HELPER FUNCTIONS ---
def process_image(image_file):
    return Image.open(image_file)

# --- ROUTES ---

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/sessions', methods=['GET', 'POST'])
def handle_sessions():
    conn = get_db_connection()
    if not conn: return jsonify({"error": "Database error"}), 500
    c = conn.cursor()
    
    if request.method == 'POST':
        c.execute("INSERT INTO sessions (title) VALUES (%s) RETURNING id", ("New Investigation",))
        session_id = c.fetchone()[0]
        conn.commit()
        c.close()
        conn.close()
        return jsonify({"id": session_id, "title": "New Investigation"})
    else:
        c.execute("SELECT * FROM sessions ORDER BY id DESC")
        sessions = [{"id": row[0], "title": row[1]} for row in c.fetchall()]
        c.close()
        conn.close()
        return jsonify(sessions)

@app.route('/history/<int:session_id>', methods=['GET'])
def get_history(session_id):
    conn = get_db_connection()
    if not conn: return jsonify([])
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

    # NEW: Prepare content for the new SDK
    contents = [user_text]
    has_image = False
    
    if image_file:
        img = process_image(image_file)
        contents.append(img)
        has_image = True

    try:
        # NEW: Generate content call
        response = client.models.generate_content(
            model="gemini-2.0-flash", # Using the newer, faster model
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.7
            )
        )
        ai_reply = response.text

        # Save to DB
        conn = get_db_connection()
        if conn:
            c = conn.cursor()
            c.execute("INSERT INTO messages (session_id, sender, content, has_image) VALUES (%s, %s, %s, %s)",
                      (session_id, "user", user_text, has_image))
            c.execute("INSERT INTO messages (session_id, sender, content, has_image) VALUES (%s, %s, %s, %s)",
                      (session_id, "bot", ai_reply, False))
            conn.commit()
            c.close()
            conn.close()

        return jsonify({"reply": ai_reply})

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(port=5000, debug=True)
