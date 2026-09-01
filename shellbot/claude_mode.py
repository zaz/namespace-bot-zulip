"""`?` mode: Claude chat assistant with per-thread conversation memory."""

import collections
import os
import threading

from .config import (
    CLAUDE_EFFORT,
    CLAUDE_MAX_CONVERSATIONS,
    CLAUDE_MAX_HISTORY,
    CLAUDE_MAX_TOKENS,
    CLAUDE_MODEL,
    CLAUDE_SYSTEM,
)


def make_claude_client():
    """Build an Anthropic client if a key is configured, else None."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        print("anthropic package not installed; Claude assistant disabled.")
        return None
    return anthropic.Anthropic()


claude_client = make_claude_client()

# One Claude conversation history per thread, LRU-ordered.
CONVERSATIONS: "collections.OrderedDict[str, list]" = collections.OrderedDict()
CONVERSATIONS_LOCK = threading.Lock()


def reset_conversation(key: str) -> None:
    with CONVERSATIONS_LOCK:
        CONVERSATIONS.pop(key, None)


def ask_claude(key: str, prompt: str) -> str:
    """Send a prompt to Claude with the thread's conversation history."""
    if claude_client is None:
        return ("Claude assistant isn't configured — set ANTHROPIC_API_KEY on "
                "the bot to enable it.")

    with CONVERSATIONS_LOCK:
        history = CONVERSATIONS.get(key)
        if history is None:
            history = []
            CONVERSATIONS[key] = history
        CONVERSATIONS.move_to_end(key)
        while len(CONVERSATIONS) > CLAUDE_MAX_CONVERSATIONS:
            CONVERSATIONS.popitem(last=False)

    messages = history + [{"role": "user", "content": prompt}]
    try:
        response = claude_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            system=CLAUDE_SYSTEM,
            output_config={"effort": CLAUDE_EFFORT},
            messages=messages,
        )
    except Exception as exc:  # pragma: no cover - network/API errors
        return f"[Claude error: {exc}]"

    if response.stop_reason == "refusal":
        return "[Claude declined to respond to that request.]"

    reply = "".join(b.text for b in response.content if b.type == "text").strip()
    if not reply:
        reply = "[Claude returned no text.]"

    # Persist the turn (text only) and trim to the history cap.
    with CONVERSATIONS_LOCK:
        history.append({"role": "user", "content": prompt})
        history.append({"role": "assistant", "content": reply})
        del history[:-CLAUDE_MAX_HISTORY]

    return reply
