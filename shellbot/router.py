"""Dispatch incoming Zulip messages to the right mode."""

import threading

from .agent_mode import reset_agent_session, run_agent
from .claude_mode import ask_claude, reset_conversation
from .config import (
    AGENT_PREFIX,
    ALLOW_ALL_SENDERS,
    ALLOW_DMS,
    ALLOWED_SENDER_IDS,
    ALLOWED_SENDERS,
    ALLOWED_STREAMS,
    CLAUDE_PREFIX,
    COMMAND_PREFIX,
    FLEET_PREFIX,
    MAX_OUTPUT,
)
from .fleet_mode import reset_fleet_session, run_fleet
from .shell_mode import format_reply, reset_session, run_in_thread
from .zulip_io import BOT_ID, reply_target, safe_send, strip_mention, thread_key


def handle_message(message: dict) -> None:
    # Ignore our own messages to avoid loops.
    if message["sender_id"] == BOT_ID:
        return

    # Channel/DM scoping. For streams, display_recipient is the stream name.
    if message["type"] == "stream":
        if ALLOWED_STREAMS and message["display_recipient"].lower() not in ALLOWED_STREAMS:
            return  # message in a non-allowed channel — ignore silently
    else:  # private / direct message
        if not ALLOW_DMS:
            return  # DMs disabled — ignore silently

    text = strip_mention(message["content"])
    mentioned = (
        "mentioned" in message.get("flags", [])
        or text != message["content"].strip()  # a leading @-mention was stripped
    )
    # Route by prefix: shell command, Claude message, or ignore.
    if text.startswith(COMMAND_PREFIX):
        mode = "shell"
        body = text[len(COMMAND_PREFIX):].strip()
    elif AGENT_PREFIX and text.startswith(AGENT_PREFIX):
        mode = "agent"
        body = text[len(AGENT_PREFIX):].strip()
    elif FLEET_PREFIX and text.startswith(FLEET_PREFIX):
        mode = "fleet"
        body = text[len(FLEET_PREFIX):].strip()
    elif CLAUDE_PREFIX and text.startswith(CLAUDE_PREFIX):
        mode = "claude"
        body = text[len(CLAUDE_PREFIX):].strip()
    elif mentioned or message["type"] == "private":
        # No prefix, but the bot was addressed directly: talk to the fleet
        # agent — the "just talk to it" interface.
        mode = "fleet"
        body = text
    else:
        return  # no known prefix and not addressed to us — ignore silently

    sender = message["sender_email"].lower()
    if (not ALLOW_ALL_SENDERS
            and sender not in ALLOWED_SENDERS
            and message["sender_id"] not in ALLOWED_SENDER_IDS):
        safe_send(
            reply_target(message,
                         f"Sorry, {message['sender_full_name']} — you're not "
                         "authorized to use this bot.")
        )
        return

    key = thread_key(message)

    if mode == "shell":
        if not body:
            safe_send(
                reply_target(message, f"Send me a command: {COMMAND_PREFIX}<shell command>")
            )
            return
        if body == ":reset":
            reset_session(key)
            safe_send(reply_target(message, "Shell for this thread was reset."))
            return
        output = run_in_thread(key, body)
        safe_send(reply_target(message, format_reply(body, output)))
        return

    if mode == "agent":
        if not body:
            safe_send(
                reply_target(message, f"Give the agent a task: {AGENT_PREFIX}<task>")
            )
            return
        if body == ":reset":
            reset_agent_session(key)
            safe_send(reply_target(message, "Agent session for this thread was reset."))
            return
        safe_send(
            reply_target(message, "Working in the Namespace Devbox… (this can take a while)")
        )

        # Run in the background so a long agent turn doesn't block the bot's
        # single message loop (other messages, including !/? , keep working).
        def _run_and_reply(key=key, body=body, message=message):
            try:
                reply = run_agent(key, body)
            except Exception as exc:  # pragma: no cover - defensive
                reply = f"[agent error: {exc}]"
            safe_send(reply_target(message, reply))

        threading.Thread(target=_run_and_reply, daemon=True).start()
        return

    if mode == "fleet":
        if not body:
            safe_send(
                reply_target(message,
                             "Give me a task (mention me or DM me with it) and "
                             "I'll work on it with full shell access. `:reset` "
                             "starts this thread's session over.")
            )
            return
        if body == ":reset":
            reset_fleet_session(key)
            safe_send(reply_target(message, "Fleet session for this thread was reset."))
            return
        safe_send(
            reply_target(message, "On it — running Claude Code… (this can take a while)")
        )

        # Same deal as agent mode: don't block the message loop on a long run.
        def _run_fleet_and_reply(key=key, body=body, message=message):
            try:
                reply = run_fleet(key, body)
            except Exception as exc:  # pragma: no cover - defensive
                reply = f"[fleet error: {exc}]"
            if len(reply) > MAX_OUTPUT:
                reply = reply[:MAX_OUTPUT] + "\n[output truncated]"
            safe_send(reply_target(message, reply))

        threading.Thread(target=_run_fleet_and_reply, daemon=True).start()
        return

    # mode == "claude"
    if not body:
        safe_send(
            reply_target(message, f"Ask me something: {CLAUDE_PREFIX}<your message>")
        )
        return
    if body == ":reset":
        reset_conversation(key)
        safe_send(reply_target(message, "Claude conversation for this thread was reset."))
        return
    reply = ask_claude(key, body)
    safe_send(reply_target(message, reply))
