#!/usr/bin/env python3
"""
jamila_core.py — Voice-First AI Assistant for Linux
Designed for blind and visually impaired users.

Voice flow:
  User speaks → speech-to-text → command dispatch →
  AI response (if needed via server) → Coqui TTS speaks response → GUI shows response

The user NEVER needs to type. Everything is voice.
"""

import os, sys, json, time, sqlite3, datetime, subprocess
import threading, webbrowser, smtplib, re
from pathlib import Path
from email.message import EmailMessage

# ── Third-party imports ────────────────────────────────────────────────────────
try:
    import requests
except ImportError:
    sys.exit("ERROR: Run ./run.sh first — 'requests' is not installed.")

try:
    import speech_recognition as sr
    SR_OK = True
except ImportError:
    SR_OK = False
    print("[WARN] SpeechRecognition not available — Jamila will prompt you to type instead.")

try:
    import gi
    gi.require_version('Gtk', '3.0')
    gi.require_version('GLib', '2.0')
    from gi.repository import Gtk, GLib, GdkPixbuf, Gdk
    GTK_OK = True
except Exception:
    GTK_OK = False

# ── Paths ─────────────────────────────────────────────────────────────────────
HOME       = Path.home()
KEY_FILE   = HOME / '.jamila_key'
DB_FILE    = HOME / '.jamila_data.db'
CREDS_FILE = HOME / '.jamila_credentials.json'
ICON_FILE  = Path(__file__).parent / 'jamila.png'
SERVER_URL = 'https://jamila.onrender.com'

# ═══════════════════════════════════════════════════════════════════════════════
# TTS — Coqui TTS with natural neural voice
# Falls back: Coqui → pyttsx3 → espeak-ng
# ═══════════════════════════════════════════════════════════════════════════════

_speak_lock = threading.Lock()
_tts_engine = None

def _build_tts():
    # 1. Coqui TTS (best, natural)
    try:
        from TTS.api import TTS as CoquiTTS
        import sounddevice as sd
        import numpy as np
        model = CoquiTTS(model_name='tts_models/en/ljspeech/tacotron2-DDC', progress_bar=False, gpu=False)
        print("[Voice] Using Coqui TTS ✦ Natural neural voice")

        def _coqui_speak(text):
            wav = model.tts(text)
            sd.play(wav, samplerate=22050, blocking=True)
        return _coqui_speak
    except Exception as e:
        print(f"[Voice] Coqui TTS unavailable ({e}), trying pyttsx3…")

    # 2. pyttsx3
    try:
        import pyttsx3
        e = pyttsx3.init()
        e.setProperty('rate', 155)
        # Prefer a warmer female voice
        voices = e.getProperty('voices')
        for v in voices:
            if any(k in (v.name + v.id).lower() for k in ('female', 'zira', 'samantha', 'hazel')):
                e.setProperty('voice', v.id)
                break
        print("[Voice] Using pyttsx3")

        def _pyttsx3_speak(text):
            e.say(text); e.runAndWait()
        return _pyttsx3_speak
    except Exception as ex:
        print(f"[Voice] pyttsx3 unavailable ({ex}), using espeak-ng…")

    # 3. espeak-ng fallback
    print("[Voice] Using espeak-ng fallback")
    def _espeak_speak(text):
        subprocess.run(['espeak-ng', '-s', '138', '-v', 'en+f3', text], capture_output=True)
    return _espeak_speak

def speak(text: str, block: bool = True):
    """Speak text aloud. All output from Jamila goes through this."""
    global _tts_engine
    print(f"[Jamila] {text}")
    with _speak_lock:
        if _tts_engine is None:
            _tts_engine = _build_tts()
    if block:
        _tts_engine(text)
    else:
        threading.Thread(target=_tts_engine, args=(text,), daemon=True).start()

# ═══════════════════════════════════════════════════════════════════════════════
# Offline SQLite storage
# ═══════════════════════════════════════════════════════════════════════════════

