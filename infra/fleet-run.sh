#!/bin/bash
# fleet-run.sh — lives on the workhorse. Receives one base64 argument encoding
# the NUL-separated argv for claude, decodes it into a real array, and execs.
# Base64 is shell-metacharacter-free, so the ssh command line that invokes this
# is safe under any remote shell (dash or bash) regardless of the task content.
set -a; . ~/.fleet-env; set +a
cd ~/agent-workspace || exit 1
mapfile -d '' A < <(printf %s "$1" | base64 -d)
exec ~/.local/bin/claude "${A[@]}"
