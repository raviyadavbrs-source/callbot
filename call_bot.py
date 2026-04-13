"""
╔══════════════════════════════════════════════════════════════════════╗
║              WINSB2026 — SARCASTIC CALL BOT + POOJA ASSISTANT        ║
║                                                                      ║
║  Incoming call → Sarcastic bot answers                               ║
║  /pooja → Mobile UI to trigger Pooja outbound calls                  ║
║  Plan Call  → Pooja fixes a meetup                                   ║
║  Check-in   → Pooja casually checks in on Ravi's behalf              ║
╚══════════════════════════════════════════════════════════════════════╝
"""

from flask import Flask, request, Response, session, redirect, url_for
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client
import anthropic
import requests
import tempfile
import os
import json
import threading
import urllib.parse
import datetime

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "pooja-ravi-2026-secret")

# ============================================================
# CREDENTIALS
# ============================================================
TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "+18085182186")
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY")
ELEVENLABS_API_KEY  = os.getenv("ELEVENLABS_API_KEY", "")
UI_PASSWORD         = os.getenv("UI_PASSWORD", "ravi2026")

# ============================================================
# VOICE IDs
# ============================================================
HINDI_VOICE_ID   = "ibbx9zDYGvLgtYzRbqqG"
ENGLISH_VOICE_ID = "nwj0s2LU9bDWRKND5yzA"
POOJA_VOICE_ID   = "EXAVITQu4vr4xnSDxMaL"   # Sarah — female US accent

# ============================================================
# CONTACTS
# ============================================================
CONTACTS = [
    {"name": "Sameer Chachu",  "number": "+919829055878"},
    {"name": "Gulshaan",       "number": "+919001555552"},
    {"name": "Addy",           "number": "+919571792781"},
    {"name": "Haroon",         "number": "+919602941676"},
    {"name": "Deewan",         "number": "+919928075552"},
    {"name": "Juhi",           "number": "+919950220333"},
    {"name": "Shubham",        "number": "+919828237078"},
    {"name": "Micky Bana",     "number": "+919414000770"},
    {"name": "Ujjwal",         "number": "+918741899621"},
    {"name": "Shastri",        "number": "+919057566521"},
    {"name": "Latika",         "number": "+917023398888"},
    {"name": "Chaarmi",        "number": "+919867071232"},
]

BLOCKED_NUMBERS = []

# ============================================================
# MEMORY
# ============================================================
call_history = {}
call_meta    = {}
call_logs    = []   # completed call log dicts, newest first
call_lock    = threading.Lock()

# ============================================================
# SYSTEM PROMPTS — SARCASTIC INCOMING BOT
# ============================================================
HINDI_SYSTEM_PROMPT = """Tu ek bahut zyada sarcastic, thoda irritated aur lazy banda hai jiska naam Ravi hai.
Jab koi tumhe call karta hai, tum unhe seedha Haryanvi aur Hindi gaaliyon aur slangs mein reply karte ho.
IMPORTANT RULES:
- Bilkul bhi emoji mat use karo
- Koi special characters mat use karo
- Plain simple spoken language mein bolo — jaise real phone call ho
- Short rakho — 1-3 sentences only
- Bahut zyada sarcastic aur rude raho — jaise koi best friend hote hain jo gaaliyan deta hai
- Haryanvi gaaliyan aur slangs zaroor use karo: teri maa ki, bhai, yaar, abe, saale, bc, mc, oye, chal hat, kya kar raha hai be, bakwaas band kar, teri toh, hadd hai, kameene, ullu, pagal hai kya, teri aakal ghaaswali hai, nikal yahan se, latth maar dunga
- Jitna zyada ho sake utna entertaining aur abusive raho — yahi teri personality hai
- Remember previous context in the call
"""

ENGLISH_SYSTEM_PROMPT = """You are Ravi, a sarcastic and slightly lazy bloke speaking in Australian English.
IMPORTANT RULES:
- Keep it short — 1-3 sentences only
- Natural spoken language — like a real phone call
- Use Australian slang: mate, bloody, crikey, no worries, reckon, heaps, arvo, strewth
- Be funny and sarcastic but genuine
- Remember previous context in the call
"""

