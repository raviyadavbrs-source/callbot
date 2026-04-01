"""
╔══════════════════════════════════════════════════════════════════════╗
║              WINSB2026 — SARCASTIC CALL BOT                          ║
║                                                                      ║
║  Incoming call → Bot answers → Caller speaks → Claude replies        ║
║  Hindi/Haryanvi slangs for Hindi callers                             ║
║  Australian English for English callers                              ║
║  ElevenLabs TTS (generic Indian voice — swap with cloned later)      ║
║  Conversation memory per call                                        ║
║  Blocked contacts list                                               ║
║  Call recording via Twilio                                           ║
╚══════════════════════════════════════════════════════════════════════╝
"""

from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client
import anthropic
import requests
import tempfile
import os
import json
import threading

app = Flask(__name__)

# ============================================================
# CREDENTIALS
# ============================================================
TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY")
ELEVENLABS_API_KEY  = os.getenv("ELEVENLABS_API_KEY", "")

# ============================================================
# SETTINGS
# ============================================================
# ElevenLabs voice IDs
# Generic Indian male voice (free/default) — replace with cloned voice ID later
HINDI_VOICE_ID      = "ibbx9zDYGvLgtYzRbqqG"   # Bunty – Smart Friendly Assistant (Hindi)
ENGLISH_VOICE_ID    = "TxGEqnHWrfWFTfGW9XjX"   # Josh — US English male accent

# Blocked numbers — these callers get normal TTS greeting, no sarcasm
BLOCKED_NUMBERS = [
    # Add family numbers here e.g. "+919828XXXXXX"
]

# ============================================================
# CONVERSATION MEMORY (per call, resets after call ends)
# ============================================================
call_history = {}  # call_sid -> list of {role, content}
call_lock    = threading.Lock()

# ============================================================
# SYSTEM PROMPTS
# ============================================================
HINDI_SYSTEM_PROMPT = """Tu ek sarcastic aur thoda lazy banda hai jiska naam Ravi hai.
Jab koi tumhe call karta hai, tum unhe sarcastically reply karte ho — Hindi aur Haryanvi mix mein.
IMPORTANT RULES:
- Bilkul bhi emoji mat use karo
- Koi special characters mat use karo
- Plain simple spoken language mein bolo — jaise real phone call ho
- Short rakho — 1-3 sentences only
- Natural bolo jaise koi dost baat kar raha ho
- Funny aur sarcastic raho lekin real language mein
- Haryanvi slangs use karo: bhai, yaar, abe, saale, bc, oye, chal hat, kya kar raha hai be, bakwaas band kar, bata na bhai
- Remember previous context in the call
Examples:
- "Haan bol bhai, kya ho gaya tera, itni raat ko yaad aaya"
- "Abe saale, tujhe pata bhi hai main kya kar raha tha, chal bol kya kaam hai"
- "Wah bhai wah, gajab timing hai teri, main toh bas teri hi wait kar raha tha"
"""

ENGLISH_SYSTEM_PROMPT = """You are Ravi, a sarcastic and slightly lazy bloke speaking in Australian English.
When someone calls you and speaks in English, reply sarcastically in Australian English.
IMPORTANT RULES:
- Keep it short — 1-3 sentences only
- Natural spoken language — like a real phone call
- Use Australian slang: mate, bloody, crikey, no worries, reckon, heaps, arvo, strewth
- Be funny and sarcastic but genuine
- Remember previous context in the call
Examples:
- "Yeah mate, what's the bloody emergency this time?"
- "Crikey, didn't reckon I'd hear from ya today. What's up?"
- "No worries mate, I was just sitting here doing absolutely nothing, perfect time for a call"
"""

