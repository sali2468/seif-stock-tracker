"""
Strategy Review Engine — StockPal
Analyzes your closed trade history against strategy_config.json
and uses Claude to suggest evidence-based improvements.
"""

import json
import os
import copy
from datetime import datetime

_CFG_FILE    = os.path.join(os.path.dirname(__file__), "strategy_config.json")
_REVIEW_FILE = os.path.join(os.path.dirname(__file__), "strategy_reviews.json")


# ── Config helpers ────────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        with open(_CFG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(config: dict):
    config["_meta"]["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    with open(_CFG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def apply_suggestion(config: dict, param_path: str, new_value) -> dict:
    """Apply a single suggestion to the config. Returns new config (original unchanged)."""
    config = copy.deepcopy(config)
    keys   = param_path.split(".")
    obj    = config
    for k in keys[:-1]:
        obj = obj.setdefault(k, {})
    obj[keys[-1]] = new_value
    return config


# ── Performance analytics ─────────────────────────────────────────────────────

def compute_performance_stats(closed_trades: list) -> dict:
    """
    Break down closed trade performance by every dimension the AI needs
    to make specific, data-backed suggestions.
    """
    if not closed_trades:
        return {}

    wins       = [t for t in closed_trades if t.get("pnl_pct", 0) > 0]
    losses     = [t for t in closed_trades if t.get("pnl_pct", 0) <= 0]
    total_pnl_usd = round(sum(t.get("pnl_dollars", 0) for t in closed_trades), 2)

    def _bucket(trades: list, label: str) -> dict:
        if not trades:
            return None
        w = [t for t in trades if t.get("pnl_pct", 0) > 0]
        return {
            "total":        len(trades),
            "wins":         len(w),
            "win_rate":     round(len(w) / len(trades) * 100, 1),
            "avg_pnl_pct":  round(sum(t.get("pnl_pct", 0)     for t in trades) / len(trades), 2),
            "total_dollars": round(sum(t.get("pnl_dollars", 0) for t in trades), 2),
            "avg_hold_days": round(sum(t.get("days_held", 0)   for t in trades) / len(trades), 1),
        }

    # By stars (signal quality at entry)
    by_stars = {}
    for s in [1, 2, 3]:
        b = _bucket([t for t in closed_trades if t.get("stars") == s], f"{s}★")
        if b: by_stars[str(s)] = b

    # By RS rank
    by_rs = {}
    for label, lo, hi in [("elite_80plus", 80, 100), ("strong_60_80", 60, 80), ("weak_below_60", 0, 60)]:
        b = _bucket([t for t in closed_trades if lo <= float(t.get("rs_rank", 0) or 0) < hi], label)
        if b: by_rs[label] = {**b, "range": f"{lo}–{hi}"}

    # By Trend Template score
    by_tt = {}
    for label, lo, hi in [("strong_6plus", 6, 9), ("moderate_4_5", 4, 6), ("weak_below_4", 0, 4)]:
        b = _bucket([t for t in closed_trades if lo <= int(t.get("trend_template", 0) or 0) < hi], label)
        if b: by_tt[label] = {**b, "range": f"{lo}–{hi}"}

    # By setup type
    by_setup = {}
    for st in ["swing", "day", "vcp", "manual"]:
        b = _bucket([t for t in closed_trades if (t.get("setup_type") or "manual") == st], st)
        if b: by_setup[st] = b

    # By signal type (BUY vs WATCH)
    by_signal = {}
    for sig in ["BUY", "WATCH"]:
        b = _bucket([t for t in closed_trades if (t.get("signal_type") or "BUY") == sig], sig)
        if b: by_signal[sig] = b

    # Exit reason breakdown
    def _exit_contains(t, *keywords):
        r = (t.get("exit_reason") or "").lower()
        return any(k in r for k in keywords)

    by_exit = {
        "stop_hit":    len([t for t in closed_trades if _exit_contains(t, "stop")]),
        "target_hit":  len([t for t in closed_trades if _exit_contains(t, "target", "profit")]),
        "stale":       len([t for t in closed_trades if _exit_contains(t, "nowhere", "stale", "days")]),
        "manual":      len([t for t in closed_trades if _exit_contains(t, "manual", "close")]),
        "overbought":  len([t for t in closed_trades if _exit_contains(t, "overbought", "rsi")]),
    }

    # Hold time distribution
    hold_days = [int(t.get("days_held", 0) or 0) for t in closed_trades]

    # Profit factor
    gross_win  = sum(t.get("pnl_dollars", 0) for t in wins)   or 0
    gross_loss = abs(sum(t.get("pnl_dollars", 0) for t in losses)) or 1
    pf = round(gross_win / gross_loss, 2)

    return {
        "total_trades":    len(closed_trades),
        "total_wins":      len(wins),
        "total_losses":    len(losses),
        "win_rate":        round(len(wins) / len(closed_trades) * 100, 1),
        "total_pnl":       total_pnl_usd,
        "profit_factor":   pf,
        "avg_win_pct":     round(sum(t.get("pnl_pct",0) for t in wins)   / max(len(wins),1),   2),
        "avg_loss_pct":    round(sum(t.get("pnl_pct",0) for t in losses) / max(len(losses),1), 2),
        "avg_hold_days":   round(sum(hold_days) / len(hold_days), 1) if hold_days else 0,
        "by_stars":        by_stars,
        "by_rs_rank":      by_rs,
        "by_trend_template": by_tt,
        "by_setup_type":   by_setup,
        "by_signal_type":  by_signal,
        "by_exit_reason":  by_exit,
    }


# ── AI review engine ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert trading strategy analyst specializing in the methods of:
- Mark Minervini (2x US Investing Champion, SEPA methodology, Trend Template)
- William O'Neil (CANSLIM, Investor's Business Daily founder)
- Stan Weinstein (Stage Analysis, 30-week MA)
- Nicolas Darvas (Box Theory)
- Linda Bradford Raschke (Short-term momentum)

