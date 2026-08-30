"""
auth.py — multi-user accounts for StockPal.

• Register username + password (bcrypt-hashed) + email.
• Login; roles: "admin" (bypasses paywalls, full access) or "user".
• Forgot password → emails a reset code (SMTP env) with on-screen fallback.
• Persistent local store: app_data/accounts.json (survives restarts, gitignored).

Admin is designated by the ADMIN_USER env var (that username becomes admin on
register), or the first account created becomes admin automatically.
Email needs SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS in .env; without them
the reset code is shown on screen instead.
"""

import os
import re
import json
import time
import hmac
import base64
import hashlib
import secrets
import smtplib
import logging
from email.message import EmailMessage

import bcrypt
import streamlit as st

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    pass

log = logging.getLogger("auth")

# ── Brute-force lockout ───────────────────────────────────────────────────────
# Module-global rate-limit state (shared across sessions — it's global throttle
# state, not per-user data). Resets on restart, which is fine.
_MAX_FAILS  = 5
_LOCK_SECS  = 900   # 15-minute lockout after too many failed logins
_FAILED: dict = {}  # username -> [count, window_start_ts]


def _username_ok(u: str) -> bool:
    """3–32 chars, must start alphanumeric, then letters/digits/._- only.
    Blocks path-traversal and other filesystem-hostile names."""
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,31}", u or ""))


def lockout_remaining(username: str) -> int:
    """Seconds remaining on a login lockout for this username, else 0."""
    u = (username or "").lower().strip()
    rec = _FAILED.get(u)
    if not rec:
        return 0
    count, start = rec
    if count >= _MAX_FAILS:
        remaining = int(_LOCK_SECS - (time.time() - start))
        if remaining > 0:
            return remaining
        _FAILED.pop(u, None)   # window expired — clear it
    return 0

# Accounts persist in TWO independent locations so a wipe of either survives:
#   primary = ~/.veterans_edge  (OUTSIDE the project — safe from repo/test cleanup)
#   mirror  = <project>/app_data (local copy)
# Losing accounts would require deleting both. Tests must monkeypatch these paths
# to a temp dir — never delete the real store.
# On Render, set DATA_DIR to the persistent-disk mount so BOTH copies land on the
# disk and survive restarts/redeploys. Unset locally → same paths as before.
_DATA_DIR = os.getenv("DATA_DIR")
_HOME_DIR = os.path.join(_DATA_DIR, ".veterans_edge") if _DATA_DIR \
            else os.path.join(os.path.expanduser("~"), ".veterans_edge")
_DIR      = _HOME_DIR                                   # secret.key lives here too
_DB       = os.path.join(_HOME_DIR, "accounts.json")
_MIRROR   = os.path.join(_DATA_DIR, "app_data", "accounts.json") if _DATA_DIR \
            else os.path.join(os.path.dirname(__file__), "app_data", "accounts.json")


# ── Persistent store (dual-location, self-healing) ────────────────────────────

def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _load() -> dict:
    # Prefer the primary; if it's gone, recover from the mirror and heal the primary.
    try:
        with open(_DB) as f:
            return json.load(f)
    except Exception:
        pass
    try:
        with open(_MIRROR) as f:
            data = json.load(f)
        try:
            _write_json(_DB, data)
        except Exception:
            pass
        return data
    except Exception:
        return {}


def _save(data: dict) -> None:
    # Write both locations; a single deletion never loses accounts.
    for p in (_DB, _MIRROR):
        try:
            _write_json(p, data)
        except Exception as e:
            log.warning("account save failed for %s: %s", p, e)