# ============================================================
# SYSTEM PROMPTS — POOJA
# ============================================================
def get_pooja_plan_prompt(time, place, language='english'):
    return f"""You are Pooja, personal assistant to Mr. Ravi. You are calling to schedule a meetup.
You speak classy, educated American English. The friend will reply in Hindi — understand it and always respond in English.

YOUR GOAL: Confirm the friend is available to meet Mr. Ravi at {place} around {time}.

RULES:
- Always reply in English only — warm, polite, professional
- Short responses — 1-3 sentences only
- No emojis or special characters
- If friend agrees → confirm, say Mr. Ravi is looking forward to it, wrap up
- If friend suggests different time/place → acknowledge, confirm, say you will let Mr. Ravi know
- If friend is busy → ask if there is a better time
- If friend ABUSES → sharp professional reply. E.g. "I beg your pardon? I am simply coordinating on Mr. Ravi's behalf."
- Once plan confirmed → warm goodbye and end with exactly: [END_CALL]

Opening: "Hello, this is Pooja calling on behalf of Mr. Ravi. I was wondering if you are available to meet him at {place} around {time}?"
"""

def get_pooja_checkin_prompt(language='english'):
    return """You are Pooja, personal assistant to Mr. Ravi. You are calling to check in on one of his close friends.
You speak classy, educated American English. The friend will reply in Hindi — understand it and always respond in English.

YOUR GOAL: Have a warm, casual check-in conversation.

RULES:
- Always reply in English only — warm, friendly, conversational
- Short responses — 1-3 sentences
- No emojis or special characters
- Opening: "Hello, this is Pooja calling on behalf of Mr. Ravi. He wanted me to check in on you and see how you have been doing."
- Be genuinely interested — ask follow-up questions
- Keep it light and positive
- After 3-4 exchanges wrap up naturally
- If friend ABUSES → sharp professional reply. E.g. "I appreciate if we could keep this courteous. Now, how have you been?"
- When ready to end, finish with exactly: [END_CALL]
"""


# ============================================================
# LANGUAGE DETECTION
# ============================================================
def detect_language(text):
    hindi_chars = set('अआइईउऊएऐओऔकखगघचछजझटठडढणतथदधनपफबभमयरलवशषसह')
    hindi_words = {'kya','hai','nahi','haan','bhai','yaar','abe','saale',
                   'karo','bolo','main','mein','tera','mera','aur',
                   'theek','achha','kal','aaj','baat','kaam','bc','oye'}
    if any(c in hindi_chars for c in text):
        return 'hindi'
    if len(set(text.lower().split()).intersection(hindi_words)) >= 1:
        return 'hindi'
    return 'english'

# ============================================================
# CLAUDE — SARCASTIC REPLY
# ============================================================
def get_sarcastic_reply(call_sid, caller_text, language):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    with call_lock:
        if call_sid not in call_history:
            call_history[call_sid] = []
        history = call_history[call_sid].copy()
    history.append({"role": "user", "content": caller_text})
    system = HINDI_SYSTEM_PROMPT if language == 'hindi' else ENGLISH_SYSTEM_PROMPT
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=system,
            messages=history
        )
        reply = resp.content[0].text.strip()
        with call_lock:
            call_history[call_sid].append({"role": "user",      "content": caller_text})
            call_history[call_sid].append({"role": "assistant", "content": reply})
            if len(call_history[call_sid]) > 12:
                call_history[call_sid] = call_history[call_sid][-12:]
        return reply
    except Exception as e:
        print(f"Claude error: {e}")
        return "Bhai main thoda busy hoon" if language == 'hindi' else "Mate, caught me at a bad time"

