#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jamila_core.py  —  Voice-first AI assistant for Linux
Designed for blind and visually impaired users.

Spacebar = hold to record, release = 2-second buffer then execute.
All responses are spoken aloud via Coqui TTS / pyttsx3 / espeak-ng.
"""

# ── stdlib only at top level (always available) ───────────────────────────────
import os
import sys
import json
import time
import re
import sqlite3
import datetime
import subprocess
import threading
import webbrowser
import smtplib
import shutil
import logging
from pathlib import Path
from email.message import EmailMessage

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s"
)
log = logging.getLogger("jamila")

# ═══════════════════════════════════════════════════════════════════════════════
# Paths & constants
# ═══════════════════════════════════════════════════════════════════════════════

HOME        = Path.home()
JAMILA_DIR  = HOME / ".jamila"
KEY_FILE    = HOME / ".jamila_key"
DB_FILE     = HOME / ".jamila_data.db"
CREDS_FILE  = HOME / ".jamila_credentials.json"
ICON_FILE   = Path(__file__).parent / "jamila.png"
SERVER_URL  = "https://jamila.onrender.com"

JAMILA_DIR.mkdir(exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════════════
# Optional imports — each guarded individually
# ═══════════════════════════════════════════════════════════════════════════════

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    log.warning("'requests' not installed — AI and server features disabled.")

try:
    import speech_recognition as sr
    HAS_SR = True
except ImportError:
    sr = None
    HAS_SR = False
    log.warning("SpeechRecognition not installed — mic disabled.")

try:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("GLib", "2.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gtk, GLib, Gdk, GdkPixbuf
    HAS_GTK = True
except Exception as _gtk_err:
    Gtk = GLib = Gdk = GdkPixbuf = None
    HAS_GTK = False
    log.warning("GTK not available (%s) — terminal mode only.", _gtk_err)

# ═══════════════════════════════════════════════════════════════════════════════
# TTS — Coqui → pyttsx3 → espeak-ng
# ═══════════════════════════════════════════════════════════════════════════════

_tts_fn = None          # callable(text: str) -> None
_tts_lock = threading.Lock()


def _build_tts():
    """Build the best available TTS engine. Called once, lazily."""
    # 1. Coqui TTS
    try:
        from TTS.api import TTS as CoquiTTS   # type: ignore
        import sounddevice as sd               # type: ignore
        model = CoquiTTS(
            model_name="tts_models/en/ljspeech/tacotron2-DDC",
            progress_bar=False,
            gpu=False,
        )
        log.info("TTS: Coqui TTS (natural neural voice)")

        def _coqui(text):
            wav = model.tts(text)
            sd.play(wav, samplerate=22050, blocking=True)

        return _coqui
    except Exception as e:
        log.info("Coqui TTS unavailable (%s), trying pyttsx3…", e)

    # 2. pyttsx3
    try:
        import pyttsx3  # type: ignore
        eng = pyttsx3.init()
        eng.setProperty("rate", 152)
        voices = eng.getProperty("voices") or []
        for v in voices:
            name_lower = (v.name or "").lower()
            id_lower   = (v.id or "").lower()
            if any(k in name_lower + id_lower for k in ("female", "zira", "samantha", "hazel", "en")):
                eng.setProperty("voice", v.id)
                break
        log.info("TTS: pyttsx3")

        def _pyttsx3(text):
            eng.say(text)
            eng.runAndWait()

        return _pyttsx3
    except Exception as e:
        log.info("pyttsx3 unavailable (%s), using espeak-ng fallback.", e)

    # 3. espeak-ng (always available on supported distros)
    log.info("TTS: espeak-ng fallback")

    def _espeak(text):
        subprocess.run(
            ["espeak-ng", "-s", "138", "-v", "en+f3", text],
            capture_output=True,
        )

    return _espeak


def speak(text, block=True):
    """Speak text aloud. Thread-safe. block=False for non-blocking."""
    global _tts_fn
    log.info("[Jamila] %s", text)
    with _tts_lock:
        if _tts_fn is None:
            _tts_fn = _build_tts()
    if block:
        _tts_fn(text)
    else:
        t = threading.Thread(target=_tts_fn, args=(text,), daemon=True)
        t.start()


# ═══════════════════════════════════════════════════════════════════════════════
# SQLite — offline persistent storage
# ═══════════════════════════════════════════════════════════════════════════════

def _get_db():
    conn = sqlite3.connect(str(DB_FILE), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS reminders (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            text       TEXT    NOT NULL,
            remind_at  TEXT    NOT NULL,
            done       INTEGER DEFAULT 0,
            created_at TEXT    DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS notes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            content    TEXT,
            created_at TEXT    DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS preferences (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS chat_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            role       TEXT,
            content    TEXT,
            created_at TEXT    DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT,
            payload   TEXT,
            run_at    TEXT,
            done      INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()


# ── Preferences ──────────────────────────────────────────────────────────────

def pref_set(key, value):
    conn = _get_db()
    conn.execute(
        "INSERT OR REPLACE INTO preferences (key, value) VALUES (?, ?)",
        (key, json.dumps(value)),
    )
    conn.commit()
    conn.close()


def pref_get(key, default=None):
    conn = _get_db()
    row = conn.execute(
        "SELECT value FROM preferences WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    if row:
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return default
    return default


# ── Reminders ────────────────────────────────────────────────────────────────

def reminder_add(text, dt):
    conn = _get_db()
    conn.execute(
        "INSERT INTO reminders (text, remind_at) VALUES (?, ?)",
        (text, dt.isoformat()),
    )
    conn.commit()
    conn.close()


def reminder_due():
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM reminders WHERE done = 0 AND remind_at <= datetime('now')"
        " ORDER BY remind_at"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def reminder_done(rid):
    conn = _get_db()
    conn.execute("UPDATE reminders SET done = 1 WHERE id = ?", (rid,))
    conn.commit()
    conn.close()


# ── Chat history (persistent conversation memory) ─────────────────────────────

def history_append(role, content):
    conn = _get_db()
    conn.execute(
        "INSERT INTO chat_history (role, content) VALUES (?, ?)",
        (role, content),
    )
    conn.commit()
    conn.close()


def history_load(limit=20):
    """Return last `limit` messages as list of dicts."""
    conn = _get_db()
    rows = conn.execute(
        "SELECT role, content FROM chat_history"
        " ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def history_clear():
    conn = _get_db()
    conn.execute("DELETE FROM chat_history")
    conn.commit()
    conn.close()


def note_save(content):
    conn = _get_db()
    conn.execute("INSERT INTO notes (content) VALUES (?)", (content,))
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Activation key helpers
# ═══════════════════════════════════════════════════════════════════════════════

def key_load():
    if KEY_FILE.exists():
        return KEY_FILE.read_text().strip()
    return None


def key_save(k):
    KEY_FILE.write_text(k.strip())
    KEY_FILE.chmod(0o600)


def key_verify_online(k):
    if not HAS_REQUESTS:
        return False, {"message": "requests not installed"}
    try:
        resp = _requests.post(
            f"{SERVER_URL}/verify-key",
            json={"key": k},
            timeout=10,
        )
        data = resp.json()
        return bool(data.get("valid")), data
    except Exception as exc:
        return False, {"message": str(exc)}


# ═══════════════════════════════════════════════════════════════════════════════
# Server sync
# ═══════════════════════════════════════════════════════════════════════════════

def server_sync(key):
    """Push reminders + preferences to Supabase (background, silent fail)."""
    if not HAS_REQUESTS:
        return
    try:
        conn = _get_db()
        reminders = [
            dict(r) for r in conn.execute(
                "SELECT text, remind_at, done FROM reminders"
            ).fetchall()
        ]
        prefs_rows = conn.execute(
            "SELECT key, value FROM preferences"
        ).fetchall()
        conn.close()
        preferences = {
            r["key"]: json.loads(r["value"]) for r in prefs_rows
        }
        _requests.post(
            f"{SERVER_URL}/user-data/save",
            json={"key": key, "reminders": reminders, "preferences": preferences},
            timeout=8,
        )
    except Exception:
        pass  # offline — local storage is authoritative


def server_load(key):
    """Pull user profile from server; update local prefs."""
    if not HAS_REQUESTS:
        return {}
    try:
        resp = _requests.post(
            f"{SERVER_URL}/user-data/load",
            json={"key": key},
            timeout=8,
        )
        data = resp.json()
        if data.get("name"):
            pref_set("user_name", data["name"])
        if isinstance(data.get("preferences"), dict):
            for k, v in data["preferences"].items():
                pref_set(k, v)
        return data
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════════════════════════
# AI call via server
# ═══════════════════════════════════════════════════════════════════════════════

def ai_ask(prompt, key):
    """Send prompt through Jamila server. Returns reply string."""
    if not HAS_REQUESTS:
        return "I cannot reach the AI right now — the requests library is missing."
    if not key:
        return "No activation key found. Please run the installer."

    name      = pref_get("user_name", "friend")
    likes     = pref_get("user_likes", [])
    dislikes  = pref_get("user_dislikes", [])

    system = (
        f"You are Jamila, a warm and caring voice-first AI assistant for {name}. "
        "Your responses will be read aloud by text-to-speech. "
        "Write naturally as speech — no markdown, no bullet points, no asterisks, "
        "no numbered lists. Keep responses concise and conversational. "
        "Be warm, patient, and encouraging. "
    )
    if likes:
        system += f"{name} likes: {', '.join(likes)}. "
    if dislikes:
        system += f"{name} dislikes: {', '.join(dislikes)}. "

    # Load persistent chat history for context
    history = history_load(limit=20)
    messages = history + [{"role": "user", "content": prompt}]

    try:
        resp = _requests.post(
            f"{SERVER_URL}/ai",
            json={
                "key": key,
                "messages": messages,
                "provider": "gemini",
                "system": system,
            },
            timeout=30,
        )
        data = resp.json()
        if "error" in data:
            return data["error"]
        reply = data.get("reply", "I did not get a response.")
        # Persist to local history
        history_append("user", prompt)
        history_append("assistant", reply)
        return reply
    except _requests.exceptions.ConnectionError:
        return "I cannot reach the Jamila server. Please check your internet connection."
    except Exception as exc:
        return f"Something went wrong: {exc}"


# ═══════════════════════════════════════════════════════════════════════════════
# Natural time parser
# ═══════════════════════════════════════════════════════════════════════════════

def parse_time(expr):
    """
    Convert natural time expressions to datetime.
    Examples: "tomorrow", "in 2 hours", "at 3pm", "next week", "tonight"
    """
    now  = datetime.datetime.now()
    expr = expr.lower().strip()

    def apply_clock(base, src):
        m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", src)
        if m:
            h  = int(m.group(1))
            mn = int(m.group(2) or 0)
            ap = m.group(3)
            if ap == "pm" and h != 12:
                h += 12
            elif ap == "am" and h == 12:
                h = 0
            return base.replace(hour=h, minute=mn, second=0, microsecond=0)
        return base.replace(hour=9, minute=0, second=0, microsecond=0)

    if "tomorrow" in expr:
        return apply_clock(now + datetime.timedelta(days=1), expr)
    if "tonight" in expr or "this evening" in expr:
        return now.replace(hour=20, minute=0, second=0, microsecond=0)
    if "next week" in expr:
        return apply_clock(now + datetime.timedelta(weeks=1), expr)
    if "next month" in expr:
        # same day next month
        m2 = (now.month % 12) + 1
        y2 = now.year + (1 if now.month == 12 else 0)
        return now.replace(year=y2, month=m2, hour=9, minute=0, second=0, microsecond=0)

    # "in N minutes/hours/days/weeks"
    m = re.search(r"in\s+(\d+)\s*(minute|hour|day|week)", expr)
    if m:
        n    = int(m.group(1))
        unit = m.group(2)
        delta_map = {
            "minute": datetime.timedelta(minutes=n),
            "hour":   datetime.timedelta(hours=n),
            "day":    datetime.timedelta(days=n),
            "week":   datetime.timedelta(weeks=n),
        }
        return now + delta_map[unit]

    # "at HH:MM am/pm"
    m = re.search(r"at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", expr)
    if m:
        h  = int(m.group(1))
        mn = int(m.group(2) or 0)
        ap = m.group(3)
        if ap == "pm" and h != 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        dt = now.replace(hour=h, minute=mn, second=0, microsecond=0)
        if dt <= now:
            dt += datetime.timedelta(days=1)
        return dt

    # Default: 1 hour from now
    return now + datetime.timedelta(hours=1)


# ═══════════════════════════════════════════════════════════════════════════════
# App / window helpers (Linux-specific, xdotool + wmctrl)
# ═══════════════════════════════════════════════════════════════════════════════

_APP_MAP = {
    "firefox":       ["firefox"],
    "browser":       ["xdg-open", "https://google.com"],
    "chrome":        ["google-chrome"],
    "chromium":      ["chromium-browser"],
    "terminal":      ["bash", "-c", "x-terminal-emulator || gnome-terminal || xterm"],
    "files":         ["bash", "-c", "nautilus ~ || nemo ~ || thunar ~ || pcmanfm ~"],
    "file manager":  ["bash", "-c", "nautilus ~ || nemo ~ || thunar ~ || pcmanfm ~"],
    "settings":      ["bash", "-c", "gnome-control-center || xfce4-settings-manager"],
    "calculator":    ["bash", "-c", "gnome-calculator || galculator || xcalc"],
    "text editor":   ["bash", "-c", "gedit || mousepad || xed || nano"],
    "music":         ["bash", "-c", "rhythmbox || audacious || clementine"],
    "music player":  ["bash", "-c", "rhythmbox || audacious || clementine"],
    "email":         ["bash", "-c", "thunderbird || evolution"],
    "photos":        ["bash", "-c", "eog || gpicview || shotwell"],
    "calendar":      ["bash", "-c", "gnome-calendar || thunderbird"],
    "vlc":           ["vlc"],
    "libreoffice":   ["libreoffice"],
    "writer":        ["libreoffice", "--writer"],
    "spreadsheet":   ["libreoffice", "--calc"],
    "presentation":  ["libreoffice", "--impress"],
}


def cmd_open(arg):
    arg = arg.strip()
    # Path on disk?
    if os.path.exists(arg):
        subprocess.Popen(
            ["xdg-open", arg],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return f"Opening {arg}"
    lo = arg.lower()
    for name, cmd in _APP_MAP.items():
        if name in lo:
            try:
                subprocess.Popen(
                    cmd,
                    shell=(len(cmd) == 2 and cmd[0] == "bash"),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return f"Opening {name}"
            except Exception as exc:
                return f"Could not open {name}: {exc}"
    # Try as raw command
    try:
        subprocess.Popen(
            arg.split(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return f"Launched {arg}"
    except FileNotFoundError:
        return f"I could not find an application called {arg}."


def cmd_close(arg):
    arg = arg.strip()
    try:
        result = subprocess.run(
            ["pkill", "-f", arg],
            capture_output=True,
        )
        if result.returncode == 0:
            return f"Closed {arg}"
        return f"No running process found for {arg}"
    except Exception as exc:
        return f"Close error: {exc}"


def cmd_window_maximize():
    try:
        subprocess.run(
            ["xdotool", "getactivewindow", "windowmaximize"],
            capture_output=True,
        )
        return "Window maximized"
    except Exception:
        return "Could not maximize window — xdotool may not be installed."


def cmd_window_minimize():
    try:
        subprocess.run(
            ["xdotool", "getactivewindow", "windowminimize"],
            capture_output=True,
        )
        return "Window minimized"
    except Exception:
        return "Could not minimize window."


def cmd_window_resize(arg):
    # "resize 800 600"
    parts = arg.strip().split()
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        w, h = parts[0], parts[1]
        try:
            win = subprocess.check_output(
                ["xdotool", "getactivewindow"]
            ).strip().decode()
            subprocess.run(
                ["xdotool", "windowsize", win, w, h],
                capture_output=True,
            )
            return f"Window resized to {w} by {h} pixels"
        except Exception as exc:
            return f"Resize error: {exc}"
    return "Please say resize followed by width and height, for example resize 800 600"


def cmd_window_move(arg):
    # "move window 100 200"
    parts = arg.strip().split()
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        x, y = parts[0], parts[1]
        try:
            win = subprocess.check_output(
                ["xdotool", "getactivewindow"]
            ).strip().decode()
            subprocess.run(
                ["xdotool", "windowmove", win, x, y],
                capture_output=True,
            )
            return f"Window moved to position {x}, {y}"
        except Exception as exc:
            return f"Move error: {exc}"
    return "Please specify X and Y coordinates."


# ═══════════════════════════════════════════════════════════════════════════════
# File operations
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_create_file(arg):
    """
    Syntax: <filename> [content...]
    Example: report.txt Hello world
    """
    arg    = arg.strip()
    parts  = arg.split(None, 1)
    name   = parts[0] if parts else ""
    content = parts[1] if len(parts) > 1 else ""
    if not name:
        return "Please provide a file name."
    path = Path(name) if name.startswith("/") else HOME / name
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Created file {path.name}"
    except Exception as exc:
        return f"Could not create file: {exc}"


def cmd_edit_file(arg):
    """
    Open a file in the default text editor.
    Syntax: <filename>
    """
    arg  = arg.strip()
    path = Path(arg) if arg.startswith("/") else HOME / arg
    if not path.exists():
        return f"File {arg} not found."
    editors = ["gedit", "mousepad", "xed", "kate", "nano"]
    for editor in editors:
        if shutil.which(editor):
            subprocess.Popen(
                [editor, str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return f"Opening {path.name} in {editor}"
    # Fallback: xdg-open
    subprocess.Popen(
        ["xdg-open", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return f"Opening {path.name}"


def cmd_delete(arg):
    arg  = arg.strip()
    path = Path(arg) if arg.startswith("/") else HOME / arg
    if not path.exists():
        return f"I could not find {arg}"
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        return f"Deleted {path.name}"
    except Exception as exc:
        return f"Could not delete {arg}: {exc}"


def cmd_list_files(arg):
    arg  = arg.strip() or str(HOME)
    path = Path(arg) if arg.startswith("/") else HOME / arg
    if not path.exists():
        return f"Path not found: {arg}"
    try:
        items = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))[:20]
        if not items:
            return f"The folder {arg} is empty."
        names = []
        for it in items:
            names.append(it.name + ("/" if it.is_dir() else ""))
        return f"In {path.name}: {', '.join(names)}"
    except PermissionError:
        return f"I do not have permission to read {arg}"
    except Exception as exc:
        return f"Could not list files: {exc}"


def cmd_copy_file(arg):
    """copy source destination"""
    parts = arg.strip().split(None, 1)
    if len(parts) < 2:
        return "Please specify source and destination."
    src  = Path(parts[0]) if parts[0].startswith("/") else HOME / parts[0]
    dest = Path(parts[1]) if parts[1].startswith("/") else HOME / parts[1]
    try:
        if src.is_dir():
            shutil.copytree(str(src), str(dest))
        else:
            shutil.copy2(str(src), str(dest))
        return f"Copied {src.name} to {dest}"
    except Exception as exc:
        return f"Copy error: {exc}"


def cmd_move_file(arg):
    """move source destination"""
    parts = arg.strip().split(None, 1)
    if len(parts) < 2:
        return "Please specify source and destination."
    src  = Path(parts[0]) if parts[0].startswith("/") else HOME / parts[0]
    dest = Path(parts[1]) if parts[1].startswith("/") else HOME / parts[1]
    try:
        shutil.move(str(src), str(dest))
        return f"Moved {src.name} to {dest}"
    except Exception as exc:
        return f"Move error: {exc}"


# ═══════════════════════════════════════════════════════════════════════════════
# System controls
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_volume(arg):
    digits = re.findall(r"\d+", arg)
    pct    = max(0, min(100, int(digits[0]))) if digits else 70
    # Try pactl first, fall back to amixer
    if shutil.which("pactl"):
        try:
            subprocess.run(
                ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{pct}%"],
                capture_output=True,
            )
            return f"Volume set to {pct} percent"
        except Exception:
            pass
    if shutil.which("amixer"):
        try:
            subprocess.run(
                ["amixer", "set", "Master", f"{pct}%"],
                capture_output=True,
            )
            return f"Volume set to {pct} percent"
        except Exception:
            pass
    return "Could not change volume — pactl and amixer not found."


def cmd_volume_up():
    if shutil.which("pactl"):
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+10%"], capture_output=True)
        return "Volume increased"
    return "Could not increase volume."


def cmd_volume_down():
    if shutil.which("pactl"):
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-10%"], capture_output=True)
        return "Volume decreased"
    return "Could not decrease volume."


def cmd_mute():
    if shutil.which("pactl"):
        subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"], capture_output=True)
        return "Volume toggled mute"
    return "Could not mute."


def cmd_screenshot(arg=""):
    ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = HOME / f"screenshot_{ts}.png"
    if shutil.which("scrot"):
        subprocess.Popen(["scrot", str(dest)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Screenshot saved as screenshot_{ts}.png"
    if shutil.which("gnome-screenshot"):
        subprocess.Popen(["gnome-screenshot", "-f", str(dest)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Screenshot saved as screenshot_{ts}.png"
    return "No screenshot tool found. Install scrot or gnome-screenshot."


# ═══════════════════════════════════════════════════════════════════════════════
# Email
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_email(arg, creds):
    """
    Syntax: to@example.com about=Subject body=Message body here
    """
    smtp = creds.get("smtp", {})
    if not smtp.get("host") or not smtp.get("username") or not smtp.get("password"):
        return (
            "Email is not configured. "
            "Please edit the credentials file at home dot jamila underscore credentials dot json"
        )
    arg = arg.strip()
    # Extract TO
    parts = arg.split(None, 1)
    if not parts:
        return "Please say the recipient email address."
    to_addr = parts[0]
    rest    = parts[1] if len(parts) > 1 else ""
    # Extract subject
    sub_m = re.search(r"(?:about|subject)=(.+?)(?:\s+body=|$)", rest, re.IGNORECASE)
    bod_m = re.search(r"body=(.+)", rest, re.IGNORECASE)
    subject = sub_m.group(1).strip() if sub_m else "Message from Jamila"
    body    = bod_m.group(1).strip() if bod_m else rest.strip() or "Sent via Jamila"
    try:
        msg             = EmailMessage()
        msg["From"]     = smtp["username"]
        msg["To"]       = to_addr
        msg["Subject"]  = subject
        msg.set_content(body)
        with smtplib.SMTP(smtp["host"], int(smtp.get("port", 587)), timeout=20) as s:
            s.starttls()
            s.login(smtp["username"], smtp["password"])
            s.send_message(msg)
        return f"Email sent to {to_addr}"
    except smtplib.SMTPAuthenticationError:
        return "Email authentication failed. Check your username and password in the credentials file."
    except Exception as exc:
        return f"Could not send email: {exc}"


# ═══════════════════════════════════════════════════════════════════════════════
# Reminder + note handlers
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_remind(arg, key):
    """
    Patterns:
      "me to pay bills tomorrow at 9am"
      "take my medicine in 2 hours"
      "call the doctor next week at 10am"
    """
    # Strip leading "me to" or "me"
    arg = re.sub(r"^me\s+to\s+", "", arg.strip(), flags=re.IGNORECASE)
    arg = re.sub(r"^me\s+",       "", arg.strip(), flags=re.IGNORECASE)

    # Find time expression at end
    time_pattern = (
        r"(tomorrow|tonight|this evening|next week|next month"
        r"|in\s+\d+\s+(?:minute|hour|day|week)s?"
        r"|at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"
    )
    m = re.search(time_pattern, arg, re.IGNORECASE)
    if m:
        task      = arg[:m.start()].strip().rstrip(",")
        time_expr = arg[m.start():]
    else:
        task      = arg.strip()
        time_expr = "in 1 hour"

    if not task:
        return "Please tell me what to remind you about."

    dt     = parse_time(time_expr)
    reminder_add(task, dt)
    threading.Thread(target=server_sync, args=(key,), daemon=True).start()
    when_str = dt.strftime("%A, %B %-d at %-I:%M %p")
    return f"Done! I will remind you to {task} on {when_str}."


def cmd_note(arg):
    content = arg.strip()
    if not content:
        return "What would you like me to note down?"
    note_save(content)
    return f"Note saved: {content[:80]}"


# ═══════════════════════════════════════════════════════════════════════════════
# Media & web
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_play(arg):
    arg = arg.strip()
    if os.path.exists(arg):
        for player in ["mpv", "vlc", "mplayer"]:
            if shutil.which(player):
                subprocess.Popen(
                    [player, arg],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return f"Playing {arg}"
    # YouTube search
    query = arg.replace(" ", "+")
    webbrowser.open(f"https://www.youtube.com/results?search_query={query}")
    return f"Searching YouTube for {arg}"


def cmd_search(arg):
    query = arg.strip().replace(" ", "+")
    webbrowser.open(f"https://duckduckgo.com/?q={query}")
    return f"Opened a search for {arg}"


# ═══════════════════════════════════════════════════════════════════════════════
# Command dispatcher
# ═══════════════════════════════════════════════════════════════════════════════

def dispatch(text, key, creds):
    """
    Route a voice/text command to the correct handler.
    Returns a response string, or '__exit__' to quit.
    """
    text = text.strip()
    lo   = text.lower()

    if not text:
        return None

    # ── Exit ─────────────────────────────────────────────────────────────────
    if lo in ("exit", "quit", "stop", "goodbye", "bye", "close jamila", "shut down"):
        return "__exit__"

    # ── App open / close ─────────────────────────────────────────────────────
    if lo.startswith("open "):
        return cmd_open(text[5:])
    if lo.startswith("close ") and lo != "close jamila":
        return cmd_close(text[6:])
    if lo.startswith("launch "):
        return cmd_open(text[7:])
    if lo.startswith("start "):
        return cmd_open(text[6:])

    # ── Window management ────────────────────────────────────────────────────
    if lo in ("maximize", "maximize window", "make window bigger", "full screen"):
        return cmd_window_maximize()
    if lo in ("minimize", "minimize window", "hide window"):
        return cmd_window_minimize()
    if lo.startswith("resize ") or lo.startswith("resize window "):
        arg = re.sub(r"^resize\s+(?:window\s+)?", "", text, flags=re.IGNORECASE)
        return cmd_window_resize(arg)
    if lo.startswith("move window "):
        return cmd_window_move(text[12:])

    # ── File operations ──────────────────────────────────────────────────────
    if lo.startswith("create file "):
        return cmd_create_file(text[12:])
    if lo.startswith("new file "):
        return cmd_create_file(text[9:])
    if lo.startswith("edit file ") or lo.startswith("open file "):
        return cmd_edit_file(re.sub(r"^(edit|open)\s+file\s+", "", text, flags=re.IGNORECASE))
    if lo.startswith("delete ") or lo.startswith("remove file ") or lo.startswith("delete file "):
        arg = re.sub(r"^(delete\s+file|remove\s+file|delete)\s+", "", text, flags=re.IGNORECASE)
        return cmd_delete(arg)
    if lo.startswith("list files") or lo.startswith("show files") or lo.startswith("what files") or lo.startswith("list folder"):
        arg = re.sub(r"^(list files?|show files?|what files?|list folder)\s*", "", text, flags=re.IGNORECASE)
        return cmd_list_files(arg)
    if lo.startswith("copy ") or lo.startswith("copy file "):
        arg = re.sub(r"^copy\s+(?:file\s+)?", "", text, flags=re.IGNORECASE)
        return cmd_copy_file(arg)
    if lo.startswith("move ") or lo.startswith("move file "):
        arg = re.sub(r"^move\s+(?:file\s+)?", "", text, flags=re.IGNORECASE)
        return cmd_move_file(arg)

    # ── Volume ───────────────────────────────────────────────────────────────
    if lo.startswith("volume ") or lo.startswith("set volume"):
        return cmd_volume(text)
    if lo in ("volume up", "turn up", "louder", "increase volume"):
        return cmd_volume_up()
    if lo in ("volume down", "turn down", "quieter", "decrease volume"):
        return cmd_volume_down()
    if lo in ("mute", "unmute", "toggle mute", "silence"):
        return cmd_mute()

    # ── Screenshot ───────────────────────────────────────────────────────────
    if lo.startswith("screenshot") or lo == "take a screenshot" or lo == "capture screen":
        return cmd_screenshot()

    # ── Media & web ──────────────────────────────────────────────────────────
    if lo.startswith("play "):
        return cmd_play(text[5:])
    if lo.startswith("search ") or lo.startswith("look up ") or lo.startswith("google "):
        arg = re.sub(r"^(search|look up|google)\s+", "", text, flags=re.IGNORECASE)
        return cmd_search(arg)

    # ── Email ────────────────────────────────────────────────────────────────
    if re.match(r"(send\s+(an\s+)?email|email)\s+", lo):
        arg = re.sub(r"^(send\s+(?:an\s+)?email|email)\s+", "", text, flags=re.IGNORECASE)
        return cmd_email(arg, creds)

    # ── Reminders ────────────────────────────────────────────────────────────
    if re.match(r"(remind|reminder|remember)\s+", lo):
        arg = re.sub(r"^(remind|reminder|remember)\s+", "", text, flags=re.IGNORECASE)
        return cmd_remind(arg, key)

    # ── Notes ────────────────────────────────────────────────────────────────
    if re.match(r"(note|make a note|take a note|save a note|write down)\s*:?\s+", lo):
        arg = re.sub(r"^(note|make a note|take a note|save a note|write down)\s*:?\s+", "", text, flags=re.IGNORECASE)
        return cmd_note(arg)

    # ── Time / date ──────────────────────────────────────────────────────────
    if any(x in lo for x in ("what time", "what is the time", "what's the time", "current time")):
        return f"It is {datetime.datetime.now().strftime('%-I:%M %p')}."
    if any(x in lo for x in ("what day", "what date", "what's today", "today's date", "what is today")):
        return f"Today is {datetime.datetime.now().strftime('%A, %B %-d, %Y')}."
    if "what year" in lo:
        return f"It is {datetime.datetime.now().year}."

    # ── Self-learning ─────────────────────────────────────────────────────────
    m = re.match(r"my name is (.+)", lo)
    if m:
        name = m.group(1).strip().title()
        pref_set("user_name", name)
        return f"Nice to meet you, {name}! I will remember your name."

    m = re.match(r"i (?:like|love|enjoy|prefer) (.+)", lo)
    if m:
        item  = m.group(1).strip()
        likes = pref_get("user_likes", [])
        if item not in likes:
            likes.append(item)
            pref_set("user_likes", likes)
        return f"Got it! I will remember that you love {item}."

    m = re.match(r"i (?:don't|do not|hate|dislike|avoid) (.+)", lo)
    if m:
        item     = m.group(1).strip()
        dislikes = pref_get("user_dislikes", [])
        if item not in dislikes:
            dislikes.append(item)
            pref_set("user_dislikes", dislikes)
        return f"Noted! I will remember that you do not like {item}."

    # ── Clear history ─────────────────────────────────────────────────────────
    if lo in ("clear history", "forget our conversation", "clear chat", "reset memory"):
        history_clear()
        return "Done. I have cleared our conversation history."

    # ── Catch-all: ask AI ─────────────────────────────────────────────────────
    return ai_ask(text, key)


# ═══════════════════════════════════════════════════════════════════════════════
# Speech recognition
# ═══════════════════════════════════════════════════════════════════════════════

def listen_once(timeout=10, phrase_limit=15):
    """
    Record audio until silence, then return transcribed text or None.
    Called after spacebar release + 2-second buffer.
    """
    if not HAS_SR:
        return None
    try:
        rec = sr.Recognizer()
        rec.energy_threshold = 300
        rec.pause_threshold  = 0.8
        rec.dynamic_energy_threshold = True
        with sr.Microphone() as source:
            rec.adjust_for_ambient_noise(source, duration=0.3)
            audio = rec.listen(source, timeout=timeout, phrase_time_limit=phrase_limit)
        return rec.recognize_google(audio)
    except sr.WaitTimeoutError:
        return None
    except sr.UnknownValueError:
        return None
    except Exception as exc:
        log.debug("listen_once: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Reminder background thread
# ═══════════════════════════════════════════════════════════════════════════════

def reminder_loop(stop_event, on_reminder=None):
    """
    Check for due reminders every 30 seconds.
    on_reminder(text) called on main thread via GLib if GTK available.
    """
    while not stop_event.is_set():
        for row in reminder_due():
            msg = f"Reminder: {row['text']}"
            speak(msg, block=False)
            if on_reminder and HAS_GTK:
                GLib.idle_add(on_reminder, msg)
            reminder_done(row["id"])
        stop_event.wait(30)


# ═══════════════════════════════════════════════════════════════════════════════
# GTK Warm GUI
# ═══════════════════════════════════════════════════════════════════════════════

_WARM_CSS = b"""
window {
  background-color: #1a1208;
}
.header {
  background: #221a0e;
  border-bottom: 1px solid #4a3520;
}
.app-title {
  font-family: Georgia, serif;
  font-size: 22px;
  font-weight: bold;
  color: #f59e2a;
}
.app-sub {
  font-size: 11px;
  color: #a08060;
}
.resp-view text {
  background-color: #1e160a;
  color: #f0e4cc;
  font-family: Georgia, serif;
  font-size: 16px;
}
.resp-view {
  background-color: #1e160a;
}
scrolledwindow {
  border: 1px solid #4a3520;
  border-radius: 10px;
  background-color: #1e160a;
}
.mic-idle {
  background-color: #f59e2a;
  color: #1a1208;
  border-radius: 30px;
  font-size: 16px;
  font-weight: bold;
  border: none;
}
.mic-recording {
  background-color: #e05a3a;
  color: #ffffff;
  border-radius: 30px;
  font-size: 16px;
  font-weight: bold;
  border: none;
}
.mic-thinking {
  background-color: #8a7050;
  color: #f0e4cc;
  border-radius: 30px;
  font-size: 16px;
  font-weight: bold;
  border: none;
}
.mic-ready {
  background-color: #3a9a6a;
  color: #ffffff;
  border-radius: 30px;
  font-size: 16px;
  font-weight: bold;
  border: none;
}
.status-label {
  color: #a08060;
  font-size: 12px;
}
.status-active {
  color: #f59e2a;
  font-size: 12px;
}
.fallback-entry {
  background-color: #2a1e0e;
  color: #f0e4cc;
  border: 1px solid #4a3520;
  border-radius: 8px;
  font-size: 14px;
}
.key-tag {
  color: #7a6040;
  font-size: 10px;
}
"""

# Spacebar state machine:
#  IDLE → SPACE_DOWN (mic starts) → SPACE_UP (2-sec grace) → EXECUTE → IDLE

_MIC_IDLE      = "idle"
_MIC_RECORDING = "recording"
_MIC_READY     = "ready"       # space released, 2s grace
_MIC_THINKING  = "thinking"


class JamilaWindow(Gtk.Window):
    """Main application window — warm, voice-first."""

    def __init__(self, key, creds):
        # Must call parent __init__ with no args or just title
        Gtk.Window.__init__(self, title="Jamila")

        self.key     = key
        self.creds   = creds
        self._state  = _MIC_IDLE
        self._audio  = None      # raw AudioData from spacebar hold
        self._grace_timer = None  # GLib source id

        self.set_default_size(480, 560)
        self.set_resizable(True)
        self.set_border_width(0)

        # CSS
        prov = Gtk.CssProvider()
        prov.load_from_data(_WARM_CSS)
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(),
            prov,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        # ── Root ──────────────────────────────────────────────────────────────
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(root)

        # ── Header ────────────────────────────────────────────────────────────
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        hdr.get_style_context().add_class("header")
        hdr.set_margin_start(18)
        hdr.set_margin_end(18)
        hdr.set_margin_top(14)
        hdr.set_margin_bottom(14)

        if ICON_FILE.exists():
            try:
                pb  = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(ICON_FILE), 40, 40, True)
                img = Gtk.Image.new_from_pixbuf(pb)
                img.set_margin_end(12)
                hdr.pack_start(img, False, False, 0)
            except Exception:
                pass

        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        lbl = Gtk.Label(label="Jamila")
        lbl.get_style_context().add_class("app-title")
        lbl.set_halign(Gtk.Align.START)
        sub = Gtk.Label(label="Hold Space to speak • Release to send")
        sub.get_style_context().add_class("app-sub")
        sub.set_halign(Gtk.Align.START)
        col.pack_start(lbl, False, False, 0)
        col.pack_start(sub, False, False, 0)
        hdr.pack_start(col, True, True, 0)

        tag = Gtk.Label(label=key[:11] + "…")
        tag.get_style_context().add_class("key-tag")
        hdr.pack_end(tag, False, False, 0)
        root.pack_start(hdr, False, False, 0)

        # ── Response text view ────────────────────────────────────────────────
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_margin_start(14)
        scroll.set_margin_end(14)
        scroll.set_margin_top(14)
        scroll.set_min_content_height(260)

        self._buf = Gtk.TextBuffer()
        self._buf.set_text(
            "Hello! I'm Jamila.\n\n"
            "Hold the Space bar to speak — release when done.\n"
            "I'll listen for 2 seconds then execute your command."
        )
        tv = Gtk.TextView(buffer=self._buf)
        tv.get_style_context().add_class("resp-view")
        tv.set_editable(False)
        tv.set_cursor_visible(False)
        tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scroll.add(tv)
        self._scroll = scroll
        self._tv     = tv
        root.pack_start(scroll, True, True, 0)

        # ── Status label ──────────────────────────────────────────────────────
        self._status = Gtk.Label(label="Hold Space bar to speak")
        self._status.get_style_context().add_class("status-label")
        self._status.set_margin_top(8)
        root.pack_start(self._status, False, False, 0)

        # ── Mic button ────────────────────────────────────────────────────────
        self._mic_btn = Gtk.Button()
        self._mic_btn.set_margin_start(36)
        self._mic_btn.set_margin_end(36)
        self._mic_btn.set_margin_top(12)
        self._mic_btn.set_margin_bottom(10)
        self._mic_btn.set_size_request(-1, 56)
        self._mic_btn.get_style_context().add_class("mic-idle")

        mic_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mic_row.set_halign(Gtk.Align.CENTER)
        self._mic_icon = Gtk.Label(label="🎙")
        self._mic_text = Gtk.Label(label="Hold Space  or  Click & Hold")
        mic_row.pack_start(self._mic_icon, False, False, 0)
        mic_row.pack_start(self._mic_text, False, False, 0)
        self._mic_btn.add(mic_row)

        # Button press/release for click-and-hold alternative
        self._mic_btn.connect("button-press-event",   self._on_btn_press)
        self._mic_btn.connect("button-release-event", self._on_btn_release)
        root.pack_start(self._mic_btn, False, False, 0)

        # ── Fallback text entry (visible if no mic) ───────────────────────────
        entry_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        entry_row.set_margin_start(14)
        entry_row.set_margin_end(14)
        entry_row.set_margin_bottom(14)
        self._entry = Gtk.Entry()
        self._entry.get_style_context().add_class("fallback-entry")
        self._entry.set_placeholder_text("Type command here (mic unavailable)…")
        self._entry.connect("activate", self._on_text_enter)
        send_btn = Gtk.Button(label="→")
        send_btn.connect("clicked", self._on_text_enter)
        entry_row.pack_start(self._entry, True, True, 0)
        entry_row.pack_start(send_btn, False, False, 0)
        entry_row.set_visible(not HAS_SR)
        root.pack_start(entry_row, False, False, 0)
        self._entry_row = entry_row

        # ── Keyboard events ───────────────────────────────────────────────────
        self.add_events(Gdk.EventMask.KEY_PRESS_MASK | Gdk.EventMask.KEY_RELEASE_MASK)
        self.connect("key-press-event",   self._on_key_press)
        self.connect("key-release-event", self._on_key_release)
        self.connect("destroy", self._on_destroy)

        self.show_all()
        if HAS_SR:
            entry_row.hide()

        # Welcome after a short delay
        GLib.timeout_add(700, self._welcome)

    # ── UI helpers ────────────────────────────────────────────────────────────

    def _set_text(self, text):
        self._buf.set_text(text)
        GLib.idle_add(self._scroll_bottom)

    def _scroll_bottom(self):
        adj = self._tv.get_vadjustment()
        adj.set_value(adj.get_upper())
        return False

    def _set_status(self, text, active=False):
        self._status.set_text(text)
        ctx = self._status.get_style_context()
        if active:
            ctx.add_class("status-active")
            ctx.remove_class("status-label")
        else:
            ctx.add_class("status-label")
            ctx.remove_class("status-active")

    def _set_mic_state(self, state):
        ctx = self._mic_btn.get_style_context()
        for cls in ("mic-idle", "mic-recording", "mic-ready", "mic-thinking"):
            ctx.remove_class(cls)
        ctx.add_class(f"mic-{state}")
        label_map = {
            _MIC_IDLE:      ("🎙",  "Hold Space  or  Click & Hold"),
            _MIC_RECORDING: ("🔴",  "Listening… release when done"),
            _MIC_READY:     ("⏳",  "Processing in 2 seconds…"),
            _MIC_THINKING:  ("💭",  "Thinking…"),
        }
        icon, text = label_map.get(state, ("🎙", ""))
        self._mic_icon.set_text(icon)
        self._mic_text.set_text(text)

    # ── Spacebar logic ────────────────────────────────────────────────────────

    def _on_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_space:
            # Ignore if typing in entry
            if self._entry.is_focus():
                return False
            # Ignore repeat key events
            if event.get_event_type() == Gdk.EventType._2BUTTON_PRESS:
                return True
            if self._state == _MIC_IDLE:
                self._start_recording()
            return True
        return False

    def _on_key_release(self, widget, event):
        if event.keyval == Gdk.KEY_space:
            if self._entry.is_focus():
                return False
            if self._state == _MIC_RECORDING:
                self._stop_recording()
            return True
        return False

    def _on_btn_press(self, widget, event):
        if event.button == 1 and self._state == _MIC_IDLE:
            self._start_recording()

    def _on_btn_release(self, widget, event):
        if event.button == 1 and self._state == _MIC_RECORDING:
            self._stop_recording()

    # ── Recording flow ────────────────────────────────────────────────────────

    def _start_recording(self):
        if not HAS_SR:
            self._set_text("Microphone not available. Please use the text box below.")
            speak("Microphone is not available. Please type your command.", block=False)
            self._entry_row.show()
            return
        self._state = _MIC_RECORDING
        self._set_mic_state(_MIC_RECORDING)
        self._set_status("Listening… release Space when done", active=True)
        # Start background listening
        threading.Thread(target=self._record_thread, daemon=True).start()

    def _record_thread(self):
        """Record audio continuously until _state changes away from RECORDING."""
        if not HAS_SR:
            return
        try:
            rec = sr.Recognizer()
            rec.energy_threshold        = 300
            rec.dynamic_energy_threshold = True
            rec.pause_threshold         = 0.6
            with sr.Microphone() as source:
                rec.adjust_for_ambient_noise(source, duration=0.2)
                # listen() blocks until silence or timeout
                audio = rec.listen(source, timeout=30, phrase_time_limit=30)
            self._audio = (rec, audio)
        except Exception as exc:
            log.debug("_record_thread: %s", exc)
            self._audio = None
        # Signal that raw audio is captured
        GLib.idle_add(self._audio_captured)

    def _audio_captured(self):
        """Called when audio thread finishes — may still be in RECORDING state."""
        if self._state == _MIC_RECORDING:
            # Space not yet released — wait for release
            pass
        return False

    def _stop_recording(self):
        """Called when spacebar / button is released."""
        self._state = _MIC_READY
        self._set_mic_state(_MIC_READY)
        self._set_status("Releasing… executing in 2 seconds", active=True)
        # 2-second grace timer
        if self._grace_timer is not None:
            GLib.source_remove(self._grace_timer)
        self._grace_timer = GLib.timeout_add(2000, self._execute_recorded)

    def _execute_recorded(self):
        """Called 2 seconds after space release."""
        self._grace_timer = None
        self._state = _MIC_THINKING
        self._set_mic_state(_MIC_THINKING)
        self._set_status("Transcribing…", active=True)
        threading.Thread(target=self._transcribe_and_run, daemon=True).start()
        return False  # Remove timer

    def _transcribe_and_run(self):
        text = None
        if self._audio is not None:
            rec, audio = self._audio
            self._audio = None
            try:
                text = rec.recognize_google(audio)
            except sr.UnknownValueError:
                text = None
            except Exception as exc:
                log.debug("transcribe: %s", exc)
                text = None

        GLib.idle_add(self._after_transcribe, text)

    def _after_transcribe(self, text):
        if not text:
            self._state = _MIC_IDLE
            self._set_mic_state(_MIC_IDLE)
            self._set_status("Did not catch that — hold Space and try again")
            return
        self._set_status(f"You said: \"{text}\"", active=True)
        self._set_text(f"You: {text}\n\nThinking…")
        threading.Thread(target=self._run_cmd, args=(text,), daemon=True).start()

    # ── Text entry fallback ───────────────────────────────────────────────────

    def _on_text_enter(self, widget):
        text = self._entry.get_text().strip()
        if not text or self._state != _MIC_IDLE:
            return
        self._entry.set_text("")
        self._state = _MIC_THINKING
        self._set_mic_state(_MIC_THINKING)
        self._set_status(f"You typed: \"{text}\"", active=True)
        self._set_text(f"You: {text}\n\nThinking…")
        threading.Thread(target=self._run_cmd, args=(text,), daemon=True).start()

    # ── Command execution ─────────────────────────────────────────────────────

    def _run_cmd(self, text):
        reply = dispatch(text, self.key, self.creds)
        GLib.idle_add(self._show_reply, reply)

    def _show_reply(self, reply):
        self._state = _MIC_IDLE
        self._set_mic_state(_MIC_IDLE)
        if reply == "__exit__":
            speak("Goodbye!", block=True)
            Gtk.main_quit()
            return
        if reply:
            self._set_text(reply)
            self._set_status("Hold Space to speak again")
            speak(reply, block=False)

    # ── Reminder callback ─────────────────────────────────────────────────────

    def show_reminder(self, msg):
        self._set_text(msg)
        self._set_status("Reminder!")
        return False

    # ── Welcome ───────────────────────────────────────────────────────────────

    def _welcome(self):
        name = pref_get("user_name", "")
        greeting = (
            f"Hello{', ' + name if name else ''}! "
            "I am Jamila, your voice assistant. "
            "Hold the Space bar to speak, release when you are done. "
            "I will execute your command two seconds after you release."
        )
        self._set_text(greeting)
        speak(greeting, block=False)
        return False

    def _on_destroy(self, widget):
        threading.Thread(target=server_sync, args=(self.key,), daemon=True).start()
        Gtk.main_quit()


# ═══════════════════════════════════════════════════════════════════════════════
# Terminal fallback
# ═══════════════════════════════════════════════════════════════════════════════

def terminal_mode(key, creds):
    speak(
        "Jamila ready in terminal mode. "
        "Press Enter with no text to speak, or type a command.",
        block=True,
    )
    while True:
        try:
            raw = input("\njamila> ").strip()
            if raw == "" and HAS_SR:
                speak("Listening…", block=False)
                time.sleep(0.3)
                raw = listen_once()
                if not raw:
                    print("Didn't catch that.")
                    continue
                print(f"You said: {raw}")
            if not raw:
                continue
            reply = dispatch(raw, key, creds)
            if reply == "__exit__":
                speak("Goodbye!")
                break
            if reply:
                print(f"\n[Jamila] {reply}")
                speak(reply)
        except (KeyboardInterrupt, EOFError):
            speak("Goodbye!")
            break


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    init_db()

    key = key_load()
    if not key:
        speak("No activation key found. Please run the installer first.")
        log.error("No key at %s — run run.sh", KEY_FILE)
        sys.exit(1)

    creds = {}
    if CREDS_FILE.exists():
        try:
            creds = json.loads(CREDS_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Could not load credentials: %s", exc)

    # Pull latest user profile from server (background, non-blocking)
    threading.Thread(target=server_load, args=(key,), daemon=True).start()

    # Reminder thread
    stop_event = threading.Event()

    if HAS_GTK:
        win = JamilaWindow(key, creds)
        threading.Thread(
            target=reminder_loop,
            args=(stop_event, win.show_reminder),
            daemon=True,
        ).start()
        try:
            Gtk.main()
        finally:
            stop_event.set()
            server_sync(key)
    else:
        threading.Thread(
            target=reminder_loop,
            args=(stop_event,),
            daemon=True,
        ).start()
        try:
            terminal_mode(key, creds)
        finally:
            stop_event.set()
            server_sync(key)


if __name__ == "__main__":
    main()
