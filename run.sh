#!/usr/bin/env bash
# ╔══════════════════════════════════════════════════╗
# ║        JAMILA  —  Linux Installer v2             ║
# ║  Voice-first AI for the blind                    ║
# ║  https://github.com/EMN90909/jamila              ║
# ╚══════════════════════════════════════════════════╝
set -euo pipefail

SERVER="https://jamila.onrender.com"
JAMILA_DIR="$HOME/.jamila"
VENV="$JAMILA_DIR/venv"
KEY_FILE="$HOME/.jamila_key"
CREDS_FILE="$HOME/.jamila_credentials.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Colours ──────────────────────────────────────────────────────────────────
A='\033[0;33m'; G='\033[0;32m'; R='\033[0;31m'; C='\033[0;36m'
B='\033[1m'; D='\033[2m'; N='\033[0m'
say()  { echo -e "${A}${B}  ✦  $*${N}"; }
ok()   { echo -e "${G}  ✓  $*${N}"; }
fail() { echo -e "${R}  ✗  $*${N}"; }
dim()  { echo -e "${D}     $*${N}"; }

clear
echo ""
echo -e "${A}${B}"
echo "     ██╗ █████╗ ███╗   ███╗██╗██╗      █████╗ "
echo "     ██║██╔══██╗████╗ ████║██║██║     ██╔══██╗"
echo "     ██║███████║██╔████╔██║██║██║     ███████║"
echo "██   ██║██╔══██║██║╚██╔╝██║██║██║     ██╔══██║"
echo "╚█████╔╝██║  ██║██║ ╚═╝ ██║██║███████╗██║  ██║"
echo " ╚════╝ ╚═╝  ╚═╝╚═╝     ╚═╝╚═╝╚══════╝╚═╝  ╚═╝"
echo -e "${N}"
echo -e "     ${B}Voice-first AI assistant for the blind${N}"
echo -e "     ${D}github.com/EMN90909/jamila  •  $SERVER${N}"
echo ""

# ═══════════════════════════════════════════════════════════════════════════
# 1 — Activation key
# ═══════════════════════════════════════════════════════════════════════════
echo -e "${A}${B}── Step 1: Activation Key ─────────────────────────────────${N}"
echo ""
say "Get your free activation key at: ${C}$SERVER${N}"
echo ""

JAMILA_KEY=""
if [ -f "$KEY_FILE" ]; then
    EXISTING=$(cat "$KEY_FILE")
    echo -e "  Found saved key: ${C}${EXISTING:0:15}…${N}"
    printf "  Re-use it? (Y/n): "
    read -r REUSE
    if [[ ! "$REUSE" =~ ^[Nn] ]]; then
        JAMILA_KEY="$EXISTING"
    fi
fi

if [ -z "$JAMILA_KEY" ]; then
    echo ""
    echo -e "  ${B}Paste your Jamila activation key${N}"
    echo -e "  ${D}Format: JML-XXXXXXXX-XXXXXXXX-XXXXXXXX${N}"
    printf "  Key: "
    read -r JAMILA_KEY
    JAMILA_KEY="$(echo "$JAMILA_KEY" | tr -d '[:space:]')"
fi

if [ -z "$JAMILA_KEY" ]; then
    fail "No key provided. Visit $SERVER to get one."
    exit 1
fi

# ═══════════════════════════════════════════════════════════════════════════
# 2 — Verify key online
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${A}${B}── Step 2: Verifying your key ─────────────────────────────${N}"
echo ""
say "Contacting Jamila server…"

TMPFILE=$(mktemp)
HTTP_CODE=$(curl -s -o "$TMPFILE" -w "%{http_code}" \
    -X POST "$SERVER/verify-key" \
    -H "Content-Type: application/json" \
    -d "{\"key\":\"$JAMILA_KEY\"}" 2>/dev/null || echo "000")

if [ "$HTTP_CODE" = "000" ]; then
    fail "Cannot reach server. Check your internet connection."
    printf "  Continue offline install anyway? (y/N): "
    read -r OFFLINE
    [[ "$OFFLINE" =~ ^[Yy] ]] || { rm -f "$TMPFILE"; exit 1; }
elif [ "$HTTP_CODE" = "200" ]; then
    VALID=$(python3 -c "import json,sys; d=json.load(open('$TMPFILE')); print(str(d.get('valid',False)).lower())" 2>/dev/null || echo "false")
    if [ "$VALID" = "true" ]; then
        PLAN=$(python3 -c "import json,sys; d=json.load(open('$TMPFILE')); print(d.get('plan','free'))" 2>/dev/null || echo "free")
        NAME=$(python3 -c "import json,sys; d=json.load(open('$TMPFILE')); print(d.get('name',''))" 2>/dev/null || echo "")
        ok "Key accepted!"
        [ -n "$NAME" ] && echo -e "     ${B}Welcome, ${A}$NAME${N} ${D}(plan: $PLAN)${N}"
    else
        MSG=$(python3 -c "import json,sys; d=json.load(open('$TMPFILE')); print(d.get('message','Invalid key'))" 2>/dev/null || echo "Invalid key")
        fail "Key rejected: $MSG"
        dim "Get a valid key at $SERVER"
        rm -f "$TMPFILE"
        exit 1
    fi
else
    fail "Server returned HTTP $HTTP_CODE. Try again later."
    rm -f "$TMPFILE"
    exit 1
fi
rm -f "$TMPFILE"

echo "$JAMILA_KEY" > "$KEY_FILE"
chmod 600 "$KEY_FILE"
ok "Key saved"

# ═══════════════════════════════════════════════════════════════════════════
# 3 — System packages
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${A}${B}── Step 3: System packages ────────────────────────────────${N}"
echo ""
say "Installing system dependencies…"

