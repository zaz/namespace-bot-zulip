#!/bin/bash
# bootstrap-head.sh — turn a fresh Namespace devbox into the Zulip bot head. Idempotent.
# Create the box with: devbox create --name zulip-head --size s --access_mode shared \
#   --auto_stop_idle_timeout 0s --no_checkout --image_ref <base image>
# Secrets are NOT here. They must arrive separately (scp, never via chat) at:
#   ~/shell-bot/.env   ~/zulip-ai-bot/zuliprc   ~/.config/ns/config.json + token.json
# Also copy in: ~/.local/bin/devbox (binary), ~/agent-workspace/CLAUDE.md
set -e
export GIT_TERMINAL_PROMPT=0   # GitHub sometimes challenges anonymous fetches; fail fast, retry below
REPO=https://github.com/zaz/namespace-bot-zulip.git
BRANCH=${HEAD_BRANCH:-feat/claude-code-fleet}
cd /home/devbox
mkdir -p .local/bin zulip-ai-bot .config/ns agent-workspace .namespace/ssh
if [ ! -d shell-bot/.git ]; then
  # A pre-placed ~/shell-bot/.env (secrets arrive before code) must survive the clone.
  for i in 1 2 3 4 5 6; do git clone -q -b "$BRANCH" "$REPO" shell-bot.new && break; rm -rf shell-bot.new; sleep 5; done
  [ -d shell-bot.new/.git ] || { echo "clone failed after retries"; exit 1; }
  [ -d shell-bot ] && cp -a shell-bot/. shell-bot.new/
  rm -rf shell-bot && mv shell-bot.new shell-bot
fi
cd shell-bot
for i in 1 2 3 4 5; do git fetch -q && break || sleep 5; done
git checkout -q "$BRANCH"; git pull -q --ff-only 2>/dev/null || echo "bootstrap-head: pull skipped (GitHub unreachable); using local checkout"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt zulip anthropic
chmod +x infra/*.sh infra/fleet-exec 2>/dev/null || true
cp infra/run-forever.sh /home/devbox/shell-bot/run-forever.sh
cp infra/fleet-exec /home/devbox/.local/bin/fleet-exec
cp infra/watch-workhorse.sh /home/devbox/watch-workhorse.sh
chmod +x /home/devbox/shell-bot/run-forever.sh /home/devbox/.local/bin/fleet-exec /home/devbox/watch-workhorse.sh
[ -x /home/devbox/.local/bin/claude ] || curl -fsSL https://claude.ai/install.sh | bash >/dev/null
echo "bootstrap-head: done (secrets present: $(ls .env ../zulip-ai-bot/zuliprc ../.config/ns/token.json 2>/dev/null | wc -l)/3)"
