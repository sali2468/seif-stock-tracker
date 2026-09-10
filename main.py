"""StockPal API — FastAPI layer over the existing Python "brain".

Backend half of the hybrid architecture. Reuses the SAME modules the Streamlit
app uses (reco, scanner, auth, state, fundamentals, market_data) — the trading
logic and the accounts are not re-implemented, only exposed over HTTP.

Run locally:  uvicorn main:app --reload --port 8000
"""
import os
import json

# Self-contained: the brain modules (reco, scanner, auth, state, ...) live in this
# same folder, so they import directly. Env vars come from Render in production; a
# local .env is loaded for development.
_HERE = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_HERE, ".env"))
except Exception:
    pass

from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import reco as _reco
import auth as _auth
import state as _state

# First-boot scan seed: on a fresh disk the Scanner would be empty, so drop the
# bundled scan into the cache dir if none exists yet (staging convenience).
try:
    import shutil as _shutil
    import scanner as _scanner
    _seed = os.path.join(_HERE, "seed_scan.pkl")
    if os.path.exists(_seed) and not os.path.exists(_scanner._RESULT_FILE):
        os.makedirs(os.path.dirname(_scanner._RESULT_FILE), exist_ok=True)
        _shutil.copyfile(_seed, _scanner._RESULT_FILE)
except Exception:
    pass

app = FastAPI(title="StockPal API", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to the frontend origin before going live
    allow_methods=["*"],
    allow_headers=["*"],
)

_TOKEN_TTL = 7 * 24 * 3600  # SPA tokens valid a week from login


# ── Auth dependency ───────────────────────────────────────────────────────────
def current_user(authorization: str = Header(default="")):
    tok = authorization
    if tok.lower().startswith("bearer "):
        tok = tok[7:]
    res = _auth.read_token(tok.strip(), max_idle=_TOKEN_TTL)
    if not res:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"username": res[0], "role": res[1]}


def _positions_for(username: str) -> dict:
    """Read one user's positions.json directly (race-free — no global override)."""
    path = os.path.join(_state._USER_ROOT, _state._safe_user(username), "positions.json")
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return (json.load(f) or {}).get("positions", {}) or {}
    except Exception:
        pass
    return {}


# ── Public endpoints ──────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "service": "stockpal-api"}


@app.get("/api/horizons")
def horizons():
    return {"horizons": _reco.HORIZONS, "default": _reco.DEFAULT_HORIZON}


@app.get("/api/reco")
def get_reco(ticker: str, horizon: str = _reco.DEFAULT_HORIZON):
    return _reco.recommend(ticker.upper().strip(), horizon)


@app.get("/api/scan")
def get_scan(max_age_s: int = 2592000):  # accept the latest cached scan (staging demo)
    try:
        from scanner import load_scan_result
        res = load_scan_result(max_age_s=max_age_s)
        if not res:
            return {"ready": False, "swing": [], "day": [], "reversals": []}
        _, swing, day, _vcp, mom = res

        def _ser(sigs, n=20):
            out = []
            for s in sigs[:n]:
                out.append({
                    "ticker": s.ticker,
                    "price": round(float(getattr(s, "price", 0) or 0), 2),
                    "stars": int(getattr(s, "stars", 0) or 0),
                    "stop": round(float(getattr(s, "stop", 0) or 0), 2),
                    "target1": round(float(getattr(s, "target1", 0) or 0), 2),
                    "why": (getattr(s, "why_buy", "") or getattr(s, "why", "") or "")[:220],
                })
            return out

        return {"ready": True, "swing": _ser(swing), "day": _ser(day), "reversals": _ser(mom)}
    except Exception as e:
        return {"ready": False, "error": str(e)}


# ── Auth endpoints ────────────────────────────────────────────────────────────
class LoginIn(BaseModel):
    username: str
    password: str


class RegisterIn(BaseModel):
    username: str
    password: str
    email: str = ""
    agreed: bool = True


@app.post("/api/auth/login")
def api_login(body: LoginIn):
    ok, role, note = _auth.login(body.username, body.password)
    if not ok:
        raise HTTPException(status_code=401, detail=note or "Invalid username or password.")
    u = body.username.lower().strip()
    return {"token": _auth.make_token(u, role), "username": u, "role": role}


@app.post("/api/auth/register")
def api_register(body: RegisterIn):
    ok, msg = _auth.register(body.username, body.password, body.email, agreed=body.agreed)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    u = body.username.lower().strip()
    _ok2, role, _ = _auth.login(body.username, body.password)
    return {"token": _auth.make_token(u, role or "user"), "username": u, "role": role or "user", "message": msg}


@app.get("/api/auth/me")
def api_me(user=Depends(current_user)):
    return user


# ── Per-user endpoints ────────────────────────────────────────────────────────
@app.get("/api/portfolio")
def api_portfolio(horizon: str = _reco.DEFAULT_HORIZON, user=Depends(current_user)):
    """The logged-in user's positions, each with a Hold/Sell/Buy-more reco."""
    positions = _positions_for(user["username"])
    out = []
    for tk, pos in positions.items():
        try:
            rec = _reco.recommend(tk, horizon, position=pos)
        except Exception:
            rec = {"error": "reco_failed"}
        out.append({"ticker": tk, "position": pos, "reco": rec})
    # strongest concern first: Sell, then Buy-more, then Hold
    _rank = {"SELL": 0, "BUY_MORE": 1, "HOLD": 2}
    out.sort(key=lambda x: _rank.get((x["reco"] or {}).get("verdict"), 3))
    return {"count": len(out), "positions": out}


# ── Serve the built React app (single service) ────────────────────────────────
# Mounted LAST so the /api/* routes above take precedence; html=True serves
# index.html at "/" and the hashed assets under it.
_STATIC = os.path.join(_HERE, "static")
if os.path.isdir(_STATIC):
    app.mount("/", StaticFiles(directory=_STATIC, html=True), name="static")
