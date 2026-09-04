#!/usr/bin/env python3
"""Zulip bot that runs shell commands on the local system.

Send the bot a direct message, or @-mention it in a stream, with a shell
command. It runs the command on the machine this bot is hosted on and replies
with the combined stdout/stderr.

Each Zulip thread (a stream topic, or a DM conversation) gets its own persistent
shell, so `cd`, `export`, and shell variables persist across commands within a
thread, and different threads are fully isolated from each other. Send `!:reset`
to discard a thread's shell and start a fresh one.

Messages prefixed with `?` are answered by Claude instead of the shell, with
per-thread conversation memory (`?:reset` clears a thread's Claude history).
Requires ANTHROPIC_API_KEY; without it the `?` prefix reports it's disabled.

Messages prefixed with `%` drive a Claude Managed Agent session (one per thread)
that executes tools in a Namespace Devbox — autonomous, can take minutes.
Requires SHELL_BOT_AGENT_ID and SHELL_BOT_ENVIRONMENT_ID. `%:reset` starts a
fresh session for the thread.

@-mentioning the bot with no other prefix (or DMing it plain text) drives a
headless Claude Code session (one per thread) running on the bot's own machine.
It has full shell access there, so with the Namespace `devbox` CLI installed
and authenticated it can spawn ephemeral worker devboxes for large parallel
tasks and expire them when done. Requires the `claude` CLI on PATH. Mention the
bot with `:reset` to start a fresh session for the thread.

SECURITY: this grants shell access to anyone allowed to talk to the bot. Keep
the sender allowlist tight, run the bot as an unprivileged user, and treat the
bot's API key like a password.

Configuration (environment variables):
  ZULIP_EMAIL / ZULIP_API_KEY / ZULIP_SITE
                             Bot credentials. If all three are set they take
                             precedence over ZULIPRC (handy for containers, so
                             no file needs mounting).
  ZULIPRC                    Path to the bot's zuliprc file (default: ./zuliprc),
                             used only when the ZULIP_* vars above are not set.
  SHELL_BOT_ALLOWED_SENDERS  Comma-separated sender emails allowed to run
                             commands. Note: organizations that hide real email
                             addresses deliver dummy user<id>@<realm> addresses
                             to the API, so real emails never match there — use
                             SHELL_BOT_ALLOWED_SENDER_IDS instead.
  SHELL_BOT_ALLOWED_SENDER_IDS
                             Comma-separated numeric Zulip user IDs allowed to
                             run commands (shown on a user's profile in the
                             Zulip UI; stable regardless of email-privacy
                             settings). A sender is authorized if their email
                             OR their user ID is allowlisted. At least one of
                             the two allowlists must be non-empty — the bot
                             refuses to start otherwise.
  SHELL_BOT_ALLOWED_STREAMS  Comma-separated channel (stream) names the bot will
                             act in. If empty, all channels are allowed. When
                             set, DMs are ignored unless SHELL_BOT_ALLOW_DMS=true.
  SHELL_BOT_ALLOW_DMS        Whether to honor direct messages (default: true, or
                             false when SHELL_BOT_ALLOWED_STREAMS is set).
  SHELL_BOT_PREFIX           Messages must start with this prefix to run as a
                             command; others are ignored (default: !).
  SHELL_BOT_TIMEOUT          Per-command timeout in seconds (default: 30)
  SHELL_BOT_SHELL            Shell used to run commands (default: /bin/bash)
  SHELL_BOT_CWD              Working directory new thread shells start in
                             (default: bot's cwd)
  SHELL_BOT_MAX_OUTPUT       Max reply characters before truncation (default: 3500)
  SHELL_BOT_MAX_SESSIONS     Max concurrent per-thread shells kept alive; the
                             least-recently-used is evicted past this (default: 50)
  ANTHROPIC_API_KEY          Enables the Claude assistant (`?` prefix). If unset,
                             `?` messages report that Claude is disabled.
  SHELL_BOT_CLAUDE_PREFIX    Prefix that routes a message to Claude (default: ?)
  SHELL_BOT_CLAUDE_MODEL     Claude model id (default: claude-opus-5)
  SHELL_BOT_CLAUDE_EFFORT    Reasoning effort: low|medium|high|xhigh|max (default: medium)
  SHELL_BOT_CLAUDE_MAX_TOKENS        Max reply tokens (default: 4096)
  SHELL_BOT_CLAUDE_MAX_HISTORY       Messages kept per thread before trimming (default: 20)
  SHELL_BOT_CLAUDE_MAX_CONVERSATIONS Max threads with Claude history (default: 200)
  SHELL_BOT_CLAUDE_SYSTEM    System prompt for the Claude assistant
  SHELL_BOT_FLEET_PREFIX     Optional extra prefix that routes a message to the
                             fleet session, besides @-mentions and DMs (default:
                             none — `>` would collide with Zulip quote syntax)
  SHELL_BOT_FLEET_BIN        Claude Code executable (default: claude)
  SHELL_BOT_FLEET_CWD        Working directory for Claude Code sessions
                             (default: SHELL_BOT_CWD)
  SHELL_BOT_FLEET_TIMEOUT    Per-turn timeout in seconds (default: 600)
  SHELL_BOT_FLEET_MAX_SESSIONS  Max threads with a live Claude Code session
                             before the least-recently-used is dropped
                             (default: 100)
"""

