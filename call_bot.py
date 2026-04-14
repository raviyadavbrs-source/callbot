"""
╔══════════════════════════════════════════════════════════════════════╗
║              WINSB2026 — SARCASTIC CALL BOT + POOJA ASSISTANT        ║
║                                                                      ║
║  Incoming call → Sarcastic bot answers                               ║
║  /jessica → Mobile UI to trigger Pooja outbound calls                  ║
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
DEEPGRAM_API_KEY    = os.getenv("DEEPGRAM_API_KEY", "d86f846652be259f234acad143a0f9d4e3027cba")
UI_PASSWORD         = os.getenv("UI_PASSWORD", "ravi2026")

# ============================================================
# VOICE IDs
# ============================================================
HINDI_VOICE_ID   = "ibbx9zDYGvLgtYzRbqqG"
ENGLISH_VOICE_ID = "nwj0s2LU9bDWRKND5yzA"
JESSICA_VOICE_ID   = "EXAVITQu4vr4xnSDxMaL"   # Jessica — female US accent (Sarah)

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

Opening: "Hello, this is Jessica calling on behalf of Mr. Ravi. I was wondering if you are available to meet him at {place} around {time}?"
"""

def get_pooja_checkin_prompt(language='english'):
    return """You are Pooja, personal assistant to Mr. Ravi. You are calling to check in on one of his close friends.
You speak classy, educated American English. The friend will reply in Hindi — understand it and always respond in English.

YOUR GOAL: Have a warm, casual check-in conversation.

RULES:
- Always reply in English only — warm, friendly, conversational
- Short responses — 1-3 sentences
- No emojis or special characters
- Opening: "Hello, this is Jessica calling on behalf of Mr. Ravi. He wanted me to check in on you and see how you have been doing."
- Be genuinely interested — ask follow-up questions
- Keep it light and positive
- After 3-4 exchanges wrap up naturally
- If friend ABUSES → sharp professional reply. E.g. "I appreciate if we could keep this courteous. Now, how have you been?"
- When ready to end, finish with exactly: [END_CALL]
"""



# ============================================================
# DEEPGRAM TRANSCRIPTION
# ============================================================
def transcribe_with_deepgram(audio_url):
    """Transcribe audio URL using Deepgram — fast Hindi/English recognition."""
    try:
        headers = {
            "Authorization": f"Token {DEEPGRAM_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "url": audio_url
        }
        params = {
            "model": "nova-2",
            "language": "hi",
            "smart_format": True,
            "punctuate": True
        }
        resp = requests.post(
            "https://api.deepgram.com/v1/listen",
            headers=headers,
            json=payload,
            params=params,
            timeout=10
        )
        if resp.status_code == 200:
            result = resp.json()
            transcript = result["results"]["channels"][0]["alternatives"][0]["transcript"]
            return transcript.strip()
    except Exception as e:
        print(f"Deepgram error: {e}")
    return ""

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
    bot       = meta.get('bot', 'jessica')
    language  = detect_language(friend_text)

    # Update language in meta
    with call_lock:
        call_meta[call_sid]['language'] = language

    if bot == 'bunty':
        system = get_bunty_prompt(call_type, meta.get('time',''), meta.get('place',''), meta.get('message',''))
    elif call_type == 'plan':
        system = get_pooja_plan_prompt(meta.get('time', 'soon'), meta.get('place', 'the usual spot'), language)
    elif call_type == 'message':
        system = get_pooja_message_prompt(meta.get('message', ''))
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
                "model_id": "eleven_flash_v2_5",
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
@app.route('/jessica', methods=['GET', 'POST'])
def jessica_ui():
    contacts_json = json.dumps(CONTACTS)
    if True:
        html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Call Assistant</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0a0f;color:#e8e8f0;min-height:100vh;padding:16px 16px 40px;}
