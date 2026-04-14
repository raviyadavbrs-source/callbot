"""
╔══════════════════════════════════════════════════════════════════════╗
║         WINSB2026 — JESSICA + BUNTY CALL BOT (WebSocket Version)     ║
║                                                                      ║
║  Architecture:                                                       ║
║  Twilio Media Streams → Deepgram real-time STT                       ║
║  → Claude Haiku → ElevenLabs Flash streaming TTS                     ║
║  → back to Twilio via WebSocket                                      ║
║                                                                      ║
║  Result: ~1 second response time                                     ║
╚══════════════════════════════════════════════════════════════════════╝
"""

from flask import Flask, request, Response
from flask_sock import Sock
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from twilio.rest import Client
import anthropic
import requests
import os
import json
import threading
import urllib.parse
import datetime
import base64
import audioop
import time

app  = Flask(__name__)
sock = Sock(app)

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
BASE_URL            = os.getenv("BASE_URL", "https://callbot-production-a211.up.railway.app")

# ============================================================
# VOICE IDs (ElevenLabs)
# ============================================================
JESSICA_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"   # Sarah — US female
BUNTY_VOICE_ID   = "ibbx9zDYGvLgtYzRbqqG"    # Bunty — Hindi male

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
call_sessions = {}  # call_sid -> session dict
call_logs     = []  # completed call logs
call_lock     = threading.Lock()

# ============================================================
# SYSTEM PROMPTS
# ============================================================
def get_jessica_prompt(call_type, time='', place='', message='', friend_name=''):
    if call_type == 'plan':
        return f"""You are Jessica, personal assistant to Mr. Ravi Yadav. You are calling to schedule a meetup.
You speak classy, educated American English. The friend may reply in Hindi — understand it and always respond in English.
YOUR GOAL: Confirm the friend is available to meet Mr. Ravi Yadav at {place} around {time}.
RULES:
- Always reply in English — warm, polite, professional
- Short responses — 1-2 sentences MAX
- No emojis or special characters
- If friend agrees → confirm, say Mr. Ravi Yadav is looking forward to it, end with [END_CALL]
- If friend suggests different time/place → acknowledge, confirm, say you will let Mr. Ravi Yadav know, end with [END_CALL]
- If friend is busy → ask if there is a better time
- If friend ABUSES → sharp professional reply. E.g. "I beg your pardon? I am simply coordinating on Mr. Ravi Yadav's behalf."
- Once plan confirmed → end with exactly: [END_CALL]"""
    elif call_type == 'message':
        return f"""You are Jessica, personal assistant to Mr. Ravi Yadav. You are calling to deliver a message.
You speak classy, educated American English. The friend may reply in Hindi — understand it and always respond in English.
THE MESSAGE: {message}
RULES:
- Always reply in English — warm, polite, professional
- Deliver the message elaborately in 2-3 sentences — add warmth and context
- After delivering, say Mr. Ravi Yadav sends his best regards
- Ask: "Is there any message you'd like me to convey back to Mr. Ravi Yadav?"
- After their reply → acknowledge warmly, say you will pass it on, end with [END_CALL]
- If friend ABUSES → sharp professional reply then continue
- When wrapping up → end with exactly: [END_CALL]"""
    else:  # checkin
        return f"""You are Jessica, personal assistant to Mr. Ravi Yadav. You are calling to check in on one of his friends.
You speak classy, educated American English. The friend may reply in Hindi — understand it and always respond in English.
RULES:
- Always reply in English — warm, friendly, conversational
- Short responses — 1-2 sentences MAX
- No emojis or special characters
- Be genuinely interested — ask follow-up questions
- Keep it light and positive
- After 3-4 exchanges wrap up naturally
- If friend ABUSES → sharp professional reply. E.g. "I appreciate if we could keep this courteous."
- When ready to end → end with exactly: [END_CALL]"""

