#!/usr/bin/env bash
# Copies the code to the server (target from deploy.env). Runtime data on the server stays untouched.
set -euo pipefail
cd "$(dirname "$0")"
source deploy.env
rsync -a --delete --chown=root:root -e "ssh -i $SSH_KEY" --exclude-from=.gitignore --exclude .git \
  ./ "$DEPLOY_USER@$DEPLOY_HOST:$DEPLOY_DIR/"
ssh -i "$SSH_KEY" "$DEPLOY_USER@$DEPLOY_HOST" \
  "systemctl is-enabled -q duolingo-streak-bot 2>/dev/null && systemctl restart duolingo-streak-bot || true"
echo "Deployed to $DEPLOY_HOST:$DEPLOY_DIR."