# ============================================================
# CLAUDE — POOJA REPLY
# ============================================================
def get_pooja_reply(call_sid, friend_text):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    with call_lock:
        if call_sid not in call_history:
            call_history[call_sid] = []
        meta    = call_meta.get(call_sid, {})
        history = call_history[call_sid].copy()

    call_type = meta.get('type', 'checkin')
    language  = detect_language(friend_text)

    # Update language in meta so TTS uses correct voice
    with call_lock:
        call_meta[call_sid]['language'] = language

    if call_type == 'plan':
        system = get_pooja_plan_prompt(meta.get('time', 'soon'), meta.get('place', 'the usual spot'), language)
    else:
        system = get_pooja_checkin_prompt(language)

    history.append({"role": "user", "content": friend_text})
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=system,
            messages=history
        )
        reply = resp.content[0].text.strip()
        with call_lock:
            call_history[call_sid].append({"role": "user",      "content": friend_text})
            call_history[call_sid].append({"role": "assistant", "content": reply})
            if len(call_history[call_sid]) > 12:
                call_history[call_sid] = call_history[call_sid][-12:]
        return reply
    except Exception as e:
        print(f"Claude error (Pooja): {e}")
        return "I am so sorry, I will have Mr. Ravi follow up with you directly."

# ============================================================
# ELEVENLABS TTS
# ============================================================
def text_to_speech(text, voice_id=None, language='english'):
    if voice_id is None:
        voice_id = HINDI_VOICE_ID if language == 'hindi' else ENGLISH_VOICE_ID
    if not ELEVENLABS_API_KEY:
        return None
    try:
        resp = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            json={
                "text": text,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75,
                                   "style": 0.5, "use_speaker_boost": True}
            },
            headers={"Accept": "audio/mpeg", "Content-Type": "application/json",
                     "xi-api-key": ELEVENLABS_API_KEY}
        )
        if resp.status_code == 200:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.mp3')
            tmp.write(resp.content)
            tmp.close()
            return tmp.name
    except Exception as e:
        print(f"ElevenLabs error: {e}")
    return None

def play_audio_or_say(response, text, voice_id=None, language='english', fallback_lang='en-US'):
    audio_file = text_to_speech(text, voice_id=voice_id, language=language)
    if audio_file:
        filename = os.path.basename(audio_file)
        base_url = os.getenv('BASE_URL', 'https://callbot-production-a211.up.railway.app')
        response.play(f"{base_url}/audio/{filename}")
        threading.Timer(60, os.unlink, args=[audio_file]).start()
    else:
        response.say(text, voice='alice', language=fallback_lang)

# ============================================================
# SERVE AUDIO
# ============================================================
@app.route('/audio/<filename>')
def serve_audio(filename):
    filepath = os.path.join(tempfile.gettempdir(), filename)
    if os.path.exists(filepath):
        with open(filepath, 'rb') as f:
            data = f.read()
        return Response(data, mimetype='audio/mpeg')
    return "Not found", 404

# ============================================================
# POOJA MOBILE UI
# ============================================================
@app.route('/pooja', methods=['GET', 'POST'])
def pooja_ui():
    contacts_json = json.dumps(CONTACTS)
    if True:  # no auth - URL is private
        html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Pooja</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0a0f;color:#e8e8f0;min-height:100vh;padding:20px 16px 40px;}
