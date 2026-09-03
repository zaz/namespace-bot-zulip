"""@-mention/DM mode: headless Claude Code sessions on the bot's machine.

Full local shell access — including, when the Namespace `devbox` CLI is
installed and authenticated, spawning ephemeral worker devboxes for large
parallel tasks. One session per thread; `:reset` starts a fresh one.
"""

import collections
import json
import os
import shutil
import subprocess
import threading
import uuid

from .config import (
    CLAUDE_MODEL,
    FLEET_BIN,
    FLEET_CWD,
    FLEET_ENV,
    FLEET_MAX_SESSIONS,
    FLEET_TIMEOUT,
)

# One Claude Code session id per thread, LRU-ordered. Persisted to disk so a
# bot restart doesn't make every thread forget its conversation.
FLEET_SESSIONS: "collections.OrderedDict[str, str]" = collections.OrderedDict()
FLEET_SESSIONS_LOCK = threading.Lock()
FLEET_SESSIONS_FILE = os.environ.get(
    "SHELL_BOT_FLEET_SESSIONS_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".fleet-sessions.json"),
)


def _load_fleet_sessions() -> None:
    try:
        with open(FLEET_SESSIONS_FILE) as fh:
            data = json.load(fh)
        for k, v in data.items():
            if isinstance(k, str) and isinstance(v, str):
                FLEET_SESSIONS[k] = v
    except (OSError, ValueError):
        pass


def _save_fleet_sessions() -> None:
    """Write the map atomically; called with FLEET_SESSIONS_LOCK held."""
    try:
        tmp = FLEET_SESSIONS_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(dict(FLEET_SESSIONS), fh)
        os.replace(tmp, FLEET_SESSIONS_FILE)
    except OSError:
        pass


_load_fleet_sessions()


def fleet_configured() -> bool:
    return shutil.which(FLEET_BIN) is not None


def reset_fleet_session(key: str) -> None:
    with FLEET_SESSIONS_LOCK:
        FLEET_SESSIONS.pop(key, None)
        _save_fleet_sessions()


def run_fleet(key: str, task: str) -> str:
    """Run one turn of the thread's Claude Code session (headless CLI)."""
    if not fleet_configured():
        return (f"Claude Code isn't installed on the bot host (`{FLEET_BIN}` "
                "not found on PATH), so fleet mode is disabled.")

    with FLEET_SESSIONS_LOCK:
        sid = FLEET_SESSIONS.get(key)
        if sid:
            FLEET_SESSIONS.move_to_end(key)

    cmd = [FLEET_BIN, "-p", task,
           "--output-format", "json",
           "--permission-mode", "bypassPermissions"]
    if CLAUDE_MODEL:
        cmd += ["--model", CLAUDE_MODEL]
    # First turn mints a session id; later turns resume it, so the thread keeps
    # its context. Resuming can fork to a new id — the result JSON tells us the
    # id to resume next time.
    cmd += ["--resume", sid] if sid else ["--session-id", str(uuid.uuid4())]

    try:
        proc = subprocess.run(
            cmd, cwd=FLEET_CWD, env=FLEET_ENV, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=FLEET_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return (f"[the fleet agent was still working after {FLEET_TIMEOUT}s "
                "and was stopped — try a smaller task, or raise "
                "SHELL_BOT_FLEET_TIMEOUT]")
    except OSError as exc:
        return f"[couldn't launch Claude Code: {exc}]"

    out = (proc.stdout or "").strip()
    reply, next_sid = "", sid
    try:
        data = json.loads(out.splitlines()[-1]) if out else {}
        reply = (data.get("result") or "").strip()
        next_sid = data.get("session_id") or next_sid
    except (ValueError, IndexError):
        reply = out  # not JSON — pass whatever the CLI printed along

    if proc.returncode != 0 and not reply:
        err = (proc.stderr or out or "").strip()
        reset_fleet_session(key)  # the session may be unusable — start fresh
        return f"[fleet agent failed (exit {proc.returncode}): {err[-500:]}]"

    if next_sid:
        with FLEET_SESSIONS_LOCK:
            FLEET_SESSIONS[key] = next_sid
            FLEET_SESSIONS.move_to_end(key)
            while len(FLEET_SESSIONS) > FLEET_MAX_SESSIONS:
                FLEET_SESSIONS.popitem(last=False)
            _save_fleet_sessions()

    return reply or "[the fleet agent finished with no message]"
