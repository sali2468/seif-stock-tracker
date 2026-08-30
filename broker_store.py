"""broker_store.py — per-user, ENCRYPTED broker credential storage.

Each user's broker API keys are encrypted at rest with Fernet (AES-128-CBC + HMAC)
and written into that user's own user_data folder — so no user can read another's
keys, and the files are useless without the encryption key (kept in ~/.veterans_edge,
outside the project and gitignored).

⚠️ These are LIVE trading credentials. Encryption at rest protects the files if they
are copied, but a running (or compromised) app must decrypt them to call the broker
API — so read-only keys and OAuth, where available, are the real blast-radius limits.
"""

import os
import json
import logging

from cryptography.fernet import Fernet

from state import _safe_user, _USER_ROOT   # reuse the same per-user folder scheme

log = logging.getLogger("broker_store")

# Key dir follows DATA_DIR (persistent disk) on Render; falls back to ~ locally.
_DATA_DIR = os.getenv("DATA_DIR")
_KEY_DIR  = os.path.join(_DATA_DIR, ".veterans_edge") if _DATA_DIR \
            else os.path.join(os.path.expanduser("~"), ".veterans_edge")
_KEY_FILE = os.path.join(_KEY_DIR, "broker.key")
_fernet = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        # Prefer an env var so the key is STABLE across Render redeploys (a new key
        # each deploy would make already-saved broker.enc undecryptable). Set
        # BROKER_ENC_KEY to a Fernet key (see README). Falls back to a disk file.
        env_key = os.getenv("BROKER_ENC_KEY")
        if env_key:
            _fernet = Fernet(env_key.encode() if isinstance(env_key, str) else env_key)
            return _fernet
        os.makedirs(_KEY_DIR, exist_ok=True)
        key = None
        try:
            with open(_KEY_FILE, "rb") as f:
                key = f.read()
        except Exception:
            pass
        if not key:
            key = Fernet.generate_key()
            try:
                with open(_KEY_FILE, "wb") as f:
                    f.write(key)
            except Exception as e:
                log.warning("could not persist broker key: %s", e)
        _fernet = Fernet(key)
    return _fernet


def _path(username: str) -> str:
    return os.path.join(_USER_ROOT, _safe_user(username), "broker.enc")


def load(username: str) -> dict:
    """Return {broker: creds_dict} for this user (decrypted), or {} if none."""
    p = _path(username)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "rb") as f:
            token = f.read()
        return json.loads(_get_fernet().decrypt(token).decode())
    except Exception as e:
        log.warning("broker load failed for %s: %s", username, e)
        return {}


def _write(username: str, data: dict) -> None:
    p = _path(username)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    token = _get_fernet().encrypt(json.dumps(data).encode())
    tmp = p + ".tmp"
    with open(tmp, "wb") as f:
        f.write(token)
    os.replace(tmp, p)


def save(username: str, broker: str, creds: dict) -> None:
    data = load(username)
    data[broker] = creds
    _write(username, data)


def clear(username: str, broker: str) -> None:
    data = load(username)
    if broker in data:
        data.pop(broker, None)
        _write(username, data)


def connected(username: str) -> list:
    return list(load(username).keys())
