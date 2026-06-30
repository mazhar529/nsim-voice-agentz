"""
╔══════════════════════════════════════════════════════════════════════╗
║           NSIM AI VOICE ASSISTANT — FULLY FREE VERSION (v3)         ║
║                                                                      ║
║  ONLY COST: your Twilio phone number rental + per-minute call rate. ║
║  Everything else below is genuinely free, no API key, no signup.    ║
║                                                                      ║
║  AI        : Pollinations.ai — free, no key, GPT-4o powered         ║
║  TTS       : edge-tts (Microsoft Edge neural voices) — free,        ║
║              no key, sounds close to premium paid TTS               ║
║  STT       : Twilio's built-in speech recognition — already         ║
║              included in your per-minute call cost, real-time,      ║
║              no extra service/latency needed                        ║
║  LANGUAGE  : auto-detected per turn (langdetect, free/offline) —    ║
║              works across Hindi, English, Tamil, Telugu, Bengali,   ║
║              Marathi, Gujarati, Kannada, Malayalam, Urdu, etc.       ║
║  HOST      : Render.com — free forever                              ║
║  LEADS     : WhatsApp alert to owner number                        ║
║                                                                      ║
║  WHAT'S NEW IN v3:                                                   ║
║  - Replaced Twilio's billed Polly <Say> voices with edge-tts        ║
║    (Microsoft neural voices) — same natural quality, but actually   ║
║    free. Twilio <Say> is kept ONLY as a last-resort fallback if     ║
║    edge-tts fails for some reason (rare).                          ║
║  - Language detection now uses langdetect instead of hand-written   ║
║    word lists, so it generalises to any language the caller uses,   ║
║    not just Hindi/English. The AI is told to always answer back     ║
║    in the same language the caller just used.                      ║
║  - Barge-in: <Play>/<Say> is nested inside <Gather>, so if the      ║
║    caller starts speaking mid-reply, Twilio cuts the audio off      ║
║    immediately and listens.                                         ║
║  - "Continue": if the caller interrupted and then says "continue"   ║
║    (or the equivalent in their language), the assistant replays     ║
║    its last full answer instead of treating it as a new question.   ║
║  - The AI is hard-restricted to ONLY use facts from database.py —   ║
║    it will not guess fees, timings, or invent a phone number.       ║
║                                                                      ║
║  DEPLOY STEPS:                                                       ║
║  1. Push this + database.py + requirements.txt + render.yaml        ║
║     to GitHub                                                        ║
║  2. Connect GitHub to render.com → Deploy                           ║
║  3. Set Twilio "A call comes in" webhook to:                        ║
║     https://YOURAPP.onrender.com/call   (note: /call, not just /)   ║
║  4. Done. Call your number.                                         ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os, re, json, time, uuid, asyncio, threading
import urllib.request, urllib.parse, base64
from pathlib import Path
from flask import Flask, request, Response
import edge_tts
from langdetect import detect_langs, DetectorFactory

from database import (
    build_knowledge, INTEREST_KEYWORDS,
    CONTACT, OWNER_WHATSAPP, COURSES
)

DetectorFactory.seed = 0  # deterministic langdetect results

app  = Flask(__name__)
PORT = int(os.environ.get("PORT", 5000))

AUDIO_DIR = Path("static/audio")
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

NSIM_INFO       = build_knowledge()
CONTACT_DIGITS  = re.sub(r"\D", "", CONTACT)

# ── Active call counter ──────────────────────────────────────────────
active_calls = 0
_call_lock   = threading.Lock()
MAX_CALLS    = 200

# ── Session memory per call ────────────────────────────────────────────
_sessions  = {}
_sess_lock = threading.Lock()

def _get_session(sid):
    with _sess_lock:
        return _sessions.setdefault(sid, {"history": [], "lang": "hi", "last_reply": ""})

def _add_hist(sid, role, text):
    with _sess_lock:
        s = _sessions.setdefault(sid, {"history": [], "lang": "hi", "last_reply": ""})
        s["history"].append({"role": role, "content": text})
        if len(s["history"]) > 10:
            s["history"] = s["history"][-10:]

def _set_lang(sid, lang):
    with _sess_lock:
        s = _sessions.setdefault(sid, {"history": [], "lang": "hi", "last_reply": ""})
        s["lang"] = lang

def _set_last_reply(sid, text):
    with _sess_lock:
        s = _sessions.setdefault(sid, {"history": [], "lang": "hi", "last_reply": ""})
        s["last_reply"] = text

def _get_last_reply(sid):
    with _sess_lock:
        return _sessions.get(sid, {}).get("last_reply", "")

def _clear(sid):
    with _sess_lock:
        _sessions.pop(sid, None)

def _purge():
    while True:
        time.sleep(900)
        with _sess_lock:
            keys = list(_sessions.keys())
            if len(keys) > MAX_CALLS:
                for k in keys[: len(keys) // 2]:
                    del _sessions[k]

threading.Thread(target=_purge, daemon=True).start()

def cleanup_audio():
    now = time.time()
    for f in AUDIO_DIR.glob("*.mp3"):
        try:
            if now - f.stat().st_mtime > 600:
                f.unlink()
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════
#  LANGUAGE SUPPORT TABLE
#  code → (human name for the AI prompt, edge-tts voice, Twilio Gather
#  speech-recognition locale). Add more rows to support more languages.
# ════════════════════════════════════════════════════════════════════
LANGUAGES = {
    "hi": ("Hindi",     "hi-IN-SwaraNeural",    "hi-IN"),
    "en": ("English",   "en-IN-NeerjaNeural",   "en-IN"),
    "ta": ("Tamil",     "ta-IN-PallaviNeural",  "ta-IN"),
    "te": ("Telugu",    "te-IN-ShrutiNeural",   "te-IN"),
    "kn": ("Kannada",   "kn-IN-SapnaNeural",    "kn-IN"),
    "ml": ("Malayalam", "ml-IN-SobhanaNeural",  "ml-IN"),
    "mr": ("Marathi",   "mr-IN-AarohiNeural",   "mr-IN"),
    "gu": ("Gujarati",  "gu-IN-DhwaniNeural",   "gu-IN"),
    "bn": ("Bengali",   "bn-IN-TanishaaNeural", "bn-IN"),
    "ur": ("Urdu",      "ur-PK-UzmaNeural",     "ur-PK"),
}
DEFAULT_LANG = "hi"


def detect_lang_confident(text, min_confidence=0.55):
    """
    Returns a language code from LANGUAGES if confidently detected,
    else None (caller code should keep the previous session language —
    this prevents the conversation randomly flipping language because
    of a short or ambiguous phrase).
    """
    if not text or len(text.strip()) < 2:
        return None
    try:
        guesses = detect_langs(text)
    except Exception:
        return None
    for g in guesses:
        if g.lang in LANGUAGES and g.prob >= min_confidence:
            return g.lang
    return None


# ════════════════════════════════════════════════════════════════════
#  "CONTINUE" DETECTION — works across languages by checking for the
#  word in every supported language plus common English/Hindi forms,
#  since callers often say "continue" even mid-other-language sentence.
# ════════════════════════════════════════════════════════════════════
_CONTINUE_TRIGGERS = {
    "continue", "resume", "proceed", "carry on", "go on",
    "jari", "jaari", "jari rakho", "jaari rakho", "aage bolo",
    "bolte raho", "phir se bolo",
}

def is_continue_request(text):
    t = text.lower().strip()
    if t in _CONTINUE_TRIGGERS:
        return True
    words = set(t.split())
    return bool(words & {"continue", "resume", "proceed", "jari", "jaari"})


# ════════════════════════════════════════════════════════════════════
#  INTEREST DETECTION → triggers WhatsApp alert
# ════════════════════════════════════════════════════════════════════
def is_interested(text):
    t = text.lower()
    return any(k in t for k in INTEREST_KEYWORDS)


# ════════════════════════════════════════════════════════════════════
#  WHATSAPP LEAD ALERT
# ════════════════════════════════════════════════════════════════════
def send_whatsapp_alert(caller_number, caller_said):
    msg = (
        f"New NSIM Lead!\n"
        f"Caller: {caller_number}\n"
        f"Said: {caller_said[:150]}\n"
        f"Time: {time.strftime('%d %b %Y %I:%M %p')}\n"
        f"Jaldi call back karein!"
    )
    print(f"[LEAD] {msg}")

    twilio_sid   = os.environ.get("TWILIO_SID", "")
    twilio_token = os.environ.get("TWILIO_TOKEN", "")

    def _send_twilio():
        try:
            payload = urllib.parse.urlencode({
                "From": "whatsapp:+14155238886",
                "To"  : f"whatsapp:+{OWNER_WHATSAPP}",
                "Body": msg,
            }).encode()
            url = f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Messages.json"
            req = urllib.request.Request(url, data=payload, method="POST")
            creds = base64.b64encode(f"{twilio_sid}:{twilio_token}".encode()).decode()
            req.add_header("Authorization", f"Basic {creds}")
            with urllib.request.urlopen(req, timeout=10) as r:
                print("[LEAD] WhatsApp sent via Twilio ✓")
        except Exception as e:
            print(f"[LEAD] Twilio WhatsApp error: {e}")

    if twilio_sid and twilio_token:
        threading.Thread(target=_send_twilio, daemon=True).start()
    else:
        print(f"[LEAD] ★ INTERESTED CALLER: {caller_number} — '{caller_said[:80]}'")
        print("[LEAD] Add TWILIO_SID + TWILIO_TOKEN in Render env for WhatsApp alerts")


# ════════════════════════════════════════════════════════════════════
#  POLLINATIONS AI — free, no key. One universal prompt template that
#  works for any supported language, restricted strictly to database.py
# ════════════════════════════════════════════════════════════════════
POLL_URL = "https://text.pollinations.ai/openai"

def _system_prompt(lang_code):
    lang_name = LANGUAGES.get(lang_code, LANGUAGES[DEFAULT_LANG])[0]
    return (
        f"You are NSIM's (National School of Internet Marketing) phone "
        f"voice assistant. The caller is speaking {lang_name}.\n\n"
        "Rules you must always follow:\n"
        f"1. Reply ONLY in natural, simple, spoken {lang_name} — the same "
        "language the caller just used. Never switch language on your own.\n"
        "2. Speak like a warm, helpful human, never like a robot.\n"
        "3. Never use any symbols — no asterisk, hash, slash, percent, "
        "bracket, dash. Just plain natural sentences.\n"
        "4. No lists or numbering. Keep it short — 2 to 3 sentences, like "
        "someone speaking on a phone call.\n"
        "5. Only answer questions related to NSIM. If asked something "
        "completely unrelated, politely steer the conversation back to NSIM.\n"
        "6. Never say or write any phone number yourself, even if you think "
        "you know it. If contact info is needed, just say the caller can "
        "call the office number — the system will insert the correct "
        "number automatically afterward.\n"
        "7. MOST IMPORTANT RULE: use ONLY the NSIM information given below. "
        "Never use your own general knowledge. Never guess a fee, duration, "
        "batch timing, or any fact. If the answer is not in the information "
        "below, say honestly that this detail is not available right now "
        "and that the office will confirm it.\n\n"
        f"NSIM information (use ONLY this data, nothing else):\n{NSIM_INFO}"
    )

def ask_ai(user_text, lang, history):
    system = _system_prompt(lang)
    messages = [{"role": "system", "content": system}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    payload = json.dumps({
        "model"      : "openai",        # GPT-4o via Pollinations, free
        "messages"   : messages,
        "max_tokens" : 130,
        "temperature": 0.4,
        "seed"       : 42,
        "private"    : True,
    }).encode()

    req = urllib.request.Request(
        POLL_URL, data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent"  : "NSIM-Voice-Agent/3.0",
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=9) as r:
            data  = json.loads(r.read())
            reply = data["choices"][0]["message"]["content"].strip()
            return _clean_reply(reply)
    except urllib.error.HTTPError as e:
        print(f"[AI] Pollinations error {e.code}")
        return _ask_ai_fallback(user_text, lang, history, system)
    except Exception as e:
        print(f"[AI] error: {e}")
        return _ask_ai_fallback(user_text, lang, history, system)


def _ask_ai_fallback(user_text, lang, history, system):
    """Fallback AI: DevToolbox free API — no key, no signup."""
    try:
        prompt = f"{system}\n\nUser: {user_text}\nAssistant:"
        payload = json.dumps({"prompt": prompt[:1000]}).encode()
        req = urllib.request.Request(
            "https://devtoolbox-api.devtoolbox-api.workers.dev/ai/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            data  = json.loads(r.read())
            reply = data.get("result", data.get("text", "")).strip()
            if reply:
                return _clean_reply(reply[:300])
    except Exception as e:
        print(f"[AI-fallback] error: {e}")

    return _err_msg(lang)


def _clean_reply(reply):
    """Strip symbols and correct any stray phone number the AI invents."""
    for sym in ["*", "#", "\\", "|", "`", "~", "^", "_", "[", "]", "(", ")", "{", "}"]:
        reply = reply.replace(sym, "")
    def _fix_number(m):
        digits = m.group(0)
        return CONTACT if digits != CONTACT_DIGITS else digits
    reply = re.sub(r"\d{10}", _fix_number, reply)
    return reply.strip()


def _err_msg(lang):
    msgs = {
        "hi": f"Abhi kuch takneeki dikkat hai. Kripya hamare office number par call karein, {CONTACT}.",
        "en": f"Technical issue right now. Please call our office at {CONTACT}.",
    }
    return msgs.get(lang, msgs["en"])


# ════════════════════════════════════════════════════════════════════
#  TEXT TO SPEECH — edge-tts (Microsoft Edge neural voices)
#  Genuinely free, no API key, natural-sounding across many languages.
#  Twilio <Say> with a basic voice is kept ONLY as an emergency
#  fallback if edge-tts fails (rare network hiccup), so a call never
#  goes completely silent.
# ════════════════════════════════════════════════════════════════════
def _edge_voice_for(lang):
    return LANGUAGES.get(lang, LANGUAGES[DEFAULT_LANG])[1]

def make_audio(text, lang):
    """Generate speech with edge-tts, return a public URL or None on failure."""
    try:
        voice = _edge_voice_for(lang)
        name  = f"{uuid.uuid4().hex}.mp3"
        path  = AUDIO_DIR / name

        async def _gen():
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(str(path))

        asyncio.run(_gen())

        if path.exists() and path.stat().st_size > 800:
            return f"{_host()}/static/audio/{name}"
        return None
    except Exception as e:
        print(f"[TTS-edge] error: {e}")
        return None


# ════════════════════════════════════════════════════════════════════
#  TWIML HELPERS
# ════════════════════════════════════════════════════════════════════
def _host():
    return os.environ.get("RENDER_EXTERNAL_URL", f"http://localhost:{PORT}")

def _xml(body):
    return Response(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n{body}\n</Response>',
        mimetype="text/xml"
    )

def _esc(text):
    return (text.replace("&", "and").replace("<", "").replace(">", "")
                .replace('"', "'"))

def _fallback_say(text, lang):
    """Emergency-only: Twilio's own basic voice if edge-tts is unreachable."""
    tl = LANGUAGES.get(lang, LANGUAGES[DEFAULT_LANG])[2]
    return f'<Say language="{tl}">{_esc(text)}</Say>'

