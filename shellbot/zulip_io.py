"""Zulip client, thread identity, and reply plumbing."""

import io
import os
import re
import subprocess
import threading

import zulip

from .config import FLEET_ATTACH_HOST, FLEET_ATTACH_MAX_BYTES, ZULIPRC


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

# Serialize outbound sends — background agent runs post replies from worker
# threads while the main loop keeps handling messages.
SEND_LOCK = threading.Lock()
_raw_send = client.send_message


def safe_send(request: dict):
    with SEND_LOCK:
        return _raw_send(request)


def thread_key(message: dict) -> str:
    """A stable identifier for the Zulip thread a message belongs to."""
    if message["type"] == "stream":
        return f"stream:{message['stream_id']}:{message['subject']}"
    ids = sorted(r["id"] for r in message["display_recipient"])
    return "dm:" + ",".join(str(i) for i in ids)


def strip_mention(content: str) -> str:
    """Remove a leading @-mention of the bot from the message content."""
    for mention in (f"@**{PROFILE['full_name']}**", f"@_**{PROFILE['full_name']}**"):
        if content.startswith(mention):
            return content[len(mention):].strip()
    return content.strip()


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


def send_long(message: dict, text: str, limit: int = 9000) -> None:
    """Post a reply, splitting it into several Zulip messages if it is long.

    Zulip caps a message at 10,000 characters. Split on paragraph boundaries
    where possible and never drop content: long agent reports often carry
    the question or the key result at the end.
    """
    chunks, buf = [], ""
    for para in text.split("\n\n"):
        cand = (buf + "\n\n" + para) if buf else para
        if len(cand) <= limit:
            buf = cand
            continue
        if buf:
            chunks.append(buf)
        while len(para) > limit:
            chunks.append(para[:limit])
            para = para[limit:]
        buf = para
    if buf:
        chunks.append(buf)
    n = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        suffix = f"\n\n*(part {i}/{n})*" if n > 1 else ""
        safe_send(reply_target(message, chunk + suffix))


_ATTACH_RE = re.compile(r"^\s*ATTACH:\s*(\S+)\s*$", re.MULTILINE)


def attach_files(text: str) -> str:
    """Replace `ATTACH: /abs/path` lines with Zulip upload links (see prod shell_bot.py)."""
    def _fetch(path):
        try:
            if FLEET_ATTACH_HOST:
                proc = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=60",
                                       FLEET_ATTACH_HOST, "cat", path], capture_output=True, timeout=120)
                return proc.stdout if proc.returncode == 0 else None
            with open(path, "rb") as fh:
                return fh.read()
        except (OSError, subprocess.SubprocessError):
            return None

    def _repl(m):
        path = m.group(1); data = _fetch(path)
        if data is None:
            return f"*(attachment `{path}` could not be fetched)*"
        if len(data) > FLEET_ATTACH_MAX_BYTES:
            return f"*(attachment `{path}` too large: {len(data)} bytes)*"
        name = os.path.basename(path)
        try:
            fh = io.BytesIO(data); fh.name = name
            res = client.upload_file(fh)
            uri = res.get("url") or res.get("uri")
            return f"[{name}]({uri})" if uri else f"*(upload of `{name}` failed: {res.get('msg', 'no url')})*"
        except Exception as exc:
            return f"*(upload of `{name}` failed: {exc})*"

    return _ATTACH_RE.sub(_repl, text)
