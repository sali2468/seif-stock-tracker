"""
Position storage — saved to positions.json so data survives restarts.
Tracks open trades, closed trades, and watchlist.
"""

import json
import os
import re
import copy
import shutil
from datetime import date

# Load .env so ADMIN_USER (the owner account) resolves in every context —
# the Streamlit app, the alert daemon, and one-off scripts all import this.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    pass

# ── Per-user storage ──────────────────────────────────────────────────────────
# Each account gets a private store at  user_data/<username>/positions.json  so
# no account can ever see another's positions, watchlist, or trades. The routing
# key comes from st.session_state (per-session in Streamlit — NOT a module global,
# which would leak across the concurrent users sharing this one process). Outside
# Streamlit (daemon/alerts) it falls back to the owner (ADMIN_USER).
# On Render (or any host with a persistent disk) set DATA_DIR to the mount path so
# per-user data survives restarts/redeploys. Unset locally → same paths as before.
_BASE_DIR    = os.getenv("DATA_DIR") or os.path.dirname(__file__)
_LEGACY_FILE = os.path.join(_BASE_DIR, "positions.json")
_USER_ROOT   = os.path.join(_BASE_DIR, "user_data")

DEFAULT_STATE = {
    "positions": {},      # ticker → trade dict
    "closed":    [],      # list of closed trade dicts
    "options":   {},      # id → option position dict
    "closed_options": [], # list of closed option dicts
    "watchlist": [
        # ── Technology ────────────────────────────────────────────────────────
        "AAPL","MSFT","NVDA","AMD","INTC","QCOM","AVGO","TXN",
        "MU","MRVL","AMAT","KLAC","LRCX","CRM","ORCL","ADBE",
        "NOW","SNOW","PLTR","COIN","UBER","LYFT","SHOP","NET",
        # ── Communication Services ────────────────────────────────────────────
        "META","GOOGL","NFLX","DIS","CMCSA","T","VZ","ROKU","SNAP","PINS",
        # ── Consumer Discretionary ────────────────────────────────────────────
        "AMZN","TSLA","NKE","MCD","SBUX","HD","LOW","TJX",
        "BKNG","ABNB","GM","F","RIVN","LCID","RH","DECK",
        # ── Consumer Staples ──────────────────────────────────────────────────
        "WMT","COST","PG","KO","PEP","PM","MO","MDLZ","CL","EL",
        # ── Healthcare & Biotech ──────────────────────────────────────────────
        "UNH","JNJ","PFE","ABBV","MRK","LLY","TMO","DHR",
        "VRTX","REGN","BIIB","GILD","MRNA","DXCM","ISRG",
        "VKTX","SMMT","RXRX","ACMR",
        # ── Financials ────────────────────────────────────────────────────────
        "JPM","BAC","GS","MS","WFC","C","V","MA","AXP",
        "BLK","SCHW","COF","SQ","PYPL","HOOD","SOFI",
        # ── Energy ────────────────────────────────────────────────────────────
        "XOM","CVX","COP","EOG","SLB","MPC","PSX","VLO",
        "OXY","DVN","FANG","HAL","XLE",
        # ── Industrials & Aerospace ───────────────────────────────────────────
        "CAT","DE","HON","UNP","LMT","RTX","GE","BA",
        "NOC","GD","HII","SPR","RKLB","LUNR","ACHR","JOBY",
        # ── Materials ─────────────────────────────────────────────────────────
        "FCX","NEM","GOLD","LIN","APD","NUE","X","CLF","AA",
        # ── Real Estate ───────────────────────────────────────────────────────
        "AMT","PLD","EQIX","SPG","O","VICI","IRM",
        # ── Utilities ─────────────────────────────────────────────────────────
        "NEE","DUK","SO","XEL","AEP","EXC",
        # ── Airlines & Travel ─────────────────────────────────────────────────
        "DAL","UAL","AAL","LUV","ALK","CCL","RCL","NCLH",
        # ── Broad ETFs ────────────────────────────────────────────────────────
        "SPY","QQQ","IWM","GLD","TLT","SLV",
        "XLF","XLK","XLE","XLV","XLI","XLY","XLP","XLB","XLRE","XLU",
    ],
}


def _safe_user(name: str) -> str:
    """Sanitize a username into a safe folder name (blocks path traversal)."""
    s = re.sub(r"[^a-z0-9_.-]", "", (name or "").lower().strip()).strip(".")
    return s or "_shared"


