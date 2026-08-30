"""
trade_queue.py — shared pending-trade state for Telegram trade execution.

Held in-memory (single process). Both alerts.py and telegram_bot.py
import from here — no circular dependency issues.

Only one trade can be pending at a time (single owner).
"""

import time
from typing import Optional

TIMEOUT_SECS = 60   # seconds before a pending trade expires

_PENDING: dict = {}  # single slot


# ── write ─────────────────────────────────────────────────────────────────────

def set_pending(
    action:    str,          # "buy" | "sell"
    ticker:    str,
    qty:       int,
    trigger:   str,          # "stop_hit" | "target_hit" | "manual"
    entry:     float = 0.0,  # original entry price (for context)
    stop:      float = 0.0,
    pnl_pct:   float = 0.0,
    pnl_dol:   float = 0.0,
    limit_price: Optional[float] = None,  # None = market order
):
    """Register a new pending trade. Overwrites any existing one."""
    global _PENDING
    _PENDING = {
        "action":      action.lower(),
        "ticker":      ticker.upper(),
        "qty":         int(qty),
        "trigger":     trigger,
        "entry":       entry,
        "stop":        stop,
        "pnl_pct":     pnl_pct,
        "pnl_dol":     pnl_dol,
        "limit_price": limit_price,   # None = market; float = limit
        "state":       "confirm",     # "confirm" | "adjust"
        "timestamp":   time.time(),
    }


def update_pending(**kwargs):
    """Patch fields on the pending trade and reset the timeout."""
    if _PENDING:
        _PENDING.update(kwargs)
        _PENDING["timestamp"] = time.time()


def get_pending() -> Optional[dict]:
    """Return the pending trade if it exists and hasn't expired, else None."""
    if not _PENDING:
        return None
    if time.time() - _PENDING.get("timestamp", 0) > TIMEOUT_SECS:
        _PENDING.clear()
        return None
    return dict(_PENDING)


def clear_pending():
    _PENDING.clear()


def seconds_left() -> int:
    if not _PENDING:
        return 0
    return max(0, int(TIMEOUT_SECS - (time.time() - _PENDING.get("timestamp", 0))))