import collections
import datetime
import json
import os
import queue
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

import zulip

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

# --- Claude assistant (per-thread conversation) ---
# Messages prefixed with CLAUDE_PREFIX are sent to the Claude API with per-thread
# conversation memory, instead of being run as shell commands.
CLAUDE_PREFIX = os.environ.get("SHELL_BOT_CLAUDE_PREFIX", "?")
CLAUDE_MODEL = os.environ.get("SHELL_BOT_CLAUDE_MODEL", "claude-opus-5")
CLAUDE_EFFORT = os.environ.get("SHELL_BOT_CLAUDE_EFFORT", "medium")
CLAUDE_MAX_TOKENS = int(os.environ.get("SHELL_BOT_CLAUDE_MAX_TOKENS", "4096"))
# Max messages (user+assistant) kept per thread before trimming the oldest.
CLAUDE_MAX_HISTORY = int(os.environ.get("SHELL_BOT_CLAUDE_MAX_HISTORY", "20"))
CLAUDE_MAX_CONVERSATIONS = int(os.environ.get("SHELL_BOT_CLAUDE_MAX_CONVERSATIONS", "200"))
CLAUDE_SYSTEM = os.environ.get(
    "SHELL_BOT_CLAUDE_SYSTEM",
    "You are a helpful assistant replying inside a Zulip chat thread. Keep "
    "replies concise and use Zulip-flavored Markdown. The conversation history "
    "you see is scoped to this one thread.",
)

# --- Namespace Managed-Agent mode (per-thread agent session) ---
# Messages prefixed with AGENT_PREFIX drive a Claude Managed Agent session that
# executes tools in a Namespace Devbox (a self_hosted environment). One session
# per thread; runs are autonomous and can take minutes.
AGENT_PREFIX = os.environ.get("SHELL_BOT_AGENT_PREFIX", "%")
AGENT_ID = os.environ.get("SHELL_BOT_AGENT_ID")
ENVIRONMENT_ID = os.environ.get("SHELL_BOT_ENVIRONMENT_ID")
AGENT_TIMEOUT = int(os.environ.get("SHELL_BOT_AGENT_TIMEOUT", "300"))
AGENT_MAX_SESSIONS = int(os.environ.get("SHELL_BOT_AGENT_MAX_SESSIONS", "100"))
AGENT_WORKSPACE = os.environ.get("SHELL_BOT_AGENT_WORKSPACE", "default")

# --- Claude Code fleet mode (per-thread local coding agent) ---
# @-mentioning the bot with no other prefix (or DMing it plain text) hands the
# message to the Claude Code CLI running headless on this machine. It gets full
# shell access here — including, if installed and authenticated, the Namespace
# `devbox` CLI — so it can spawn ephemeral worker devboxes for large parallel
# tasks and tear them down again. One Claude Code session per thread; mention
# the bot with `:reset` to start a fresh one. SHELL_BOT_FLEET_PREFIX optionally
# adds a prefix trigger as well (off by default — `>` collides with Zulip's
# quote syntax).
FLEET_PREFIX = os.environ.get("SHELL_BOT_FLEET_PREFIX", "")
FLEET_BIN = os.environ.get("SHELL_BOT_FLEET_BIN", "claude")
FLEET_CWD = os.environ.get("SHELL_BOT_FLEET_CWD") or CWD
FLEET_TIMEOUT = int(os.environ.get("SHELL_BOT_FLEET_TIMEOUT", "600"))
FLEET_MAX_SESSIONS = int(os.environ.get("SHELL_BOT_FLEET_MAX_SESSIONS", "100"))
# Host to fetch agent-produced attachments from (the agent runs there and has
# no Zulip credentials). Empty = attachments are local files on this host.
FLEET_ATTACH_HOST = os.environ.get("SHELL_BOT_FLEET_ATTACH_HOST", "")
FLEET_ATTACH_MAX_BYTES = int(os.environ.get("SHELL_BOT_FLEET_ATTACH_MAX_BYTES", str(25 * 1024 * 1024)))