def _owner() -> str:
    return _safe_user(os.getenv("ADMIN_USER", "") or "_shared")


# Daemon-only override: lets the alert daemon read/write a SPECIFIC user's store,
# so it can monitor every user's positions and route alerts to each user's chat.
_daemon_user = None


def set_active_user_override(username):
    global _daemon_user
    _daemon_user = _safe_user(username) if username else None


def users_with_data() -> list:
    """Usernames that have a positions store — for the alert daemon to iterate."""
    try:
        return [d for d in os.listdir(_USER_ROOT)
                if os.path.isdir(os.path.join(_USER_ROOT, d))]
    except Exception:
        return []


def _active_user() -> str:
    """The account whose store to read/write — the logged-in Streamlit user; the
    daemon's override user when set; else the owner (daemon/alerts/scripts)."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        if get_script_run_ctx(suppress_warning=True) is not None:
            import streamlit as st
            u = st.session_state.get("username")
            if u:
                return _safe_user(u)
    except Exception:
        pass
    if _daemon_user:
        return _daemon_user
    return _owner()


def _state_file() -> str:
    return os.path.join(_USER_ROOT, _active_user(), "positions.json")


def _load() -> dict:
    path = _state_file()
    # One-time migration: the owner's legacy global store → their private store.
    if not os.path.exists(path) and _active_user() == _owner() and os.path.exists(_LEGACY_FILE):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            shutil.copyfile(_LEGACY_FILE, path)
        except Exception:
            pass
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
                # merge missing keys from default
                for k, v in DEFAULT_STATE.items():
                    data.setdefault(k, v)
                return data
        except Exception:
            pass
    return copy.deepcopy(DEFAULT_STATE)


def _save(state: dict):
    path = _state_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, path)


def get_state() -> dict:
    return _load()


def add_position(ticker: str, entry: float, stop: float,
                 target1: float, target2: float, qty: int, notes: str = "",
                 setup_type: str = "", stars: int = 0,
                 rs_rank: float = 0.0, trend_template: int = 0,
                 sector: str = ""):
    """
    Open a new position.  Pass scanner metadata (setup_type, stars, rs_rank,
    trend_template, sector) so the performance dashboard can break down results
    by signal quality later.
    """
    state = _load()
    tk = ticker.upper()

    # Already hold this ticker? COMBINE rather than overwrite: weighted-average the
    # entry / stop / targets by share count, sum the shares, and readjust the levels.
    _existing = state["positions"].get(tk)
    if _existing:
        _oq  = float(_existing.get("qty", 0) or 0)
        _nq  = float(qty or 0)
        _tot = _oq + _nq
        if _tot > 0:
            def _wavg(_old, _new):
                try:
                    return round((float(_old) * _oq + float(_new) * _nq) / _tot, 4)
                except Exception:
                    return _new
            _existing["entry"]   = _wavg(_existing.get("entry", entry),     entry)
            _existing["stop"]    = _wavg(_existing.get("stop", stop),       stop)
            _existing["target1"] = _wavg(_existing.get("target1", target1), target1)
            _existing["target2"] = _wavg(_existing.get("target2", target2), target2)
            _existing["qty"]           = round(_tot, 6)
            _existing["qty_remaining"] = round(float(_existing.get("qty_remaining", _oq)) + _nq, 6)
            if notes:
                _existing["notes"] = (_existing.get("notes", "") + " | " + notes).strip(" |")
            _existing.setdefault("adds", []).append(
                {"qty": _nq, "entry": round(float(entry), 4), "date": date.today().isoformat()})
            _save(state)
            return

    state["positions"][tk] = {
        "ticker":         tk,
        "entry":          entry,
        "stop":           stop,
        "target1":        target1,
        "target2":        target2,
        "qty":            qty,
        "notes":          notes,
        "date_in":        date.today().isoformat(),
        "days_held":      0,
        # ── signal metadata (for performance analysis) ────────────────────────
        "setup_type":     setup_type,      # "swing" | "day" | "vcp" | "manual"
        "stars":          stars,           # 1–3 quality at entry
        "rs_rank":        rs_rank,         # 0–99 RS rank at entry
        "trend_template": trend_template,  # 0–8 TT score at entry
        "sector":         sector,          # sector at entry
        # ── managed exit system (scanner positions only) ───────────────────────
        "managed":        setup_type in ("swing", "day", "vcp"),
        "qty_remaining":  qty,            # tracks shares left after partial exits
        "exit1_done":     False,          # T1 hit and sold
        "exit2_done":     False,          # T2 hit and sold
        "trailing":       False,          # trailing stop active on remainder
        "trail_stop":     None,           # current trailing stop price
        # Alert flags — set True once alert has fired to prevent spamming
        "alerted_stop":       False,
        "alerted_target":     False,
        "alerted_raise":      False,   # reset to False whenever stop is raised
        "alerted_stale":      False,
        "alerted_overbought": False,
        "alerted_watch":      False,   # WATCH CLOSELY one-time alert
        "alerted_chart_sell": False,   # chart-analysis SELL signal alert
    }
    _save(state)


def close_position(ticker: str, exit_price: float, reason: str):
    state = _load()
    ticker = ticker.upper()
    if ticker not in state["positions"]:
        return
    pos = state["positions"].pop(ticker)
    pnl_pct    = (exit_price - pos["entry"]) / pos["entry"] * 100
    pnl_dollars = (exit_price - pos["entry"]) * pos["qty"]
    state["closed"].append({
        **pos,
        "exit_price":  exit_price,
        "exit_reason": reason,
        "date_out":    date.today().isoformat(),
        "pnl_pct":     round(pnl_pct, 2),
        "pnl_dollars": round(pnl_dollars, 2),
    })
    _save(state)


def partial_exit_position(ticker: str, qty_sold: int, exit_price: float, reason: str) -> dict:
    """
    Sell part of a managed position.
    Reduces qty_remaining, records partial exit, returns updated pos dict.
    Does NOT close the position — call close_position() for a full exit.
    """
    state = _load()
    ticker = ticker.upper()
    if ticker not in state["positions"]:
        return {}
    pos   = state["positions"][ticker]
    entry = float(pos.get("entry", exit_price))
    pnl_pct = round((exit_price - entry) / entry * 100, 2) if entry else 0
    pnl_dol = round((exit_price - entry) * qty_sold, 2)

    pos["qty_remaining"] = round(max(0.0, float(pos.get("qty_remaining", pos["qty"])) - qty_sold), 6)

    state.setdefault("partial_exits", []).append({
        "ticker":      ticker,
        "qty_sold":    qty_sold,
        "exit_price":  exit_price,
        "exit_reason": reason,
        "date":        date.today().isoformat(),
        "pnl_pct":     pnl_pct,
        "pnl_dollars": pnl_dol,
    })
    _save(state)
    return dict(pos)


def add_to_position(ticker: str, qty_bought: int, add_price: float) -> dict:
    """
    Record a pullback add. Increases qty_remaining, updates avg entry price,
    logs the add in the position's 'adds' list.
    Returns updated position dict.
    """
    state  = _load()
    ticker = ticker.upper()
    if ticker not in state["positions"]:
        return {}
    pos = state["positions"][ticker]

    orig_qty  = float(pos.get("qty_remaining", pos["qty"]))
    orig_entry = float(pos.get("entry", add_price))

    # Weighted average entry
    new_total_qty  = round(orig_qty + qty_bought, 6)
    new_avg_entry  = round(
        (orig_entry * orig_qty + add_price * qty_bought) / new_total_qty, 4
    )

    pos["qty_remaining"] = new_total_qty
    pos["entry"]         = new_avg_entry   # blended entry for P&L display

    pos.setdefault("adds", []).append({
        "qty":   qty_bought,
        "price": add_price,
        "date":  date.today().isoformat(),
    })

    _save(state)
    return dict(pos)


def update_managed_flags(ticker: str, **flags):
    """
    Patch any managed-exit fields on a position:
      exit1_done, exit2_done, trailing, trail_stop, qty_remaining, …
    """
    state = _load()
    ticker = ticker.upper()
    if ticker in state["positions"]:
        state["positions"][ticker].update(flags)
        _save(state)


def update_stop(ticker: str, new_stop: float):
    state = _load()
    ticker = ticker.upper()
    if ticker in state["positions"]:
        state["positions"][ticker]["stop"] = new_stop
        # Reset alerts that should re-fire if conditions deteriorate again
        state["positions"][ticker]["alerted_raise"]      = False
        state["positions"][ticker]["alerted_watch"]      = False
        state["positions"][ticker]["alerted_chart_sell"] = False
        _save(state)


def update_targets(ticker: str, target1: float, target2: float):
    """Raise profit targets — only ever moves upward, never lowers them."""
    state = _load()
    ticker = ticker.upper()
    if ticker in state["positions"]:
        pos     = state["positions"][ticker]
        changed = False
        if target1 > pos.get("target1", 0):
            pos["target1"] = round(target1, 2)
            changed = True
        if target2 > pos.get("target2", 0):
            pos["target2"] = round(target2, 2)
            changed = True
        if changed:
            _save(state)


def mark_position_alerted(ticker: str, flag: str):
    """
    flag = 'alerted_stop' | 'alerted_target' | 'alerted_raise'
           | 'alerted_stale' | 'alerted_overbought' | 'alerted_watch'
    """
    state = _load()
    ticker = ticker.upper()
    if ticker in state["positions"]:
        state["positions"][ticker][flag] = True
        _save(state)


def add_to_watchlist(ticker: str):
    state = _load()
    t = ticker.upper()
    if t not in state["watchlist"]:
        state["watchlist"].append(t)
        _save(state)


def remove_from_watchlist(ticker: str):
    state = _load()
    t = ticker.upper()
    if t in state["watchlist"]:
        state["watchlist"].remove(t)
        _save(state)


def set_watchlist(tickers: list):
    """Replace the entire watchlist (deduped, uppercased)."""
    state = _load()
    seen, clean = set(), []
    for t in tickers:
        u = t.upper().strip()
        if u and u not in seen:
            seen.add(u)
            clean.append(u)
    state["watchlist"] = clean
    _save(state)


def load_sp500() -> list:
    """
    Fetch the current S&P 500 ticker list from Wikipedia.
    Returns list of tickers, or empty list on failure.
    """
    try:
        import pandas as pd
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            attrs={"id": "constituents"},
        )
        tickers = tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist()
        return [t.upper() for t in tickers if t]
    except Exception:
        return []


def get_positions() -> dict:
    return _load()["positions"]


def get_closed() -> list:
    return _load()["closed"]


def get_watchlist() -> list:
    return _load()["watchlist"]


# ── Options ───────────────────────────────────────────────────────────────────

def _opt_id(underlying: str, opt_type: str, strike: float, expiry: str) -> str:
    return f"{underlying.upper()}_{opt_type.lower()}_{strike}_{expiry}"


def add_option(underlying: str, opt_type: str, strike: float, expiry: str,
               contracts: int, entry_price: float, stop_price: float,
               target_price: float, notes: str = "") -> str:
    """Add an option position. Returns the position ID."""
    state = _load()
    oid   = _opt_id(underlying, opt_type, strike, expiry)
    state.setdefault("options", {})[oid] = {
        "id":          oid,
        "underlying":  underlying.upper(),
        "opt_type":    opt_type.lower(),   # "call" or "put"
        "strike":      strike,
        "expiry":      expiry,             # "YYYY-MM-DD"
        "contracts":   contracts,
        "entry_price": entry_price,        # premium per share
        "stop_price":  stop_price,
        "target_price": target_price,
        "date_in":     date.today().isoformat(),
        "notes":       notes,
        # Alert flags — set True once we've sent the alert so we don't spam
        "alerted_stop":   False,
        "alerted_target": False,
        "alerted_expiry": False,
    }
    _save(state)
    return oid


def get_options() -> dict:
    return _load().get("options", {})


def get_closed_options() -> list:
    return _load().get("closed_options", [])


def close_option(oid: str, exit_price: float, reason: str):
    state = _load()
    opts  = state.get("options", {})
    if oid not in opts:
        return
    pos = opts.pop(oid)
    pnl_per   = round((exit_price - pos["entry_price"]) * 100, 2)
    pnl_total = round(pnl_per * pos["contracts"], 2)
    pnl_pct   = round((exit_price - pos["entry_price"]) / pos["entry_price"] * 100, 2)
    state.setdefault("closed_options", []).append({
        **pos,
        "exit_price":  exit_price,
        "exit_reason": reason,
        "date_out":    date.today().isoformat(),
        "pnl_per_contract": pnl_per,
        "pnl_total":   pnl_total,
        "pnl_pct":     pnl_pct,
    })
    _save(state)


def mark_option_alerted(oid: str, flag: str):
    """flag = 'alerted_stop' | 'alerted_target' | 'alerted_expiry'"""
    state = _load()
    if oid in state.get("options", {}):
        state["options"][oid][flag] = True
        _save(state)
