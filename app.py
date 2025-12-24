import os
import json
import psycopg2
import base64
import threading
import razorpay
import time
from flask import Flask, render_template, request, jsonify, send_from_directory
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

RAZORPAY_KEY_ID = os.environ.get('RAZORPAY_KEY_ID', 'rzp_test_YOUR_KEY_ID')
RAZORPAY_KEY_SECRET = os.environ.get('RAZORPAY_KEY_SECRET', 'YOUR_KEY_SECRET')

razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

DOMAIN = 'https://osint-chatbot.onrender.com' # Change to http://127.0.0.1:5000 for local testing

# --- FIREBASE SETUP ---
if firebase_creds_json and not firebase_admin._apps:
    try:
        cred_dict = json.loads(firebase_creds_json)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
    except Exception as e:
        print(f"Firebase Init Error: {e}")

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
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (id SERIAL PRIMARY KEY, user_id TEXT, title TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages (id SERIAL PRIMARY KEY, session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE, sender TEXT, content TEXT, has_image BOOLEAN, image_data TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_profiles (user_id TEXT PRIMARY KEY, profile_data TEXT, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    c.close()
    conn.close()

init_db()

# --- MEMORY FUNCTIONS ---
def get_long_term_memory(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT profile_data FROM user_profiles WHERE user_id = %s", (user_id,))
    row = c.fetchone()
    c.close()
    conn.close()
    return row[0] if row else "No prior information."

def update_long_term_memory(user_id, last_message, last_reply):
    try:
        current_memory = get_long_term_memory(user_id)
        update_prompt = f"User Dossier: {current_memory}\nLatest Interaction:\nUser: {last_message}\nAI: {last_reply}\nTask: Update the dossier with new consistent facts or preferences. Keep it concise."
        resp = client.models.generate_content(model="gemini-2.0-flash", contents=update_prompt)
        new_memory = resp.text.strip()
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""INSERT INTO user_profiles (user_id, profile_data) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET profile_data = EXCLUDED.profile_data""", (user_id, new_memory))
        conn.commit()
        c.close()
        conn.close()
    except: pass

def verify_user(req):
    auth_header = req.headers.get('Authorization')
    if not auth_header or not auth_header.startswith("Bearer "): return None
    try:
        return auth.verify_id_token(auth_header.split("Bearer ")[1])['uid']
    except: return None

# --- ROUTES ---
@app.route('/')
def home(): return render_template('index.html')

# --- 2. THE CHECKOUT ROUTE (Razorpay Version) ---
@app.route('/create-checkout-session')
@login_required
def create_checkout_session():
    try:
        # Create a "Payment Link" (Standard Hosted Page)
        payment_link_data = {
            "amount": 500,  # Amount in PAISE (500 paise = ₹5.00)
            "currency": "USD",
            "accept_partial": False,
            "description": "Legacy OSINT Pro Upgrade",
            "customer": {
                "name": current_user.name if hasattr(current_user, 'name') else "Agent",
                "email": current_user.email,
            },
            "notify": {
                "sms": False,
                "email": True
            },
            "reminder_enable": False,
            "callback_url": DOMAIN + "/success", # Where to go after payment
            "callback_method": "get"
        }

        payment_link = razorpay_client.payment_link.create(payment_link_data)
        
        # Get the URL and redirect the user there
        payment_url = payment_link['short_url']
        return redirect(payment_url)

    except Exception as e:
        return jsonify(error=str(e)), 403

# --- 3. THE SUCCESS ROUTE ---
@app.route('/success')
@login_required
def success():
    # Razorpay sends data in the URL params:
    # ?razorpay_payment_id=pay_...&razorpay_payment_link_id=...&razorpay_payment_link_status=paid
    
    payment_status = request.args.get('razorpay_payment_link_status')
    
    if payment_status == 'paid':
        # UPGRADE THE USER
        current_user.is_premium = True
        db.session.commit()
        return render_template('success.html')
    else:
        return "Payment Failed or Cancelled", 400

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
    if request.method == 'DELETE': c.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
    elif request.method == 'PUT': c.execute("UPDATE sessions SET title = %s WHERE id = %s", (request.json.get('title'), session_id))
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
    if not c.fetchone(): return jsonify({"error": "Access denied"}), 403
    c.execute("SELECT sender, content, image_data FROM messages WHERE session_id = %s ORDER BY id ASC", (session_id,))
    messages = [{"sender": r[0], "content": r[1], "image": r[2]} for r in c.fetchall()]
    return jsonify({"title": "Chat", "messages": messages})

@app.route('/chat', methods=['POST'])
def chat():
    try:
        user_id = verify_user(request)
        if not user_id: return jsonify({"error": "Unauthorized"}), 401

        session_id = request.form.get('session_id')
        user_text = request.form.get('message', '')
        image_file = request.files.get('image')

        conn = get_db_connection()
        c = conn.cursor()
        
        # 1. Fetch History
        c.execute("SELECT sender, content, has_image, image_data FROM messages WHERE session_id = %s ORDER BY id DESC LIMIT 5", (session_id,))
        history = c.fetchall()[::-1]

        user_profile = get_long_term_memory(user_id)
        
        # --- SYSTEM PROMPT (STRICT FORMATTING) ---
        system_instruction = f"""
        You are 'OSINT-MIND', a senior cyber-intelligence analyst.
        USER DOSSIER: {user_profile}
        
        INSTRUCTIONS:
        1. THOUGHT PROCESS:
           - For complex queries, start with a hidden block: <think> [Reasoning/Plan] </think>.
           - For simple greetings ("hi"), skip the <think> block.
        
        2. FORMATTING RULES (CRITICAL):
           - Write your actual answer in **PLAIN TEXT** or standard Markdown (bold, lists, etc).
           - **NEVER** wrap the entire response in a code block (``` or ```markdown). 
           - **ONLY** use code blocks (```python) when you are providing actual Python code snippets.
           - Provide clickable links for maps or profiles.
        """

        contents = []
        
        # 2. Add History Context
        for sender, msg, has_img, img_data in history:
            role = "user" if sender == "user" else "model"
            parts = []
            if msg: parts.append(types.Part.from_text(text=msg))
            
            # SAFE IMAGE LOADING (Prevents Crash)
            if has_img and img_data:
                try:
                    if len(img_data) > 100: 
                        image_bytes = base64.b64decode(img_data)
                        parts.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))
                except Exception as e:
                    print(f"Skipping bad history image: {e}")
            
            if parts: contents.append(types.Content(role=role, parts=parts))
        
        # 3. Add Current Message
        current_parts = []
        if user_text: current_parts.append(types.Part.from_text(text=user_text))
        
        image_b64 = None
        if image_file:
            try:
                img = Image.open(image_file)
                buf = io.BytesIO()
                img = img.convert('RGB')
                img.save(buf, format='JPEG')
                image_bytes = buf.getvalue()
                
                # CRITICAL FIX: Ensure we don't send empty bytes to Gemini
                if len(image_bytes) > 0:
                    current_parts.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))
                    image_b64 = base64.b64encode(image_bytes).decode('utf-8')
            except Exception as e:
                print(f"Image Error: {e}")
                pass # Continue without image if it fails

        if current_parts:
            contents.append(types.Content(role="user", parts=current_parts))

        # 4. Generate Response
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.7
            )
        )
        ai_reply = response.text

        # 5. Save & Return
        c.execute("INSERT INTO messages (session_id, sender, content, has_image, image_data) VALUES (%s, %s, %s, %s, %s)",
                  (session_id, "user", user_text, bool(image_b64), image_b64))
        c.execute("INSERT INTO messages (session_id, sender, content, has_image, image_data) VALUES (%s, %s, %s, %s, %s)",
                  (session_id, "bot", ai_reply, False, None))
        conn.commit()
        
        if user_text:
            threading.Thread(target=update_long_term_memory, args=(user_id, user_text, ai_reply)).start()

        if len(history) == 0:
            try:
                t_resp = client.models.generate_content(model="gemini-2.0-flash", contents=f"Generate 3-word title: {user_text}")
                c.execute("UPDATE sessions SET title = %s WHERE id = %s", (t_resp.text.strip().replace('"',''), session_id))
                conn.commit()
            except: pass

        c.close()
        conn.close()
        return jsonify({"reply": ai_reply})

    except Exception as e:
        print(f"CRITICAL ERROR: {e}")
        return jsonify({"error": str(e)}), 500

# --- ANDROID APP VERIFICATION ---
@app.route('/.well-known/assetlinks.json')
def assetlinks():
    # This serves the file from the root directory
    return send_from_directory('.', 'assetlinks.json')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

# --- LEGAL & SUPPORT PAGES ---
@app.route('/terms')
def terms():
    return render_template('terms.html')

@app.route('/refund-policy')
def refund_policy():
    return render_template('refund_policy.html')

@app.route('/support')
def support():
    return render_template('support.html')
    
# --- SERVICE WORKER ---
@app.route('/sw.js')
def service_worker():
    from flask import make_response
    response = make_response(send_from_directory('.', 'sw.js'))
    response.headers['Content-Type'] = 'application/javascript'
    return response

# Ensure this is still in app.py
@app.route('/create-checkout-session')
def create_checkout_session():
    # ... (your stripe logic) ...

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
