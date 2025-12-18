import os
import psycopg2
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from google import genai
from google.genai import types
from PIL import Image
import io

app = Flask(__name__)
CORS(app)

# --- CONFIGURATION ---
# Get keys from Environment Variables (set these in Render)
api_key = os.getenv("GEMINI_API_KEY") 
db_url = os.getenv("DATABASE_URL")

# Fix for Render Database URL internal inconsistencies
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

# NEW CLIENT SETUP
client = genai.Client(api_key=api_key)

system_instruction = """
You are 'OSINT-MIND', a senior cyber-intelligence analyst mentor. 
Your goal is to help students learn Open Source Intelligence techniques ethically and legally.
Format your responses using Markdown. Be concise, technical, and precise.
If an image is provided, analyze it meticulously for OSINT clues (metadata, landmarks, text, shadows, technology).
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
    # Ensure tables exist
    c.execute('''CREATE TABLE IF NOT EXISTS sessions 
                 (id SERIAL PRIMARY KEY, title TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages 
                 (id SERIAL PRIMARY KEY, session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE, 
                  sender TEXT, content TEXT, has_image BOOLEAN, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    c.close()
    conn.close()

# Initialize DB on startup
init_db()

# --- HELPER FUNCTIONS ---
def process_image(image_file):
    # In a real app, you might save this to S3/Cloud Storage. 
    # For now, we pass the file object directly to Gemini.
    return Image.open(image_file)

# --- ROUTES ---

@app.route('/')
def home():
    return render_template('index.html')

# --- SESSION MANAGEMENT ---

@app.route('/sessions', methods=['GET', 'POST'])
def handle_sessions():
    conn = get_db_connection()
    if not conn: return jsonify({"error": "Database error"}), 500
    c = conn.cursor()
    
    if request.method == 'POST':
        # Create new session
        default_title = "New Investigation"
        c.execute("INSERT INTO sessions (title) VALUES (%s) RETURNING id, title", (default_title,))
        new_session = c.fetchone()
        conn.commit()
        c.close()
        conn.close()
        return jsonify({"id": new_session[0], "title": new_session[1]})
    else:
        # Get all sessions ordered by newest first
        c.execute("SELECT id, title FROM sessions ORDER BY id DESC")
        sessions = [{"id": row[0], "title": row[1]} for row in c.fetchall()]
        c.close()
        conn.close()
        return jsonify(sessions)

@app.route('/sessions/<int:session_id>', methods=['PUT', 'DELETE'])
def manage_session(session_id):
    conn = get_db_connection()
    if not conn: return jsonify({"error": "Database error"}), 500
    c = conn.cursor()

    if request.method == 'DELETE':
        # Delete session (CASCADE will delete messages due to init_db change)
        c.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
        conn.commit()
        c.close()
        conn.close()
        return jsonify({"status": "deleted", "id": session_id})

    elif request.method == 'PUT':
        # Rename session
        new_title = request.json.get('title')
        if not new_title: return jsonify({"error": "Title required"}), 400
        c.execute("UPDATE sessions SET title = %s WHERE id = %s", (new_title, session_id))
        conn.commit()
        c.close()
        conn.close()
        return jsonify({"status": "updated", "id": session_id, "title": new_title})

# --- CHAT HISTORY ---

@app.route('/history/<int:session_id>', methods=['GET'])
def get_history(session_id):
    conn = get_db_connection()
    if not conn: return jsonify([])
    c = conn.cursor()
    # Get session title first
    c.execute("SELECT title FROM sessions WHERE id = %s", (session_id,))
    title_row = c.fetchone()
    title = title_row[0] if title_row else "Investigation"

    # Get messages
    c.execute("SELECT sender, content, has_image FROM messages WHERE session_id = %s ORDER BY id ASC", (session_id,))
    messages = [{"sender": row[0], "content": row[1], "has_image": row[2]} for row in c.fetchall()]
    c.close()
    conn.close()
    return jsonify({"title": title, "messages": messages})

# --- CHAT INTERACTION ---

@app.route('/chat', methods=['POST'])
def chat():
    session_id = request.form.get('session_id')
    user_text = request.form.get('message')
    image_file = request.files.get('image')

    if not session_id: return jsonify({"error": "No session ID"}), 400

    contents = []
    if user_text: contents.append(user_text)
    has_image = False
    
    if image_file:
        img = process_image(image_file)
        contents.append(img)
        has_image = True

    if not contents: return jsonify({"error": "No input provided"}), 400

    try:
        # Generate content call using Gemini 2.0 Flash
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.7,
                max_output_tokens=4096
            )
        )
        ai_reply = response.text

        # Save to DB
        conn = get_db_connection()
        if conn:
            c = conn.cursor()
            # Save User Message
            c.execute("INSERT INTO messages (session_id, sender, content, has_image) VALUES (%s, %s, %s, %s)",
                      (session_id, "user", user_text or "[Image Uploaded]", has_image))
            # Save AI Message
            c.execute("INSERT INTO messages (session_id, sender, content, has_image) VALUES (%s, %s, %s, %s)",
                      (session_id, "bot", ai_reply, False))
            
            # Auto-title logic: Update title if it's the first message in session
            c.execute("SELECT count(*) FROM messages WHERE session_id = %s", (session_id,))
            count = c.fetchone()[0]
            if count <= 2 and user_text:
                # Generate a concise title
                title_prompt = f"Generate a very concise, 3-4 word technical title for this OSINT query: {user_text}"
                try:
                    title_resp = client.models.generate_content(
                        model="gemini-2.0-flash", contents=title_prompt)
                    clean_title = title_resp.text.strip().replace('"','').replace('*','')
                    c.execute("UPDATE sessions SET title = %s WHERE id = %s", (clean_title, session_id))
                except:
                    pass # Ignore title generation errors

            conn.commit()
            c.close()
            conn.close()

        return jsonify({"reply": ai_reply})

    except Exception as e:
        print(f"Error: {e}")
        # Check for billing/quota issues specifically
        err_str = str(e)
        if "429" in err_str or "quota" in err_str.lower():
            return jsonify({"error": "Rate limit exceeded or billing required. Please check Google Cloud console."}), 429
        return jsonify({"error": str(e)}), 500

# Placeholder for Activity Tab
@app.route('/activity', methods=['GET'])
def get_activity():
    # In the future, you can query the DB for usage stats here.
    return jsonify({"stats": ["System initialized.", "Database connected.", "Waiting for target data."]})


if __name__ == '__main__':
    # Use port 5000 for local development
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
