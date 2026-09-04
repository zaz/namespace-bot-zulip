#!/bin/bash
# graceful-restart.sh — runs on the head (started by the devbox spec's `graceful`
# session on every boot). The platform kills this instance 3h after boot. Rather
# than let that land mid-request, restart the box ourselves at ~2h45m of uptime,
# at a moment when no agent turn is in flight. The peer watchdog (or the GitHub
# cron) wakes us within a minute; startup sessions bring everything back.
# Set GRACEFUL_MAX_WAIT (seconds) to cap how long we wait for an idle moment.
SELF=${SELF_BOX:-zulip-bot-head}
WH=${WORKHORSE_BOX:-fleet-workhorse.devbox.namespace}
LOG=/home/devbox/graceful-restart.log
exec 9>/home/devbox/.graceful-restart.lock
flock -n 9 || exit 0
export PATH="/home/devbox/.local/bin:$PATH"
LIMIT=$((165 * 60))            # 2h45m
MAX_WAIT=${GRACEFUL_MAX_WAIT:-600}
boot=$(date -d "$(uptime -s)" +%s)
while true; do
  now=$(date +%s)
  if [ $((now - boot)) -ge $LIMIT ]; then
    waited=0
    while [ $waited -lt $MAX_WAIT ]; do
      inflight=$(timeout 60 ssh -o BatchMode=yes -o ConnectTimeout=30 -o StrictHostKeyChecking=no "$WH" \
        'ps -eo args | grep -E "[f]leet-run|claude -p" | grep -v grep | wc -l' 2>/dev/null | tail -1)
      [ "${inflight:-0}" = "0" ] && break
      sleep 30; waited=$((waited + 30))
    done
    echo "$(date -Is) uptime $(( (now - boot) / 60 ))m: graceful stop (inflight=${inflight:-?}, waited ${waited}s)" >> "$LOG"
    devbox stop "$SELF" --force >/dev/null 2>&1 || echo "$(date -Is) devbox stop failed" >> "$LOG"
    sleep 300   # if we are still alive, the stop failed; try again next loop
  fi
  sleep 60
done