def _media_tag(text, lang):
    """Returns a <Play> tag for edge-tts audio, or a <Say> fallback."""
    url = make_audio(text, lang)
    if url:
        return f"<Play>{url}</Play>"
    return _fallback_say(text, lang)

def _speak_with_bargein(text, lang, action):
    """
    Speaks the reply WHILE listening for speech at the same time. If the
    caller starts talking mid-reply, Twilio cuts the audio instantly and
    sends what they said to `action` — this is the interrupt behavior.
    """
    tl    = LANGUAGES.get(lang, LANGUAGES[DEFAULT_LANG])[2]
    media = _media_tag(text, lang)
    return (
        f'  <Gather input="speech" action="{_host()}{action}" method="POST" '
        f'speechTimeout="auto" timeout="7" language="{tl}">\n'
        f'    {media}\n'
        f'  </Gather>'
    )

def _speak_plain(text, lang):
    return f"  {_media_tag(text, lang)}"


# ════════════════════════════════════════════════════════════════════
#  ROUTES
# ════════════════════════════════════════════════════════════════════

@app.route("/call", methods=["GET", "POST"])
def incoming_call():
    global active_calls
    cleanup_audio()

    with _call_lock:
        active_calls += 1
        cur = active_calls
    print(f"[CALL] New — active: {cur}")

    if cur > MAX_CALLS:
        with _call_lock:
            active_calls -= 1
        msg = (f"Abhi bahut saare log baat kar rahe hain. "
               f"Thodi der baad call karein ya hamare office number par call karein, {CONTACT}.")
        return _xml(f"{_speak_plain(msg, 'hi')}\n  <Hangup/>")

    greet = (
        "Namaste! Aap NSIM ke AI assistant se baat kar rahe hain. "
        "Digital Marketing, Data Science aur doosre courses ke baare mein "
        "Hindi ya English mein poochhein, main madad karoonga."
    )
    return _xml(f"""
{_speak_with_bargein(greet, "hi", "/answer")}
  <Redirect method="POST">{_host()}/silent</Redirect>""")