if command -v apt-get &>/dev/null; then
    sudo apt-get update -qq 2>&1 | tail -1
    sudo apt-get install -y \
        python3 python3-pip python3-venv python3-dev \
        python3-gi python3-gi-cairo gir1.2-gtk-3.0 libglib2.0-dev \
        portaudio19-dev libsndfile1 libffi-dev libssl-dev \
        espeak-ng ffmpeg mpv xdotool pulseaudio-utils \
        build-essential wget curl git scrot 2>&1 | grep -cE "(newly installed|upgraded)" || true
    ok "apt packages installed"

elif command -v dnf &>/dev/null; then
    sudo dnf install -y \
        python3 python3-pip python3-gobject gtk3-devel \
        portaudio-devel libsndfile espeak-ng ffmpeg mpv \
        xdotool pulseaudio-utils gcc openssl-devel scrot 2>&1 | tail -3
    ok "dnf packages installed"

elif command -v pacman &>/dev/null; then
    sudo pacman -Sy --noconfirm \
        python python-pip python-gobject gtk3 \
        portaudio libsndfile espeak-ng ffmpeg mpv \
        xdotool pulseaudio base-devel scrot 2>&1 | tail -3
    ok "pacman packages installed"

else
    echo -e "${A}  Unknown package manager — skipping system packages.${N}"
    dim "Install manually: python3, portaudio, espeak-ng, GTK3, xdotool"
fi

# ═══════════════════════════════════════════════════════════════════════════
# 4 — Python venv
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${A}${B}── Step 4: Python environment ─────────────────────────────${N}"
echo ""
say "Creating Python virtual environment…"
mkdir -p "$JAMILA_DIR"
if [ ! -d "$VENV" ]; then
    python3 -m venv "$VENV"
fi
# shellcheck source=/dev/null
source "$VENV/bin/activate"
pip install --upgrade pip -q
ok "Virtual environment ready"

say "Installing Python packages (this may take a minute)…"
pip install -q \
    requests \
    SpeechRecognition \
    PyAudio \
    pyttsx3 \
    sounddevice \
    soundfile \
    numpy \
    Pillow
ok "Base Python packages installed"

say "Installing Coqui TTS (natural neural voice — ~200 MB)…"
dim "This gives Jamila a warm, human-sounding voice."
if pip install -q TTS 2>/dev/null; then
    ok "Coqui TTS installed — natural voice active"
else
    echo -e "${A}  Coqui TTS unavailable — pyttsx3 voice will be used instead${N}"
fi

# ═══════════════════════════════════════════════════════════════════════════
# 5 — Install files
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${A}${B}── Step 5: Installing Jamila files ────────────────────────${N}"
echo ""
say "Copying to $JAMILA_DIR…"
cp -r "$SCRIPT_DIR"/. "$JAMILA_DIR/"
chmod +x "$JAMILA_DIR/jamila_core.py"
ok "Files installed"

if [ ! -f "$CREDS_FILE" ]; then
    cat > "$CREDS_FILE" << 'JSON'
{
  "_info": "Edit this file to enable email. Never share it.",
  "smtp": {
    "host":     "smtp.gmail.com",
    "port":     587,
    "username": "your@gmail.com",
    "password": "your-app-password"
  }
}
JSON
    chmod 600 "$CREDS_FILE"
    dim "Created $CREDS_FILE — edit to enable email sending"
fi

# ═══════════════════════════════════════════════════════════════════════════
# 6 — Create launcher
# ═══════════════════════════════════════════════════════════════════════════
echo ""
say "Creating launchers…"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/jamila" << LAUNCHER
#!/usr/bin/env bash
source "$VENV/bin/activate"
exec python3 "$JAMILA_DIR/jamila_core.py" "\$@"
LAUNCHER
chmod +x "$HOME/.local/bin/jamila"

# Add to PATH if needed
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    dim "Added ~/.local/bin to PATH in .bashrc"
fi

# Desktop shortcut
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/jamila.desktop" << DESKTOP
[Desktop Entry]
Name=Jamila
GenericName=Voice AI Assistant
Comment=Voice-first AI for the blind
Exec=$HOME/.local/bin/jamila
Icon=$JAMILA_DIR/jamila.png
Terminal=false
Type=Application
Categories=Accessibility;Utility;Education;
Keywords=voice;ai;assistant;blind;accessibility;
StartupNotify=true
DESKTOP
ok "Launcher + desktop shortcut created"

# ═══════════════════════════════════════════════════════════════════════════
# 7 — Pre-warm Coqui voice model in background
# ═══════════════════════════════════════════════════════════════════════════
echo ""
say "Pre-loading voice model in background…"
(
    source "$VENV/bin/activate"
    python3 - << 'PYEOF' &
try:
    from TTS.api import TTS
    TTS(model_name="tts_models/en/ljspeech/tacotron2-DDC", progress_bar=False, gpu=False)
    print("     Voice model ready.")
except Exception as e:
    print(f"     Voice model: {e}")
PYEOF
)

# ═══════════════════════════════════════════════════════════════════════════
# Done
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${A}${B}╔══════════════════════════════════════════════════════════╗"
echo    "║                                                          ║"
echo    "║   ✦  Jamila installed successfully!                     ║"
echo    "║                                                          ║"
echo -e "╚══════════════════════════════════════════════════════════╝${N}"
echo ""
echo -e "  Start Jamila:   ${C}${B}jamila${N}   ${D}(may need to restart terminal)${N}"
echo -e "  Or:             Find Jamila in your Applications menu"
echo ""
echo -e "  ${D}Dashboard : $SERVER/dashboard.html${N}"
echo -e "  ${D}Key file  : $KEY_FILE${N}"
echo ""
echo -e "${G}${B}  Hold Space to speak — Jamila is listening. ✦${N}"
echo ""