You review trading strategy configurations and closed trade performance data to suggest specific, quantified improvements grounded in trader research and the trader's own data."""


def run_ai_review(closed_trades: list, config: dict) -> dict:
    """
    Uses Claude to analyze performance vs config and return specific suggestions.
    Returns a dict with 'suggestions', 'summary', 'grade', etc.
    """
    from ai_analysis import _llm_complete, _ai_available
    if not _ai_available():
        return {"error": "no_key", "message": "Add ANTHROPIC_API_KEY (or GROQ_API_KEY) to your .env file"}

    stats = compute_performance_stats(closed_trades)
    if not stats:
        return {"error": "no_data", "message": "No closed trades to analyze yet. Close your first position to enable the review."}

    # Strip _meta and _comment keys from config for cleaner prompt
    clean_cfg = {k: v for k, v in config.items() if not k.startswith("_")}

    prompt = f"""## Current Strategy Configuration
```json
{json.dumps(clean_cfg, indent=2)}
```

## Closed Trade Performance Data
```json
{json.dumps(stats, indent=2)}
```

## Your Task
Analyze this data and return a JSON object with actionable, evidence-based improvements.

Rules:
- Only suggest changes where the data supports it OR where trader research clearly applies
- If a bucket has fewer than 5 trades, note data is insufficient and base suggestion on trader research
- Be specific with numbers — don't say "increase threshold", say "change from 70 to 75"
- Keep suggestions practical and prioritized

Required JSON structure:
{{
  "summary": "2-3 sentence overview of what the data shows",
  "grade": "A|B|C|D",
  "grade_reason": "one sentence explaining the grade",
  "what_is_working": ["list", "of", "specific", "strengths"],
  "biggest_risk": "single most important risk or weakness",
  "suggestions": [
    {{
      "id": "unique_id",
      "category": "entry_gate|entry_scoring|exit|day_trade|sizing",
      "title": "short title",
      "param_path": "exact.dot.path.in.config",
      "current_value": <current>,
      "suggested_value": <suggested>,
      "reason": "plain English with specific data: e.g. 'RS<70 trades show 31% WR vs 59% for RS≥70 in your data'",
      "trader_basis": "Minervini|O'Neil|Weinstein|Darvas|data-driven",
      "data_support": "strong|moderate|insufficient",
      "expected_impact": "e.g. '+5-8% win rate based on RS rank data above'",
      "priority": "high|medium|low"
    }}
  ],
  "regime_notes": "Any observations about how performance varies by market regime",
  "next_review_trigger": "When to run the next review, e.g. 'after 20 more closed trades' or 'after Q3 earnings season'"
}}

Return ONLY the JSON. No markdown, no preamble."""

    try:
        raw = _llm_complete(SYSTEM_PROMPT + "\n\n" + prompt, max_tokens=4000).strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rsplit("```", 1)[0]
        return json.loads(raw.strip())
    except json.JSONDecodeError as e:
        return {"error": "parse_error", "message": f"Claude returned invalid JSON: {e}"}
    except Exception as e:
        return {"error": "api_error", "message": str(e)}


# ── Review history ────────────────────────────────────────────────────────────

def save_review(review: dict, stats: dict):
    """Persist review + snapshot of stats to disk."""
    try:
        history = get_review_history()
        history.append({
            "timestamp":    datetime.now().isoformat(),
            "review":       review,
            "stats_snapshot": {
                "total_trades": stats.get("total_trades"),
                "win_rate":     stats.get("win_rate"),
                "total_pnl":    stats.get("total_pnl"),
            }
        })
        with open(_REVIEW_FILE, "w") as f:
            json.dump(history[-20:], f, indent=2)   # keep last 20
    except Exception:
        pass


def get_review_history() -> list:
    try:
        if os.path.exists(_REVIEW_FILE):
            with open(_REVIEW_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return []