@app.route("/answer", methods=["POST"])
def answer():
    sid        = request.form.get("CallSid", "x")
    user_text  = request.form.get("SpeechResult", "").strip()
    caller_num = request.form.get("From", "unknown")

    print(f"[{sid[:8]}] Said: {user_text!r}")

    if not user_text:
        return silent()

    sess = _get_session(sid)

    # ── Caller interrupted, then asked the assistant to continue ──────
    if is_continue_request(user_text):
        lang = sess["lang"]
        last = _get_last_reply(sid)
        if last:
            lead = {"hi": "theek hai, jaise main bol raha tha — "}.get(lang, "okay, continuing — ")
            combined = lead + last
        else:
            combined = {
                "hi": "Maaf kijiye, abhi continue karne ke liye kuch nahi hai. Aap apna sawaal phir se poochh sakte hain.",
            }.get(lang, "Sorry, there is nothing to continue right now. Please ask your question again.")
        return _xml(f"""
{_speak_with_bargein(combined, lang, "/answer")}
  <Redirect method="POST">{_host()}/bye</Redirect>""")

    detected = detect_lang_confident(user_text)
    lang = detected if detected else sess["lang"]
    _set_lang(sid, lang)

    history = list(sess["history"])
    reply   = ask_ai(user_text, lang, history)

    print(f"[{sid[:8]}] AI ({lang}): {reply!r}")

    _add_hist(sid, "user", user_text)
    _add_hist(sid, "assistant", reply)

    if is_interested(user_text):
        send_whatsapp_alert(caller_num, user_text)
        reply += {
            "hi": " Hamari team aapko jald hi contact karegi.",
        }.get(lang, " Our team will contact you very soon.")

    prompt = {"hi": "Aur koi sawaal hai?"}.get(lang, "Do you have any other questions?")
    combined = f"{reply} {prompt}"
    _set_last_reply(sid, reply)

    return _xml(f"""
{_speak_with_bargein(combined, lang, "/answer")}
  <Redirect method="POST">{_host()}/bye</Redirect>""")