def db():
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    c = db()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            remind_at TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS preferences (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT,
            content TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    ''')
    c.commit(); c.close()

def pref_set(key, val):
    c = db(); c.execute('INSERT OR REPLACE INTO preferences(key,value) VALUES(?,?)', (key, json.dumps(val))); c.commit(); c.close()

def pref_get(key, default=None):
    c = db(); row = c.execute('SELECT value FROM preferences WHERE key=?', (key,)).fetchone(); c.close()
    return json.loads(row['value']) if row else default

def add_reminder(text, dt: datetime.datetime):
    c = db(); c.execute('INSERT INTO reminders(text,remind_at) VALUES(?,?)', (text, dt.isoformat())); c.commit(); c.close()

def due_reminders():
    c = db()
    rows = c.execute("SELECT * FROM reminders WHERE done=0 AND remind_at <= datetime('now') ORDER BY remind_at").fetchall()
    c.close(); return rows

def done_reminder(rid):
    c = db(); c.execute('UPDATE reminders SET done=1 WHERE id=?', (rid,)); c.commit(); c.close()

def save_note(text):
    c = db(); c.execute('INSERT INTO notes(content) VALUES(?)', (text,)); c.commit(); c.close()

# ═══════════════════════════════════════════════════════════════════════════════
# Activation key
# ═══════════════════════════════════════════════════════════════════════════════

def load_key() -> str | None:
    return KEY_FILE.read_text().strip() if KEY_FILE.exists() else None

def save_key(key: str):
    KEY_FILE.write_text(key.strip()); KEY_FILE.chmod(0o600)

def verify_key(key: str):
    try:
        r = requests.post(f'{SERVER_URL}/verify-key', json={'key': key}, timeout=10)
        d = r.json()
        return d.get('valid', False), d
    except Exception as e:
        return False, {'message': str(e)}

# ═══════════════════════════════════════════════════════════════════════════════
# Server sync
# ═══════════════════════════════════════════════════════════════════════════════

def sync_to_server(key: str):
    """Push local reminders + preferences to Supabase via server."""
    try:
        c = db()
        reminders = [dict(r) for r in c.execute('SELECT text,remind_at,done FROM reminders').fetchall()]
        prefs_rows = c.execute('SELECT key,value FROM preferences').fetchall()
        c.close()
        preferences = {r['key']: json.loads(r['value']) for r in prefs_rows}
        requests.post(f'{SERVER_URL}/user-data/save',
                      json={'key': key, 'reminders': reminders, 'preferences': preferences},
                      timeout=8)
    except Exception:
        pass  # offline — local is source of truth

def load_from_server(key: str):
    """Pull user data from Supabase; updates local preferences with profile info."""
    try:
        r = requests.post(f'{SERVER_URL}/user-data/load', json={'key': key}, timeout=8)
        d = r.json()
        if 'name' in d and d['name']:
            pref_set('user_name', d['name'])
        if 'preferences' in d:
            for k, v in d['preferences'].items():
                pref_set(k, v)
        return d
    except Exception:
        return {}

# ═══════════════════════════════════════════════════════════════════════════════
# AI call via server
# ═══════════════════════════════════════════════════════════════════════════════

def ask_ai(prompt: str, key: str, history: list) -> tuple[str, list]:
    """
    Send prompt to server. Server validates key, deducts call, calls AI provider.
    Returns (reply_text, updated_history).
    """
    name   = pref_get('user_name', 'friend')
    likes  = pref_get('user_likes', [])
    dislikes = pref_get('user_dislikes', [])

    system = (
        f"You are Jamila, a warm and caring voice-first AI assistant for {name}. "
        "Your responses will be spoken aloud via text-to-speech, so write naturally as if speaking — "
        "no markdown, no bullet points, no asterisks. Keep responses concise and conversational. "
        "Be warm, encouraging, and patient. "
    )
    if likes:    system += f"{name} likes {', '.join(likes)}. "
    if dislikes: system += f"{name} dislikes {', '.join(dislikes)}. "

    messages = list(history) + [{'role': 'user', 'content': prompt}]

    try:
        r = requests.post(f'{SERVER_URL}/ai',
                          json={'key': key, 'messages': messages, 'provider': 'gemini', 'system': system},
                          timeout=30)
        data = r.json()
        if 'error' in data:
            return data['error'], history
        reply = data.get('reply', 'I did not get a response, sorry.')
        # Update history (keep last 10 exchanges = 20 messages)
        history = messages + [{'role': 'assistant', 'content': reply}]
        if len(history) > 20:
            history = history[-20:]
        return reply, history
    except requests.exceptions.ConnectionError:
        return "I cannot reach the Jamila server right now. Please check your internet connection.", history
    except Exception as e:
        return f"Something went wrong: {str(e)}", history

# ═══════════════════════════════════════════════════════════════════════════════
# Time expression parser
# ═══════════════════════════════════════════════════════════════════════════════

def parse_time(expr: str) -> datetime.datetime:
    """Convert natural time expressions to datetime."""
    now = datetime.datetime.now()
    expr = expr.lower().strip()

    def with_time(base: datetime.datetime, time_expr: str) -> datetime.datetime:
        m = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', time_expr)
        if m:
            h = int(m.group(1)); mn = int(m.group(2) or 0)
            ampm = m.group(3)
            if ampm == 'pm' and h != 12: h += 12
            if ampm == 'am' and h == 12: h = 0
            return base.replace(hour=h, minute=mn, second=0, microsecond=0)
        return base.replace(hour=9, minute=0, second=0, microsecond=0)

    if 'tomorrow' in expr:
        base = now + datetime.timedelta(days=1)
        return with_time(base, expr)

    if 'tonight' in expr or 'this evening' in expr:
        return now.replace(hour=20, minute=0, second=0)

    if 'next week' in expr:
        base = now + datetime.timedelta(weeks=1)
        return with_time(base, expr)

    m = re.search(r'in (\d+)\s*(minute|hour|day|week)', expr)
    if m:
        n = int(m.group(1)); unit = m.group(2)
        deltas = {'minute': 'minutes', 'hour': 'hours', 'day': 'days', 'week': 'weeks'}
        return now + datetime.timedelta(**{deltas[unit]: n})

    m = re.search(r'at (\d{1,2})(?::(\d{2}))?\s*(am|pm)?', expr)
    if m:
        h = int(m.group(1)); mn = int(m.group(2) or 0); ampm = m.group(3)
        if ampm == 'pm' and h != 12: h += 12
        if ampm == 'am' and h == 12: h = 0
        dt = now.replace(hour=h, minute=mn, second=0, microsecond=0)
        if dt < now: dt += datetime.timedelta(days=1)
        return dt

    return now + datetime.timedelta(hours=1)  # default: in 1 hour

# ═══════════════════════════════════════════════════════════════════════════════
# Command handlers
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_open(arg: str) -> str:
    APP_MAP = {
        'firefox': ['firefox'], 'browser': ['xdg-open', 'https://google.com'],
        'chrome': ['google-chrome'], 'terminal': ['gnome-terminal'],
        'files': ['nautilus', str(HOME)], 'file manager': ['nautilus'],
        'settings': ['gnome-control-center'], 'calculator': ['gnome-calculator'],
        'text editor': ['gedit'], 'music player': ['rhythmbox'],
        'email': ['thunderbird'], 'photos': ['eog'],
    }
    arg = arg.strip()
    if os.path.exists(arg):
        subprocess.Popen(['xdg-open', arg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Opening {arg}"
    for name, cmd in APP_MAP.items():
        if name in arg.lower():
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return f"Opening {name} for you"
            except Exception as e:
                return f"I could not open {name}. Error: {e}"
    try:
        subprocess.Popen([arg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Launching {arg}"
    except Exception:
        return f"I could not find or open {arg}. Please check the name."

def cmd_close(arg: str) -> str:
    try:
        subprocess.run(['pkill', '-f', arg.strip()], capture_output=True)
        return f"Closed {arg}"
    except Exception as e:
        return f"I had trouble closing {arg}: {e}"

def cmd_create_file(arg: str) -> str:
    parts = arg.strip().split(' ', 1)
    name = parts[0]; content = parts[1] if len(parts) > 1 else ''
    try:
        path = Path(name) if name.startswith('/') else HOME / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return f"Created file {path.name}"
    except Exception as e:
        return f"I could not create that file: {e}"

def cmd_delete(arg: str) -> str:
    arg = arg.strip()
    try:
        path = Path(arg) if arg.startswith('/') else HOME / arg
        if not path.exists():
            return f"I could not find a file called {arg}"
        if path.is_dir():
            import shutil; shutil.rmtree(path)
        else:
            path.unlink()
        return f"Deleted {path.name}"
    except Exception as e:
        return f"I could not delete {arg}: {e}"

def cmd_list_files(arg: str) -> str:
    arg = arg.strip() or str(HOME)
    try:
        items = sorted(Path(arg).iterdir(), key=lambda p: (p.is_file(), p.name))[:15]
        names = [f.name + ('/' if f.is_dir() else '') for f in items]
        return f"In {arg} I can see: {', '.join(names)}"
    except Exception as e:
        return f"I could not list files there: {e}"

def cmd_volume(arg: str) -> str:
    digits = ''.join(filter(str.isdigit, arg))
    pct = max(0, min(100, int(digits))) if digits else 70
    try:
        subprocess.run(['pactl', 'set-sink-volume', '@DEFAULT_SINK@', f'{pct}%'], capture_output=True)
        return f"Volume set to {pct} percent"
    except Exception as e:
        return f"I could not change the volume: {e}"

def cmd_email(arg: str, creds: dict) -> str:
    smtp = creds.get('smtp', {})
    if not smtp.get('host'):
        return "Email is not set up yet. Please edit the credentials file at home dot jamila underscore credentials dot json"
    # Parse: "to@example.com about=Subject body=Hello"
    parts = arg.strip().split(' ', 1)
    to = parts[0]; rest = parts[1] if len(parts) > 1 else ''
    subject_m = re.search(r'(?:about|subject)=([^\s].*?)(?=\s+body=|$)', rest)
    body_m    = re.search(r'body=(.+)', rest)
    subject   = subject_m.group(1).strip() if subject_m else 'Message from Jamila'
    body      = body_m.group(1).strip() if body_m else rest or 'Sent via Jamila'

    try:
        msg = EmailMessage()
        msg['From'] = smtp['username']; msg['To'] = to
        msg['Subject'] = subject; msg.set_content(body)
        with smtplib.SMTP(smtp['host'], smtp.get('port', 587), timeout=20) as s:
            s.starttls(); s.login(smtp['username'], smtp['password']); s.send_message(msg)
        return f"Email sent to {to}"
    except Exception as e:
        return f"I could not send the email: {e}"

def cmd_remind(arg: str, key: str) -> str:
    # "me to pay bills tomorrow at 9am" or "take medicine in 2 hours"
    m = re.match(r'(?:me\s+to\s+|to\s+)?(.+?)\s+(tomorrow|tonight|in \d+|next week|at \d)', arg, re.IGNORECASE)
    if m:
        task = m.group(1).strip()
        time_expr = arg[m.start(2):]
    else:
        # Fallback: whole string is the task, default 1 hour
        task = arg.strip(); time_expr = 'in 1 hour'

    dt = parse_time(time_expr)
    add_reminder(task, dt)
    threading.Thread(target=sync_to_server, args=(key,), daemon=True).start()
    when = dt.strftime('%A, %B %d at %-I:%M %p')
    return f"Done! I will remind you to {task} on {when}"

def cmd_note(arg: str) -> str:
    save_note(arg.strip())
    return f"Note saved: {arg[:60]}"

def cmd_play(arg: str) -> str:
    if os.path.exists(arg):
        try:
            subprocess.Popen(['mpv', '--no-video', arg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Playing {arg}"
        except Exception:
            pass
    webbrowser.open(f'https://www.youtube.com/results?search_query={arg.replace(" ", "+")}')
    return f"Searching YouTube for {arg}"

def cmd_search(arg: str) -> str:
    webbrowser.open(f'https://duckduckgo.com/?q={arg.replace(" ", "+")}')
    return f"Opened a search for {arg}"

# ═══════════════════════════════════════════════════════════════════════════════
# Central command dispatcher
# ═══════════════════════════════════════════════════════════════════════════════

def dispatch(cmd: str, key: str, creds: dict, history: list) -> tuple[str, list]:
    """Route a voice command to the right handler. Returns (response_text, updated_history)."""
    c = cmd.strip()
    lo = c.lower()

    if not lo:
        return None, history

    # ── Exit ─────────────────────────────────────────────────────────────────
    if lo in ('exit', 'quit', 'stop', 'goodbye', 'bye', 'close jamila', 'shut down'):
        return '__exit__', history

    # ── App / file control ────────────────────────────────────────────────────
    if lo.startswith('open '):              return cmd_open(c[5:]),          history
    if lo.startswith('close ') and 'close jamila' not in lo:
                                            return cmd_close(c[6:]),         history
    if lo.startswith('create file '):       return cmd_create_file(c[12:]),  history
    if lo.startswith('delete '):            return cmd_delete(c[7:]),        history
    if lo.startswith('list files') or lo.startswith('show files') or lo.startswith('what files'):
        folder = c.split(' ', 2)[2] if len(c.split()) > 2 else ''
        return cmd_list_files(folder), history

    # ── Media & web ───────────────────────────────────────────────────────────
    if lo.startswith('play '):              return cmd_play(c[5:]),          history
    if lo.startswith('search ') or lo.startswith('look up '):
        arg = c.split(' ', 1)[1]
        return cmd_search(arg),                                              history
    if lo.startswith('volume ') or lo.startswith('set volume'):
        return cmd_volume(lo),                                               history

    # ── Email ─────────────────────────────────────────────────────────────────
    if lo.startswith('email ') or lo.startswith('send email ') or lo.startswith('send an email '):
        m = re.match(r'^(?:send (?:an )?email |email )(.*)', c, re.IGNORECASE)
        return cmd_email(m.group(1) if m else c[6:], creds),                history

    # ── Reminders & notes ─────────────────────────────────────────────────────
    if lo.startswith('remind ') or lo.startswith('reminder ') or lo.startswith('remember '):
        arg = re.sub(r'^(remind|reminder|remember)\s+', '', c, flags=re.IGNORECASE)
        return cmd_remind(arg, key),                                         history
    if lo.startswith('note ') or lo.startswith('make a note ') or lo.startswith('take a note '):
        arg = re.sub(r'^(note|make a note|take a note)\s+', '', c, flags=re.IGNORECASE)
        return cmd_note(arg),                                                history

    # ── Time / date ───────────────────────────────────────────────────────────
    if any(x in lo for x in ('what time', 'what is the time', "what's the time")):
        return f"It is {datetime.datetime.now().strftime('%-I:%M %p')}", history
    if any(x in lo for x in ('what day', 'what date', "what's today", 'today is')):
        return f"Today is {datetime.datetime.now().strftime('%A, %B %d, %Y')}", history

    # ── Learn about user ─────────────────────────────────────────────────────
    m = re.match(r'my name is (.+)', lo)
    if m:
        pref_set('user_name', m.group(1).strip().title())
        return f"Nice to meet you! I will remember your name.", history
    m = re.match(r'i (?:like|love|enjoy) (.+)', lo)
    if m:
        likes = pref_get('user_likes', [])
        likes.append(m.group(1).strip())
        pref_set('user_likes', likes)
        return f"Got it! I will remember that you love {m.group(1)}.", history

    # ── Catch-all: ask AI ─────────────────────────────────────────────────────
    return ask_ai(c, key, history)

# ═══════════════════════════════════════════════════════════════════════════════
# Speech input
# ═══════════════════════════════════════════════════════════════════════════════

def listen() -> str | None:
    if not SR_OK:
        return None
    try:
        rec = sr.Recognizer()
        rec.energy_threshold = 300
        rec.pause_threshold = 0.8
        with sr.Microphone() as src:
            rec.adjust_for_ambient_noise(src, duration=0.4)
            audio = rec.listen(src, timeout=10, phrase_time_limit=15)
        return rec.recognize_google(audio)
    except sr.WaitTimeoutError:
        return None
    except sr.UnknownValueError:
        return None
    except Exception as e:
        print(f"[Listen] {e}")
        return None

# ═══════════════════════════════════════════════════════════════════════════════
# Reminder background thread
# ═══════════════════════════════════════════════════════════════════════════════

def reminder_loop(stop: threading.Event, gui_callback=None):
    while not stop.is_set():
        for r in due_reminders():
            msg = f"Reminder: {r['text']}"
            speak(msg, block=False)
            if gui_callback:
                GLib.idle_add(gui_callback, msg)
            done_reminder(r['id'])
        stop.wait(30)

# ═══════════════════════════════════════════════════════════════════════════════
# GTK Warm GUI
# ═══════════════════════════════════════════════════════════════════════════════

WARM_CSS = b"""
/* ── Window ── */
window, .main-box {
  background-color: #1a1208;
}

/* ── Header ── */
.header {
  background: linear-gradient(180deg, #2c2010 0%, #221a0e 100%);
  border-bottom: 1px solid #4a3520;
  padding: 0px;
}

/* ── Title ── */
.jamila-title {
  font-family: serif;
  font-size: 22px;
  font-weight: bold;
  color: #f59e2a;
}
.jamila-subtitle {
  font-size: 11px;
  color: #a08060;
}

/* ── Status dot ── */
.status-dot {
  font-size: 10px;
  color: #6ec98f;
}

/* ── Response box ── */
.response-scroll {
  background-color: #221a0e;
  border: 1px solid #4a3520;
  border-radius: 14px;
}
.response-text {
  background-color: #221a0e;
  color: #ede0c8;
  font-family: serif;
  font-size: 16px;
  padding: 8px;
}
.response-text text {
  background-color: #221a0e;
  color: #ede0c8;
}

/* ── Mic button ── */
.mic-idle {
  background: linear-gradient(135deg, #f59e2a 0%, #e07b5a 100%);
  color: #1a1208;
  border-radius: 50px;
  font-size: 17px;
  font-weight: bold;
  border: none;
  padding: 0px;
  box-shadow: 0 4px 24px rgba(245,158,42,0.3);
}
.mic-listening {
  background: linear-gradient(135deg, #6ec98f 0%, #2a9a6a 100%);
  color: #1a1208;
  border-radius: 50px;
  font-size: 17px;
  font-weight: bold;
  border: none;
  padding: 0px;
  box-shadow: 0 4px 28px rgba(110,201,143,0.4);
}
.mic-thinking {
  background: linear-gradient(135deg, #a08060 0%, #7a6040 100%);
  color: #1a1208;
  border-radius: 50px;
  font-size: 17px;
  font-weight: bold;
  border: none;
  padding: 0px;
}

/* ── Status bar ── */
.status-bar {
  font-size: 12px;
  color: #a08060;
}
.status-bar-active {
  font-size: 12px;
  color: #f59e2a;
}

/* ── Input area (hidden — fallback only) ── */
.input-entry {
  background-color: #2c2010;
  color: #ede0c8;
  border: 1px solid #4a3520;
  border-radius: 10px;
  font-size: 14px;
  padding: 4px;
}

/* ── Tag label ── */
.plan-tag {
  font-size: 10px;
  color: #a08060;
  background: rgba(160,128,96,0.1);
  border-radius: 5px;
  padding: 2px 6px;
}
"""

class JamilaApp(Gtk.Window):
    def __init__(self, key: str, creds: dict):
        super().__init__(title='Jamila')
        self.key      = key
        self.creds    = creds
        self.history  = []
        self.busy     = False

        self.set_default_size(500, 580)
        self.set_resizable(True)
        self.set_border_width(0)

        # Apply CSS
        prov = Gtk.CssProvider()
        prov.load_from_data(WARM_CSS)
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # ── Root layout ───────────────────────────────────────────────────────
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.get_style_context().add_class('main-box')
        self.add(root)

        # ── Header ───────────────────────────────────────────────────────────
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header.get_style_context().add_class('header')
        header.set_margin_start(18); header.set_margin_end(18)
        header.set_margin_top(14);   header.set_margin_bottom(14)

        # Icon
        if ICON_FILE.exists():
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(ICON_FILE), 42, 42, True)
                icon_img = Gtk.Image.new_from_pixbuf(pb)
                icon_img.set_margin_end(12)
                header.pack_start(icon_img, False, False, 0)
            except Exception:
                pass

        # Title block
        title_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        lbl_title = Gtk.Label(label='Jamila')
        lbl_title.get_style_context().add_class('jamila-title')
        lbl_title.set_halign(Gtk.Align.START)
        lbl_sub = Gtk.Label(label='Your voice, your computer')
        lbl_sub.get_style_context().add_class('jamila-subtitle')
        lbl_sub.set_halign(Gtk.Align.START)
        title_col.pack_start(lbl_title, False, False, 0)
        title_col.pack_start(lbl_sub, False, False, 0)
        header.pack_start(title_col, True, True, 0)

        # Key tag
        tag = Gtk.Label(label=f'{key[:11]}…')
        tag.get_style_context().add_class('plan-tag')
        header.pack_end(tag, False, False, 0)

        root.pack_start(header, False, False, 0)

        # ── Response area ─────────────────────────────────────────────────────
        scroll = Gtk.ScrolledWindow()
        scroll.get_style_context().add_class('response-scroll')
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_margin_start(16); scroll.set_margin_end(16)
        scroll.set_margin_top(14)
        scroll.set_min_content_height(280)

        self.response_buf = Gtk.TextBuffer()
        self.response_buf.set_text("Hello! Press the big button below or the Space key, and speak to me. I'm listening.")
        self.resp_view = Gtk.TextView(buffer=self.response_buf)
        self.resp_view.get_style_context().add_class('response-text')
        self.resp_view.set_editable(False)
        self.resp_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.resp_view.set_cursor_visible(False)
        scroll.add(self.resp_view)
        root.pack_start(scroll, True, True, 0)

        # ── Status label ─────────────────────────────────────────────────────
        self.status_lbl = Gtk.Label(label='Press the mic button or Space to speak')
        self.status_lbl.get_style_context().add_class('status-bar')
        self.status_lbl.set_margin_top(10)
        root.pack_start(self.status_lbl, False, False, 0)

        # ── Mic button ────────────────────────────────────────────────────────
        self.mic_btn = Gtk.Button()
        self.mic_btn.get_style_context().add_class('mic-idle')
        self.mic_btn.set_margin_start(40); self.mic_btn.set_margin_end(40)
        self.mic_btn.set_margin_top(14);  self.mic_btn.set_margin_bottom(10)
        self.mic_btn.set_size_request(-1, 58)

        mic_inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mic_inner.set_halign(Gtk.Align.CENTER)
        self.mic_icon_lbl = Gtk.Label(label='🎙')
        self.mic_icon_lbl.set_name('mic-icon')
        self.mic_text_lbl = Gtk.Label(label='Speak to Jamila')
        mic_inner.pack_start(self.mic_icon_lbl, False, False, 0)
        mic_inner.pack_start(self.mic_text_lbl, False, False, 0)
        self.mic_btn.add(mic_inner)
        self.mic_btn.connect('clicked', self.on_mic)
        root.pack_start(self.mic_btn, False, False, 0)

        # ── Text fallback (shown when mic unavailable) ────────────────────────
        self.input_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.input_row.set_margin_start(16); self.input_row.set_margin_end(16)
        self.input_row.set_margin_bottom(14)
        self.input_row.set_visible(not SR_OK)

        self.entry = Gtk.Entry()
        self.entry.get_style_context().add_class('input-entry')
        self.entry.set_placeholder_text('Type a command (mic not available)…')
        self.entry.connect('activate', self.on_type_submit)
        send = Gtk.Button(label='→')
        send.connect('clicked', self.on_type_submit)
        self.input_row.pack_start(self.entry, True, True, 0)
        self.input_row.pack_start(send, False, False, 0)
        root.pack_start(self.input_row, False, False, 0)

        # ── Keyboard shortcuts ────────────────────────────────────────────────
        self.connect('key-press-event', self.on_key)
        self.connect('destroy', self.on_quit)
        self.show_all()
        if SR_OK:
            self.input_row.hide()

        # Welcome
        GLib.timeout_add(600, self._welcome)

    # ── UI helpers ────────────────────────────────────────────────────────────
    def set_response(self, text: str):
        self.response_buf.set_text(text)
        GLib.idle_add(self._scroll_bottom)

    def _scroll_bottom(self):
        adj = self.resp_view.get_vadjustment()
        adj.set_value(adj.get_upper())
        return False

    def append_response(self, text: str):
        end = self.response_buf.get_end_iter()
        self.response_buf.insert(end, '\n\n' + text)
        GLib.idle_add(self._scroll_bottom)

    def set_status(self, text: str, active=False):
        self.status_lbl.set_text(text)
        ctx = self.status_lbl.get_style_context()
        if active:
            ctx.add_class('status-bar-active'); ctx.remove_class('status-bar')
        else:
            ctx.add_class('status-bar'); ctx.remove_class('status-bar-active')

    def set_mic_state(self, state: str):
        """state: 'idle' | 'listening' | 'thinking'"""
        ctx = self.mic_btn.get_style_context()
        for cls in ('mic-idle', 'mic-listening', 'mic-thinking'):
            ctx.remove_class(cls)
        ctx.add_class(f'mic-{state}')
        labels = {
            'idle':      ('🎙', 'Speak to Jamila'),
            'listening': ('🔴', 'Listening…'),
            'thinking':  ('⏳', 'Thinking…'),
        }
        icon, text = labels[state]
        self.mic_icon_lbl.set_text(icon)
        self.mic_text_lbl.set_text(text)
        self.mic_btn.set_sensitive(state == 'idle')

    # ── Events ────────────────────────────────────────────────────────────────
    def on_key(self, widget, event):
        if event.keyval == Gdk.KEY_space and not self.entry.is_focus():
            if not self.busy:
                self.on_mic(None)
            return True

    def on_mic(self, _):
        if self.busy or not SR_OK:
            if not SR_OK:
                self.set_response("Microphone is not available. Please type in the box below.")
                speak("The microphone is not available. Please type your command.", block=False)
            return
        self.busy = True
        self.set_mic_state('listening')
        self.set_status('Listening… speak now', active=True)
        threading.Thread(target=self._do_listen, daemon=True).start()

    def _do_listen(self):
        text = listen()
        GLib.idle_add(self._after_listen, text)

    def _after_listen(self, text):
        if not text:
            self.busy = False
            self.set_mic_state('idle')
            self.set_status("Didn't catch that — please try again")
            return
        self.set_status(f'You said: "{text}"', active=True)
        self._run_command(text)

    def on_type_submit(self, _):
        text = self.entry.get_text().strip()
        if not text or self.busy: return
        self.entry.set_text('')
        self.set_status(f'You typed: "{text}"', active=True)
        self.busy = True
        self.set_mic_state('thinking')
        threading.Thread(target=self._execute, args=(text,), daemon=True).start()

    def _run_command(self, cmd: str):
        self.set_mic_state('thinking')
        self.set_status('Processing…', active=True)
        threading.Thread(target=self._execute, args=(cmd,), daemon=True).start()

    def _execute(self, cmd: str):
        reply, self.history = dispatch(cmd, self.key, self.creds, self.history)
        GLib.idle_add(self._show_reply, reply)

    def _show_reply(self, reply):
        self.busy = False
        self.set_mic_state('idle')
        if reply == '__exit__':
            Gtk.main_quit(); return
        if reply:
            self.set_response(reply)
            speak(reply, block=False)
            self.set_status('Press the mic button or Space to speak again')

    def _welcome(self):
        name = pref_get('user_name', '')
        greeting = f"Hello{', ' + name if name else ''}! I am Jamila, your voice assistant. Press the big button or the Space key and speak to me. I am ready."
        self.set_response(greeting)
        speak(greeting, block=False)
        return False

    def on_quit(self, _):
        key = self.key
        threading.Thread(target=sync_to_server, args=(key,), daemon=True).start()
        Gtk.main_quit()

# ═══════════════════════════════════════════════════════════════════════════════
# Terminal fallback mode
# ═══════════════════════════════════════════════════════════════════════════════

def terminal_mode(key: str, creds: dict):
    speak("Jamila is ready. Press Enter to speak, or type a command.")
    history = []
    while True:
        try:
            raw = input('\n[Jamila] Press Enter to speak or type: ').strip()
            if raw == '' and SR_OK:
                speak("Listening…", block=False)
                raw = listen()
                if not raw:
                    print("Didn't catch that.")
                    continue
                print(f'You said: {raw}')

            reply, history = dispatch(raw, key, creds, history)
            if reply == '__exit__':
                speak("Goodbye!"); break
            if reply:
                speak(reply)
        except (KeyboardInterrupt, EOFError):
            speak("Goodbye!"); break

# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    init_db()

    key = load_key()
    if not key:
        speak("No activation key found. Please run the installer first.")
        print("ERROR: No key at ~/.jamila_key — run ./run.sh")
        sys.exit(1)

    creds = {}
    if CREDS_FILE.exists():
        try: creds = json.loads(CREDS_FILE.read_text())
        except Exception: pass

    # Pull user data from server in background
    threading.Thread(target=load_from_server, args=(key,), daemon=True).start()

    # Reminder background thread
    stop = threading.Event()
    reminder_cb = None

    if GTK_OK:
        app = JamilaApp(key, creds)
        reminder_cb = lambda msg: app.set_response(msg) or speak(msg, block=False)
        threading.Thread(target=reminder_loop, args=(stop, reminder_cb), daemon=True).start()
        try:
            Gtk.main()
        finally:
            stop.set()
            sync_to_server(key)
    else:
        threading.Thread(target=reminder_loop, args=(stop,), daemon=True).start()
        try:
            terminal_mode(key, creds)
        finally:
            stop.set()
            sync_to_server(key)

if __name__ == '__main__':
    main()