.tabs{display:flex;gap:0;margin-bottom:20px;background:#13131a;border-radius:14px;padding:4px;}
.tab{flex:1;padding:11px;text-align:center;border-radius:10px;cursor:pointer;font-size:13px;font-weight:600;color:#666;transition:all 0.15s;}
.tab.active{background:linear-gradient(135deg,#7c3aed,#db2777);color:white;}
.panel{display:none;}
.panel.active{display:block;}
.header{text-align:center;padding:16px 0 20px;}
.avatar{width:56px;height:56px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:24px;margin:0 auto 10px;}
.avatar.jessica{background:linear-gradient(135deg,#7c3aed,#db2777);box-shadow:0 0 20px rgba(124,58,237,0.4);}
.avatar.bunty{background:linear-gradient(135deg,#b45309,#dc2626);box-shadow:0 0 20px rgba(180,83,9,0.4);}
h2{font-size:20px;font-weight:700;}
.sub{font-size:12px;color:#888;margin-top:3px;}
.card{background:#13131a;border:1px solid #1e1e2e;border-radius:14px;padding:16px;margin-bottom:12px;}
.label{font-size:10px;text-transform:uppercase;letter-spacing:1.5px;color:#666;margin-bottom:8px;}
input,textarea{width:100%;background:#1a1a26;border:1px solid #1e1e2e;border-radius:10px;padding:12px 14px;color:#e8e8f0;font-size:15px;outline:none;font-family:inherit;}
input:focus,textarea:focus{border-color:#7c3aed;}
input::placeholder,textarea::placeholder{color:#444;}
textarea{min-height:80px;resize:vertical;}
.toggle-row{display:flex;gap:8px;flex-wrap:wrap;}
.toggle-btn{flex:1;min-width:80px;padding:10px 8px;border-radius:10px;border:2px solid #1e1e2e;background:#1a1a26;color:#888;font-size:12px;font-weight:600;cursor:pointer;text-align:center;}
.toggle-btn.selected{border-color:#db2777;background:#1f0f1a;color:#f472b6;}
.toggle-btn.bunty-selected{border-color:#dc2626;background:#1f0a0a;color:#f87171;}
.call-btn{width:100%;padding:15px;border-radius:13px;border:none;color:white;font-size:15px;font-weight:700;cursor:pointer;margin-top:4px;}
.call-btn.jessica-btn{background:linear-gradient(135deg,#7c3aed,#db2777);box-shadow:0 4px 18px rgba(124,58,237,0.35);}
.call-btn.bunty-btn{background:linear-gradient(135deg,#b45309,#dc2626);box-shadow:0 4px 18px rgba(180,83,9,0.35);}
.call-btn:disabled{opacity:0.4;}
.status{text-align:center;padding:12px;border-radius:10px;font-size:13px;font-weight:500;margin-top:12px;display:none;}
.status.success{background:#0d2010;color:#4ade80;border:1px solid #166534;}
.status.error{background:#200d0d;color:#f87171;border:1px solid #7f1d1d;}
.timer{text-align:center;font-size:22px;font-weight:700;color:#a78bfa;padding:10px 0;display:none;}
.log-card{background:#13131a;border:1px solid #1e1e2e;border-radius:13px;padding:14px;margin-bottom:10px;}
.log-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:5px;}
.log-name{font-size:14px;font-weight:700;}
.log-meta{font-size:11px;color:#555;margin-top:2px;}
.log-status{font-size:10px;font-weight:700;padding:3px 8px;border-radius:20px;text-transform:uppercase;}
.log-status.ok{background:#0d2010;color:#4ade80;}
.log-status.na{background:#1f0f0f;color:#f87171;}
.tr-toggle{background:#1a1a26;border:1px solid #1e1e2e;border-radius:8px;color:#888;font-size:11px;padding:5px 10px;cursor:pointer;margin-top:7px;}
.transcript{margin-top:8px;background:#0d0d14;border-radius:9px;padding:10px;max-height:180px;overflow-y:auto;}
.tr-line{font-size:11px;color:#c8c8e0;margin-bottom:5px;line-height:1.5;}
.tr-role{color:#a78bfa;font-weight:700;}
.logs-header{display:flex;justify-content:space-between;align-items:center;margin-top:20px;margin-bottom:12px;}
.logs-title{font-size:10px;text-transform:uppercase;letter-spacing:1.5px;color:#666;}
.refresh-btn{background:#1a1a26;border:1px solid #1e1e2e;border-radius:8px;color:#a78bfa;font-size:11px;padding:5px 12px;cursor:pointer;}
</style>
</head>
<body>
<div class="tabs">
  <div class="tab active" id="tab-jessica" onclick="switchTab('jessica')">&#128188; Jessica</div>
  <div class="tab" id="tab-bunty" onclick="switchTab('bunty')">&#128293; Bunty</div>
</div>

<!-- JESSICA PANEL -->
<div class="panel active" id="panel-jessica">
<div class="header">
  <div class="avatar jessica">&#128198;</div>
  <h2>Jessica</h2>
  <p class="sub">Mr. Ravi Yadav's Personal Assistant</p>
</div>
<div class="card">
  <div class="label">Name</div>
  <input type="text" id="j-name" placeholder="e.g. Sameer" style="margin-bottom:10px;">
  <div class="label" style="margin-top:4px;">Phone Number</div>
  <input type="tel" id="j-phone" placeholder="+91 98290 55878">
</div>
<div class="card">
  <div class="label">Call Type</div>
  <div class="toggle-row">
    <div class="toggle-btn selected" id="j-btn-checkin" onclick="jSelectType('checkin')">&#128075; Check-in</div>
    <div class="toggle-btn" id="j-btn-plan" onclick="jSelectType('plan')">&#128197; Plan Meet</div>
    <div class="toggle-btn" id="j-btn-message" onclick="jSelectType('message')">&#128140; Message</div>
  </div>
</div>
<div class="card" id="j-plan-fields" style="display:none;">
  <div class="label">Time</div>
  <input type="text" id="j-time" placeholder="e.g. tomorrow 6pm" style="margin-bottom:10px;">
  <div class="label" style="margin-top:4px;">Place</div>
  <input type="text" id="j-place" placeholder="e.g. Cafe Coffee Day">
</div>
<div class="card" id="j-message-fields" style="display:none;">
  <div class="label">Your Message</div>
  <textarea id="j-message" placeholder="e.g. will not be able to attend the marriage"></textarea>
</div>
<div class="timer" id="j-timer">00:00</div>
<button class="call-btn jessica-btn" id="j-call-btn" onclick="jessicaCall()">&#128222; Call Now</button>
<div class="status" id="j-status"></div>
<div class="logs-header">
  <div class="logs-title">&#128222; Call History</div>
  <button class="refresh-btn" onclick="loadLogs()">&#8635; Refresh</button>
</div>
<div id="logs-container"><div style="color:#555;font-size:13px;text-align:center;padding:16px;">No calls yet.</div></div>
</div>

<!-- BUNTY PANEL -->
<div class="panel" id="panel-bunty">
<div class="header">
  <div class="avatar bunty">&#128293;</div>
  <h2>Bunty</h2>
  <p class="sub">Ravi ka Khaas Yaar</p>
</div>
<div class="card">
  <div class="label">Naam</div>
  <input type="text" id="b-name" placeholder="e.g. Sameer" style="margin-bottom:10px;">
  <div class="label" style="margin-top:4px;">Number</div>
  <input type="tel" id="b-phone" placeholder="+91 98290 55878">
</div>
<div class="card">
  <div class="label">Call Type</div>
  <div class="toggle-row">
    <div class="toggle-btn bunty-selected" id="b-btn-checkin" onclick="bSelectType('checkin')">&#128075; Haal-chaal</div>
    <div class="toggle-btn" id="b-btn-plan" onclick="bSelectType('plan')">&#128197; Milna</div>
    <div class="toggle-btn" id="b-btn-message" onclick="bSelectType('message')">&#128140; Message</div>
  </div>
</div>
<div class="card" id="b-plan-fields" style="display:none;">
  <div class="label">Time</div>
  <input type="text" id="b-time" placeholder="e.g. kal shaam 6 baje" style="margin-bottom:10px;">
  <div class="label" style="margin-top:4px;">Jagah</div>
  <input type="text" id="b-place" placeholder="e.g. Tapri, C-scheme">
</div>
<div class="card" id="b-message-fields" style="display:none;">
  <div class="label">Message</div>
  <textarea id="b-message" placeholder="e.g. shaadi mein nahi aa payega"></textarea>
</div>
<div class="timer" id="b-timer">00:00</div>
<button class="call-btn bunty-btn" id="b-call-btn" onclick="buntyCall()">&#128222; Call Maar</button>
<div class="status" id="b-status"></div>
</div>

<script>
var jCallType = 'checkin';
var bCallType = 'checkin';
var timerInterval = null;
var timerSeconds = 0;

function switchTab(tab) {
  document.getElementById('tab-jessica').classList.toggle('active', tab==='jessica');
  document.getElementById('tab-bunty').classList.toggle('active', tab==='bunty');
  document.getElementById('panel-jessica').classList.toggle('active', tab==='jessica');
  document.getElementById('panel-bunty').classList.toggle('active', tab==='bunty');
}

function jSelectType(t) {
  jCallType = t;
  ['checkin','plan','message'].forEach(function(x){ document.getElementById('j-btn-'+x).classList.toggle('selected', t===x); });
  document.getElementById('j-plan-fields').style.display = t==='plan' ? 'block' : 'none';
  document.getElementById('j-message-fields').style.display = t==='message' ? 'block' : 'none';
}

function bSelectType(t) {
  bCallType = t;
  ['checkin','plan','message'].forEach(function(x){ document.getElementById('b-btn-'+x).classList.toggle('bunty-selected', t===x); });
  document.getElementById('b-plan-fields').style.display = t==='plan' ? 'block' : 'none';
  document.getElementById('b-message-fields').style.display = t==='message' ? 'block' : 'none';
}

function startTimer(prefix) {
  timerSeconds = 0;
  var el = document.getElementById(prefix+'-timer');
  el.style.display = 'block';
  timerInterval = setInterval(function() {
    timerSeconds++;
    var m = Math.floor(timerSeconds/60).toString().padStart(2,'0');
    var s = (timerSeconds%60).toString().padStart(2,'0');
    el.textContent = m+':'+s;
  }, 1000);
}

function stopTimer(prefix) {
  if (timerInterval) clearInterval(timerInterval);
  document.getElementById(prefix+'-timer').style.display = 'none';
}

async function jessicaCall() {
  var phone = document.getElementById('j-phone').value.trim();
  var fname = document.getElementById('j-name').value.trim();
  if (!fname) { showStatus('j-status', 'Enter a name.', 'error'); return; }
  if (!phone) { showStatus('j-status', 'Enter a phone number.', 'error'); return; }
  if (!phone.startsWith('+')) phone = '+91' + phone.replace(/^0/,'');
  if (jCallType==='plan') {
    if (!document.getElementById('j-time').value.trim() || !document.getElementById('j-place').value.trim()) {
      showStatus('j-status', 'Enter time and place.', 'error'); return;
    }
  }
  if (jCallType==='message' && !document.getElementById('j-message').value.trim()) {
    showStatus('j-status', 'Enter a message.', 'error'); return;
  }
  var btn = document.getElementById('j-call-btn');
  btn.disabled = true; btn.textContent = 'Calling...';
  var body = { to: phone, call_type: jCallType, friend_name: fname, bot: 'jessica' };
  if (jCallType==='plan') { body.time=document.getElementById('j-time').value.trim(); body.place=document.getElementById('j-place').value.trim(); }
  if (jCallType==='message') { body.message=document.getElementById('j-message').value.trim(); }
  try {
    var res = await fetch('/call/outbound', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
    var data = await res.json();
    if (res.ok) { showStatus('j-status', 'Jessica is calling '+fname+'...', 'success'); startTimer('j'); btn.textContent = 'On Call...'; }
    else { showStatus('j-status', data.error||'Call failed.', 'error'); btn.disabled=false; btn.textContent='\u260e Call Now'; }
  } catch(e) { showStatus('j-status', 'Network error.', 'error'); btn.disabled=false; btn.textContent='\u260e Call Now'; }
}

async function buntyCall() {
  var phone = document.getElementById('b-phone').value.trim();
  var fname = document.getElementById('b-name').value.trim();
  if (!fname) { showStatus('b-status', 'Naam daalo.', 'error'); return; }
  if (!phone) { showStatus('b-status', 'Number daalo.', 'error'); return; }
  if (!phone.startsWith('+')) phone = '+91' + phone.replace(/^0/,'');
  if (bCallType==='plan') {
    if (!document.getElementById('b-time').value.trim() || !document.getElementById('b-place').value.trim()) {
      showStatus('b-status', 'Time aur jagah daalo.', 'error'); return;
    }
  }
  if (bCallType==='message' && !document.getElementById('b-message').value.trim()) {
    showStatus('b-status', 'Message daalo.', 'error'); return;
  }
  var btn = document.getElementById('b-call-btn');
  btn.disabled = true; btn.textContent = 'Call ho raha hai...';
  var body = { to: phone, call_type: bCallType, friend_name: fname, bot: 'bunty' };
  if (bCallType==='plan') { body.time=document.getElementById('b-time').value.trim(); body.place=document.getElementById('b-place').value.trim(); }
  if (bCallType==='message') { body.message=document.getElementById('b-message').value.trim(); }
  try {
    var res = await fetch('/call/outbound', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
    var data = await res.json();
    if (res.ok) { showStatus('b-status', 'Bunty call maar raha hai '+fname+' ko...', 'success'); startTimer('b'); btn.textContent = 'Call pe hai...'; }
    else { showStatus('b-status', data.error||'Call fail ho gaya.', 'error'); btn.disabled=false; btn.textContent='\u260e Call Maar'; }
  } catch(e) { showStatus('b-status', 'Network error.', 'error'); btn.disabled=false; btn.textContent='\u260e Call Maar'; }
}

function showStatus(id, msg, type) {
  var el = document.getElementById(id);
  el.textContent = msg; el.className = 'status '+type; el.style.display = 'block';
  setTimeout(function(){ el.style.display='none'; }, 5000);
}

function loadLogs() {
  fetch('/jessica/logs').then(function(r){ return r.json(); }).then(function(logs) {
    var c = document.getElementById('logs-container');
    if (!logs||!logs.length) { c.innerHTML='<div style="color:#555;font-size:13px;text-align:center;padding:16px;">No calls yet.</div>'; return; }
    c.innerHTML = logs.map(function(log) {
      var botLabel = log.bot==='bunty' ? '&#128293; Bunty' : '&#128198; Jessica';
      var badge = log.call_type==='plan' ? '&#128197; Plan' : log.call_type==='message' ? '&#128140; Msg' : '&#128075; Check-in';
      var details = log.call_type==='plan' ? '<div style="font-size:11px;color:#888;margin:4px 0;">'+log.place+' &bull; '+log.time_proposed+'</div>' : '';
      var tr = log.transcript&&log.transcript.length ? '<div class="transcript" id="tr-'+log.call_sid+'" style="display:none">'+log.transcript.map(function(l){ var p=l.split(': '); return '<div class="tr-line"><span class="tr-role">'+p[0]+':</span> '+p.slice(1).join(': ')+'</div>'; }).join('')+'</div>' : '';
      var audio = log.recording_url ? '<audio controls style="width:100%;margin-top:8px;border-radius:8px;" src="'+log.recording_url+'"></audio>' : '<div style="font-size:11px;color:#555;margin-top:5px;">Recording processing...</div>';
      return '<div class="log-card"><div class="log-top"><div><div class="log-name">'+log.friend_name+' <span style="font-size:11px;color:#666;">'+botLabel+'</span></div><div class="log-meta">'+badge+' &bull; '+log.timestamp+' &bull; '+log.duration+'s</div></div><div class="log-status '+( log.status==='completed'?'ok':'na')+'">'+log.status+'</div></div>'+details+'<button class="tr-toggle" onclick="toggleTr(\\''+log.call_sid+'\\')">&#128172; Transcript</button>'+tr+audio+'</div>';
    }).join('');
  }).catch(function(){});
}
function toggleTr(sid) { var el=document.getElementById('tr-'+sid); if(el) el.style.display=el.style.display==='none'?'block':'none'; }
loadLogs();
</script>
</body>
</html>"""
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
    data        = request.get_json(force=True) or {}
    to          = data.get('to', '').strip()
    call_type   = data.get('call_type', 'checkin')
    time        = data.get('time', '').strip()
    place       = data.get('place', '').strip()
    friend_name = data.get('friend_name', '').strip() or to
    friend_name = data.get('friend_name', '').strip() or to
    message     = data.get('message', '').strip()
    bot         = data.get('bot', 'jessica')
    if not to:
        return {"error": "to is required"}, 400
    if call_type == 'plan' and (not time or not place):
        return {"error": "time and place required for plan call"}, 400
    if call_type == 'message' and not message:
        return {"error": "message is required for message delivery call"}, 400

    print(f"\n📤 Outbound [{call_type}] → {friend_name} ({to}) | Time: {time} | Place: {place}")

    try:
        twilio = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        base   = os.getenv('BASE_URL', 'https://callbot-production-a211.up.railway.app')
        params = urllib.parse.urlencode({'call_type': call_type, 'time': time, 'place': place, 'friend_name': friend_name, 'message': message, 'bot': bot})
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
    friend_name  = request.args.get('friend_name', 'Friend')
    message      = request.args.get('message', '')
    bot          = request.args.get('bot', 'jessica')
    print(f"\n🤝 Picked up | SID: {call_sid} | {friend_name} | Type: {call_type}")

    with call_lock:
        call_history[call_sid] = []
        call_meta[call_sid]    = {'type': call_type, 'time': time, 'place': place, 'friend_name': friend_name, 'message': message, 'bot': bot, 'language': 'english'}

    if bot == 'bunty':
        if call_type == 'plan':
            opening = f"Oye {friend_name} bhai, Bunty bol raha hoon. Abe sun, Ravi pooch raha tha ki {time} ko {place} pe milna ho sakta hai kya?"
        elif call_type == 'message':
            opening = f"Oye {friend_name} bhai, Bunty bol raha hoon, Ravi ki taraf se ek baat pohnchaani thi."
        else:
            opening = f"Oye {friend_name} bhai, Bunty bol raha hoon. Ravi ne bola tha tera haal-chaal poochhun. Kya scene hai tera?"
    else:
        name_part = f"Hello, is this {friend_name}? " if friend_name and not friend_name.startswith('+') and not friend_name.isdigit() else "Hello! "
        if call_type == 'plan':
            opening = name_part + f"This is Jessica, personal assistant to Mr. Ravi Yadav. I was wondering if you are available to meet him at {place} around {time}?"
        elif call_type == 'message':
            opening = name_part + "This is Jessica, personal assistant to Mr. Ravi Yadav. I am calling to pass on a message from him."
        else:
            opening = name_part + "This is Jessica, personal assistant to Mr. Ravi Yadav. He wanted me to check in on you and see how you have been doing."
    response = VoiceResponse()
    voice_id = HINDI_VOICE_ID if bot == 'bunty' else JESSICA_VOICE_ID
    fallback = 'hi-IN' if bot == 'bunty' else 'en-US'
    play_audio_or_say(response, opening, voice_id=voice_id, fallback_lang=fallback)

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


    with call_lock:
        resp_bot = call_meta.get(call_sid, {}).get('bot', 'jessica')
    resp_voice    = HINDI_VOICE_ID if resp_bot == 'bunty' else JESSICA_VOICE_ID
    resp_fallback = 'hi-IN' if resp_bot == 'bunty' else 'en-US'
    play_audio_or_say(response, clean_reply, voice_id=resp_voice, fallback_lang=resp_fallback)
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
            role    = 'Jessica' if msg['role'] == 'assistant' else 'Friend'
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
            'bot':           meta.get('bot', 'jessica'),
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
@app.route('/jessica/logs', methods=['GET'])
def get_logs():
    with call_lock:
        logs = list(call_logs)
    return json.dumps(logs), 200, {'Content-Type': 'application/json'}



# ============================================================
# HINDI MALE ABUSIVE OUTBOUND BOT — BUNTY
# ============================================================
def get_bunty_prompt(call_type, time='', place='', message=''):
    if call_type == 'plan':
        return f"""Tu Bunty hai, Ravi ka khaas banda. Tu outbound call kar raha hai apne yaar ko milne ka plan fix karne ke liye.
GOAL: Confirm kar ki yaar {place} pe {time} ko Ravi se milne ke liye free hai.
RULES:
- Haryanvi/Hindi mein baat kar — bilkul natural, jaise real dost hote hain
- Short rakho — 1-3 sentences
- Gaaliyan aur slangs zaroor use kar: bhai, yaar, abe, saale, bc, oye, chal
- Warm aur friendly raho lekin abusive bhi — jaise close dost hote hain
- Agar yaar agree kare → confirm karo aur wrap up karo
- Agar alag time/place bole → acknowledge karo, confirm karo
- Plan confirm hone pe exactly likho: [END_CALL]
Opening: "Oye {'{friend_name}'} bhai, Ravi ka yaar Bunty bol raha hoon. Abe sun, Ravi pooch raha tha ki {time} ko {place} pe milna ho sakta hai kya?"
"""
    elif call_type == 'message':
        return f"""Tu Bunty hai, Ravi ka khaas banda. Tu ek message deliver karne ke liye call kar raha hai.
MESSAGE: {message}
RULES:
- Haryanvi/Hindi mein baat kar
- Message ko naturally aur warmly deliver kar — elaborate karo thoda
- Phir poochho: "Koi message hai Ravi ke liye?"
- Unka reply acknowledge karo aur wrap up karo
- End pe exactly likho: [END_CALL]
Opening: "Oye {'{friend_name}'} bhai, Bunty bol raha hoon, Ravi ki taraf se call hai."
"""
    else:  # checkin
        return """Tu Bunty hai, Ravi ka khaas banda. Tu check-in call kar raha hai.
GOAL: Casually poochho kaisa hai, kya chal raha hai.
RULES:
- Haryanvi/Hindi mein baat kar — bilkul natural
- Short rakho — 1-3 sentences
- Gaaliyan aur slangs use kar warmly
- Genuinely interested raho
- 3-4 exchanges ke baad naturally wrap up karo
- End pe exactly likho: [END_CALL]
Opening: "Oye bhai, Bunty bol raha hoon. Ravi ne bola tha tera haal-chaal poochhun. Kya scene hai tera?"
"""

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
    print(f"\U0001f4e4 Pooja UI:          GET  /jessica")
    print(f"\U0001f4e4 Pooja outbound:    POST /call/outbound")
    print(f"\U0001f50a Audio serve:       GET  /audio/<filename>")
    print(f"\U0001f511 UI Password:       {UI_PASSWORD}")
    print(f"\U0001f3ad ElevenLabs: {'\u2705 Configured' if ELEVENLABS_API_KEY else '\u274c Not configured -- using Twilio TTS'}")
    print("=" * 60)
    port = int(os.getenv('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