@app.route("/call_ended", methods=["POST"])
def call_ended():
    global active_calls
    sid = request.form.get("CallSid", "x")
    _clear(sid)
    with _call_lock:
        active_calls = max(0, active_calls - 1)
    print(f"[CALL] Ended {sid[:8]} — active: {active_calls}")
    return "", 204


@app.route("/silent", methods=["GET", "POST"])
def silent():
    msg = (f"Aapki awaaz nahi aayi. Kripya sawaal poochhein ya "
           f"hamare office number par call karein, {CONTACT}. Shukriya.")
    return _xml(f"{_speak_plain(msg, 'hi')}\n  <Hangup/>")


@app.route("/bye", methods=["GET", "POST"])
def bye():
    msg = ("Shukriya NSIM ko call karne ke liye. "
           "Koi bhi sawaal ho to dobaara zaroor call karein. Namaste.")
    return _xml(f"{_speak_plain(msg, 'hi')}\n  <Hangup/>")


@app.route("/health")
def health():
    return {
        "status"          : "running ✓",
        "agent"           : "NSIM Voice Assistant v3",
        "ai"              : "Pollinations.ai (GPT-4o) — free, no key",
        "ai_fallback"     : "DevToolbox API — free, no key",
        "tts"             : "edge-tts (Microsoft neural voices) — free, no key",
        "stt"             : "Twilio built-in speech recognition (included in call cost)",
        "languages"       : list(LANGUAGES.keys()),
        "whatsapp_owner"  : f"+{OWNER_WHATSAPP}",
        "whatsapp_alerts" : "Twilio enabled" if os.environ.get("TWILIO_SID") else "logging only — add TWILIO_SID in Render env",
        "active_calls"    : active_calls,
        "max_calls"       : MAX_CALLS,
        "courses"         : len(COURSES),
        "render_url"      : os.environ.get("RENDER_EXTERNAL_URL", "local"),
    }, 200


