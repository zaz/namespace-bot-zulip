#!/bin/bash
# bootstrap-head.sh — turn a fresh Namespace devbox into the Zulip bot head. Idempotent.
# Create the box with: devbox create --name zulip-head --size s --access_mode shared \
#   --auto_stop_idle_timeout 0s --no_checkout --image_ref <base image>
# Secrets are NOT here. They must arrive separately (scp, never via chat) at:
#   ~/shell-bot/.env   ~/zulip-ai-bot/zuliprc   ~/.config/ns/config.json + token.json
# Also copy in: ~/.local/bin/devbox (binary), ~/agent-workspace/CLAUDE.md
set -e
REPO=https://github.com/zaz/namespace-bot-zulip.git
BRANCH=${HEAD_BRANCH:-feat/claude-code-fleet}
cd /home/devbox
mkdir -p .local/bin zulip-ai-bot .config/ns agent-workspace .namespace/ssh
[ -d shell-bot/.git ] || git clone -q -b "$BRANCH" "$REPO" shell-bot
cd shell-bot
git fetch -q && git checkout -q "$BRANCH" && git pull -q --ff-only
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt zulip anthropic
chmod +x infra/*.sh infra/fleet-exec 2>/dev/null || true
cp infra/run-forever.sh /home/devbox/shell-bot/run-forever.sh
cp infra/fleet-exec /home/devbox/.local/bin/fleet-exec
cp infra/watch-workhorse.sh /home/devbox/watch-workhorse.sh
chmod +x /home/devbox/shell-bot/run-forever.sh /home/devbox/.local/bin/fleet-exec /home/devbox/watch-workhorse.sh
[ -x /home/devbox/.local/bin/claude ] || curl -fsSL https://claude.ai/install.sh | bash >/dev/null
echo "bootstrap-head: done (secrets present: $(ls .env ../zulip-ai-bot/zuliprc ../.config/ns/token.json 2>/dev/null | wc -l)/3)"
