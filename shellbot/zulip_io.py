"""Zulip client, thread identity, and reply plumbing."""

import os
import threading

import zulip

from .config import ZULIPRC


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
