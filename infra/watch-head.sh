#!/bin/bash
# watch-head.sh — runs on fleet-workhorse. Every minute: ssh to the head (wakes a
# stopped devbox, counts as activity), start the bot supervisor if it is not
# running, and start the head's reverse watchdog if it died. Liveness is probed
# with flock -n on each daemon's lock file — no process-name matching, which
# self-matched the probe's own command line in earlier versions.
HEAD=${HEAD_BOX:-zulip-bot-head.devbox.namespace}
LOG=/home/devbox/watch-head.log
exec 9>/home/devbox/.watch-head.lock
flock -n 9 || { echo "already running"; exit 0; }
mkdir -p /.namespace/tasks 2>/dev/null; echo 'fleet watchdog' > /.namespace/tasks/fleet-watchdog 2>/dev/null
( while true; do
    timeout 3700 ssh -o BatchMode=yes -o ConnectTimeout=120 -o StrictHostKeyChecking=no -o ServerAliveInterval=30 "$HEAD" 'sleep 3600' >/dev/null 2>&1
    sleep 10
  done ) &
while true; do
  out=$(timeout 240 ssh -o BatchMode=yes -o ConnectTimeout=120 -o StrictHostKeyChecking=no "$HEAD" '
    if flock -n /home/devbox/shell-bot/.supervisor.lock true 2>/dev/null; then setsid nohup /home/devbox/shell-bot/run-forever.sh >/dev/null 2>&1 & echo BOT-RESTARTED; else echo BOT-UP; fi
    if flock -n /home/devbox/.watch-workhorse.lock true 2>/dev/null; then setsid nohup /home/devbox/watch-workhorse.sh >/dev/null 2>&1 & echo WD-RESTARTED; else echo WD-UP; fi' 2>&1)
  case "$out" in
    *RESTARTED*) echo "$(date -Is) $(echo "$out" | tr '\n' ' ')" >> "$LOG" ;;
    *BOT-UP*) ;;
    *) echo "$(date -Is) ssh failed: ${out: -200}" >> "$LOG" ;;
  esac
  sleep 60
done