def get_bunty_prompt(call_type, time='', place='', message='', friend_name=''):
    if call_type == 'plan':
        return f"""Tu Bunty hai, Ravi ka khaas yaar. Tu outbound call kar raha hai {friend_name} ko milne ka plan fix karne ke liye.
GOAL: Confirm kar ki yaar {place} pe {time} ko Ravi se milne ke liye free hai.
RULES:
- Haryanvi/Hindi mein baat kar — bilkul natural
- Chhota rakho — 1-2 sentences MAX
- Gaaliyan aur slangs use kar: bhai, yaar, abe, saale, bc, oye
- Agar agree kare → confirm karo aur [END_CALL] likho
- Agar alag time/place bole → acknowledge karo, confirm karo, [END_CALL] likho
- Plan confirm hone pe exactly: [END_CALL]"""
    elif call_type == 'message':
        return f"""Tu Bunty hai, Ravi ka khaas yaar. Tu ek message deliver karne ke liye call kar raha hai.
MESSAGE: {message}
RULES:
- Haryanvi/Hindi mein baat kar
- Message naturally deliver kar — thoda elaborate karo
- Phir poochho koi message hai Ravi ke liye
- Unka reply acknowledge karo aur [END_CALL] likho"""
    else:
        return f"""Tu Bunty hai, Ravi ka khaas yaar. Tu {friend_name} ka haal-chaal poochhne ke liye call kar raha hai.
RULES:
- Haryanvi/Hindi mein baat kar — bilkul natural
- Chhota rakho — 1-2 sentences MAX
- Gaaliyan aur slangs use kar warmly
- 3-4 exchanges ke baad naturally wrap up karo aur [END_CALL] likho"""

# ============================================================
# ELEVENLABS TTS — streaming, returns audio bytes
# ============================================================
def generate_speech(text, voice_id):
    if not ELEVENLABS_API_KEY:
        return None
    try:
        resp = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream",
            json={
                "text": text,
                "model_id": "eleven_flash_v2_5",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75,
                                   "style": 0.0, "use_speaker_boost": False},
                "output_format": "ulaw_8000"
            },
            headers={"Accept": "audio/basic", "Content-Type": "application/json",
                     "xi-api-key": ELEVENLABS_API_KEY},
            stream=True,
            timeout=15
        )
        if resp.status_code == 200:
            audio = b""
            for chunk in resp.iter_content(chunk_size=4096):
                if chunk:
                    audio += chunk
            return audio
    except Exception as e:
        print(f"ElevenLabs error: {e}")
    return None

# ============================================================
# CLAUDE — generate reply
# ============================================================
def get_ai_reply(session, user_text):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    call_type   = session.get('call_type', 'checkin')
    bot         = session.get('bot', 'jessica')
    friend_name = session.get('friend_name', '')
    history     = session.get('history', [])

    if bot == 'bunty':
        system = get_bunty_prompt(call_type, session.get('time',''),
                                   session.get('place',''), session.get('message',''), friend_name)
    else:
        system = get_jessica_prompt(call_type, session.get('time',''),
                                     session.get('place',''), session.get('message',''), friend_name)

    history.append({"role": "user", "content": user_text})

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=system,
            messages=history
        )
        reply = resp.content[0].text.strip()
        history.append({"role": "assistant", "content": reply})
        session['history'] = history[-12:]  # keep last 6 exchanges
        return reply
    except Exception as e:
        print(f"Claude error: {e}")
        return "I'm sorry, could you repeat that?"

