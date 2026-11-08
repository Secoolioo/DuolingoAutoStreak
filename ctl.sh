#!/usr/bin/env bash
# Bot-Befehle auf dem Server als Service-User, z. B.: ./ctl.sh run-now | status | resume | plan
DIR="$(cd "$(dirname "$0")" && pwd)"
exec runuser -u duobot -- env HOME="$DIR/data" PLAYWRIGHT_BROWSERS_PATH="$DIR/browsers" \
  "$DIR/.venv/bin/python" "$DIR/bot.py" "$@"
