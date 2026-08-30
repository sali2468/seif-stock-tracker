"""
Level-break alert system.

Tickers are added to the watchlist via the Scanner page "🔔 Alert" button.
The Telegram bot checks every monitor cycle and fires once per level-break,
then marks it alerted so it won't repeat.

Storage: level_alerts.json  (gitignored)
"""

import json, os, time
from typing import List, Tuple

_FILE = os.path.join(os.path.dirname(__file__), "level_alerts.json")


# ── Storage helpers ───────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        with open(_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict):
    try:
        with open(_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ── Public API ────────────────────────────────────────────────────────────────

def watch_ticker(ticker: str, levels: dict):
    """
    Register a ticker for level-break alerts.
    levels keys (all optional):
      52w_high        — break above = breakout alert
      prev_day_high   — break above = momentum continuation
      prev_day_low    — break below = weakness alert
      ema200          — break above or below = trend change
      signal_target1  — break above = target hit (Telegram)
      signal_stop     — break below = stop alert (Telegram)
    """
    data = _load()
    entry = data.get(ticker.upper(), {})
    # Preserve existing alerted flags — only reset for new levels
    old_alerted = entry.get("alerted", {})
    new_alerted = {k: old_alerted.get(k, False) for k in levels}
    data[ticker.upper()] = {
        "levels":     levels,
        "added":      time.time(),
        "last_price": entry.get("last_price"),
        "alerted":    new_alerted,
    }
    _save(data)


def get_watched_tickers() -> dict:
    return _load()


def update_last_price(ticker: str, price: float):
    """Called by the monitor so check_breaks can compare previous vs current."""
    data = _load()
    if ticker.upper() in data:
        data[ticker.upper()]["last_price"] = price
        _save(data)


def check_breaks(ticker: str, current_price: float) -> List[Tuple[str, float, str]]:
    """
    Compare current_price against stored levels.
    Returns list of (label, level_value, direction) for any newly-crossed levels.
    direction = 'above' or 'below'
    Only fires once per level (marks alerted=True after firing).
    Resets when price moves back to the 'safe' side by > 1%.
    """
    data = _load()
    key  = ticker.upper()
    if key not in data:
        return []

    entry      = data[key]
    levels     = entry.get("levels", {})
    alerted    = entry.get("alerted", {})
    prev_price = entry.get("last_price")

    if prev_price is None:
        # First time — just store price, don't alert
        data[key]["last_price"] = current_price
        _save(data)
        return []

    # Map: level_key → (human label, direction_to_alert)
    level_defs = {
        "52w_high":       ("52-Week High 🚀",        "above"),
        "prev_day_high":  ("Yesterday's High",        "above"),
        "prev_day_low":   ("Yesterday's Low ⚠️",     "below"),
        "ema200":         ("200-Day Moving Average",  "either"),
        "signal_target1": ("Signal Target 1 🎯",      "above"),
        "signal_stop":    ("Signal Stop Level 🛑",    "below"),
    }

    breaks = []
    changed = False

    for lkey, (label, direction) in level_defs.items():
        level_val = levels.get(lkey)
        if level_val is None:
            continue

        already_alerted = alerted.get(lkey, False)

        # ── Detect cross ──────────────────────────────────────────────────────
        crossed_above = prev_price < level_val <= current_price
        crossed_below = prev_price > level_val >= current_price

        if direction == "above":
            crossed = crossed_above
            reset   = current_price < level_val * 0.99   # price fell back
        elif direction == "below":
            crossed = crossed_below
            reset   = current_price > level_val * 1.01   # price recovered
        else:  # "either"
            crossed = crossed_above or crossed_below
            reset   = False  # keep alerted for trend changes

        # Auto-reset so level can fire again if price recovers
        if already_alerted and reset:
            alerted[lkey] = False
            changed = True

        if crossed and not already_alerted:
            effective_dir = "above" if crossed_above else "below"
            breaks.append((label, level_val, effective_dir))
            alerted[lkey] = True
            changed = True

    # Save updated state
    data[key]["last_price"] = current_price
    data[key]["alerted"]    = alerted
    if changed or prev_price != current_price:
        _save(data)

    return breaks


def remove_ticker(ticker: str):
    data = _load()
    data.pop(ticker.upper(), None)
    _save(data)


def reset_ticker(ticker: str):
    """Clear all alerted flags so levels can fire again."""
    data = _load()
    if ticker.upper() in data:
        data[ticker.upper()]["alerted"] = {}
        _save(data)


def ticker_is_watched(ticker: str) -> bool:
    return ticker.upper() in _load()
