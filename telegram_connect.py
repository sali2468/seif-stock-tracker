"""telegram_connect.py — per-user Telegram alert connections.

One shared bot (@Edgetrackerbot) serves every user privately. Each user starts a
private chat with the bot through a one-time deep link that carries a unique code;
we read that code back via getUpdates, capture their personal chat_id, and store it
on their account. Alerts then go ONLY to that user's own chat — Telegram keeps every
chat private, so no user can ever see another user's messages.
"""

import os
import json
import time
import secrets
import logging

import requests

log = logging.getLogger("telegram_connect")

_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_API = f"https://api.telegram.org/bot{_TOKEN}"
_bot_username = None


def enabled() -> bool:
    return bool(_TOKEN)


def bot_username() -> str:
    """The bot's @username (cached), used to build the t.me deep link."""
    global _bot_username
    if _bot_username is None:
        _bot_username = ""
        if _TOKEN:
            try:
                r = requests.get(f"{_API}/getMe", timeout=8).json()
                _bot_username = r.get("result", {}).get("username", "") or ""
            except Exception as e:
                log.warning("getMe failed: %s", e)
    return _bot_username


def new_token() -> str:
    """A short unique connect code tied to one user's connect attempt."""
    return "u" + secrets.token_hex(5)


def connect_link(token: str) -> str:
    bu = bot_username()
    return f"https://t.me/{bu}?start={token}" if bu else ""


def find_chat_id(token: str):
    """Look through recent bot messages for '/start <token>' and return that
    user's chat_id (as a string), or None if they haven't started the bot yet."""
    if not _TOKEN:
        return None
    try:
        r = requests.get(f"{_API}/getUpdates", params={"timeout": 0, "limit": 100}, timeout=10).json()
        for upd in reversed(r.get("result", []) or []):
            msg = upd.get("message") or upd.get("edited_message") or {}
            text = (msg.get("text") or "").strip()
            if text in (f"/start {token}", token):
                cid = msg.get("chat", {}).get("id")
                return str(cid) if cid is not None else None
    except Exception as e:
        log.warning("find_chat_id failed: %s", e)
    return None


# ── Pending connect tokens (shared between the app and the daemon's bot) ──────
# The app records token→user here; the daemon's /start handler resolves it and
# stores the user's chat_id. A file is used so both processes see the same data.
_PENDING = os.path.join(os.path.dirname(__file__), ".telegram_pending.json")


def _load_pending() -> dict:
    try:
        with open(_PENDING) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_pending(d: dict) -> None:
    try:
        tmp = _PENDING + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, _PENDING)
    except Exception as e:
        log.warning("save pending failed: %s", e)


def create_pending(token: str, username: str) -> None:
    now = time.time()
    d = {k: v for k, v in _load_pending().items() if v.get("exp", 0) > now}  # prune expired
    d[token] = {"user": username, "exp": now + 900}                          # 15-min window
    _save_pending(d)


def resolve_pending(token: str):
    """Return the username for a valid pending token (consuming it), else None."""
    d = _load_pending()
    rec = d.pop(token, None)
    _save_pending(d)
    if rec and rec.get("exp", 0) > time.time():
        return rec.get("user")
    return None


def send_to(chat_id: str, text: str) -> bool:
    """Send one message to one specific chat_id (a single user's private chat)."""
    if not (_TOKEN and chat_id):
        return False
    try:
        r = requests.post(
            f"{_API}/sendMessage",
            json={"chat_id": str(chat_id), "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
        return bool(r.ok and r.json().get("ok"))
    except Exception as e:
        log.warning("send_to failed: %s", e)
        return False
