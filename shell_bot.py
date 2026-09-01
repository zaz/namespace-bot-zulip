#!/usr/bin/env python3
"""Zulip bot that runs shell commands and Claude agents. Entry point.

The implementation lives in the `shellbot` package:
config, zulip_io, shell_mode (`!`), claude_mode (`?`), agent_mode (`%`),
fleet_mode (@-mention/DM), router, main.

See the README for usage and the full configuration reference.

SECURITY: this grants shell access to anyone allowed to talk to the bot. Keep
the sender allowlist tight, run the bot as an unprivileged user, and treat the
bot's API key like a password.
"""

from shellbot.main import main

if __name__ == "__main__":
    main()