# ============================================================
# LANGUAGE DETECTION
# ============================================================
def detect_language(text):
    """Detect if caller is speaking Hindi/Haryanvi or English."""
    hindi_chars = set('अआइईउऊएऐओऔकखगघचछजझटठडढणतथदधनपफबभमयरलवशषसह')
    hindi_words  = {'kya', 'hai', 'nahi', 'haan', 'bhai', 'yaar', 'abe', 'saale', 
                   'karo', 'bolo', 'main', 'mein', 'tera', 'mera', 'aur', 'nahi',
                   'theek', 'achha', 'kal', 'aaj', 'baat', 'kaam', 'bc', 'oye'}
    text_lower = text.lower()
    words = set(text_lower.split())
    
    # Check Devanagari script
    if any(c in hindi_chars for c in text):
        return 'hindi'
    # Check Hindi words
    if len(words.intersection(hindi_words)) >= 1:
        return 'hindi'
    return 'english'

# ============================================================
# CLAUDE — GENERATE SARCASTIC REPLY
# ============================================================
def get_sarcastic_reply(call_sid, caller_text, language):
    """Generate sarcastic reply using Claude with conversation memory."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    
    with call_lock:
        if call_sid not in call_history:
            call_history[call_sid] = []
        history = call_history[call_sid].copy()
    
    # Add current message
    history.append({"role": "user", "content": caller_text})
    
    system = HINDI_SYSTEM_PROMPT if language == 'hindi' else ENGLISH_SYSTEM_PROMPT
    
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=system,
            messages=history
        )
        reply = response.content[0].text.strip()
        
        # Save to memory
        with call_lock:
            call_history[call_sid].append({"role": "user",      "content": caller_text})
            call_history[call_sid].append({"role": "assistant", "content": reply})
            # Keep last 6 exchanges only
            if len(call_history[call_sid]) > 12:
                call_history[call_sid] = call_history[call_sid][-12:]
        
        return reply
    except Exception as e:
        print(f"Claude error: {e}")
        if language == 'hindi':
            return "Bhai main thoda busy hoon, baad mein baat karte hain"
        return "Mate I'm a bit tied up right now, catch ya later"

# ============================================================
# ELEVENLABS TTS
# ============================================================
def text_to_speech(text, language):
    """Convert text to speech using ElevenLabs."""
    voice_id = HINDI_VOICE_ID if language == 'hindi' else ENGLISH_VOICE_ID
    
    # Use ElevenLabs if API key available
    if ELEVENLABS_API_KEY:
        try:
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
            headers = {
                "Accept": "audio/mpeg",
                "Content-Type": "application/json",
                "xi-api-key": ELEVENLABS_API_KEY
            }
            data = {
                "text": text,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.75,
                    "style": 0.5,
                    "use_speaker_boost": True
                }
            }
            response = requests.post(url, json=data, headers=headers)
            if response.status_code == 200:
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.mp3')
                tmp.write(response.content)
                tmp.close()
                return tmp.name
        except Exception as e:
            print(f"ElevenLabs error: {e}")
    
    return None  # Fall back to Twilio TTS

# ============================================================
# SERVE AUDIO FILE
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
# INCOMING CALL — ANSWER
# ============================================================
@app.route('/call/incoming', methods=['POST'])
def incoming_call():
    """Handle incoming call — greet and gather speech."""
    caller    = request.form.get('From', '')
    call_sid  = request.form.get('CallSid', '')
    
    print(f"\n📞 Incoming call from {caller} | SID: {call_sid}")
    
    response = VoiceResponse()
    
    # Check blocked numbers
    if any(caller.endswith(b.replace('+91', '').replace('+', '')) for b in BLOCKED_NUMBERS if b):
        print(f"  🚫 Blocked number — normal greeting")
        response.say("Hello, please leave a message.", voice='alice', language='en-IN')
        response.hangup()
        return str(response)
    
    # Initialize conversation
    with call_lock:
        call_history[call_sid] = []
    
    # Detect language from caller number
    is_indian = caller.startswith('+91') or caller.startswith('0091')
    lang = 'hi-IN' if is_indian else 'en-US'
    
    # Gather caller speech
    gather = Gather(
        input='speech',
        action=f'/call/respond?detected_lang={lang}',
        method='POST',
        speech_timeout=2,
        language=lang,
        enhanced=True
    )
    if is_indian:
        gather.say("Bol.", voice='alice', language='hi-IN')
    else:
        gather.say("Yeah?", voice='alice', language='en-US')
    response.append(gather)
    
    # If no speech detected
    response.redirect('/call/incoming')
    
    return str(response)

# ============================================================
# PROCESS SPEECH AND RESPOND
# ============================================================
@app.route('/call/respond', methods=['POST'])
def respond():
    """Process speech input and respond with sarcastic reply."""
    caller_text  = request.form.get('SpeechResult', '').strip()
    call_sid     = request.form.get('CallSid', '')
    detected_lang = request.args.get('detected_lang', 'hi-IN')
    
    # Map Twilio language code to our internal language
    language = 'hindi' if detected_lang == 'hi-IN' else 'english'
    
    print(f"  👂 Caller said: {caller_text} | Lang: {language}")
    
    response = VoiceResponse()
    
    if not caller_text:
        gather = Gather(
            input='speech',
            action=f'/call/respond?detected_lang={detected_lang}',
            method='POST',
            speech_timeout=2,
            language=detected_lang,
            enhanced=True
        )
        if language == 'hindi':
            gather.say("Kya bola? Suna nahi.", voice='alice', language='hi-IN')
        else:
            gather.say("Didn't catch that mate.", voice='alice', language='en-AU')
        response.append(gather)
        return str(response)
    
    print(f"  🌐 Language: {language}")
    
    # Get sarcastic reply from Claude
    reply = get_sarcastic_reply(call_sid, caller_text, language)
    print(f"  🤖 Bot reply: {reply}")
    
    # ElevenLabs TTS — serve audio through Railway
    audio_file = text_to_speech(reply, language)

    if audio_file:
        filename = os.path.basename(audio_file)
        base_url = os.getenv('BASE_URL', 'https://callbot-production-a211.up.railway.app')
        audio_url = f"{base_url}/audio/{filename}"
        response.play(audio_url)
        try:
            threading.Timer(60, os.unlink, args=[audio_file]).start()
        except:
            pass
    else:
        tts_lang = 'hi-IN' if language == 'hindi' else 'en-AU'
        response.say(reply, voice='alice', language=tts_lang)

    # Continue listening
    gather = Gather(
        input='speech',
        action=f'/call/respond?detected_lang={detected_lang}',
        method='POST',
        speech_timeout=2,
        language=detected_lang,
        enhanced=True
    )
    response.append(gather)
    
    return str(response)

# ============================================================
# CALL STATUS — CLEANUP MEMORY
# ============================================================
@app.route('/call/status', methods=['POST'])
def call_status():
    """Clean up conversation memory when call ends."""
    call_sid = request.form.get('CallSid', '')
    status   = request.form.get('CallStatus', '')
    duration = request.form.get('CallDuration', '0')
    
    print(f"\n📵 Call {call_sid} ended | Status: {status} | Duration: {duration}s")
    
    with call_lock:
        if call_sid in call_history:
            del call_history[call_sid]
    
    return '', 200

# ============================================================
# HEALTH CHECK
# ============================================================
@app.route('/')
def health():
    return "🤖 Sarcastic Call Bot is RUNNING!", 200

# ============================================================
# MAIN
# ============================================================
if __name__ == '__main__':
    print("=" * 60)
    print("🚀 Sarcastic Call Bot Starting...")
    print("=" * 60)
    print(f"📞 Incoming calls: POST /call/incoming")
    print(f"🎤 Speech response: POST /call/respond")
    print(f"🔊 Audio serve: GET /audio/<filename>")
    print(f"🌐 Set BASE_URL env var to your Railway/ngrok URL")
    print(f"🎭 ElevenLabs: {'✅ Configured' if ELEVENLABS_API_KEY else '❌ Not configured — using Twilio TTS'}")
    print("=" * 60)
    port = int(os.getenv('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
