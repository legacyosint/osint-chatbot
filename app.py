import os
import json
import psycopg2
import base64
import threading
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
    
    # Session & Message Tables
    c.execute('''CREATE TABLE IF NOT EXISTS sessions 
                 (id SERIAL PRIMARY KEY, user_id TEXT, title TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages 
                 (id SERIAL PRIMARY KEY, session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE, 
                  sender TEXT, content TEXT, has_image BOOLEAN, image_data TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # NEW: User Profile Table (Long Term Memory)
    c.execute('''CREATE TABLE IF NOT EXISTS user_profiles 
                 (user_id TEXT PRIMARY KEY, profile_data TEXT, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # Migrations (Add columns if missing)
    try:
        c.execute("SELECT image_data FROM messages LIMIT 1")
    except:
        conn.rollback()
        c.execute("ALTER TABLE messages ADD COLUMN image_data TEXT")
        conn.commit()

    conn.commit()
    c.close()
    conn.close()

init_db()

# --- MEMORY FUNCTIONS ---

def get_long_term_memory(user_id):
    """Fetches the AI's notes about this user."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT profile_data FROM user_profiles WHERE user_id = %s", (user_id,))
    row = c.fetchone()
    c.close()
    conn.close()
    return row[0] if row else "No prior information known about this user."

def update_long_term_memory(user_id, last_message, last_reply):
    """Background task to update the user's profile based on the latest interaction."""
    try:
        current_memory = get_long_term_memory(user_id)
        
        # Ask Gemini to update the profile
        update_prompt = f"""
        You are maintaining a 'User Dossier' for an OSINT analyst.
        
        Current Dossier:
        {current_memory}
        
        Latest Interaction:
        User: {last_message}
        AI: {last_reply}
        
        Task: Update the Dossier with any new consistent facts, preferences, or technical skills the user demonstrated. 
        Keep it concise. If nothing new, return the Current Dossier exactly.
        """
        
        resp = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=update_prompt
        )
        new_memory = resp.text.strip()
        
        conn = get_db_connection()
        c = conn.cursor()
        # Upsert (Insert or Update)
        c.execute("""
            INSERT INTO user_profiles (user_id, profile_data) VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE SET profile_data = EXCLUDED.profile_data
        """, (user_id, new_memory))
        conn.commit()
        c.close()
        conn.close()
        print(f"Memory updated for {user_id}")
    except Exception as e:
        print(f"Memory Update Failed: {e}")

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
        return None

# --- ROUTES ---

@app.route('/')
def home(): return render_template('index.html')

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
        return jsonify({"id": new_session[0], "title": new_session[1]})
    else:
        c.execute("SELECT id, title FROM sessions WHERE user_id = %s ORDER BY id DESC", (user_id,))
        sessions = [{"id": row[0], "title": row[1]} for row in c.fetchall()]
        return jsonify(sessions)

@app.route('/sessions/<int:session_id>', methods=['PUT', 'DELETE'])
def manage_session(session_id):
    user_id = verify_user(request)
    if not user_id: return jsonify({"error": "Unauthorized"}), 401
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id FROM sessions WHERE id = %s AND user_id = %s", (session_id, user_id))
    if not c.fetchone(): return jsonify({"error": "Access denied"}), 404

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
    c.execute("SELECT sender, content, image_data FROM messages WHERE session_id = %s ORDER BY id ASC", (session_id,))
    messages = [{"sender": row[0], "content": row[1], "image": row[2]} for row in c.fetchall()]
    return jsonify({"title": row[0], "messages": messages})

@app.route('/chat', methods=['POST'])
def chat():
    user_id = verify_user(request)
    if not user_id: return jsonify({"error": "Unauthorized"}), 401

    session_id = request.form.get('session_id')
    user_text = request.form.get('message')
    image_file = request.files.get('image')

    # 1. Fetch Context (Short Term Memory)
    conn = get_db_connection()
    c = conn.cursor()
    # Get last 10 messages for context
    c.execute("SELECT sender, content FROM messages WHERE session_id = %s ORDER BY id DESC LIMIT 10", (session_id,))
    rows = c.fetchall()
    history = rows[::-1] # Reverse to chronological order

    # 2. Fetch User Profile (Long Term Memory)
    user_profile = get_long_term_memory(user_id)
    
    # 3. Construct System Prompt with Memory
    # NOTE: We do NOT send the user_name here to avoid confirmation bias.
    system_instruction = f"""
    You are 'OSINT-MIND', a senior cyber-intelligence analyst. 
    
    USER DOSSIER (Your knowledge about this specific user):
    {user_profile}
    
    INSTRUCTIONS:
    - Use the Dossier to personalize your response (e.g. if they know Python, show code).
    - Analyze images objectively. Do NOT assume the person in the image is the user unless explicitly stated.
    - Be concise, technical, and precise.
    """

    # 4. Build Input for Gemini
    contents = []
    
    # Add history context
    for sender, msg in history:
        role = "user" if sender == "user" else "model"
        # We only pass text history to keep token usage low
        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg)]))
    
    # Add Current User Input
    current_parts = []
    if user_text: current_parts.append(types.Part.from_text(text=user_text))
    
    image_b64 = None
    if image_file:
        img = Image.open(image_file)
        current_parts.append(img)
        # Process for DB
        image_file.seek(0)
        image_b64 = base64.b64encode(image_file.read()).decode('utf-8')

    if current_parts:
        contents.append(types.Content(role="user", parts=current_parts))

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=contents, # Now includes history!
            config=types.GenerateContentConfig(
                system_instruction=system_instruction, # Now includes Profile!
                temperature=0.7
            )
        )
        ai_reply = response.text

        # 5. Save to DB
        c.execute("INSERT INTO messages (session_id, sender, content, has_image, image_data) VALUES (%s, %s, %s, %s, %s)",
                  (session_id, "user", user_text or "", bool(image_file), image_b64))
        c.execute("INSERT INTO messages (session_id, sender, content, has_image, image_data) VALUES (%s, %s, %s, %s, %s)",
                  (session_id, "bot", ai_reply, False, None))
        conn.commit()
        
        # 6. Trigger Learning in Background (Fire and Forget)
        if user_text:
            threading.Thread(target=update_long_term_memory, args=(user_id, user_text, ai_reply)).start()

        # Auto-title logic
        if len(history) == 0:
            try:
                title_prompt = f"Generate a technical 4-word title for: {user_text}"
                title_resp = client.models.generate_content(model="gemini-2.0-flash", contents=title_prompt)
                clean_title = title_resp.text.strip().replace('"','')
                c.execute("UPDATE sessions SET title = %s WHERE id = %s", (clean_title, session_id))
                conn.commit()
            except: pass

        c.close()
        conn.close()
        return jsonify({"reply": ai_reply})

    except Exception as e:
        print(e)
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
