"""All environment-variable configuration, parsed once at import.

See the README's Configuration table for what each variable does.
"""

import os

ZULIPRC = os.environ.get("ZULIPRC", "zuliprc")
ALLOWED_SENDERS = {
    e.strip().lower()
    for e in os.environ.get("SHELL_BOT_ALLOWED_SENDERS", "").split(",")
    if e.strip()
}
# SHELL_BOT_ALLOWED_SENDERS="*" opens the bot to every user it can see (the
# channel/DM scoping still applies). For orgs where everyone is trusted.
ALLOW_ALL_SENDERS = "*" in ALLOWED_SENDERS
# Zulip orgs that hide real email addresses hand the API dummy addresses of the
# form user<id>@<realm>, so email allowlists can't match there. Numeric user IDs
# (visible on a user's Zulip profile) work regardless of email-privacy settings.
ALLOWED_SENDER_IDS = {
    int(i.strip())
    for i in os.environ.get("SHELL_BOT_ALLOWED_SENDER_IDS", "").split(",")
    if i.strip()
}
ALLOWED_STREAMS = {
    s.strip().lower()
    for s in os.environ.get("SHELL_BOT_ALLOWED_STREAMS", "").split(",")
    if s.strip()
}
# If any streams are allowlisted, ignore DMs by default (lock to the channel).
ALLOW_DMS = os.environ.get(
    "SHELL_BOT_ALLOW_DMS", "false" if ALLOWED_STREAMS else "true"
).lower() in ("1", "true", "yes")
# Messages must start with this prefix to be treated as commands. Others are
# ignored, so the bot stays quiet during normal conversation.
COMMAND_PREFIX = os.environ.get("SHELL_BOT_PREFIX", "!")
TIMEOUT = int(os.environ.get("SHELL_BOT_TIMEOUT", "30"))
SHELL = os.environ.get("SHELL_BOT_SHELL", "/bin/bash")
CWD = os.environ.get("SHELL_BOT_CWD") or None
MAX_OUTPUT = int(os.environ.get("SHELL_BOT_MAX_OUTPUT", "3500"))
# Each Zulip thread gets its own persistent shell; cap how many we keep alive.
MAX_SESSIONS = int(os.environ.get("SHELL_BOT_MAX_SESSIONS", "50"))

# Environment handed to the per-thread shells. Strip the bot's own config so a
# command like `env` can't read the Zulip / Anthropic API keys, the allowlist,
# etc. Anything else the host provides (PATH, HOME, …) is passed through.
_SECRET_ENV_PREFIXES = ("ZULIP", "SHELL_BOT_", "ANTHROPIC")
SHELL_ENV = {
    k: v for k, v in os.environ.items() if not k.startswith(_SECRET_ENV_PREFIXES)
}
SHELL_ENV.update({"PS1": "", "PS2": "", "TERM": "dumb"})

# --- Claude assistant (`?` prefix, per-thread conversation) ---
CLAUDE_PREFIX = os.environ.get("SHELL_BOT_CLAUDE_PREFIX", "?")
CLAUDE_MODEL = os.environ.get("SHELL_BOT_CLAUDE_MODEL", "claude-opus-5")
CLAUDE_EFFORT = os.environ.get("SHELL_BOT_CLAUDE_EFFORT", "medium")
CLAUDE_MAX_TOKENS = int(os.environ.get("SHELL_BOT_CLAUDE_MAX_TOKENS", "4096"))
# Max messages (user+assistant) kept per thread before trimming the oldest.
CLAUDE_MAX_HISTORY = int(os.environ.get("SHELL_BOT_CLAUDE_MAX_HISTORY", "20"))
CLAUDE_MAX_CONVERSATIONS = int(
    os.environ.get("SHELL_BOT_CLAUDE_MAX_CONVERSATIONS", "200")
)
CLAUDE_SYSTEM = os.environ.get(
    "SHELL_BOT_CLAUDE_SYSTEM",
    "You are a helpful assistant replying inside a Zulip chat thread. Keep "
    "replies concise and use Zulip-flavored Markdown. The conversation history "
    "you see is scoped to this one thread.",
)

# --- Namespace Managed-Agent mode (`%` prefix, per-thread agent session) ---
# The sessions API only exists on the first-party Anthropic platform, so this
# mode gets its own credentials: gateway keys (e.g. Eden) that serve the `?`
# assistant and fleet mode can't create Console-visible sessions.
AGENT_API_KEY = (os.environ.get("SHELL_BOT_AGENT_API_KEY")
                 or os.environ.get("ANTHROPIC_API_KEY"))
AGENT_BASE_URL = (os.environ.get("SHELL_BOT_AGENT_BASE_URL")
                  or "https://api.anthropic.com")
AGENT_PREFIX = os.environ.get("SHELL_BOT_AGENT_PREFIX", "%")
AGENT_ID = os.environ.get("SHELL_BOT_AGENT_ID")
ENVIRONMENT_ID = os.environ.get("SHELL_BOT_ENVIRONMENT_ID")
AGENT_TIMEOUT = int(os.environ.get("SHELL_BOT_AGENT_TIMEOUT", "300"))
AGENT_MAX_SESSIONS = int(os.environ.get("SHELL_BOT_AGENT_MAX_SESSIONS", "100"))
AGENT_WORKSPACE = os.environ.get("SHELL_BOT_AGENT_WORKSPACE", "default")

# --- Claude Code fleet mode (@-mention/DM, per-thread local coding agent) ---
# SHELL_BOT_FLEET_PREFIX optionally adds a prefix trigger as well (off by
# default — `>` collides with Zulip's quote syntax).
FLEET_PREFIX = os.environ.get("SHELL_BOT_FLEET_PREFIX", "")
FLEET_BIN = os.environ.get("SHELL_BOT_FLEET_BIN", "claude")
FLEET_CWD = os.environ.get("SHELL_BOT_FLEET_CWD") or CWD
FLEET_TIMEOUT = int(os.environ.get("SHELL_BOT_FLEET_TIMEOUT", "600"))
FLEET_MAX_SESSIONS = int(os.environ.get("SHELL_BOT_FLEET_MAX_SESSIONS", "100"))

# Claude Code needs the Anthropic credentials that SHELL_ENV deliberately
# strips; pass those through, but keep the Zulip key and bot config hidden.
FLEET_ENV = dict(SHELL_ENV)
for _var in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
    if os.environ.get(_var):
        FLEET_ENV[_var] = os.environ[_var]
