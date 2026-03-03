#!/usr/bin/env bash
# ╔══════════════════════════════════════════════════════════╗
# ║              JAMILA — Linux Installer                    ║
# ║    Voice-first AI assistant for the blind               ║
# ║    https://github.com/EMN90909/jamila                   ║
# ╚══════════════════════════════════════════════════════════╝
set -e

SERVER="https://jamila.onrender.com"
JAMILA_HOME="$HOME/.jamila"
VENV="$JAMILA_HOME/venv"
KEY_FILE="$HOME/.jamila_key"
CREDS_FILE="$HOME/.jamila_credentials.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Terminal colours ──────────────────────────────────────────────────────────
AMBER='\033[0;33m'; GREEN='\033[0;32m'; RED='\033[0;31m'
CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; RESET='\033[0m'
say() { echo -e "${AMBER}${BOLD}  ✦ $*${RESET}"; }
ok()  { echo -e "${GREEN}  ✓ $*${RESET}"; }
err() { echo -e "${RED}  ✗ $*${RESET}"; }
dim() { echo -e "${DIM}    $*${RESET}"; }

clear
echo ""
echo -e "${AMBER}${BOLD}"
echo "     ██╗ █████╗ ███╗   ███╗██╗██╗      █████╗ "
echo "     ██║██╔══██╗████╗ ████║██║██║     ██╔══██╗"
echo "     ██║███████║██╔████╔██║██║██║     ███████║"
echo "██   ██║██╔══██║██║╚██╔╝██║██║██║     ██╔══██║"
echo "╚█████╔╝██║  ██║██║ ╚═╝ ██║██║███████╗██║  ██║"
echo " ╚════╝ ╚═╝  ╚═╝╚═╝     ╚═╝╚═╝╚══════╝╚═╝  ╚═╝"
echo -e "${RESET}"
echo -e "${BOLD}  Voice-first AI assistant — designed for the blind  .By- EMTRA${RESET}"
echo -e "${DIM}  https://github.com/EMN90909/jamila${RESET}"
echo ""

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Activation key
# ═══════════════════════════════════════════════════════════════════════════════
echo -e "${AMBER}${BOLD}┌─ Step 1: Activation Key ───────────────────────────────────┐${RESET}"
echo ""
say "Get your free key at: ${CYAN}$SERVER${RESET}"
echo ""

JAMILA_KEY=""

if [ -f "$KEY_FILE" ]; then
  EXISTING=$(cat "$KEY_FILE")
  echo -e "  Found existing key: ${CYAN}${EXISTING:0:15}…${RESET}"
  echo -n "  Use this key? (Y/n): "
  read -r REUSE
  if [[ ! "$REUSE" =~ ^[Nn] ]]; then
    JAMILA_KEY="$EXISTING"
  fi
fi

if [ -z "$JAMILA_KEY" ]; then
  echo ""
  echo -e "  ${BOLD}Enter your Jamila activation key${RESET}"
  echo -e "  ${DIM}Format: JML-XXXXXXXX-XXXXXXXX-XXXXXXXX${RESET}"
  echo -n "  Key: "
  read -r JAMILA_KEY
  JAMILA_KEY=$(echo "$JAMILA_KEY" | tr -d '[:space:]')
fi

if [ -z "$JAMILA_KEY" ]; then
  err "No key provided. Get one at $SERVER"
  exit 1
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2 — Verify key online
# ═══════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${AMBER}${BOLD}┌─ Step 2: Verifying your key ───────────────────────────────┐${RESET}"
echo ""
say "Connecting to Jamila server…"

HTTP=$(curl -s -o /tmp/jamila_verify.json -w "%{http_code}" \
  -X POST "$SERVER/verify-key" \
  -H "Content-Type: application/json" \
  -d "{\"key\": \"$JAMILA_KEY\"}" 2>/dev/null || echo "000")

if [ "$HTTP" = "000" ]; then
  err "Cannot reach server. Check your internet connection."
  echo -n "  Continue offline anyway? (y/N): "
  read -r CONT
  [[ "$CONT" =~ ^[Yy] ]] || exit 1
elif [ "$HTTP" = "200" ]; then
  VALID=$(python3 -c "import json; d=json.load(open('/tmp/jamila_verify.json')); print(d.get('valid','false'))" 2>/dev/null || echo "false")
  if [ "$VALID" = "True" ] || [ "$VALID" = "true" ]; then
    PLAN=$(python3 -c "import json; d=json.load(open('/tmp/jamila_verify.json')); print(d.get('plan','free'))" 2>/dev/null)
    NAME=$(python3 -c "import json; d=json.load(open('/tmp/jamila_verify.json')); print(d.get('name',''))" 2>/dev/null)
    ok "Key verified!"
    [ -n "$NAME" ] && echo -e "  ${BOLD}Welcome, ${AMBER}$NAME${RESET} ${DIM}(plan: $PLAN)${RESET}"
  else
    MSG=$(python3 -c "import json; d=json.load(open('/tmp/jamila_verify.json')); print(d.get('message','Invalid key'))" 2>/dev/null)
    err "Key rejected: $MSG"
    echo ""
    dim "Get a valid key at: $SERVER"
    exit 1
  fi
else
  err "Server error (HTTP $HTTP). Try again."
  exit 1
fi

# Save key
echo "$JAMILA_KEY" > "$KEY_FILE"
chmod 600 "$KEY_FILE"
ok "Key saved to $KEY_FILE"

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3 — System packages
# ═══════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${AMBER}${BOLD}┌─ Step 3: System packages ──────────────────────────────────┐${RESET}"
echo ""
say "Installing system dependencies…"