def _hash(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def _check(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False


def _admin_user() -> str:
    return os.getenv("ADMIN_USER", "").lower().strip()


# ── Account operations ────────────────────────────────────────────────────────

def register(username: str, password: str, email: str, agreed: bool = False):
    users = _load()
    u = username.lower().strip()
    if not _username_ok(u):
        return False, ("Username must be 3–32 characters: start with a letter or "
                       "number, then letters, numbers, . _ - only.")
    if not password:
        return False, "Username and password are required."
    if len(password) < 8:
        return False, "Password must be at least 8 characters."
    if not agreed:
        return False, "You must read and accept the Terms of Use & Risk Disclaimer to create an account."
    if u in users:
        return False, "That username is already taken."
    role = "admin" if u == _admin_user() else "user"   # only ADMIN_USER is ever admin
    users[u] = {"password": _hash(password), "email": email.strip().lower(),
                "role": role, "created": time.time(),
                "agreed_terms": True, "agreed_at": time.time()}  # legal record of consent
    _save(users)
    return True, ("Account created — you're the owner/admin." if role == "admin"
                  else "Account created.")


def login(username: str, password: str):
    """Return (ok, role, note). `note` carries a user-facing reason on failure
    (e.g. a lockout message); None on success or a plain wrong password."""
    u = username.lower().strip()
    locked = lockout_remaining(u)
    if locked:
        m, s = divmod(locked, 60)
        return False, None, f"Too many attempts. Try again in {m}m {s:02d}s."
    rec = _load().get(u)
    if rec and _check(password, rec["password"]):
        _FAILED.pop(u, None)
        role = "admin" if u == _admin_user() else "user"   # env is the authority
        return True, role, None
    # record the failure (reset a stale window first)
    f = _FAILED.get(u)
    if not f or (time.time() - f[1]) > _LOCK_SECS:
        f = [0, time.time()]
    f[0] += 1
    _FAILED[u] = f
    return False, None, None


def _send_email(to: str, subject: str, body: str) -> bool:
    host = os.getenv("SMTP_HOST", ""); user = os.getenv("SMTP_USER", ""); pw = os.getenv("SMTP_PASS", "")
    if not (host and user and pw and to):
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = subject; msg["From"] = user; msg["To"] = to
        msg.set_content(body)
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587"))) as s:
            s.starttls(); s.login(user, pw); s.send_message(msg)
        return True
    except Exception as e:
        log.warning("password-reset email failed: %s", e)
        return False


def request_reset(email: str):
    """Return (ok, message, fallback_code). fallback_code is set only if email couldn't be sent."""
    users = _load(); email = email.strip().lower()
    match = [u for u, r in users.items() if r.get("email") == email]
    if not match:
        return False, "No account found with that email.", None
    code = secrets.token_hex(3).upper()  # 6-char code
    users[match[0]]["reset"] = {"code": code, "exp": time.time() + 1800}
    _save(users)
    sent = _send_email(email, "StockPal — password reset",
                       f"Your password reset code is {code}. It expires in 30 minutes.")
    if sent:
        return True, "Reset code emailed to you.", None
    return True, "Email isn't configured — use the code below.", code


def reset_password(email: str, code: str, new_pw: str):
    if len(new_pw) < 6:
        return False, "New password must be at least 6 characters."
    users = _load(); email = email.strip().lower()
    for u, r in users.items():
        rs = r.get("reset")
        if r.get("email") == email and rs and rs["code"] == code.strip().upper() and time.time() < rs["exp"]:
            r["password"] = _hash(new_pw); r.pop("reset", None); _save(users)
            return True, "Password updated — log in with your new password."
    return False, "Invalid or expired reset code."


# ── Session helpers ───────────────────────────────────────────────────────────

# ── Signed session token (for the "stay logged in" cookie) ────────────────────

def _secret() -> bytes:
    # Prefer an env var (stable across Render redeploys — set SESSION_SECRET to any
    # long random string). Falls back to a file-backed key for local runs.
    _env = os.getenv("SESSION_SECRET")
    if _env:
        return _env.encode()
    os.makedirs(_DIR, exist_ok=True)
    kp = os.path.join(_DIR, "secret.key")
    try:
        with open(kp, "rb") as f:
            return f.read()
    except Exception:
        s = secrets.token_bytes(32)
        try:
            with open(kp, "wb") as f:
                f.write(s)
        except Exception:
            pass
        return s


def make_token(username: str, role: str) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps({"u": username, "r": role, "t": int(time.time())}).encode()).decode()
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def read_token(token: str, max_idle: int = 900):
    """Return (username, role) if the token is valid and fresher than max_idle (15 min), else None."""
    try:
        body, sig = token.split(".", 1)
        if not hmac.compare_digest(sig, hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]):
            return None
        d = json.loads(base64.urlsafe_b64decode(body.encode()))
        if time.time() - d.get("t", 0) > max_idle:
            return None
        if d.get("u") not in _load():          # account was deleted
            return None
        return d["u"], d.get("r", "user")
    except Exception:
        return None


def is_admin() -> bool:
    # Authoritative: admin is whoever ADMIN_USER names — never a stored/session role.
    u = (st.session_state.get("username") or "").lower().strip()
    return bool(u) and u == _admin_user()


def current_user() -> str:
    return st.session_state.get("username", "")


def logout():
    for k in ("authenticated", "username", "role"):
        st.session_state.pop(k, None)


def list_users() -> list:
    return [{"username": u, "email": r.get("email", ""), "role": r.get("role", "user")}
            for u, r in _load().items()]