# Claude Code needs the Anthropic credentials that SHELL_ENV deliberately
# strips; pass those through, but keep the Zulip key and bot config hidden.
FLEET_ENV = dict(SHELL_ENV)
for _var in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
    if os.environ.get(_var):
        FLEET_ENV[_var] = os.environ[_var]

def make_client() -> zulip.Client:
    """Build a Zulip client from ZULIP_* env vars, or fall back to a zuliprc."""
    email = os.environ.get("ZULIP_EMAIL")
    api_key = os.environ.get("ZULIP_API_KEY")
    site = os.environ.get("ZULIP_SITE")
    if email and api_key and site:
        return zulip.Client(email=email, api_key=api_key, site=site)
    return zulip.Client(config_file=ZULIPRC)


client = make_client()
PROFILE = client.get_profile()
BOT_ID = PROFILE["user_id"]
BOT_EMAIL = PROFILE["email"]


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

# Serialize outbound sends — background agent runs post replies from worker
# threads while the main loop keeps handling messages.
SEND_LOCK = threading.Lock()
_raw_send = client.send_message


def safe_send(request: dict):
    with SEND_LOCK:
        return _raw_send(request)


_ATTACH_RE = re.compile(r"^\s*ATTACH:\s*(\S+)\s*$", re.MULTILINE)


def attach_files(text: str) -> str:
    """Replace `ATTACH: /abs/path` lines with Zulip upload links.

    The agent (on the fleet host) writes a file and prints that line; this
    host fetches it over ssh (or reads it locally) and uploads it with the
    bot's Zulip credentials, which never leave this host.
    """
    def _fetch(path: str) -> "bytes | None":
        try:
            if FLEET_ATTACH_HOST:
                proc = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=60",
                     FLEET_ATTACH_HOST, "cat", path],
                    capture_output=True, timeout=120,
                )
                return proc.stdout if proc.returncode == 0 else None
            with open(path, "rb") as fh:
                return fh.read()
        except (OSError, subprocess.SubprocessError):
            return None

    def _repl(m: "re.Match") -> str:
        path = m.group(1)
        data = _fetch(path)
        if data is None:
            return f"*(attachment `{path}` could not be fetched)*"
        if len(data) > FLEET_ATTACH_MAX_BYTES:
            return f"*(attachment `{path}` too large: {len(data)} bytes)*"
        name = os.path.basename(path)
        try:
            import io
            fh = io.BytesIO(data)
            fh.name = name
            res = client.upload_file(fh)
            uri = res.get("url") or res.get("uri")
            if not uri:
                return f"*(upload of `{name}` failed: {res.get('msg', 'no url')})*"
            return f"[{name}]({uri})"
        except Exception as exc:  # pragma: no cover - network
            return f"*(upload of `{name}` failed: {exc})*"

    return _ATTACH_RE.sub(_repl, text)


def send_long(message: dict, text: str, limit: int = 9000) -> None:
    """Post a reply, splitting it into several Zulip messages if it is long.

    Zulip caps a message at 10,000 characters. Split on paragraph boundaries
    where possible so tables and code blocks are less likely to be cut, and
    never drop content: long agent reports often carry the question or the
    key result at the end.
    """
    chunks, buf = [], ""
    for para in text.split("\n\n"):
        cand = (buf + "\n\n" + para) if buf else para
        if len(cand) <= limit:
            buf = cand
            continue
        if buf:
            chunks.append(buf)
        while len(para) > limit:  # a single oversized paragraph
            chunks.append(para[:limit])
            para = para[limit:]
        buf = para
    if buf:
        chunks.append(buf)
    n = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        suffix = f"\n\n*(part {i}/{n})*" if n > 1 else ""
        safe_send(reply_target(message, chunk + suffix))


