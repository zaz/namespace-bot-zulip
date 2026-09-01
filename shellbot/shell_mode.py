"""`!` mode: one persistent bash session per Zulip thread."""

import collections
import os
import re
import select
import signal
import subprocess
import threading
import time

from .config import CWD, MAX_OUTPUT, MAX_SESSIONS, SHELL, SHELL_ENV, TIMEOUT


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


def format_reply(command: str, output: str) -> str:
    if len(output) > MAX_OUTPUT:
        output = output[:MAX_OUTPUT] + "\n[output truncated]"
    return f"`$ {command}`\n```\n{output}\n```"
