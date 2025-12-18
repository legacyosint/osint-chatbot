import os
import json
import psycopg2
import base64
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from google import genai
from google.genai import types
from PIL import Image
import firebase_admin
from firebase_admin import credentials, auth
import io

app = Flask(__name__)
CORS(app)

# --- CONFIGURATION ---
api_key = os.getenv("GEMINI_API_KEY") 
db_url = os.getenv("DATABASE_URL")
firebase_creds_json = os.getenv("FIREBASE_CREDENTIALS")

# --- FIREBASE SETUP ---
if firebase_creds_json and not firebase_admin._apps:
    try:
        cred_dict = json.loads(firebase_creds_json)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        print("Firebase Admin Initialized")
    except Exception as e:
        print(f"Firebase Init Error: {e}")

# Fix for Render Database URL
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

client = genai.Client(api_key=api_key)

system_instruction = """
You are 'OSINT-MIND', a senior cyber-intelligence analyst. 
Format your responses using Markdown. Be concise, technical, and precise.
"""

# --- AUTH HELPER ---
def verify_user(req):
    auth_header = req.headers.get('Authorization')
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    token = auth_header.split("Bearer ")[1]
    try:
        decoded_token = auth.verify_id_token(token)
        return decoded_token['uid']
    except Exception as e:
        print(f"Token verification failed: {e}")
        return None

# --- DATABASE ---
def get_db_connection():
    try:
        return psycopg2.connect(db_url)
    except Exception as e:
        print(f"DB Connection Error: {e}")
        return None

def init_db():
    conn = get_db_connection()
    if not conn: return
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sessions 
                 (id SERIAL PRIMARY KEY, user_id TEXT, title TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages 
                 (id SERIAL PRIMARY KEY, session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE, 
                  sender TEXT, content TEXT, has_image BOOLEAN, image_data TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # --- MIGRATIONS (Auto-fix old tables) ---
    try:
        c.execute("SELECT user_id FROM sessions LIMIT 1")
    except:
        conn.rollback()
        c.execute("ALTER TABLE sessions ADD COLUMN user_id TEXT")
        conn.commit()
    
    try:
        c.execute("SELECT image_data FROM messages LIMIT 1")
    except:
        conn.rollback()
        print("Migrating DB: Adding image_data column...")
        c.execute("ALTER TABLE messages ADD COLUMN image_data TEXT")
        conn.commit()

    conn.commit()
    c.close()
    conn.close()

init_db()

# --- ROUTES ---

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/sessions', methods=['GET', 'POST'])
def handle_sessions():
    user_id = verify_user(request)
    if not user_id: return jsonify({"error": "Unauthorized"}), 401

    conn = get_db_connection()
    c = conn.cursor()
    
    if request.method == 'POST':
        c.execute("INSERT INTO sessions (user_id, title) VALUES (%s, %s) RETURNING id, title", (user_id, "New Investigation"))
        new_session = c.fetchone()
        conn.commit()
    else:
        c.execute("SELECT id, title FROM sessions WHERE user_id = %s ORDER BY id DESC", (user_id,))
        sessions = [{"id": row[0], "title": row[1]} for row in c.fetchall()]
        return jsonify(sessions)
    
    c.close()
    conn.close()
    return jsonify({"id": new_session[0], "title": new_session[1]})

@app.route('/sessions/<int:session_id>', methods=['PUT', 'DELETE'])
def manage_session(session_id):
    user_id = verify_user(request)
    if not user_id: return jsonify({"error": "Unauthorized"}), 401

    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute("SELECT id FROM sessions WHERE id = %s AND user_id = %s", (session_id, user_id))
    if not c.fetchone():
        return jsonify({"error": "Access denied"}), 404

    if request.method == 'DELETE':
        c.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
    elif request.method == 'PUT':
        new_title = request.json.get('title')
        c.execute("UPDATE sessions SET title = %s WHERE id = %s", (new_title, session_id))
        
    conn.commit()
    c.close()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/history/<int:session_id>', methods=['GET'])
def get_history(session_id):
    user_id = verify_user(request)
    if not user_id: return jsonify({"error": "Unauthorized"}), 401

    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute("SELECT title FROM sessions WHERE id = %s AND user_id = %s", (session_id, user_id))
    row = c.fetchone()
    if not row: return jsonify({"error": "Access denied"}), 403
    
    title = row[0]
    # UPDATED: Now selecting image_data as well
    c.execute("SELECT sender, content, image_data FROM messages WHERE session_id = %s ORDER BY id ASC", (session_id,))
    messages = [{"sender": row[0], "content": row[1], "image": row[2]} for row in c.fetchall()]
    
    c.close()
    conn.close()
    return jsonify({"title": title, "messages": messages})

@app.route('/chat', methods=['POST'])
def chat():
    user_id = verify_user(request)
    if not user_id: return jsonify({"error": "Unauthorized"}), 401

    session_id = request.form.get('session_id')
    user_text = request.form.get('message')
    user_name = request.form.get('user_name', 'Agent')
    image_file = request.files.get('image')

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id FROM sessions WHERE id = %s AND user_id = %s", (session_id, user_id))
    if not c.fetchone(): return jsonify({"error": "Access denied"}), 403
    c.close()
    conn.close()

    contents = []
    if user_text:
        contents.append(user_text)
    
    image_b64 = None
    if image_file:
        # 1. Process for AI
        img = Image.open(image_file)
        contents.append(img)
        
        # 2. Process for Database Storage (Base64)
        image_file.seek(0) # Reset pointer
        file_bytes = image_file.read()
        image_b64 = base64.b64encode(file_bytes).decode('utf-8')
    
    if not contents: return jsonify({"error": "Empty input"}), 400

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.7
            )
        )
        ai_reply = response.text

        conn = get_db_connection()
        c = conn.cursor()
        
        # Save User Message (With Image Data if present)
        c.execute("INSERT INTO messages (session_id, sender, content, has_image, image_data) VALUES (%s, %s, %s, %s, %s)",
                  (session_id, "user", user_text or "", bool(image_file), image_b64))
        
        # Save Bot Message
        c.execute("INSERT INTO messages (session_id, sender, content, has_image, image_data) VALUES (%s, %s, %s, %s, %s)",
                  (session_id, "bot", ai_reply, False, None))
        
        # Auto-rename logic
        c.execute("SELECT count(*) FROM messages WHERE session_id = %s", (session_id,))
        if c.fetchone()[0] <= 2 and user_text:
            try:
                title_prompt = f"Generate a technical 4-word title for: {user_text}"
                title_resp = client.models.generate_content(model="gemini-2.0-flash", contents=title_prompt)
                clean_title = title_resp.text.strip().replace('"','')
                c.execute("UPDATE sessions SET title = %s WHERE id = %s", (clean_title, session_id))
            except: pass

        conn.commit()
        c.close()
        conn.close()
        return jsonify({"reply": ai_reply})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
