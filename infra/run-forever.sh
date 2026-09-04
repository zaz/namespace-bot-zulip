#!/bin/bash
# run-forever.sh — supervisor for the Zulip shell bot (started by the devbox spec's
# `bot` session on every boot). Restarts the bot if it exits. Single instance is
# enforced with flock (pid files misfire after reboots: pids recycle and the tmux
# wrapper's own command line mentions this script).
exec 9>/home/devbox/shell-bot/.supervisor.lock
flock -n 9 || { echo "supervisor already running"; exit 0; }
cd /home/devbox/shell-bot || exit 1
# Namespace idle detection: any file under /.namespace/tasks marks the devbox busy.
mkdir -p /.namespace/tasks 2>/dev/null; echo 'zulip bot service' > /.namespace/tasks/zulip-bot 2>/dev/null
export PATH="/home/devbox/.local/bin:$PATH"
while true; do
  set -a; . ./.env; set +a
  echo "[supervisor] starting bot $(date -Is)" >> shell_bot.log
  .venv/bin/python -u shell_bot.py >> shell_bot.log 2>&1
  echo "[supervisor] bot exited ($?) $(date -Is); restarting in 5s" >> shell_bot.log
  sleep 5
done