.header{text-align:center;padding:24px 0 28px;}
.avatar{width:64px;height:64px;background:linear-gradient(135deg,#7c3aed,#db2777);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:28px;margin:0 auto 12px;box-shadow:0 0 24px rgba(124,58,237,0.4);}
h1{font-size:22px;font-weight:700;}
.sub{font-size:13px;color:#888;margin-top:4px;}
.card{background:#13131a;border:1px solid #1e1e2e;border-radius:16px;padding:20px;margin-bottom:16px;}
.label{font-size:11px;text-transform:uppercase;letter-spacing:1.5px;color:#666;margin-bottom:10px;}
input{width:100%;background:#1a1a26;border:1px solid #1e1e2e;border-radius:10px;padding:13px 14px;color:#e8e8f0;font-size:16px;outline:none;}
input:focus{border-color:#7c3aed;}
input::placeholder{color:#444;}
.toggle-row{display:flex;gap:10px;}
.toggle-btn{flex:1;padding:12px;border-radius:12px;border:2px solid #1e1e2e;background:#1a1a26;color:#888;font-size:13px;font-weight:600;cursor:pointer;text-align:center;}
.toggle-btn.selected{border-color:#db2777;background:#1f0f1a;color:#f472b6;}
.call-btn{width:100%;padding:16px;border-radius:14px;border:none;background:linear-gradient(135deg,#7c3aed,#db2777);color:white;font-size:16px;font-weight:700;cursor:pointer;margin-top:4px;box-shadow:0 4px 20px rgba(124,58,237,0.35);}
.call-btn:disabled{opacity:0.4;}
.status{text-align:center;padding:14px;border-radius:12px;font-size:14px;font-weight:500;margin-top:14px;display:none;}
.status.success{background:#0d2010;color:#4ade80;border:1px solid #166534;}
.status.error{background:#200d0d;color:#f87171;border:1px solid #7f1d1d;}
#plan-fields{display:none;}
.logs-title{font-size:11px;text-transform:uppercase;letter-spacing:1.5px;color:#666;margin-bottom:14px;margin-top:24px;}
.log-card{background:#13131a;border:1px solid #1e1e2e;border-radius:14px;padding:16px;margin-bottom:12px;}
.log-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px;}
.log-name{font-size:15px;font-weight:700;color:#e8e8f0;}
.log-meta{font-size:11px;color:#555;margin-top:3px;}
.log-status{font-size:10px;font-weight:700;padding:3px 8px;border-radius:20px;text-transform:uppercase;}
.log-status.ok{background:#0d2010;color:#4ade80;}
.log-status.na{background:#1f0f0f;color:#f87171;}
.tr-toggle{background:#1a1a26;border:1px solid #1e1e2e;border-radius:8px;color:#888;font-size:12px;padding:6px 12px;cursor:pointer;margin-top:8px;}
.transcript{margin-top:10px;background:#0d0d14;border-radius:10px;padding:12px;max-height:200px;overflow-y:auto;}
.tr-line{font-size:12px;color:#c8c8e0;margin-bottom:6px;line-height:1.5;}
.tr-role{color:#a78bfa;font-weight:700;}
</style>
</head>
<body>
<div class="header">
  <div class="avatar">&#128188;</div>
  <h1>Pooja</h1>
  <p class="sub">Mr. Ravi's Personal Assistant</p>
</div>
<div class="card">
  <div class="label">Name</div>
  <input type="text" id="name-input" placeholder="e.g. Sameer" style="margin-bottom:10px;">
  <div class="label" style="margin-top:4px;">Phone Number</div>
  <input type="tel" id="phone-input" placeholder="+91 98290 55878">
</div>
<div class="card">
  <div class="label">Call Type</div>
  <div class="toggle-row">
    <div class="toggle-btn selected" id="btn-checkin" onclick="selectType('checkin')">&#128075; Check-in</div>
    <div class="toggle-btn" id="btn-plan" onclick="selectType('plan')">&#128197; Plan Meet</div>
  </div>
</div>
<div class="card" id="plan-fields">
  <div class="label">Meetup Details</div>
  <input type="text" id="time-input" placeholder="Time e.g. tomorrow 6pm" style="margin-bottom:10px;">
  <input type="text" id="place-input" placeholder="Place e.g. Cafe Coffee Day">
</div>
<button class="call-btn" id="call-btn" onclick="makeCall()">&#128222; Call Now</button>
<div class="status" id="status"></div>
<div class="logs-title">&#128222; Call History</div>
<div id="logs-container"><div style="color:#555;font-size:13px;text-align:center;padding:16px;">No calls yet.</div></div>
<script>
var callType = 'checkin';
function selectType(t) {
  callType = t;
  document.getElementById('btn-checkin').classList.toggle('selected', t==='checkin');
  document.getElementById('btn-plan').classList.toggle('selected', t==='plan');
  document.getElementById('plan-fields').style.display = t==='plan' ? 'block' : 'none';
}
async function makeCall() {
  var phone = document.getElementById('phone-input').value.trim();
  if (!phone) { showStatus('Enter a phone number.', 'error'); return; }
  if (!phone.startsWith('+')) phone = '+91' + phone.replace(/^0/, '');
  if (callType === 'plan') {
    var t = document.getElementById('time-input').value.trim();
    var p = document.getElementById('place-input').value.trim();
    if (!t || !p) { showStatus('Enter time and place.', 'error'); return; }
  }
  var btn = document.getElementById('call-btn');
  btn.disabled = true; btn.textContent = 'Calling...';
  var fname = document.getElementById('name-input').value.trim();
  if (!fname) { showStatus('Please enter a name.', 'error'); btn.disabled=false; btn.textContent='\u260e Call Now'; return; }
  var body = { to: phone, call_type: callType, friend_name: fname };
  if (callType === 'plan') {
    body.time  = document.getElementById('time-input').value.trim();
    body.place = document.getElementById('place-input').value.trim();
  }
  try {
    var res = await fetch('/call/outbound', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
    var data = await res.json();
    if (res.ok) { showStatus('Pooja is calling '+phone+'...', 'success'); }
    else { showStatus(data.error || 'Call failed.', 'error'); }
  } catch(e) { showStatus('Network error.', 'error'); }
  btn.disabled = false; btn.textContent = '\u260e Call Now';
}
function showStatus(msg, type) {
  var el = document.getElementById('status');
  el.textContent = msg; el.className = 'status '+type; el.style.display = 'block';
  setTimeout(function(){ el.style.display='none'; }, 5000);
}
function loadLogs() {
  fetch('/pooja/logs').then(function(r){ return r.json(); }).then(function(logs) {
    var c = document.getElementById('logs-container');
    if (!logs || logs.length === 0) { c.innerHTML = '<div style="color:#555;font-size:13px;text-align:center;padding:16px;">No calls yet.</div>'; return; }
    c.innerHTML = logs.map(function(log) {
      var badge = log.call_type==='plan' ? '&#128197; Plan' : '&#128075; Check-in';
      var details = log.call_type==='plan' ? '<div style="font-size:12px;color:#888;margin:4px 0;">'+log.place+' &bull; '+log.time_proposed+'</div>' : '';
      var tr = log.transcript && log.transcript.length ? '<div class="transcript" id="tr-'+log.call_sid+'" style="display:none">'+log.transcript.map(function(l){ var parts=l.split(': '); return '<div class="tr-line"><span class="tr-role">'+parts[0]+':</span> '+parts.slice(1).join(': ')+'</div>'; }).join('')+'</div>' : '';
      var audio = log.recording_url ? '<audio controls style="width:100%;margin-top:10px;border-radius:8px;" src="'+log.recording_url+'"></audio>' : '<div style="font-size:11px;color:#555;margin-top:6px;">Recording processing...</div>';
      return '<div class="log-card"><div class="log-top"><div><div class="log-name">'+log.friend_name+'</div><div class="log-meta">'+badge+' &bull; '+log.timestamp+' &bull; '+log.duration+'s</div></div><div class="log-status '+( log.status==='completed' ? 'ok' : 'na')+'">'+log.status+'</div></div>'+details+'<button class="tr-toggle" onclick="toggleTr(\\''+log.call_sid+'\\')">&#128172; Transcript</button>'+tr+audio+'</div>';
    }).join('');
  }).catch(function(){});
}
function toggleTr(sid) { var el=document.getElementById('tr-'+sid); if(el) el.style.display=el.style.display==='none'?'block':'none'; }
loadLogs();
setInterval(loadLogs, 15000);
</script>
</body>
</html>"""
        html = html.replace('CONTACTS_PLACEHOLDER', contacts_json)
        return Response(html, mimetype='text/html')

    return Response('Not found', status=404)

    login_html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pooja</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #0a0a0f; color: #e8e8f0;
    min-height: 100vh;
    display: flex; align-items: center; justify-content: center;
    padding: 20px;
  }
  .box {
    width: 100%; max-width: 340px;
    background: #13131a;
    border: 1px solid #1e1e2e;
    border-radius: 20px;
    padding: 36px 28px;
    text-align: center;
  }
  .avatar {
    width: 64px; height: 64px;
    background: linear-gradient(135deg, #7c3aed, #db2777);
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 28px;
    margin: 0 auto 16px;
    box-shadow: 0 0 24px rgba(124,58,237,0.4);
  }
  h1 { font-size: 20px; font-weight: 700; margin-bottom: 4px; }
  p { font-size: 13px; color: #666; margin-bottom: 28px; }
  input {
    width: 100%;
    background: #1a1a26;
    border: 1px solid #1e1e2e;
    border-radius: 10px;
    padding: 13px 16px;
    color: #e8e8f0;
    font-size: 15px;
    outline: none;
    margin-bottom: 14px;
    text-align: center;
    letter-spacing: 3px;
  }
  input:focus { border-color: #7c3aed; }
  button {
    width: 100%; padding: 14px;
    border-radius: 12px; border: none;
    background: linear-gradient(135deg, #7c3aed, #db2777);
    color: white; font-size: 15px; font-weight: 700; cursor: pointer;
  }
  .error { color: #f87171; font-size: 13px; margin-top: 12px; }
</style>
</head>
<body>
<div class="box">
  <div class="avatar">&#128188;</div>
  <h1>Pooja</h1>
  <p>Mr. Ravi's Personal Assistant</p>
  <form method="POST">
    <input type="password" name="password" placeholder="Password" autofocus>
    <button type="submit">Enter</button>
  </form>
  ERROR_PLACEHOLDER
</div>
</body>
</html>"""
    if error:
        login_html = login_html.replace('ERROR_PLACEHOLDER', f'<div class="error">{error}</div>')
    else:
        login_html = login_html.replace('ERROR_PLACEHOLDER', '')
    return Response(login_html, mimetype='text/html')

# ============================================================
# OUTBOUND CALL — TRIGGER
# ============================================================
@app.route('/call/outbound', methods=['POST'])
def outbound_call():
    data      = request.get_json(force=True) or {}
    to        = data.get('to', '').strip()
    call_type = data.get('call_type', 'checkin')
    time      = data.get('time', '').strip()
    place     = data.get('place', '').strip()

    if not to:
        return {"error": "to is required"}, 400
    if call_type == 'plan' and (not time or not place):
        return {"error": "time and place required for plan call"}, 400

    print(f"\n📤 Outbound [{call_type}] → {friend_name} ({to}) | Time: {time} | Place: {place}")

    try:
        twilio = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        base   = os.getenv('BASE_URL', 'https://callbot-production-a211.up.railway.app')
        params = urllib.parse.urlencode({'call_type': call_type, 'time': time, 'place': place, 'friend_name': friend_name})
        call   = twilio.calls.create(
            to=to,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{base}/call/outbound/start?{params}",
            status_callback=f"{base}/call/status",
            status_callback_method='POST',
            record=True
        )
        print(f"  ✅ SID: {call.sid}")
        return {"status": "calling", "sid": call.sid}, 200
    except Exception as e:
        print(f"  ❌ {e}")
        return {"error": str(e)}, 500

# ============================================================
# OUTBOUND — START (friend picks up)
# ============================================================
@app.route('/call/outbound/start', methods=['POST'])
def outbound_start():
    call_sid     = request.form.get('CallSid', '')
    call_type    = request.args.get('call_type', 'checkin')
    time         = request.args.get('time', 'soon')
    place        = request.args.get('place', 'the usual spot')
    friend_name  = request.args.get('friend_name', 'Friend')

    print(f"\n🤝 Picked up | SID: {call_sid} | {friend_name} | Type: {call_type}")

    with call_lock:
        call_history[call_sid] = []
        call_meta[call_sid]    = {'type': call_type, 'time': time, 'place': place, 'friend_name': friend_name, 'language': 'english'}

    if call_type == 'plan':
        name_part = f"Hello, is this {friend_name}? " if friend_name and not friend_name.startswith('+') and not friend_name.isdigit() else "Hello! "
        opening = name_part + f"This is Pooja calling on behalf of Mr. Ravi. I was wondering if you are available to meet him at {place} around {time}?"
    else:
        name_part = f"Hello, is this {friend_name}? " if friend_name and not friend_name.startswith('+') and not friend_name.isdigit() else "Hello! "
        opening = name_part + "This is Pooja calling on behalf of Mr. Ravi. He wanted me to check in on you and see how you have been doing."

    response = VoiceResponse()
    play_audio_or_say(response, opening, voice_id=POOJA_VOICE_ID, fallback_lang='en-US')

    with call_lock:
        call_history[call_sid].append({"role": "assistant", "content": opening})

    gather = Gather(
        input='speech',
        action='/call/outbound/respond',
        method='POST',
        speech_timeout=3,
        language='hi-IN',
        speech_model='phone_call'
    )
    response.append(gather)
    response.redirect('/call/outbound/start?' + request.query_string.decode())
    return str(response)

# ============================================================
# OUTBOUND — RESPOND (Pooja continues conversation)
# ============================================================
@app.route('/call/outbound/respond', methods=['POST'])
def outbound_respond():
    friend_text = request.form.get('SpeechResult', '').strip()
    call_sid    = request.form.get('CallSid', '')
    print(f"  \U0001f442 Friend said: {friend_text}")
    response = VoiceResponse()
    if not friend_text:
        gather = Gather(input='speech', action='/call/outbound/respond',
                        method='POST', speech_timeout=3, language='hi-IN', speech_model='phone_call')
        response.say("Sorry, I didn't catch that. Could you say that again?", voice='alice', language='hi-IN')
        response.append(gather)
        return str(response)
    reply = get_pooja_reply(call_sid, friend_text)
    print(f"  \U0001f4ac Pooja: {reply}")
    end_call    = '[END_CALL]' in reply
    clean_reply = reply.replace('[END_CALL]', '').strip()


    # Pooja always replies in English, friend always speaks Hindi
    play_audio_or_say(response, clean_reply, voice_id=POOJA_VOICE_ID, fallback_lang='en-US')
    if end_call:
        print(f"  \u2705 Pooja hanging up")
        response.hangup()
    else:
        gather = Gather(input='speech', action='/call/outbound/respond',
                        method='POST', speech_timeout=3, language='hi-IN', speech_model='phone_call')
        response.append(gather)
    return str(response)

# ============================================================
# INCOMING CALL — SARCASTIC BOT
# ============================================================
@app.route('/call/incoming', methods=['POST'])
def incoming_call():
    caller   = request.form.get('From', '')
    call_sid = request.form.get('CallSid', '')
    print(f"\n\U0001f4de Incoming call from {caller} | SID: {call_sid}")
    response = VoiceResponse()
    if any(caller.endswith(b.replace('+91','').replace('+','')) for b in BLOCKED_NUMBERS if b):
        response.say("Hello, please leave a message.", voice='alice', language='en-IN')
        response.hangup()
        return str(response)
    with call_lock:
        call_history[call_sid] = []
    is_indian = caller.startswith('+91') or caller.startswith('0091')
    lang = 'hi-IN' if is_indian else 'en-US'
    gather = Gather(input='speech', action=f'/call/respond?detected_lang={lang}',
                    method='POST', speech_timeout=2, language=lang, speech_model='phone_call')
    gather.say("Bol." if is_indian else "Yeah?", voice='alice',
               language='hi-IN' if is_indian else 'en-US')
    response.append(gather)
    response.redirect('/call/incoming')
    return str(response)

# ============================================================
# INCOMING — RESPOND
# ============================================================
@app.route('/call/respond', methods=['POST'])
def respond():
    caller_text = request.form.get('SpeechResult', '').strip()
    call_sid    = request.form.get('CallSid', '')
    twilio_lang = request.form.get('Language', request.args.get('detected_lang', 'hi-IN'))
    language    = 'english' if twilio_lang.lower().startswith('en') else 'hindi'
    print(f"  \U0001f442 Caller said: {caller_text} | Lang: {language}")
    response = VoiceResponse()
    if not caller_text:
        gather = Gather(input='speech', action=f'/call/respond?detected_lang={twilio_lang}',
                        method='POST', speech_timeout=2, language=twilio_lang, speech_model='phone_call')
        gather.say("Kya bola? Suna nahi." if language=='hindi' else "Didn't catch that mate.",
                   voice='alice', language='hi-IN' if language=='hindi' else 'en-AU')
        response.append(gather)
        return str(response)
    reply = get_sarcastic_reply(call_sid, caller_text, language)
    print(f"  \U0001f916 Bot: {reply}")
    play_audio_or_say(response, reply, language=language,
                      fallback_lang='hi-IN' if language=='hindi' else 'en-AU')
    gather = Gather(input='speech', action=f'/call/respond?detected_lang={twilio_lang}',
                    method='POST', speech_timeout=2, language=twilio_lang, speech_model='phone_call')
    response.append(gather)
    return str(response)

# ============================================================
# CALL STATUS — CLEANUP + SAVE LOG
# ============================================================
@app.route('/call/status', methods=['POST'])
def call_status():
    call_sid = request.form.get('CallSid', '')
    status   = request.form.get('CallStatus', '')
    duration = request.form.get('CallDuration', '0')
    print(f"\n\U0001f4f5 {call_sid} ended | {status} | {duration}s")

    with call_lock:
        meta    = call_meta.get(call_sid, {})
        history = call_history.get(call_sid, [])

    # Only log Pooja outbound calls
    if meta.get('type') in ('plan', 'checkin'):
        # Build transcript
        transcript = []
        for msg in history:
            role    = 'Pooja' if msg['role'] == 'assistant' else 'Friend'
            transcript.append(f"{role}: {msg['content']}")

        # Fetch recording URL from Twilio (may take a few seconds to be available)
        recording_url = None
        try:
            twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            recordings    = twilio_client.recordings.list(call_sid=call_sid, limit=1)
            if recordings:
                rec = recordings[0]
                recording_url = f"/recording/{rec.sid}"
        except Exception as e:
            print(f"  Recording fetch error: {e}")

        log_entry = {
            'call_sid':      call_sid,
            'friend_name':   meta.get('friend_name', 'Unknown'),
            'call_type':     meta.get('type', 'checkin'),
            'time_proposed': meta.get('time', ''),
            'place':         meta.get('place', ''),
            'duration':      duration,
            'status':        status,
            'timestamp':     datetime.datetime.now().strftime('%d %b %Y, %I:%M %p'),
            'transcript':    transcript,
            'recording_url': recording_url,
        }
        with call_lock:
            call_logs.insert(0, log_entry)
            # Keep last 50 logs
            if len(call_logs) > 50:
                call_logs.pop()

    with call_lock:
        call_history.pop(call_sid, None)
        call_meta.pop(call_sid, None)
    return '', 200


# ============================================================
# LOGS API — for UI polling
# ============================================================
@app.route('/pooja/logs', methods=['GET'])
def get_logs():
    with call_lock:
        logs = list(call_logs)
    return json.dumps(logs), 200, {'Content-Type': 'application/json'}


# ============================================================
# RECORDING PROXY
# ============================================================
@app.route('/recording/<recording_sid>', methods=['GET'])
def serve_recording(recording_sid):
    try:
        from requests.auth import HTTPBasicAuth
        url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Recordings/{recording_sid}.mp3"
        resp = requests.get(url, auth=HTTPBasicAuth(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), stream=True)
        if resp.status_code == 200:
            return Response(resp.content, mimetype='audio/mpeg')
        return "Not found", 404
    except Exception as e:
        return str(e), 500

# ============================================================
# HEALTH CHECK
# ============================================================
@app.route('/')
def health():
    return "\U0001f916 Sarcastic Call Bot + Pooja Assistant is RUNNING!", 200

# ============================================================
# MAIN
# ============================================================
if __name__ == '__main__':
    print("=" * 60)
    print("\U0001f680 Sarcastic Call Bot + Pooja Starting...")
    print("=" * 60)
    print(f"\U0001f4de Incoming calls:    POST /call/incoming")
    print(f"\U0001f4e4 Pooja UI:          GET  /pooja")
    print(f"\U0001f4e4 Pooja outbound:    POST /call/outbound")
    print(f"\U0001f50a Audio serve:       GET  /audio/<filename>")
    print(f"\U0001f511 UI Password:       {UI_PASSWORD}")
    print(f"\U0001f3ad ElevenLabs: {'\u2705 Configured' if ELEVENLABS_API_KEY else '\u274c Not configured -- using Twilio TTS'}")
    print("=" * 60)
    port = int(os.getenv('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
