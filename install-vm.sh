#!/usr/bin/env bash
# One-time setup on the Debian/Ubuntu server, run as root inside the project folder: ./install-vm.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
apt-get update -qq
apt-get install -y -qq python3-venv rsync >/dev/null
id duobot &>/dev/null || useradd --system --home-dir "$DIR/data" --shell /usr/sbin/nologin duobot
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
PLAYWRIGHT_BROWSERS_PATH="$DIR/browsers" "$DIR/.venv/bin/playwright" install --with-deps chromium
mkdir -p "$DIR/data/accounts" "$DIR/logs" "$DIR/debug"
chown -R duobot:duobot "$DIR/data" "$DIR/logs" "$DIR/debug"
chmod 700 "$DIR/data" "$DIR/data/accounts"
chmod -R a+rX "$DIR/browsers" "$DIR/.venv"
sed "s|@DIR@|$DIR|g" "$DIR/duolingo-streak-bot.service" > /etc/systemd/system/duolingo-streak-bot.service
systemctl daemon-reload
echo "Done. Start with: systemctl enable --now duolingo-streak-bot"