class ShellSession:
    """A long-lived bash process backing one Zulip thread.

    Commands are fed to the shell's stdin so state (cwd, env, shell variables)
    persists across commands. Each command is wrapped in a `{ ...; } </dev/null`
    group so it can't read from — and consume — our command stream, while still
    running in the current shell (so `cd`/`export` take effect). A random sentinel
    line printed after each command marks completion and carries the exit code.
    """

    def __init__(self) -> None:
        token = os.urandom(6).hex()
        self.sentinel = f"__NSBOT_{token}__"
        self._pat = re.compile(
            rb"\n" + re.escape(self.sentinel.encode()) + rb" (\d+)\r?\n"
        )
        self.proc = subprocess.Popen(
            [SHELL],
            cwd=CWD,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            bufsize=0,
            env=SHELL_ENV,
        )
        self._fd = self.proc.stdout.fileno()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def run(self, command: str) -> str:
        """Run one command in this shell, returning combined stdout/stderr."""
        payload = (
            "{\n" + command + "\n} </dev/null\n"
            + f"printf '\\n%s %d\\n' '{self.sentinel}' \"$?\"\n"
        )
        self.proc.stdin.write(payload.encode())
        self.proc.stdin.flush()

        acc = b""
        deadline = time.monotonic() + TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Can't interrupt just the command without killing this
                # non-interactive shell, so reset the whole thread shell.
                self.close()
                return (
                    f"[command timed out after {TIMEOUT}s; "
                    "this thread's shell was reset]"
                )
            r, _, _ = select.select([self._fd], [], [], remaining)
            if not r:
                continue
            chunk = os.read(self._fd, 65536)
            if not chunk:
                self.close()
                return "[shell exited]"
            acc += chunk
            m = self._pat.search(acc)
            if m:
                out = acc[: m.start()].decode(errors="replace")
                code = int(m.group(1))
                body = out.rstrip("\n")
                if not body:
                    body = f"[no output, exit code {code}]"
                elif code != 0:
                    body += f"\n[exit code {code}]"
                return body

    def close(self) -> None:
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except Exception:
                pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            pass


# One shell per thread, in LRU order so we can evict the oldest past the cap.
SESSIONS: "collections.OrderedDict[str, ShellSession]" = collections.OrderedDict()
SESSIONS_LOCK = threading.Lock()


def thread_key(message: dict) -> str:
    """A stable identifier for the Zulip thread a message belongs to."""
    if message["type"] == "stream":
        return f"stream:{message['stream_id']}:{message['subject']}"
    ids = sorted(r["id"] for r in message["display_recipient"])
    return "dm:" + ",".join(str(i) for i in ids)


def reset_session(key: str) -> None:
    with SESSIONS_LOCK:
        sess = SESSIONS.pop(key, None)
    if sess:
        sess.close()


def run_in_thread(key: str, command: str) -> str:
    """Run a command in the thread's shell, (re)creating it as needed."""
    for attempt in (1, 2):  # retry once if the shell died between commands
        with SESSIONS_LOCK:
            sess = SESSIONS.get(key)
            if sess is not None and not sess.alive():
                sess.close()
                del SESSIONS[key]
                sess = None
            if sess is None:
                sess = ShellSession()
                SESSIONS[key] = sess
            SESSIONS.move_to_end(key)
            while len(SESSIONS) > MAX_SESSIONS:
                _, evicted = SESSIONS.popitem(last=False)
                evicted.close()
        try:
            return sess.run(command)
        except (BrokenPipeError, OSError):
            reset_session(key)
            if attempt == 2:
                return "[shell error; the thread's shell was reset — try again]"
    return "[shell error]"  # unreachable


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


# One Managed Agent session id per thread, LRU-ordered.
AGENT_SESSIONS: "collections.OrderedDict[str, str]" = collections.OrderedDict()
AGENT_SESSIONS_LOCK = threading.Lock()