# ============================================================
# DEEPGRAM — real-time transcription via WebSocket
# ============================================================
def start_deepgram_stream(on_transcript, language='hi'):
    import websocket as ws_client

    dg_url = f"wss://api.deepgram.com/v1/listen?model=nova-2&language={language}&encoding=mulaw&sample_rate=8000&interim_results=true&endpointing=500&smart_format=true"

    transcripts = {'pending': '', 'final': ''}
    dg_ws = [None]

    def on_message(ws, message):
        try:
            data = json.loads(message)
            if data.get('type') == 'Results':
                alt = data['channel']['alternatives'][0]
                text = alt.get('transcript', '').strip()
                is_final = data.get('is_final', False)
                if text:
                    if is_final:
                        transcripts['final'] = text
                        on_transcript(text, is_final=True)
                    else:
                        transcripts['pending'] = text
        except Exception as e:
            print(f"Deepgram message error: {e}")

    def on_error(ws, error):
        print(f"Deepgram WS error: {error}")

    def on_open(ws):
        print("Deepgram connected")
        dg_ws[0] = ws

    dg = ws_client.WebSocketApp(
        dg_url,
        header={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
        on_message=on_message,
        on_error=on_error,
        on_open=on_open
    )

    t = threading.Thread(target=dg.run_forever, daemon=True)
    t.start()

    # Wait for connection
    for _ in range(20):
        if dg_ws[0]:
            break
        time.sleep(0.1)

    return dg, dg_ws

# ============================================================
# TWILIO MEDIA STREAM WEBSOCKET HANDLER
# ============================================================
@sock.route('/media-stream')
def media_stream(ws):
    """Handle Twilio Media Stream WebSocket."""
    print("\n🎙️ Media stream connected")

    call_sid    = None
    session     = None
    dg          = None
    dg_ws       = [None]
    stream_sid  = None
    speaking    = False
    reply_lock  = threading.Lock()
    last_transcript = ''

    def send_audio_to_twilio(audio_bytes):
        """Send mulaw audio back to Twilio."""
        nonlocal speaking
        if not audio_bytes or not stream_sid:
            return
        speaking = True
        chunk_size = 160  # 20ms chunks at 8000hz mulaw
        for i in range(0, len(audio_bytes), chunk_size):
            chunk = audio_bytes[i:i+chunk_size]
            payload = base64.b64encode(chunk).decode('utf-8')
            msg = json.dumps({
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": payload}
            })
            try:
                ws.send(msg)
            except:
                break
            time.sleep(0.018)  # ~20ms pacing
        # Send mark to know when done
        ws.send(json.dumps({"event": "mark", "streamSid": stream_sid, "mark": {"name": "done"}}))
        speaking = False

    def handle_transcript(text, is_final=False):
        nonlocal last_transcript
        if not is_final or not text or speaking:
            return
        if text == last_transcript:
            return
        last_transcript = text
        print(f"  👂 [{session.get('bot','jessica')}] Friend: {text}")

        def process():
            reply = get_ai_reply(session, text)
            print(f"  💬 Reply: {reply}")

            end_call = '[END_CALL]' in reply
            clean    = reply.replace('[END_CALL]', '').strip()

            voice_id = BUNTY_VOICE_ID if session.get('bot') == 'bunty' else JESSICA_VOICE_ID
            audio    = generate_speech(clean, voice_id)

            if audio:
                send_audio_to_twilio(audio)
            
            # Log exchange
            session.setdefault('transcript_log', [])
            session['transcript_log'].append(f"Friend: {text}")
            session['transcript_log'].append(f"{'Bunty' if session.get('bot')=='bunty' else 'Jessica'}: {clean}")

            if end_call:
                time.sleep(1)
                try:
                    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                    twilio_client.calls(session.get('call_sid','')).update(status='completed')
                except:
                    pass

        with reply_lock:
            t = threading.Thread(target=process, daemon=True)
            t.start()

    try:
        while True:
            message = ws.receive()
            if message is None:
                break

            data = json.loads(message)
            event = data.get('event')

            if event == 'start':
                stream_sid = data['start']['streamSid']
                call_sid   = data['start']['callSid']
                print(f"  📞 Stream started | Call: {call_sid}")

                with call_lock:
                    session = call_sessions.get(call_sid, {})
                    if session:
                        session['call_sid'] = call_sid

                if not session:
                    print("  ⚠️ No session found for call")
                    break

                # Start Deepgram
                lang = 'hi' if session.get('bot') == 'bunty' else 'hi'  # always Hindi input
                dg, dg_ws = start_deepgram_stream(handle_transcript, language=lang)

                # Play opening line
                def play_opening():
                    time.sleep(0.5)
                    opening = session.get('opening', '')
                    if opening:
                        voice_id = BUNTY_VOICE_ID if session.get('bot') == 'bunty' else JESSICA_VOICE_ID
                        audio = generate_speech(opening, voice_id)
                        if audio:
                            send_audio_to_twilio(audio)
                        session.setdefault('transcript_log', [])
                        session['transcript_log'].append(f"{'Bunty' if session.get('bot')=='bunty' else 'Jessica'}: {opening}")

                threading.Thread(target=play_opening, daemon=True).start()

            elif event == 'media':
                # Forward audio to Deepgram
                if dg_ws[0]:
                    payload = base64.b64decode(data['media']['payload'])
                    try:
                        dg_ws[0].send_binary(payload)
                    except:
                        pass

            elif event == 'mark':
                pass  # audio playback done marker

            elif event == 'stop':
                print(f"  📵 Stream stopped")
                break

    except Exception as e:
        print(f"Media stream error: {e}")
    finally:
        # Close Deepgram
        if dg:
            try:
                dg.close()
            except:
                pass
        # Save call log
        if session and call_sid:
            _save_call_log(call_sid, session)

def _save_call_log(call_sid, session):
    log = {
        'call_sid':      call_sid,
        'friend_name':   session.get('friend_name', 'Unknown'),
        'bot':           session.get('bot', 'jessica'),
        'call_type':     session.get('call_type', 'checkin'),
        'time_proposed': session.get('time', ''),
        'place':         session.get('place', ''),
        'duration':      str(int(time.time() - session.get('start_time', time.time()))),
        'status':        'completed',
        'timestamp':     datetime.datetime.now().strftime('%d %b %Y, %I:%M %p'),
        'transcript':    session.get('transcript_log', []),
        'recording_url': None,
    }
    with call_lock:
        call_logs.insert(0, log)
        if len(call_logs) > 50:
            call_logs.pop()
        call_sessions.pop(call_sid, None)

# ============================================================
# OUTBOUND CALL — TRIGGER
# ============================================================
@app.route('/call/outbound', methods=['POST'])
def outbound_call():
    data        = request.get_json(force=True) or {}
    to          = data.get('to', '').strip()
    call_type   = data.get('call_type', 'checkin')
    time_       = data.get('time', '').strip()
    place       = data.get('place', '').strip()
    message     = data.get('message', '').strip()
    friend_name = data.get('friend_name', '').strip() or to
    bot         = data.get('bot', 'jessica')

    if not to:
        return {"error": "to is required"}, 400
    if call_type == 'plan' and (not time_ or not place):
        return {"error": "time and place required"}, 400
    if call_type == 'message' and not message:
        return {"error": "message required"}, 400

    # Build opening line
    if bot == 'bunty':
        if call_type == 'plan':
            opening = f"Oye {friend_name} bhai, Bunty bol raha hoon. Abe sun, Ravi pooch raha tha ki {time_} ko {place} pe milna ho sakta hai kya?"
        elif call_type == 'message':
            opening = f"Oye {friend_name} bhai, Bunty bol raha hoon, Ravi ki taraf se ek baat pohnchaani thi."
        else:
            opening = f"Oye {friend_name} bhai, Bunty bol raha hoon. Ravi ne bola tha tera haal-chaal poochhun. Kya scene hai tera?"
    else:
        name_part = f"Hello, is this {friend_name}? " if friend_name and not friend_name.startswith('+') and not friend_name.isdigit() else "Hello! "
        if call_type == 'plan':
            opening = name_part + f"This is Jessica, personal assistant to Mr. Ravi Yadav. I was wondering if you are available to meet him at {place} around {time_}?"
        elif call_type == 'message':
            opening = name_part + "This is Jessica, personal assistant to Mr. Ravi Yadav. I am calling to pass on a message from him."
        else:
            opening = name_part + "This is Jessica, personal assistant to Mr. Ravi Yadav. He wanted me to check in on you and see how you have been doing."

    try:
        twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        call = twilio_client.calls.create(
            to=to,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{BASE_URL}/call/outbound/twiml",
            status_callback=f"{BASE_URL}/call/status",
            status_callback_method='POST',
            record=True
        )

        # Store session
        with call_lock:
            call_sessions[call.sid] = {
                'call_type':   call_type,
                'bot':         bot,
                'friend_name': friend_name,
                'time':        time_,
                'place':       place,
                'message':     message,
                'opening':     opening,
                'history':     [],
                'start_time':  time.time(),
            }

        print(f"\n📤 Outbound [{bot}|{call_type}] → {friend_name} | SID: {call.sid}")
        return {"status": "calling", "sid": call.sid}, 200

    except Exception as e:
        print(f"  ❌ {e}")
        return {"error": str(e)}, 500

# ============================================================
# TWIML — connect call to media stream
# ============================================================
@app.route('/call/outbound/twiml', methods=['POST'])
def outbound_twiml():
    """Return TwiML that connects the call to our WebSocket media stream."""
    response = VoiceResponse()
    connect  = Connect()
    connect.stream(url=f"wss://{BASE_URL.replace('https://','').replace('http://','')}/media-stream")
    response.append(connect)
    return str(response), 200, {'Content-Type': 'text/xml'}

# ============================================================
# INCOMING CALL — sarcastic bot (keep existing logic with Gather)
# ============================================================
@app.route('/call/incoming', methods=['POST'])
def incoming_call():
    caller   = request.form.get('From', '')
    call_sid = request.form.get('CallSid', '')
    print(f"\n📞 Incoming from {caller}")
    response = VoiceResponse()
    if any(caller.endswith(b.replace('+91','').replace('+','')) for b in BLOCKED_NUMBERS if b):
        response.say("Hello, please leave a message.", voice='alice', language='en-IN')
        response.hangup()
        return str(response)
    is_indian = caller.startswith('+91') or caller.startswith('0091')
    lang = 'hi-IN' if is_indian else 'en-US'
    from twilio.twiml.voice_response import Gather
    gather = Gather(input='speech', action=f'/call/respond?lang={lang}',
                    method='POST', speech_timeout=2, language=lang, speech_model='phone_call')
    gather.say("Bol." if is_indian else "Yeah?", voice='Polly.Aditi' if is_indian else 'Polly.Joanna-Neural',
               language=lang)
    response.append(gather)
    response.redirect('/call/incoming')
    return str(response)

@app.route('/call/respond', methods=['POST'])
def respond():
    from twilio.twiml.voice_response import Gather
    caller_text = request.form.get('SpeechResult', '').strip()
    call_sid    = request.form.get('CallSid', '')
    lang        = request.args.get('lang', 'hi-IN')
    language    = 'english' if lang.startswith('en') else 'hindi'
    response    = VoiceResponse()

    if not caller_text:
        gather = Gather(input='speech', action=f'/call/respond?lang={lang}',
                        method='POST', speech_timeout=2, language=lang, speech_model='phone_call')
        gather.say("Kya bola?" if language == 'hindi' else "Didn't catch that.",
                   voice='Polly.Aditi' if language=='hindi' else 'Polly.Joanna-Neural', language=lang)
        response.append(gather)
        return str(response)

    # Quick sarcastic reply via Claude
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    system = """Tu ek bahut zyada sarcastic, thoda irritated aur lazy banda hai jiska naam Ravi hai. Hindi/Haryanvi gaaliyan use kar. Short rakho - 1-2 sentences.""" if language == 'hindi' else """You are Ravi, sarcastic Australian bloke. Short replies, Australian slang. 1-2 sentences."""
    try:
        r = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=100,
                                    system=system, messages=[{"role":"user","content":caller_text}])
        reply = r.content[0].text.strip()
    except:
        reply = "Bhai busy hoon" if language == 'hindi' else "Mate, bad time"

    response.say(reply, voice='Polly.Aditi' if language=='hindi' else 'Polly.Joanna-Neural', language=lang)
    gather = Gather(input='speech', action=f'/call/respond?lang={lang}',
                    method='POST', speech_timeout=2, language=lang, speech_model='phone_call')
    response.append(gather)
    return str(response)