if command -v apt-get &>/dev/null; then
  sudo apt-get update -qq 2>&1 | tail -2
  sudo apt-get install -y \
    python3 python3-pip python3-venv python3-dev \
    python3-gi python3-gi-cairo gir1.2-gtk-3.0 \
    portaudio19-dev libsndfile1 libffi-dev libssl-dev \
    espeak-ng ffmpeg mpv xdotool pulseaudio-utils \
    build-essential wget curl git 2>&1 | grep -E '(installed|upgraded|error)' | head -10
  ok "apt packages ready"

elif command -v dnf &>/dev/null; then
  sudo dnf install -y \
    python3 python3-pip python3-gobject gtk3 \
    portaudio-devel libsndfile espeak-ng ffmpeg mpv \
    xdotool pulseaudio-utils gcc openssl-devel 2>&1 | tail -5
  ok "dnf packages ready"

elif command -v pacman &>/dev/null; then
  sudo pacman -Sy --noconfirm \
    python python-pip python-gobject gtk3 \
    portaudio libsndfile espeak-ng ffmpeg mpv \
    xdotool pulseaudio base-devel 2>&1 | tail -5
  ok "pacman packages ready"

else
  echo -e "${AMBER}  Unknown package manager — skipping system packages.${RESET}"
  dim "You may need to install: python3, portaudio, espeak-ng, GTK3 manually."
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 4 — Python virtual environment
# ═══════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${AMBER}${BOLD}┌─ Step 4: Python environment ───────────────────────────────┐${RESET}"
echo ""
say "Setting up Python virtual environment…"
mkdir -p "$JAMILA_HOME"
[ ! -d "$VENV" ] && python3 -m venv "$VENV"
source "$VENV/bin/activate"
pip install --upgrade pip -q
ok "Virtual environment ready"

say "Installing Python packages…"
pip install -q \
  requests \
  SpeechRecognition \
  PyAudio \
  pyttsx3 \
  sounddevice \
  soundfile \
  numpy \
  Pillow
ok "Base packages installed"

say "Installing Coqui TTS — natural neural voice (this may take a few minutes)…"
dim "Coqui TTS uses ~80MB model for warm, human-sounding speech."
if pip install -q TTS 2>/dev/null; then
  ok "Coqui TTS installed ✦ Natural voice active"
else
  echo -e "${AMBER}  Coqui TTS install failed — pyttsx3 will be used instead (still works fine)${RESET}"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 5 — Copy Jamila files
# ═══════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${AMBER}${BOLD}┌─ Step 5: Installing Jamila ────────────────────────────────┐${RESET}"
echo ""
say "Copying files to $JAMILA_HOME…"
cp -r "$SCRIPT_DIR"/* "$JAMILA_HOME/"
chmod +x "$JAMILA_HOME/jamila_core.py"
ok "Jamila files installed"

# Credentials template
if [ ! -f "$CREDS_FILE" ]; then
  cat > "$CREDS_FILE" << 'CREDS'
{
  "_info": "Edit this file to enable email features. Never share it.",
  "smtp": {
    "host":     "smtp.gmail.com",
    "port":     587,
    "username": "your@gmail.com",
    "password": "your-app-password"
  }
}
CREDS
  chmod 600 "$CREDS_FILE"
  dim "Created $CREDS_FILE — edit it to enable email sending."
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 6 — Create launcher + desktop shortcut
# ═══════════════════════════════════════════════════════════════════════════════
echo ""
say "Creating launchers…"

# Shell command
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/jamila" << LAUNCHER
#!/usr/bin/env bash
source "$VENV/bin/activate"
python3 "$JAMILA_HOME/jamila_core.py" "\$@"
LAUNCHER
chmod +x "$HOME/.local/bin/jamila"

# Add to PATH if needed
if ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
  dim "Added ~/.local/bin to PATH in .bashrc"
fi

# Desktop entry
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/jamila.desktop" << DESKTOP
[Desktop Entry]
Name=Jamila
Comment=Voice-first AI assistant for the blind
Exec=$HOME/.local/bin/jamila
Icon=$JAMILA_HOME/jamila.png
Terminal=false
Type=Application
Categories=Accessibility;Utility;
Keywords=voice;ai;assistant;blind;accessibility;
StartupNotify=true
DESKTOP

ok "Launcher + desktop shortcut created"

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 7 — Pre-download voice model (background)
# ═══════════════════════════════════════════════════════════════════════════════
echo ""
say "Pre-loading Coqui TTS voice model in the background…"
dim "This ensures Jamila's voice is instant on first launch."
(
  source "$VENV/bin/activate"
  python3 -c "
try:
    from TTS.api import TTS
    TTS(model_name='tts_models/en/ljspeech/tacotron2-DDC', progress_bar=False, gpu=False)
    print('  [Voice] Model ready')
except Exception as e:
    print(f'  [Voice] {e}')
" 2>/dev/null &
)

# ═══════════════════════════════════════════════════════════════════════════════
# DONE
# ═══════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${AMBER}${BOLD}╔══════════════════════════════════════════════════════════╗"
echo    "║                                                          ║"
echo    "║   ✦  Jamila is installed and ready!                     ║"
echo    "║                                                          ║"
echo    "╚══════════════════════════════════════════════════════════╝"
echo -e "${RESET}"
echo -e "  ${BOLD}Start Jamila:${RESET}  ${CYAN}jamila${RESET}"
echo -e "  ${BOLD}Or:${RESET}           Find Jamila in your Applications menu"
echo ""
echo -e "  ${DIM}Dashboard:  $SERVER/dashboard.html${RESET}"
echo -e "  ${DIM}Key file:   ~/.jamila_key${RESET}"
echo ""
echo -e "${GREEN}${BOLD}  Speak up — Jamila is listening. ✦${RESET}"
echo ""
