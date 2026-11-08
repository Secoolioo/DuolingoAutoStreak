#!/usr/bin/env bash
# Spielt den Code auf den Server (Ziel aus deploy.env). Laufzeitdaten auf dem Server bleiben unberührt.
set -euo pipefail
cd "$(dirname "$0")"
source deploy.env
rsync -a --delete --chown=root:root -e "ssh -i $SSH_KEY" --exclude-from=.gitignore --exclude .git \
  ./ "$DEPLOY_USER@$DEPLOY_HOST:$DEPLOY_DIR/"
ssh -i "$SSH_KEY" "$DEPLOY_USER@$DEPLOY_HOST" \
  "systemctl is-enabled -q duolingo-streak-bot 2>/dev/null && systemctl restart duolingo-streak-bot || true"
echo "Deploy nach $DEPLOY_HOST:$DEPLOY_DIR fertig."
