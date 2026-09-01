#!/usr/bin/env bash
# Build the shell bot image, push it to the Namespace workspace registry, and
# launch it on an automatically-created Namespace VM.
#
# Prereqs (one-time):
#   nsc login
#   nsc docker login
#
# Config: copy .env.example to .env and fill it in, or export these yourself:
#   ZULIP_EMAIL, ZULIP_API_KEY, ZULIP_SITE   (bot credentials)
#   SHELL_BOT_ALLOWED_SENDERS                 (comma-separated allowlist)
# Optional:
#   IMAGE_NAME    (default: zulip-shell-bot)
#   DURATION      (VM lifetime, e.g. 24h; default: 12h)
#   MACHINE_TYPE  (e.g. 2x8; default: nsc default)
set -euo pipefail

cd "$(dirname "$0")"

# Load .env if present.
if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

: "${ZULIP_EMAIL:?set ZULIP_EMAIL (bot email)}"
: "${ZULIP_API_KEY:?set ZULIP_API_KEY (bot API key)}"
: "${ZULIP_SITE:?set ZULIP_SITE (https://your-zulip.example.com)}"
: "${SHELL_BOT_ALLOWED_SENDERS:?set SHELL_BOT_ALLOWED_SENDERS (comma-separated emails)}"

IMAGE_NAME="${IMAGE_NAME:-zulip-shell-bot}"
DURATION="${DURATION:-12h}"

echo ">> Building and pushing image '$IMAGE_NAME' via Namespace remote builder..."
build_out="$(nsc build . --name "$IMAGE_NAME" --push 2>&1 | tee /dev/stderr)"

# Extract the pushed image reference (e.g. nscr.io/<workspace>/zulip-shell-bot:latest).
image_ref="$(printf '%s\n' "$build_out" | grep -oE 'nscr\.io/[^[:space:]"]+' | tail -1)"
if [[ -z "$image_ref" ]]; then
  echo "!! Could not determine pushed image reference from build output." >&2
  echo "   Run 'nsc run --image nscr.io/<workspace>/$IMAGE_NAME:latest ...' manually." >&2
  exit 1
fi
echo ">> Pushed: $image_ref"

machine_args=()
if [[ -n "${MACHINE_TYPE:-}" ]]; then
  machine_args=(--machine_type "$MACHINE_TYPE")
fi

# Forward optional channel-scoping settings when present.
scope_args=()
if [[ -n "${SHELL_BOT_ALLOWED_STREAMS:-}" ]]; then
  scope_args+=(-e "SHELL_BOT_ALLOWED_STREAMS=$SHELL_BOT_ALLOWED_STREAMS")
fi
if [[ -n "${SHELL_BOT_ALLOW_DMS:-}" ]]; then
  scope_args+=(-e "SHELL_BOT_ALLOW_DMS=$SHELL_BOT_ALLOW_DMS")
fi
# Forward the Claude assistant key when present (enables the `?` prefix).
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  scope_args+=(-e "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY")
fi
if [[ -n "${SHELL_BOT_CLAUDE_MODEL:-}" ]]; then
  scope_args+=(-e "SHELL_BOT_CLAUDE_MODEL=$SHELL_BOT_CLAUDE_MODEL")
fi
# Forward the Namespace Managed-Agent settings when present (the `%` prefix).
if [[ -n "${SHELL_BOT_AGENT_ID:-}" ]]; then
  scope_args+=(-e "SHELL_BOT_AGENT_ID=$SHELL_BOT_AGENT_ID")
fi
if [[ -n "${SHELL_BOT_ENVIRONMENT_ID:-}" ]]; then
  scope_args+=(-e "SHELL_BOT_ENVIRONMENT_ID=$SHELL_BOT_ENVIRONMENT_ID")
fi

echo ">> Launching bot on a new Namespace VM (duration $DURATION)..."
nsc run \
  --image "$image_ref" \
  --name "$IMAGE_NAME" \
  --duration "$DURATION" \
  ${machine_args[@]+"${machine_args[@]}"} \
  -e "ZULIP_EMAIL=$ZULIP_EMAIL" \
  -e "ZULIP_API_KEY=$ZULIP_API_KEY" \
  -e "ZULIP_SITE=$ZULIP_SITE" \
  -e "SHELL_BOT_ALLOWED_SENDERS=$SHELL_BOT_ALLOWED_SENDERS" \
  ${scope_args[@]+"${scope_args[@]}"}

echo ">> Done. The bot is now listening on Zulip from the VM."
echo "   Inspect with:  nsc list        (find the instance)"
echo "                  nsc logs <id>   (view bot output)"
echo "                  nsc destroy <id>(tear it down)"
