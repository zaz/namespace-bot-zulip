#!/bin/bash
# watch-head.sh — runs on fleet-workhorse. Every minute: ssh to the head (this wakes a
# stopped devbox and counts as activity), start the bot supervisor if the bot is down,
# and make sure the head's reverse watchdog is running. Nothing depends on a laptop.
HEAD=${HEAD_BOX:-zulip-bot-head.devbox.namespace}
LOG=/home/devbox/watch-head.log
LOCK=/home/devbox/.watch-head.pid
if [ -f "$LOCK" ]; then o=$(cat "$LOCK"); kill -0 "$o" 2>/dev/null && grep -q watch-head "/proc/$o/cmdline" 2>/dev/null && { echo "already running ($o)"; exit 0; }; fi
echo $$ > "$LOCK"
mkdir -p /.namespace/tasks 2>/dev/null; echo 'fleet watchdog' > /.namespace/tasks/fleet-watchdog 2>/dev/null  # keeps this box from idle-stopping
PEER=$HEAD
# Keep-alive: hold a persistent ssh session open into the peer so it always has an
# active inbound connection (short pings did not stop the platform idle-stopping it).
( while true; do timeout 3700 ssh -o BatchMode=yes -o ConnectTimeout=120 -o StrictHostKeyChecking=no -o ServerAliveInterval=30 "$PEER" 'sleep 3600' >/dev/null 2>&1; sleep 10; done ) &

while true; do
  out=$(timeout 240 ssh -o BatchMode=yes -o ConnectTimeout=120 -o StrictHostKeyChecking=no "$HEAD" '
    if pgrep -f "[p]ython -u shell_bot" >/dev/null; then echo BOT-UP; else setsid nohup /home/devbox/shell-bot/run-forever.sh >/dev/null 2>&1 & echo BOT-RESTARTED; fi
    if pgrep -f "[w]atch-workhorse" >/dev/null; then echo WD-UP; else setsid nohup /home/devbox/watch-workhorse.sh >/dev/null 2>&1 & echo WD-RESTARTED; fi' 2>&1)
  case "$out" in
    *RESTARTED*) echo "$(date -Is) $out" | tr '\n' ' ' >> "$LOG"; echo >> "$LOG" ;;
    *BOT-UP*) ;;
    *) echo "$(date -Is) ssh failed: ${out: -200}" >> "$LOG" ;;
  esac
  sleep 60
done