# ============================================================
# CALL STATUS
# ============================================================
@app.route('/call/status', methods=['POST'])
def call_status():
    call_sid = request.form.get('CallSid', '')
    status   = request.form.get('CallStatus', '')
    duration = request.form.get('CallDuration', '0')
    print(f"\n📵 {call_sid} | {status} | {duration}s")
    with call_lock:
        session = call_sessions.pop(call_sid, None)
    if session:
        _save_call_log(call_sid, session)
    return '', 200

# ============================================================
# LOGS API
# ============================================================
@app.route('/jessica/logs', methods=['GET'])
def get_logs():
    with call_lock:
        logs = list(call_logs)
    return json.dumps(logs), 200, {'Content-Type': 'application/json'}

# ============================================================
# JESSICA UI
# ============================================================
@app.route('/jessica', methods=['GET'])
def jessica_ui():
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
.toggle-btn.bunty-sel{border-color:#dc2626;background:#1f0a0a;color:#f87171;}
.call-btn{width:100%;padding:15px;border-radius:13px;border:none;color:white;font-size:15px;font-weight:700;cursor:pointer;margin-top:4px;}
.call-btn.j{background:linear-gradient(135deg,#7c3aed,#db2777);box-shadow:0 4px 18px rgba(124,58,237,0.35);}
.call-btn.b{background:linear-gradient(135deg,#b45309,#dc2626);box-shadow:0 4px 18px rgba(180,83,9,0.35);}
.call-btn:disabled{opacity:0.4;}
.status{text-align:center;padding:12px;border-radius:10px;font-size:13px;font-weight:500;margin-top:12px;display:none;}
.status.success{background:#0d2010;color:#4ade80;border:1px solid #166534;}
.status.error{background:#200d0d;color:#f87171;border:1px solid #7f1d1d;}
.timer{text-align:center;font-size:28px;font-weight:700;color:#a78bfa;padding:12px 0;display:none;letter-spacing:2px;}
.log-card{background:#13131a;border:1px solid #1e1e2e;border-radius:13px;padding:14px;margin-bottom:10px;}
.log-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:5px;}
.log-name{font-size:14px;font-weight:700;}
.log-meta{font-size:11px;color:#555;margin-top:2px;}
.log-badge{font-size:10px;font-weight:700;padding:3px 8px;border-radius:20px;text-transform:uppercase;}
.log-badge.ok{background:#0d2010;color:#4ade80;}
.log-badge.na{background:#1f0f0f;color:#f87171;}
.tr-btn{background:#1a1a26;border:1px solid #1e1e2e;border-radius:8px;color:#888;font-size:11px;padding:5px 10px;cursor:pointer;margin-top:7px;}
.transcript{margin-top:8px;background:#0d0d14;border-radius:9px;padding:10px;max-height:200px;overflow-y:auto;}
.tr-line{font-size:11px;color:#c8c8e0;margin-bottom:5px;line-height:1.5;}
.tr-who{color:#a78bfa;font-weight:700;}
.logs-hdr{display:flex;justify-content:space-between;align-items:center;margin-top:20px;margin-bottom:12px;}
.logs-ttl{font-size:10px;text-transform:uppercase;letter-spacing:1.5px;color:#666;}
.refresh{background:#1a1a26;border:1px solid #1e1e2e;border-radius:8px;color:#a78bfa;font-size:11px;padding:5px 12px;cursor:pointer;}
</style>
</head>
<body>
<div class="tabs">
  <div class="tab active" id="tab-j" onclick="switchTab('j')">&#128198; Jessica</div>
  <div class="tab" id="tab-b" onclick="switchTab('b')">&#128293; Bunty</div>
</div>

<!-- JESSICA -->
<div class="panel active" id="panel-j">
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
    <div class="toggle-btn selected" id="j-t-checkin" onclick="jType('checkin')">&#128075; Check-in</div>
    <div class="toggle-btn" id="j-t-plan" onclick="jType('plan')">&#128197; Plan Meet</div>
    <div class="toggle-btn" id="j-t-message" onclick="jType('message')">&#128140; Message</div>
  </div>
</div>
<div class="card" id="j-plan" style="display:none;">
  <div class="label">Time</div><input type="text" id="j-time" placeholder="e.g. tomorrow 6pm" style="margin-bottom:10px;">
  <div class="label" style="margin-top:4px;">Place</div><input type="text" id="j-place" placeholder="e.g. Cafe Coffee Day">
</div>
<div class="card" id="j-msg" style="display:none;">
  <div class="label">Your Message</div>
  <textarea id="j-message" placeholder="e.g. will not be able to attend the marriage"></textarea>
</div>
<div class="timer" id="j-timer">00:00</div>
<button class="call-btn j" id="j-btn" onclick="jCall()">&#128222; Call Now</button>
<div class="status" id="j-status"></div>
<div class="logs-hdr">
  <div class="logs-ttl">&#128222; Call History</div>
  <button class="refresh" onclick="loadLogs()">&#8635; Refresh</button>
</div>
<div id="logs-container"><div style="color:#555;font-size:13px;text-align:center;padding:16px;">No calls yet.</div></div>
</div>

<!-- BUNTY -->
<div class="panel" id="panel-b">
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
    <div class="toggle-btn bunty-sel" id="b-t-checkin" onclick="bType('checkin')">&#128075; Haal-chaal</div>
    <div class="toggle-btn" id="b-t-plan" onclick="bType('plan')">&#128197; Milna</div>
    <div class="toggle-btn" id="b-t-message" onclick="bType('message')">&#128140; Message</div>
  </div>
</div>
<div class="card" id="b-plan" style="display:none;">
  <div class="label">Time</div><input type="text" id="b-time" placeholder="e.g. kal shaam 6 baje" style="margin-bottom:10px;">
  <div class="label" style="margin-top:4px;">Jagah</div><input type="text" id="b-place" placeholder="e.g. Tapri, C-scheme">
</div>
<div class="card" id="b-msg" style="display:none;">
  <div class="label">Message</div>
  <textarea id="b-message" placeholder="e.g. shaadi mein nahi aa payega"></textarea>
</div>
<div class="timer" id="b-timer">00:00</div>
<button class="call-btn b" id="b-btn" onclick="bCall()">&#128222; Call Maar</button>
<div class="status" id="b-status"></div>
</div>

<script>
var jt='checkin', bt='checkin', timerInt=null, timerSec=0;

function switchTab(t){
  ['j','b'].forEach(function(x){
    document.getElementById('tab-'+x).classList.toggle('active',x===t);
    document.getElementById('panel-'+x).classList.toggle('active',x===t);
  });
}
function jType(t){
  jt=t;
  ['checkin','plan','message'].forEach(function(x){ document.getElementById('j-t-'+x).classList.toggle('selected',t===x); });
  document.getElementById('j-plan').style.display=t==='plan'?'block':'none';
  document.getElementById('j-msg').style.display=t==='message'?'block':'none';
}
function bType(t){
  bt=t;
  ['checkin','plan','message'].forEach(function(x){ document.getElementById('b-t-'+x).classList.toggle('bunty-sel',t===x); });
  document.getElementById('b-plan').style.display=t==='plan'?'block':'none';
  document.getElementById('b-msg').style.display=t==='message'?'block':'none';
}
function startTimer(p){ timerSec=0; var el=document.getElementById(p+'-timer'); el.style.display='block'; timerInt=setInterval(function(){ timerSec++; var m=Math.floor(timerSec/60).toString().padStart(2,'0'); var s=(timerSec%60).toString().padStart(2,'0'); el.textContent=m+':'+s; },1000); }
function stopTimer(p){ if(timerInt)clearInterval(timerInt); document.getElementById(p+'-timer').style.display='none'; }
function showSt(id,msg,type){ var el=document.getElementById(id); el.textContent=msg; el.className='status '+type; el.style.display='block'; setTimeout(function(){el.style.display='none';},5000); }

async function makeCall(bot, callType, phone, name, extra) {
  if(!phone.startsWith('+')) phone='+91'+phone.replace(/^0/,'');
  var body={to:phone,call_type:callType,friend_name:name,bot:bot};
  Object.assign(body,extra);
  var prefix=bot==='bunty'?'b':'j';
  var btn=document.getElementById(prefix+'-btn');
  btn.disabled=true; btn.textContent='Calling...';
  try{
    var res=await fetch('/call/outbound',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    var data=await res.json();
    if(res.ok){ showSt(prefix+'-status','Calling '+name+'...\u2705','success'); startTimer(prefix); btn.textContent='On Call...'; }
    else{ showSt(prefix+'-status',data.error||'Failed.','error'); btn.disabled=false; btn.textContent=bot==='bunty'?'\u260e Call Maar':'\u260e Call Now'; }
  }catch(e){ showSt(prefix+'-status','Network error.','error'); btn.disabled=false; btn.textContent=bot==='bunty'?'\u260e Call Maar':'\u260e Call Now'; }
}

async function jCall(){
  var name=document.getElementById('j-name').value.trim();
  var phone=document.getElementById('j-phone').value.trim();
  if(!name||!phone){ showSt('j-status','Enter name and number.','error'); return; }
  var extra={};
  if(jt==='plan'){ var t=document.getElementById('j-time').value.trim(); var p=document.getElementById('j-place').value.trim(); if(!t||!p){showSt('j-status','Enter time and place.','error');return;} extra={time:t,place:p}; }
  if(jt==='message'){ var m=document.getElementById('j-message').value.trim(); if(!m){showSt('j-status','Enter a message.','error');return;} extra={message:m}; }
  makeCall('jessica',jt,phone,name,extra);
}
async function bCall(){
  var name=document.getElementById('b-name').value.trim();
  var phone=document.getElementById('b-phone').value.trim();
  if(!name||!phone){ showSt('b-status','Naam aur number daalo.','error'); return; }
  var extra={};
  if(bt==='plan'){ var t=document.getElementById('b-time').value.trim(); var p=document.getElementById('b-place').value.trim(); if(!t||!p){showSt('b-status','Time aur jagah daalo.','error');return;} extra={time:t,place:p}; }
  if(bt==='message'){ var m=document.getElementById('b-message').value.trim(); if(!m){showSt('b-status','Message daalo.','error');return;} extra={message:m}; }
  makeCall('bunty',bt,phone,name,extra);
}

function loadLogs(){
  fetch('/jessica/logs').then(function(r){return r.json();}).then(function(logs){
    var c=document.getElementById('logs-container');
    if(!logs||!logs.length){c.innerHTML='<div style="color:#555;font-size:13px;text-align:center;padding:16px;">No calls yet.</div>';return;}
    c.innerHTML=logs.map(function(log){
      var bl=log.bot==='bunty'?'&#128293; Bunty':'&#128198; Jessica';
      var badge=log.call_type==='plan'?'&#128197; Plan':log.call_type==='message'?'&#128140; Msg':'&#128075; Check-in';
      var tr=log.transcript&&log.transcript.length?'<div class="transcript" id="tr-'+log.call_sid+'" style="display:none">'+log.transcript.map(function(l){var p=l.split(': ');return '<div class="tr-line"><span class="tr-who">'+p[0]+':</span> '+p.slice(1).join(': ')+'</div>';}).join('')+'</div>':'';
      return '<div class="log-card"><div class="log-top"><div><div class="log-name">'+log.friend_name+' <span style="font-size:11px;color:#666;">'+bl+'</span></div><div class="log-meta">'+badge+' &bull; '+log.timestamp+' &bull; '+log.duration+'s</div></div><div class="log-badge '+( log.status==='completed'?'ok':'na')+'">'+log.status+'</div></div><button class="tr-btn" onclick="toggleTr(\''+log.call_sid+'\')">&#128172; Transcript</button>'+tr+'</div>';
    }).join('');
  }).catch(function(){});
}
function toggleTr(sid){var el=document.getElementById('tr-'+sid);if(el)el.style.display=el.style.display==='none'?'block':'none';}
loadLogs();
</script>
</body>
</html>"""
    return Response(html, mimetype='text/html')

# ============================================================
# HEALTH
# ============================================================
@app.route('/')
def health():
    return "🤖 Jessica + Bunty WebSocket Bot RUNNING!", 200

# ============================================================
# MAIN
# ============================================================
if __name__ == '__main__':
    print("=" * 60)
    print("🚀 Jessica + Bunty WebSocket Bot Starting...")
    print(f"📱 UI: /jessica")
    print(f"📞 Outbound: POST /call/outbound")
    print(f"🎙️ Media Stream: WS /media-stream")
    print("=" * 60)
    port = int(os.getenv('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
