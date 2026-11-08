#!/usr/bin/env bash
# Einmalige Einrichtung auf dem Debian-Server, als root im Projektordner: ./install-vm.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
apt-get update -qq
apt-get install -y -qq python3-venv rsync >/dev/null
id duobot &>/dev/null || useradd --system --home-dir "$DIR/data" --shell /usr/sbin/nologin duobot
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
PLAYWRIGHT_BROWSERS_PATH="$DIR/browsers" "$DIR/.venv/bin/playwright" install --with-deps chromium
mkdir -p "$DIR/data" "$DIR/logs" "$DIR/debug"
chown -R duobot:duobot "$DIR/data" "$DIR/logs" "$DIR/debug"
chmod 700 "$DIR/data"
chmod -R a+rX "$DIR/browsers" "$DIR/.venv"
sed "s|@DIR@|$DIR|g" "$DIR/duolingo-streak-bot.service" > /etc/systemd/system/duolingo-streak-bot.service
systemctl daemon-reload
echo "Fertig. Start: systemctl enable --now duolingo-streak-bot"
