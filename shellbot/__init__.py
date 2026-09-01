"""Zulip shell bot, split into small modules.

- config      — all environment-variable configuration in one place
- zulip_io    — Zulip client, thread identity, reply plumbing
- shell_mode  — `!` per-thread persistent shells
- claude_mode — `?` Claude chat assistant with per-thread memory
- agent_mode  — `%` Claude Managed Agent sessions (Namespace Devbox)
- fleet_mode  — @-mention/DM headless Claude Code sessions
- router      — message dispatch across the modes
- main        — startup checks, banner, event loop

`shell_bot.py` at the repo root is the entry point.
"""