def set_role(username: str, role: str):
    users = _load(); u = username.lower().strip()
    if u in users:
        users[u]["role"] = role; _save(users)


# ── Per-user Telegram (each user's own private alert channel) ─────────────────

def set_telegram(username: str, chat_id: str):
    users = _load(); u = username.lower().strip()
    if u in users:
        users[u]["telegram_chat_id"] = str(chat_id); _save(users)


def get_telegram(username: str) -> str:
    return _load().get((username or "").lower().strip(), {}).get("telegram_chat_id", "")


def clear_telegram(username: str):
    users = _load(); u = username.lower().strip()
    if u in users and "telegram_chat_id" in users[u]:
        users[u].pop("telegram_chat_id", None); _save(users)


def all_telegram() -> dict:
    """{username: chat_id} for every user who has connected Telegram — for the daemon."""
    return {u: r["telegram_chat_id"] for u, r in _load().items() if r.get("telegram_chat_id")}


# ── Per-user broker risk consent (recorded once, before any live connection) ──

def get_broker_consent(username: str) -> bool:
    return bool(_load().get((username or "").lower().strip(), {}).get("broker_consent"))


def set_broker_consent(username: str, agreed: bool = True):
    import time
    users = _load(); u = username.lower().strip()
    if u in users:
        if agreed:
            users[u]["broker_consent"] = True
            users[u]["broker_consent_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        else:
            users[u].pop("broker_consent", None)
            users[u].pop("broker_consent_at", None)
        _save(users)


# ── Auth gate + UI ────────────────────────────────────────────────────────────

def require_login() -> bool:
    if st.session_state.get("authenticated"):
        return True
    _show_auth()
    return False


def _show_auth():
    _c1, _c2, _c3 = st.columns([1, 2, 1])
    with _c2:
        st.markdown(
            '<div style="text-align:center;margin:40px 0 8px">'
            '<span style="font-size:1.9rem;font-weight:800;letter-spacing:-.02em;color:var(--fg)">StockPal</span>'
            '<span class="brand-pro" style="font-size:.7rem">PRO</span></div>'
            '<div style="text-align:center;color:var(--muted);font-size:.9rem;margin-bottom:22px">'
            'AI trading dashboard</div>', unsafe_allow_html=True)

        _tab_login, _tab_new, _tab_reset = st.tabs(["Log in", "Create account", "Forgot password"])

        with _tab_login:
            with st.form("login_form"):
                _u = st.text_input("Username")
                _p = st.text_input("Password", type="password")
                if st.form_submit_button("Log in", type="primary", use_container_width=True):
                    ok, role, note = login(_u, _p)
                    if ok:
                        st.session_state.update(authenticated=True, username=_u.lower().strip(), role=role)
                        st.rerun()
                    else:
                        st.error(note or "Invalid username or password.")

        with _tab_new:
            from legal import render_terms_expander
            render_terms_expander()
            with st.form("register_form"):
                _nu = st.text_input("Choose a username")
                _ne = st.text_input("Email (for password resets)")
                _np = st.text_input("Choose a password", type="password")
                _agree = st.checkbox(
                    "I have read and agree to the Terms of Use & Risk Disclaimer above, and I understand "
                    "this app is for education only and is **not** financial advice.")
                if st.form_submit_button("Create account", type="primary", use_container_width=True):
                    ok, msg = register(_nu, _np, _ne, agreed=_agree)
                    if ok:
                        _, role, _ = login(_nu, _np)
                        st.session_state.update(authenticated=True, username=_nu.lower().strip(), role=role)
                        st.success(msg); st.rerun()
                    else:
                        st.error(msg)

        with _tab_reset:
            if not st.session_state.get("_reset_stage"):
                with st.form("reset_req_form"):
                    _re = st.text_input("Your account email")
                    if st.form_submit_button("Send reset code", use_container_width=True):
                        ok, msg, code = request_reset(_re)
                        (st.success if ok else st.error)(msg)
                        if ok:
                            st.session_state["_reset_stage"] = _re.strip().lower()
                            if code:
                                st.info(f"Reset code: **{code}**")
                            st.rerun()
            else:
                with st.form("reset_do_form"):
                    st.caption(f"Resetting: {st.session_state['_reset_stage']}")
                    _code = st.text_input("Reset code")
                    _newp = st.text_input("New password", type="password")
                    if st.form_submit_button("Update password", type="primary", use_container_width=True):
                        ok, msg = reset_password(st.session_state["_reset_stage"], _code, _newp)
                        (st.success if ok else st.error)(msg)
                        if ok:
                            st.session_state.pop("_reset_stage", None)

    from legal import render_footer
    render_footer()
    st.stop()
