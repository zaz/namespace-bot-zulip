#!/bin/bash
# run-forever.sh — supervisor for the Zulip shell bot (no systemd on devboxes).
# Restarts the bot if it exits/crashes. Idempotent: refuses to start twice.
LOCK=/home/devbox/shell-bot/.supervisor.pid
if [ -f "$LOCK" ]; then
  old=$(cat "$LOCK")
  # Only honour the lock if that pid is really a supervisor (pids recycle after reboot).
  if kill -0 "$old" 2>/dev/null && grep -q run-forever "/proc/$old/cmdline" 2>/dev/null; then
    echo "supervisor already running (pid $old)"; exit 0
  fi
fi
echo $$ > "$LOCK"
cd /home/devbox/shell-bot || exit 1
export PATH="/home/devbox/.local/bin:$PATH"
while true; do
  set -a; . ./.env; set +a
  echo "[supervisor] starting bot $(date -Is)" >> shell_bot.log
  .venv/bin/python -u shell_bot.py >> shell_bot.log 2>&1
  echo "[supervisor] bot exited ($?) $(date -Is); restarting in 5s" >> shell_bot.log
  sleep 5
done