def agent_configured() -> bool:
    return bool(claude_client and AGENT_ID and ENVIRONMENT_ID)


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
    session = claude_client.beta.sessions.create(
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
            stats = claude_client.beta.environments.work.stats(
                environment_id=ENVIRONMENT_ID
            )
            return getattr(stats, "workers_polling", None)
        except Exception as exc:
            log(f"work.stats failed: {exc}")
            return None

    try:
        claude_client.beta.sessions.events.send(
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
            events = claude_client.beta.sessions.events.list(session_id=session_id).data
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
                "ANTHROPIC_API_KEY) to enable it.")
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


# One Claude Code session id per thread, LRU-ordered. Persisted to disk so a
# bot restart (deploys, platform instance restarts) doesn't make every thread
# forget its conversation — the transcripts themselves live on the fleet host.
FLEET_SESSIONS: "collections.OrderedDict[str, str]" = collections.OrderedDict()
FLEET_SESSIONS_LOCK = threading.Lock()
LAST_SEEN_FILE = os.environ.get(
    "SHELL_BOT_LAST_SEEN_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last-seen-id"),
)
FLEET_SESSIONS_FILE = os.environ.get(
    "SHELL_BOT_FLEET_SESSIONS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".fleet-sessions.json"),
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

# --- Night-mode ack ---
# 22:00–02:00 local: kettle on; 02:00–06:30: coffee. Otherwise the plain ack.
NIGHT_TZ = os.environ.get("SHELL_BOT_TZ", "Europe/London")
FLEET_ACK_DAY = "On it — running Claude Code… (this can take a while)"
FLEET_ACK_NIGHT = "Getting some coffee… (this can take a while)"
FLEET_ACK_EVENING = "Ah, a late one. Let me put the kettle on… (this can take a while)"


def fleet_ack() -> str:
    """The 'working on it' message, with evening (22:00–02:00) and night (02:00–06:30) variants."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo(NIGHT_TZ))
    except Exception:
        now = datetime.datetime.now()
    minutes = now.hour * 60 + now.minute
    if 2 * 60 <= minutes < 6 * 60 + 30:
        return FLEET_ACK_NIGHT
    if minutes >= 22 * 60 or minutes < 2 * 60:
        return FLEET_ACK_EVENING
    return FLEET_ACK_DAY


def strip_mention(content: str) -> str:
    """Remove a leading @-mention of the bot from the message content."""
    for mention in (f"@**{PROFILE['full_name']}**", f"@_**{PROFILE['full_name']}**"):
        if content.startswith(mention):
            return content[len(mention):].strip()
    return content.strip()


def format_reply(command: str, output: str) -> str:
    if len(output) > MAX_OUTPUT:
        output = output[:MAX_OUTPUT] + "\n[output truncated]"
    return f"`$ {command}`\n```\n{output}\n```"


def _remember_seen(message_id: int) -> None:
    try:
        tmp = LAST_SEEN_FILE + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(str(message_id))
        os.replace(tmp, LAST_SEEN_FILE)
    except OSError:
        pass


def catch_up_missed_messages() -> None:
    """Handle messages that arrived while the bot was down.

    The platform stops this box periodically and it takes a minute or two to
    be woken; mentions in that window would otherwise be silently lost.
    """
    try:
        with open(LAST_SEEN_FILE) as fh:
            last_id = int(fh.read().strip())
    except (OSError, ValueError):
        return
    try:
        res = client.get_messages({"anchor": last_id, "num_before": 0,
                                   "num_after": 200, "apply_markdown": False})
    except Exception as exc:  # pragma: no cover - network
        print(f"catch-up failed: {exc}", flush=True)
        return
    missed = [m for m in res.get("messages", []) if m["id"] > last_id]
    if missed:
        print(f"catching up on {len(missed)} message(s) missed while down", flush=True)
    for m in missed:
        try:
            handle_message(m)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"catch-up: error handling message {m['id']}: {exc}", flush=True)


def handle_message(message: dict) -> None:
    _remember_seen(message["id"])
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
            send_long(message, reply)

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
            reply_target(message, fleet_ack())
        )

        # Same deal as agent mode: don't block the message loop on a long run.
        def _run_fleet_and_reply(key=key, body=body, message=message):
            try:
                reply = run_fleet(key, body)
            except Exception as exc:  # pragma: no cover - defensive
                reply = f"[fleet error: {exc}]"
            if "ATTACH:" in reply:
                reply = attach_files(reply)
            send_long(message, reply)

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


def reply_target(message: dict, text: str) -> dict:
    """Build a send_message request that replies where the message came from."""
    if message["type"] == "private":
        recipients = [
            r["email"]
            for r in message["display_recipient"]
            if r["id"] != BOT_ID
        ]
        return {"type": "private", "to": recipients, "content": text}
    return {
        "type": "stream",
        "to": message["display_recipient"],
        "topic": message["subject"],
        "content": text,
    }


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
    catch_up_missed_messages()
    client.call_on_each_message(handle_message)


if __name__ == "__main__":
    main()
