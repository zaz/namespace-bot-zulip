"""`%` mode: Claude Managed Agent sessions executing in a Namespace Devbox.

Anthropic runs the agent loop; Namespace runs the Devbox worker; this bot is
the client relaying messages into the Zulip thread. Sessions are visible on
platform.claude.com. See https://namespace.so/docs/integrations/claude
"""

import collections
import os
import queue
import threading
import time

from .config import (
    AGENT_API_KEY,
    AGENT_BASE_URL,
    AGENT_ID,
    AGENT_MAX_SESSIONS,
    AGENT_TIMEOUT,
    AGENT_WORKSPACE,
    ENVIRONMENT_ID,
)


def make_agent_client():
    """Anthropic *platform* client for the sessions API, or None.

    Deliberately separate from claude_mode's client: that one may point at a
    gateway (ANTHROPIC_BASE_URL), which proxies plain message calls but not
    the sessions API. This client pins the base URL to the platform so `%`
    sessions land in the org's Claude Console (platform.claude.com).
    """
    if not AGENT_API_KEY:
        return None
    try:
        import anthropic
    except ImportError:
        return None
    return anthropic.Anthropic(api_key=AGENT_API_KEY, base_url=AGENT_BASE_URL)


agent_client = make_agent_client()

# One Managed Agent session id per thread, LRU-ordered.
AGENT_SESSIONS: "collections.OrderedDict[str, str]" = collections.OrderedDict()
AGENT_SESSIONS_LOCK = threading.Lock()


def agent_configured() -> bool:
    return bool(agent_client and AGENT_ID and ENVIRONMENT_ID)


def session_url(session_id: str) -> str:
    return (f"https://platform.claude.com/workspaces/{AGENT_WORKSPACE}"
            f"/sessions/{session_id}")


def reset_agent_session(key: str) -> None:
    with AGENT_SESSIONS_LOCK:
        AGENT_SESSIONS.pop(key, None)


def _get_or_create_agent_session(key: str) -> str:
    with AGENT_SESSIONS_LOCK:
        sid = AGENT_SESSIONS.get(key)
        if sid:
            AGENT_SESSIONS.move_to_end(key)
            return sid
    # Create outside the lock (network call). Override the toolset to auto-allow
    # so the agent runs tools without pausing for per-call approval (the sender
    # is already allowlisted and it's the user's own Devbox).
    session = agent_client.beta.sessions.create(
        agent={
            "type": "agent_with_overrides",
            "id": AGENT_ID,
            "tools": [{
                "type": "agent_toolset_20260401",
                "default_config": {
                    "enabled": True,
                    "permission_policy": {"type": "always_allow"},
                },
            }],
        },
        environment_id=ENVIRONMENT_ID,
        title=f"Zulip {key}",
    )
    with AGENT_SESSIONS_LOCK:
        AGENT_SESSIONS[key] = session.id
        AGENT_SESSIONS.move_to_end(key)
        while len(AGENT_SESSIONS) > AGENT_MAX_SESSIONS:
            AGENT_SESSIONS.popitem(last=False)
    return session.id


