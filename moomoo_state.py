"""
Moomoo trade-plan persistence.
Saved to moomoo_plans.json so plans survive restarts.

A "trade plan" is created when the user hits "Send to Moomoo" and contains:
  - entry order details + the auto stop-loss + auto take-profit orders placed
  - pullback-add config (ATR zones, EMA touch, RSI, volume filter)
  - staged-exit state (which partials have fired)
  - full order-ID history so we can query / cancel

Status flow:
  pending → active → partial → closed | cancelled
"""

import json
import os
from datetime import datetime

PLAN_FILE = os.path.join(os.path.dirname(__file__), "moomoo_plans.json")

_DEFAULT = {"plans": {}}          # ticker → list[plan_dict]  (one ticker can have multiple plans)


# ─── internal load / save ─────────────────────────────────────────────────────

def _load() -> dict:
    if os.path.exists(PLAN_FILE):
        try:
            with open(PLAN_FILE, "r") as f:
                data = json.load(f)
                data.setdefault("plans", {})
                return data
        except Exception:
            pass
    return dict(_DEFAULT)


def _save(state: dict):
    with open(PLAN_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ─── public helpers ───────────────────────────────────────────────────────────

def get_all_plans() -> dict:
    """Return {ticker: [plan, …]}."""
    return _load()["plans"]


def get_active_plans() -> list:
    """Return every plan whose status is pending / active / partial."""
    active = []
    for plans in _load()["plans"].values():
        for p in plans:
            if p["status"] in ("pending", "active", "partial"):
                active.append(p)
    return active


def get_plans_for(ticker: str) -> list:
    return _load()["plans"].get(ticker.upper(), [])


def add_plan(plan: dict) -> str:
    """
    Persist a new trade plan and return its plan_id.
    Caller must supply all required fields (see create_plan_dict helper below).
    """
    state = _load()
    ticker = plan["ticker"].upper()
    state["plans"].setdefault(ticker, [])
    state["plans"][ticker].append(plan)
    _save(state)
    return plan["plan_id"]


def update_plan(plan_id: str, updates: dict):
    """Patch arbitrary fields on a plan identified by plan_id."""
    state = _load()
    for ticker_plans in state["plans"].values():
        for p in ticker_plans:
            if p["plan_id"] == plan_id:
                p.update(updates)
                _save(state)
                return
    raise KeyError(f"Plan {plan_id} not found")


def close_plan(plan_id: str, reason: str, pnl_pct: float = 0.0):
    """Mark a plan closed."""
    update_plan(plan_id, {
        "status":       "closed",
        "close_reason": reason,
        "pnl_pct":      round(pnl_pct, 2),
        "closed_at":    datetime.utcnow().isoformat(),
    })


def cancel_plan(plan_id: str, reason: str = "manual"):
    update_plan(plan_id, {
        "status":        "cancelled",
        "cancel_reason": reason,
        "cancelled_at":  datetime.utcnow().isoformat(),
    })


def delete_all_closed():
    """Remove closed/cancelled plans (housekeeping)."""
    state = _load()
    for ticker in list(state["plans"].keys()):
        state["plans"][ticker] = [
            p for p in state["plans"][ticker]
            if p["status"] not in ("closed", "cancelled")
        ]
        if not state["plans"][ticker]:
            del state["plans"][ticker]
    _save(state)


# ─── plan-dict factory ────────────────────────────────────────────────────────

def create_plan_dict(
    ticker: str,
    entry_price: float,
    qty: int,
    stop_price: float,
    target1_price: float,           # 2R exit (33 % of position)
    target2_price: float,           # 3R exit (33 % of position)
    atr: float,
    ema21: float,
    trd_env: str = "SIMULATE",      # "REAL" or "SIMULATE"
    notes: str = "",
) -> dict:
    """
    Build a new plan dict with all default fields.
    plan_id is a timestamp string — unique enough for this app.
    """
    import time
    plan_id = f"{ticker.upper()}_{int(time.time()*1000)}"

    # Pullback-add zone (ATR-scaled)
    pb_zone_high = round(entry_price - 0.5 * atr, 4)
    pb_zone_low  = round(entry_price - 1.0 * atr, 4)
    danger_zone  = round(entry_price - 1.5 * atr, 4)

    # Risk / reward
    risk     = entry_price - stop_price
    rr1      = round((target1_price - entry_price) / risk, 2) if risk > 0 else 0
    rr2      = round((target2_price - entry_price) / risk, 2) if risk > 0 else 0

    return {
        # ── identity ──────────────────────────────────────────────
        "plan_id":        plan_id,
        "ticker":         ticker.upper(),
        "trd_env":        trd_env,            # REAL or SIMULATE
        "status":         "pending",          # pending→active→partial→closed
        "created_at":     datetime.utcnow().isoformat(),

        # ── entry ─────────────────────────────────────────────────
        "entry_price":    round(entry_price, 4),
        "qty":            qty,                # initial shares (50 % of full size)
        "qty_remaining":  qty,

        # ── risk ──────────────────────────────────────────────────
        "stop_price":     round(stop_price, 4),
        "target1_price":  round(target1_price, 4),  # 2R
        "target2_price":  round(target2_price, 4),  # 3R
        "rr1":            rr1,
        "rr2":            rr2,

        # ── technical context ─────────────────────────────────────
        "atr":            round(atr, 4),
        "ema21":          round(ema21, 4),

        # ── pullback-add config ───────────────────────────────────
        "pullback": {
            "enabled":       True,
            "adds_done":     0,
            "max_adds":      2,              # 25 % each add
            "add_qty":       max(1, qty // 2),  # ~25% of full position per add
            "zone_high":     pb_zone_high,
            "zone_low":      pb_zone_low,
            "danger_zone":   danger_zone,
            "require_rsi_lt":60,
            "require_vol_surge": True,       # need 1.3× avg volume
        },

        # ── staged exits ──────────────────────────────────────────
        "exits": {
            "exit1_done":  False,            # 33% at 2R
            "exit2_done":  False,            # 33% at 3R
            "trailing":    False,            # trail the remainder
            "trail_stop":  None,
        },

        # ── time stop ─────────────────────────────────────────────
        "time_stop_days": 21,

        # ── order IDs (filled by moomoo_integration) ──────────────
        "orders": {
            "entry_order_id":   None,
            "stop_order_id":    None,
            "target1_order_id": None,
            "target2_order_id": None,
            "add_order_ids":    [],
        },

        # ── notes ─────────────────────────────────────────────────
        "notes": notes,
        "close_reason": None,
        "closed_at":    None,
        "pnl_pct":      None,
    }
