"""Startup checks, banner, and the Zulip event loop."""

import os
import sys

from .agent_mode import agent_configured
from .claude_mode import claude_client
from .config import (
    AGENT_ID,
    AGENT_PREFIX,
    ALLOW_ALL_SENDERS,
    ALLOWED_SENDER_IDS,
    ALLOWED_SENDERS,
    CLAUDE_MODEL,
    CLAUDE_PREFIX,
    COMMAND_PREFIX,
    ENVIRONMENT_ID,
    FLEET_BIN,
    FLEET_CWD,
    FLEET_PREFIX,
    MAX_SESSIONS,
)
from .fleet_mode import fleet_configured
from .router import handle_message
from .zulip_io import BOT_EMAIL, BOT_ID, client


def main() -> None:
    if not ALLOWED_SENDERS and not ALLOWED_SENDER_IDS and not ALLOW_ALL_SENDERS:
        sys.exit(
            "Refusing to start: the sender allowlist is empty. Set "
            "SHELL_BOT_ALLOWED_SENDERS (comma-separated emails) and/or "
            "SHELL_BOT_ALLOWED_SENDER_IDS (comma-separated Zulip user IDs; "
            "required on orgs that hide real email addresses)."
        )
    print(f"Shell bot running as {BOT_EMAIL} (id {BOT_ID}).")
    allowed = sorted(ALLOWED_SENDERS) + [str(i) for i in sorted(ALLOWED_SENDER_IDS)]
    print(f"Allowed senders: {', '.join(allowed)}")
    print(f"Per-thread shells enabled (max {MAX_SESSIONS}); shell prefix '{COMMAND_PREFIX}'.")
    if claude_client is not None:
        print(f"Claude assistant enabled: model {CLAUDE_MODEL}, prefix '{CLAUDE_PREFIX}'.")
    else:
        print("Claude assistant disabled (set ANTHROPIC_API_KEY to enable).")
    if agent_configured():
        print(f"Namespace agent mode enabled: agent {AGENT_ID}, env {ENVIRONMENT_ID}, "
              f"prefix '{AGENT_PREFIX}'.")
    else:
        print("Namespace agent mode disabled (set SHELL_BOT_AGENT_ID + "
              "SHELL_BOT_ENVIRONMENT_ID to enable).")
    if fleet_configured():
        trigger = "@-mention/DM"
        if FLEET_PREFIX:
            trigger += f" or prefix '{FLEET_PREFIX}'"
        print(f"Claude Code fleet mode enabled: {trigger}, "
              f"cwd {FLEET_CWD or os.getcwd()}.")
    else:
        print(f"Claude Code fleet mode disabled ('{FLEET_BIN}' not on PATH).")
    client.call_on_each_message(handle_message)
