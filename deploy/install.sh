#!/usr/bin/env bash
# Install / update jelpricajos on an Ubuntu 22.04/24.04 VPS (x86_64 or arm64).
#
# Run from the unpacked project folder:
#   sudo bash deploy/install.sh                 # install or update
#   sudo CF_TUNNEL_TOKEN=xxxx bash deploy/install.sh   # + permanent Cloudflare tunnel
#
# Safe to re-run: it updates code and dependencies in place and keeps the database,
# the env file and the cookies.
set -euo pipefail

APP_NAME="jelpricajos"
APP_USER="jelpricajos"
APP_DIR="/opt/${APP_NAME}"
DATA_DIR="/var/lib/${APP_NAME}"
ENV_FILE="/etc/${APP_NAME}.env"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ -f "${SRC_DIR}/app.py" ]] || die "run this from the project folder (app.py not found in ${SRC_DIR})"
command -v apt-get >/dev/null || die "this script supports Debian/Ubuntu only"

ARCH="$(dpkg --print-architecture)"   # amd64 | arm64

# ---------------------------------------------------------------- swap (small VPS)
mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if (( mem_mb < 2500 )) && ! swapon --show | grep -q .; then
  log "Low RAM (${mem_mb} MB): creating a 2 GB swap file"
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
if (( mem_mb < 2500 )); then
  # Prefer keeping the app in RAM; use swap only as a safety net.
  echo 'vm.swappiness=10' > /etc/sysctl.d/90-jelpricajos-swap.conf
  sysctl -q -p /etc/sysctl.d/90-jelpricajos-swap.conf || true
fi

# ---------------------------------------------------------------- system packages
log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ffmpeg curl unzip ca-certificates rsync >/dev/null

PY_OK=$(python3 -c 'import sys; print(int(sys.version_info >= (3, 10)))')
[[ "$PY_OK" == "1" ]] || die "Python 3.10+ required (found $(python3 --version))"

# ---------------------------------------------------------------- time zone
# "Today" statistics and announced session times use the server's local time.
# VPS images default to UTC; use Serbian time unless TIMEZONE says otherwise.
TIMEZONE="${TIMEZONE:-Europe/Belgrade}"
if [[ "$(timedatectl show -p Timezone --value 2>/dev/null)" != "$TIMEZONE" ]]; then
  log "Setting time zone to ${TIMEZONE}"
  timedatectl set-timezone "$TIMEZONE" || echo "  (could not set time zone)"
fi

# ---------------------------------------------------------------- server hardening
# Opt out with HARDEN=0.
if [[ "${HARDEN:-1}" == "1" ]]; then
  log "Hardening: automatic security updates + fail2ban for SSH"
  apt-get install -y -qq unattended-upgrades fail2ban python3-systemd >/dev/null
  cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
  systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true

  # Ban IPs that keep failing SSH logins (5 failures in 10 min -> 1 h ban).
  cat > /etc/fail2ban/jail.d/jelpricajos-sshd.local <<'EOF'
[sshd]
enabled  = true
backend  = systemd
maxretry = 5
findtime = 10m
bantime  = 1h
EOF
  systemctl enable fail2ban >/dev/null 2>&1 || true
  systemctl restart fail2ban || echo "  (fail2ban did not start; check: journalctl -u fail2ban)"

  # Key-only SSH, but ONLY if the account running this script already logs in with a key,
  # so a password-based setup can never be locked out.
  LOGIN_USER="${SUDO_USER:-root}"
  LOGIN_HOME="$(getent passwd "$LOGIN_USER" | cut -d: -f6)"
  if [[ -s "${LOGIN_HOME}/.ssh/authorized_keys" ]]; then
    mkdir -p /etc/ssh/sshd_config.d
    cat > /etc/ssh/sshd_config.d/10-jelpricajos-hardening.conf <<'EOF'
# Added by jelpricajos deploy/install.sh
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
MaxAuthTries 4
EOF
    if sshd -t 2>/dev/null; then
      systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
      echo "  SSH: password login disabled (key-only)"
    else
      rm -f /etc/ssh/sshd_config.d/10-jelpricajos-hardening.conf
      echo "  SSH: config check failed, left unchanged"
    fi
  else
    echo "  SSH: no authorized_keys for ${LOGIN_USER}; password login left enabled."
    echo "       Set up an SSH key, then re-run this script to switch to key-only login."
  fi
fi

# Deno: the JavaScript runtime yt-dlp uses for YouTube extraction.
if ! command -v deno >/dev/null; then
  log "Installing Deno (for yt-dlp)"
  curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y >/dev/null \
    || echo "  (Deno install failed — yt-dlp still works for live HLS, just with a warning)"
fi

# ---------------------------------------------------------------- user + folders
if ! id "$APP_USER" >/dev/null 2>&1; then
  log "Creating system user ${APP_USER}"
  useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi
mkdir -p "$APP_DIR" "$DATA_DIR"

log "Copying application to ${APP_DIR}"
rsync -a --delete \
  --exclude '.venv/' --exclude 'data/' --exclude '__pycache__/' --exclude '.pytest_cache/' \
  --exclude 'extension/' --exclude 'cookies.txt' --exclude '*.pyc' \
  "${SRC_DIR}/" "${APP_DIR}/"

# Bring along an existing database (first install only, never overwrites).
if [[ -f "${SRC_DIR}/data/speeches.db" && ! -f "${DATA_DIR}/speeches.db" ]]; then
  log "Importing existing speech history"
  cp "${SRC_DIR}/data/speeches.db" "${DATA_DIR}/speeches.db"
