#!/usr/bin/env bash
# Provision a second SV watcher vantage point on a Raspberry Pi (arm64).
#
# Usage:
#   sudo bash install-rpi.sh /path/to/sv-source
#
# Playwright ships no arm64 Linux Chromium build, so this uses the distro
# Chromium via PLAYWRIGHT_CHROMIUM_EXECUTABLE / channel instead.
set -euo pipefail

SRC="${1:-.}"
APP_DIR=/opt/sv
SERVICE=sv-watcher.service
RUN_USER="${SUDO_USER:-$(id -un)}"

if [[ "$(uname -m)" != "aarch64" ]]; then
  echo "WARNING: expected aarch64 (64-bit Raspberry Pi OS / Ubuntu arm64)." >&2
fi

apt-get update
apt-get install -y python3 python3-venv python3-pip xvfb chromium fonts-liberation \
  libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
  libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2

CHROMIUM_BIN="$(command -v chromium || command -v chromium-browser)"
if [[ -z "$CHROMIUM_BIN" ]]; then
  echo "ERROR: no chromium binary found." >&2
  exit 1
fi

mkdir -p "$APP_DIR" "$APP_DIR/data"
cp -r "$SRC"/*.py "$SRC"/requirements.txt "$APP_DIR"/
[[ -f "$APP_DIR/.env" ]] || cp "$SRC/config.example.env" "$APP_DIR/.env"

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
# Playwright's own browser download is skipped: we drive the system Chromium.
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"

cat > "/etc/systemd/system/$SERVICE" <<EOF
[Unit]
Description=SV appointment watcher (Raspberry Pi vantage point)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
Environment=DISPLAY=:99
Environment=PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=$CHROMIUM_BIN
ExecStart=/usr/bin/xvfb-run -a --server-args="-screen 0 1280x800x24" $APP_DIR/.venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE"

cat <<EOF

Installed. Before this node is useful, edit $APP_DIR/.env and set:

  TELEGRAM_BOT_TOKEN=...        # same bot as the LXC node
  TELEGRAM_CHAT_ID=...          # same chat, so both nodes alert to one place
  NODE_NAME=rpi-home2           # anything unique; alerts are prefixed with it
  SCHEDULE_OFFSET_SECONDS=150   # ~half of SLOW_INTERVAL_SECONDS, so the two
                                # nodes interleave instead of polling together
  DATABASE_PATH=$APP_DIR/data/sv.db

Then: sudo systemctl restart $SERVICE && journalctl -u $SERVICE -f
EOF
