#!/bin/bash
# watch-workhorse.sh — runs on the head. Every 5 min: ssh to the workhorse (wakes it
# if stopped, counts as activity) and restart its watch-head.sh if it died.
WH=${WORKHORSE_BOX:-fleet-workhorse.devbox.namespace}
LOG=/home/devbox/watch-workhorse.log
LOCK=/home/devbox/.watch-workhorse.pid
if [ -f "$LOCK" ]; then o=$(cat "$LOCK"); kill -0 "$o" 2>/dev/null && grep -q watch-workhorse "/proc/$o/cmdline" 2>/dev/null && { echo "already running ($o)"; exit 0; }; fi
echo $$ > "$LOCK"
while true; do
  out=$(ssh -o BatchMode=yes -o ConnectTimeout=120 -o StrictHostKeyChecking=no "$WH" '
    if pgrep -f "[w]atch-head" >/dev/null; then echo WD-UP; else setsid nohup /home/devbox/watch-head.sh >/dev/null 2>&1 & echo WD-RESTARTED; fi' 2>&1)
  case "$out" in
    *RESTARTED*) echo "$(date -Is) $out" >> "$LOG" ;;
    *WD-UP*) ;;
    *) echo "$(date -Is) ssh failed: ${out: -200}" >> "$LOG" ;;
  esac
  sleep 300
done