def _drain_session(session_id: str, prompt: str, out_q: "queue.Queue") -> None:
    """Run one turn of a Managed Agent session; put (status, text) on out_q.

    Built-in tool calls are auto-allowed and executed by the Namespace Devbox
    worker (self_hosted env). We POLL events.list rather than SSE-stream: for this
    self-hosted setup the stream does not reliably deliver the events emitted after
    the worker executes, whereas polling does.
    """
    debug = os.environ.get("SHELL_BOT_AGENT_DEBUG", "1") not in ("0", "false", "")

    def log(msg: str) -> None:
        if debug:
            print(f"[agent {session_id}] {msg}", flush=True)

    def workers_polling() -> "int | None":
        try:
            stats = agent_client.beta.environments.work.stats(
                environment_id=ENVIRONMENT_ID
            )
            return getattr(stats, "workers_polling", None)
        except Exception as exc:
            log(f"work.stats failed: {exc}")
            return None

    try:
        agent_client.beta.sessions.events.send(
            session_id=session_id,
            events=[{"type": "user.message",
                     "content": [{"type": "text", "text": prompt}]}],
        )
    except Exception as exc:
        out_q.put(("error", f"[agent error: {exc}]"))
        return

    deadline = time.monotonic() + AGENT_TIMEOUT
    no_worker_strikes = 0
    while True:
        if time.monotonic() >= deadline:
            out_q.put(("timeout", ""))
            return
        time.sleep(3)
        try:
            events = agent_client.beta.sessions.events.list(session_id=session_id).data
        except Exception as exc:
            out_q.put(("error", f"[agent error: {exc}]"))
            return

        parts = []
        status = None
        for ev in events:
            etype = getattr(ev, "type", None)
            if etype == "agent.message":
                for block in ev.content:
                    if getattr(block, "type", None) == "text":
                        parts.append(block.text)
            elif etype == "session.status_idle":
                stop = getattr(ev, "stop_reason", None)
                status = getattr(stop, "type", None) if stop else None
            elif etype == "session.status_terminated":
                status = "terminated"

        text = "".join(parts).strip()
        if status == "terminated":
            out_q.put(("terminated", text))
            return
        if status in ("end_turn", "retries_exhausted"):
            out_q.put(("idle", text))
            return
        if status == "requires_action":
            # Waiting for the Devbox worker to execute a tool. If nothing is
            # polling for several checks, fail fast with a clear message.
            polling = workers_polling()
            log(f"requires_action; workers_polling={polling}")
            if polling == 0:
                no_worker_strikes += 1
                if no_worker_strikes >= 3:
                    out_q.put(("no_worker", text))
                    return
            else:
                no_worker_strikes = 0
        # otherwise keep polling until terminal or deadline


def run_agent(key: str, prompt: str) -> str:
    """Run one turn against the thread's Namespace-backed agent session."""
    if not agent_configured():
        return ("The Namespace agent mode isn't configured — set "
                "SHELL_BOT_AGENT_ID and SHELL_BOT_ENVIRONMENT_ID (and "
                "SHELL_BOT_AGENT_API_KEY, a first-party Anthropic platform "
                "key) to enable it.")
    # Try up to twice: a reused thread session may be stuck mid-tool-call (e.g.
    # a prior turn timed out or ran during an outage), which makes a new
    # user.message 400. On error we drop the session and retry once fresh.
    for attempt in (1, 2):
        try:
            session_id = _get_or_create_agent_session(key)
        except Exception as exc:
            return f"[couldn't start an agent session: {exc}]"

        out_q: "queue.Queue" = queue.Queue()
        threading.Thread(
            target=_drain_session, args=(session_id, prompt, out_q), daemon=True
        ).start()
        # The drain self-terminates at AGENT_TIMEOUT; give the queue a bit longer
        # so we receive its status rather than racing it.
        try:
            status, text = out_q.get(timeout=AGENT_TIMEOUT + 20)
        except queue.Empty:
            return (f"Still working in the Devbox after {AGENT_TIMEOUT}s — it keeps "
                    f"running. Follow it here: {session_url(session_id)}")

        text = (text or "").strip()
        if status == "error":
            reset_agent_session(key)  # drop the (possibly stuck) session
            if attempt == 1:
                continue  # retry once with a fresh session
            return text
        break

    if status == "terminated":
        reset_agent_session(key)  # ended — next message starts a fresh session
        return text or "[the agent session ended]"
    if status == "no_worker":
        reset_agent_session(key)
        note = ("⚠️ The agent queued a command but **no Namespace Devbox worker "
                "is running** to execute it, so it can't complete. Make sure a "
                "Devbox worker is polling this environment, then retry.\n"
                f"Session: {session_url(session_id)}")
        return (text + "\n\n" + note).strip() if text else note
    if status == "timeout":
        return (f"Still working in the Devbox after {AGENT_TIMEOUT}s — it keeps "
                f"running. Follow it here: {session_url(session_id)}")
    return text or "[the agent finished with no message]"