@app.route("/")
def root():
    wa_ok = bool(os.environ.get("TWILIO_SID"))
    langs_html = "".join(f"<span style='display:inline-block;background:#eef2ff;color:#4338ca;"
                          f"padding:3px 10px;border-radius:99px;font-size:.78rem;margin:2px'>"
                          f"{name}</span>" for name, _, _ in LANGUAGES.values())
    return f"""<!DOCTYPE html>
<html><head><title>NSIM Voice Agent</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,sans-serif;background:#f0f4f8;min-height:100vh;padding:40px 16px}}
.wrap{{max-width:640px;margin:auto}}
h1{{font-size:1.7rem;color:#0f172a;margin-bottom:4px}}
.sub{{color:#64748b;margin-bottom:28px;font-size:.95rem}}
.card{{background:#fff;border-radius:14px;padding:22px;margin-bottom:16px;
       box-shadow:0 1px 4px #0000000d;border:1px solid #e2e8f0}}
.card h3{{font-size:.85rem;text-transform:uppercase;letter-spacing:.06em;
          color:#94a3b8;margin-bottom:14px}}
.row{{display:flex;justify-content:space-between;align-items:center;
      padding:8px 0;border-bottom:1px solid #f8fafc;font-size:.9rem}}
.row:last-child{{border:none}}
.ok{{color:#16a34a;font-weight:600}}
.warn{{color:#d97706;font-weight:600}}
code{{background:#f1f5f9;padding:2px 8px;border-radius:5px;font-size:11px;word-break:break-all}}
.badge{{display:inline-block;padding:6px 16px;border-radius:99px;font-size:.8rem;
        font-weight:700;background:#dcfce7;color:#15803d;margin-bottom:16px}}
</style></head><body><div class="wrap">
<h1>🎙️ NSIM Voice Assistant v3</h1>
<p class="sub">Free AI + free neural voices — only Twilio call minutes cost money</p>

<div class="card">
<div class="badge">● Live</div>
<h3>System Status</h3>
<div class="row"><span>AI Engine</span><span class="ok">Pollinations.ai (GPT-4o) ✓</span></div>
<div class="row"><span>AI Fallback</span><span class="ok">DevToolbox API ✓</span></div>
<div class="row"><span>Voice (TTS)</span><span class="ok">edge-tts neural ✓ (free)</span></div>
<div class="row"><span>Speech recognition</span><span class="ok">Twilio built-in ✓</span></div>
<div class="row"><span>WhatsApp alerts</span>
  <span class="{'ok' if wa_ok else 'warn'}">{'Twilio enabled ✓' if wa_ok else '⚠ Add TWILIO_SID in Render env'}</span>
</div>
<div class="row"><span>Owner WhatsApp</span><span>+{OWNER_WHATSAPP}</span></div>
<div class="row"><span>Active calls</span><strong>{active_calls} / {MAX_CALLS}</strong></div>
<div class="row"><span>Courses loaded</span><strong>{len(COURSES)}</strong></div>
</div>

<div class="card">
<h3>Supported Languages (auto-detected)</h3>
{langs_html}
</div>

<div class="card">
<h3>Twilio Webhook URLs</h3>
<div class="row"><span>Incoming call</span>
  <code>{os.environ.get("RENDER_EXTERNAL_URL","https://yourapp.onrender.com")}/call</code>
</div>
<div class="row"><span>Status callback</span>
  <code>{os.environ.get("RENDER_EXTERNAL_URL","https://yourapp.onrender.com")}/call_ended</code>
</div>
</div>

</div></body></html>"""


if __name__ == "__main__":
    print("=" * 58)
    print("  NSIM Voice Agent v3 — FULLY FREE AI + TTS")
    print("  AI      : Pollinations.ai (GPT-4o, no key)")
    print("  TTS     : edge-tts neural voices (free, no key)")
    print("  STT     : Twilio built-in (included in call cost)")
    print(f"  Owner   : +{OWNER_WHATSAPP}")
    print(f"  Courses : {len(COURSES)} loaded")
    print("=" * 58)
    app.run(host="0.0.0.0", port=PORT, debug=False)