fi
if [[ -f "${SRC_DIR}/cookies.txt" ]]; then
  install -m 600 "${SRC_DIR}/cookies.txt" "${DATA_DIR}/cookies.txt"
fi
chown -R "$APP_USER:$APP_USER" "$DATA_DIR"
chown -R root:root "$APP_DIR"

# ---------------------------------------------------------------- Python env
log "Setting up Python environment (first run downloads ~1 GB, takes a few minutes)"
[[ -d "${APP_DIR}/.venv" ]] || python3 -m venv "${APP_DIR}/.venv"
PIP="${APP_DIR}/.venv/bin/pip"
"$PIP" install -q --upgrade pip wheel
if [[ "$ARCH" == "amd64" ]]; then
  # CPU-only PyTorch: avoids pulling ~2.5 GB of CUDA libraries a VPS can't use.
  "$PIP" install -q torch torchvision --index-url https://download.pytorch.org/whl/cpu
fi
"$PIP" install -q -r "${APP_DIR}/requirements.txt"
"$PIP" install -q --upgrade yt-dlp   # YouTube changes often; always take the latest

# ---------------------------------------------------------------- config
if [[ ! -f "$ENV_FILE" ]]; then
  log "Writing ${ENV_FILE}"
  install -m 640 -o root -g "$APP_USER" "${SRC_DIR}/deploy/jelpricajos.env.example" "$ENV_FILE"
fi
# Use cookies automatically if present and not configured yet.
if [[ -f "${DATA_DIR}/cookies.txt" ]] && ! grep -q '^YTDLP_COOKIES_FILE=' "$ENV_FILE"; then
  echo "YTDLP_COOKIES_FILE=${DATA_DIR}/cookies.txt" >> "$ENV_FILE"
fi

# ---------------------------------------------------------------- OCR models
log "Downloading EasyOCR models"
sudo -u "$APP_USER" HOME="$DATA_DIR" "${APP_DIR}/.venv/bin/python" -c \
  "import easyocr; easyocr.Reader(['rs_cyrillic'], gpu=False, verbose=False)" \
  || die "EasyOCR model download failed"

# ---------------------------------------------------------------- systemd
log "Installing systemd service"
install -m 644 "${SRC_DIR}/deploy/jelpricajos.service" "/etc/systemd/system/${APP_NAME}.service"
systemctl daemon-reload
systemctl enable "${APP_NAME}" >/dev/null
systemctl restart "${APP_NAME}"

# ---------------------------------------------------------------- Cloudflare tunnel
if ! command -v cloudflared >/dev/null; then
  log "Installing cloudflared"
  curl -fsSL -o /tmp/cloudflared.deb \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}.deb"
  dpkg -i /tmp/cloudflared.deb >/dev/null && rm -f /tmp/cloudflared.deb
fi

if [[ -n "${CF_TUNNEL_TOKEN:-}" ]]; then
  log "Configuring permanent Cloudflare tunnel"
  systemctl disable --now jelpricajos-tunnel 2>/dev/null || true
  if systemctl list-unit-files | grep -q '^cloudflared.service'; then
    cloudflared service uninstall >/dev/null 2>&1 || true
  fi
  cloudflared service install "$CF_TUNNEL_TOKEN"
  TUNNEL_MODE="permanent"
elif ! systemctl is-enabled cloudflared >/dev/null 2>&1; then
  # Only (re)start the quick tunnel when it is new or its unit changed: every restart
  # creates a NEW random URL, which would break links people already have.
  UNIT=/etc/systemd/system/jelpricajos-tunnel.service
  if ! cmp -s "${SRC_DIR}/deploy/jelpricajos-tunnel.service" "$UNIT"; then
    log "Starting a temporary quick tunnel (random *.trycloudflare.com URL)"
    install -m 644 "${SRC_DIR}/deploy/jelpricajos-tunnel.service" "$UNIT"
    systemctl daemon-reload
    systemctl enable jelpricajos-tunnel >/dev/null
    systemctl restart jelpricajos-tunnel
  else
    log "Quick tunnel already running: keeping its URL"
    systemctl enable --now jelpricajos-tunnel >/dev/null
  fi
  TUNNEL_MODE="quick"
else
  TUNNEL_MODE="permanent"
fi

# ---------------------------------------------------------------- health check
log "Waiting for the app to come up"
for _ in $(seq 1 90); do
  if curl -fsS http://127.0.0.1:8000/api/v1/health >/dev/null 2>&1; then break; fi
  sleep 2
done
if ! curl -fsS http://127.0.0.1:8000/api/v1/health; then
  journalctl -u "$APP_NAME" -n 40 --no-pager
  die "the app did not start — see the log above"
fi
echo

if [[ "$TUNNEL_MODE" == "quick" ]]; then
  URL=""
  for _ in $(seq 1 30); do
    URL=$(journalctl -u jelpricajos-tunnel --no-pager -o cat 2>/dev/null \
          | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1 || true)
    [[ -n "$URL" ]] && break
    sleep 2
  done
  log "Done. Public URL (changes when the tunnel restarts): ${URL:-run: sudo bash ${APP_DIR}/deploy/status.sh}"
else
  log "Done. Reachable at the hostname configured for your Cloudflare tunnel."
fi
echo "  Status:  sudo bash ${APP_DIR}/deploy/status.sh"
echo "  Logs:    journalctl -u ${APP_NAME} -f"
echo "  Config:  ${ENV_FILE}  (then: sudo systemctl restart ${APP_NAME})"
