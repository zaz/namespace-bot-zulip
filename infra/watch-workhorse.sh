#!/bin/bash
# watch-workhorse.sh — runs on the head (started by the devbox spec's `watchdog`
# session on every boot). Every minute: ssh to the workhorse (wakes it if stopped,
# counts as activity) and restart its watch-head.sh if it died.
#
# Stagger: this workspace stops every instance 3h after it boots. If both boxes
# booted within 15 min of each other they will also STOP together, and then
# nothing wakes either. When that alignment is detected, stop the workhorse and
# hold off waking it for 90 min so the two 3h clocks end up offset. (An agent
# turn via fleet-exec may wake it earlier; that is fine — it also de-aligns.)
WH=${WORKHORSE_BOX:-fleet-workhorse.devbox.namespace}
LOG=/home/devbox/watch-workhorse.log
LOCK=/home/devbox/.watch-workhorse.pid
HOLD=/home/devbox/.watch-workhorse.hold   # epoch seconds until which we must not wake WH
if [ -f "$LOCK" ]; then o=$(cat "$LOCK"); kill -0 "$o" 2>/dev/null && grep -q watch-workhorse "/proc/$o/cmdline" 2>/dev/null && { echo "already running ($o)"; exit 0; }; fi
echo $$ > "$LOCK"
export PATH="/home/devbox/.local/bin:$PATH"

holding() { [ -f "$HOLD" ] && [ "$(date +%s)" -lt "$(cat "$HOLD")" ]; }

# Keep-alive: persistent ssh session into the peer (paused while holding).
( while true; do
    if holding; then sleep 30; continue; fi
    timeout 3700 ssh -o BatchMode=yes -o ConnectTimeout=120 -o StrictHostKeyChecking=no -o ServerAliveInterval=30 "$WH" 'sleep 3600' >/dev/null 2>&1
    sleep 10
  done ) &

while true; do
  if holding; then sleep 60; continue; fi
  out=$(timeout 240 ssh -o BatchMode=yes -o ConnectTimeout=120 -o StrictHostKeyChecking=no "$WH" '
    echo WH-BOOT=$(date -d "$(uptime -s)" +%s)
    if pgrep -f "[w]atch-head" >/dev/null; then echo WD-UP; else setsid nohup /home/devbox/watch-head.sh >/dev/null 2>&1 & echo WD-RESTARTED; fi' 2>&1)
  case "$out" in
    *RESTARTED*) echo "$(date -Is) $out" | tr '\n' ' ' >> "$LOG"; echo >> "$LOG" ;;
    *WD-UP*) ;;
    *) echo "$(date -Is) ssh failed: ${out: -200}" >> "$LOG" ;;
  esac
  wh_boot=$(echo "$out" | sed -n 's/^WH-BOOT=//p' | head -1)
  my_boot=$(date -d "$(uptime -s)" +%s)
  now=$(date +%s)
  if [ -n "$wh_boot" ] && [ $((now - my_boot)) -gt 600 ]; then
    diff=$((wh_boot - my_boot)); [ $diff -lt 0 ] && diff=$((-diff))
    if [ $diff -lt 900 ]; then
      echo "$(date -Is) boot clocks aligned (diff ${diff}s): stopping workhorse, holding 90 min to stagger" >> "$LOG"
      echo $((now + 5400)) > "$HOLD"
      devbox stop "${WH%%.*}" --force >/dev/null 2>&1 || echo "$(date -Is) devbox stop failed" >> "$LOG"
    fi
  fi
  sleep 60
done
