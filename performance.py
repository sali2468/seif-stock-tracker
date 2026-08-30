"""
Performance analytics — reads closed trades from state and computes stats.

Also provides:
  • get_lessons()     — repeating mistakes / pattern warnings
  • get_groq_summary()— compact text for the Telegram bot's context
"""

from __future__ import annotations
from typing import List, Dict, Any
from datetime import date


# ── helpers ───────────────────────────────────────────────────────────────────

def _safe(val, default=0.0):
    try:
        return float(val) if val not in (None, "", "N/A") else default
    except Exception:
        return default


def _pnl(t: dict) -> float:
    return _safe(t.get("pnl_pct", 0))


def _dollars(t: dict) -> float:
    return _safe(t.get("pnl_dollars", 0))


def _setup(t: dict) -> str:
    return (t.get("setup_type") or "manual").lower()


def _sector(t: dict) -> str:
    return t.get("sector") or "Unknown"


def _stars(t: dict) -> int:
    return int(t.get("stars") or 0)


def _rs(t: dict) -> float:
    return _safe(t.get("rs_rank", 0))


def _tt(t: dict) -> int:
    return int(t.get("trend_template") or 0)


# ── core stats ────────────────────────────────────────────────────────────────

def compute_stats(closed: List[dict]) -> Dict[str, Any]:
    """
    Full performance breakdown from a list of closed trade dicts.
    Returns a dict with all metrics needed by the dashboard and Groq.
    """
    if not closed:
        return {"empty": True}

    # ── sort by date ──────────────────────────────────────────────────────────
    trades = sorted(closed, key=lambda t: t.get("date_out", ""))

    wins  = [t for t in trades if _pnl(t) > 0]
    loses = [t for t in trades if _pnl(t) <= 0]

    total        = len(trades)
    win_count    = len(wins)
    loss_count   = len(loses)
    win_rate     = round(win_count / total * 100, 1) if total else 0.0

    avg_win      = round(sum(_pnl(t) for t in wins)  / win_count,  2) if wins  else 0.0
    avg_loss     = round(sum(_pnl(t) for t in loses) / loss_count, 2) if loses else 0.0
    avg_pnl      = round(sum(_pnl(t) for t in trades) / total, 2)

    gross_wins   = sum(_dollars(t) for t in wins)
    gross_losses = abs(sum(_dollars(t) for t in loses))
    profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else float("inf")
    total_dollars = round(sum(_dollars(t) for t in trades), 2)

    # expectancy: avg $ per trade (positive = edge exists)
    expectancy = round(total_dollars / total, 2) if total else 0.0

    # ── equity curve (cumulative $ P&L by date) ───────────────────────────────
    equity_curve: List[dict] = []
    running = 0.0
    for t in trades:
        running += _dollars(t)
        equity_curve.append({
            "date":   t.get("date_out", ""),
            "ticker": t.get("ticker", ""),
            "pnl":    _dollars(t),
            "equity": round(running, 2),
        })

    # ── current win/loss streak ───────────────────────────────────────────────
    streak = 0
    if trades:
        last_win = _pnl(trades[-1]) > 0
        for t in reversed(trades):
            if (_pnl(t) > 0) == last_win:
                streak += 1
            else:
                break
    streak_type = "win" if (trades and _pnl(trades[-1]) > 0) else "loss"

    # ── breakdown by setup type ───────────────────────────────────────────────
    setup_stats: Dict[str, dict] = {}
    for t in trades:
        s = _setup(t)
        setup_stats.setdefault(s, {"trades": [], "wins": 0, "total": 0})
        setup_stats[s]["trades"].append(t)
        setup_stats[s]["total"] += 1
        if _pnl(t) > 0:
            setup_stats[s]["wins"] += 1

    setup_breakdown = {}
    for s, v in setup_stats.items():
        n  = v["total"]
        w  = v["wins"]
        ts = v["trades"]
        setup_breakdown[s] = {
            "total":    n,
            "wins":     w,
            "win_rate": round(w / n * 100, 1) if n else 0,
            "avg_pnl":  round(sum(_pnl(t) for t in ts) / n, 2) if n else 0,
            "total_$":  round(sum(_dollars(t) for t in ts), 2),
        }

    # ── breakdown by sector ───────────────────────────────────────────────────
    sector_stats: Dict[str, dict] = {}
    for t in trades:
        sec = _sector(t)
        sector_stats.setdefault(sec, {"total": 0, "wins": 0, "pnl": 0.0})
        sector_stats[sec]["total"] += 1
        sector_stats[sec]["pnl"]   += _dollars(t)
        if _pnl(t) > 0:
            sector_stats[sec]["wins"] += 1

    sector_breakdown = {}
    for sec, v in sector_stats.items():
        n = v["total"]
        sector_breakdown[sec] = {
            "total":    n,
            "wins":     v["wins"],
            "win_rate": round(v["wins"] / n * 100, 1) if n else 0,
            "total_$":  round(v["pnl"], 2),
        }

    # ── breakdown by quality tier (stars) ────────────────────────────────────
    quality_stats: Dict[int, dict] = {}
    for t in trades:
        st = _stars(t)
        quality_stats.setdefault(st, {"total": 0, "wins": 0, "pnl": 0.0})
        quality_stats[st]["total"] += 1
        quality_stats[st]["pnl"]   += _dollars(t)
        if _pnl(t) > 0:
            quality_stats[st]["wins"] += 1

    quality_breakdown = {}
    for st, v in quality_stats.items():
        n = v["total"]
        quality_breakdown[st] = {
            "total":    n,
            "wins":     v["wins"],
            "win_rate": round(v["wins"] / n * 100, 1) if n else 0,
            "total_$":  round(v["pnl"], 2),
        }

    # ── RS tier breakdown ─────────────────────────────────────────────────────
    rs_tiers = {"Elite (80+)": [], "Strong (60-79)": [], "Weak (<60)": []}
    for t in trades:
        r = _rs(t)
        if r >= 80:
            rs_tiers["Elite (80+)"].append(t)
        elif r >= 60:
            rs_tiers["Strong (60-79)"].append(t)
        else:
            rs_tiers["Weak (<60)"].append(t)

    rs_breakdown = {}
    for tier, ts in rs_tiers.items():
        if not ts:
            continue
        w = sum(1 for t in ts if _pnl(t) > 0)
        rs_breakdown[tier] = {
            "total":    len(ts),
            "wins":     w,
            "win_rate": round(w / len(ts) * 100, 1),
            "avg_pnl":  round(sum(_pnl(t) for t in ts) / len(ts), 2),
        }

    # ── best / worst trades ───────────────────────────────────────────────────
    by_pct = sorted(trades, key=_pnl)
    worst5 = [_trade_summary(t) for t in by_pct[:5]]
    best5  = [_trade_summary(t) for t in by_pct[-5:][::-1]]

    # ── avg hold time ─────────────────────────────────────────────────────────
    hold_days = []
    for t in trades:
        try:
            d_in  = date.fromisoformat(t.get("date_in",  ""))
            d_out = date.fromisoformat(t.get("date_out", ""))
            hold_days.append((d_out - d_in).days)
        except Exception:
            pass
    avg_hold = round(sum(hold_days) / len(hold_days), 1) if hold_days else 0.0

    return {
        "empty":            False,
        "total":            total,
        "win_count":        win_count,
        "loss_count":       loss_count,
        "win_rate":         win_rate,
        "avg_win_pct":      avg_win,
        "avg_loss_pct":     avg_loss,
        "avg_pnl_pct":      avg_pnl,
        "profit_factor":    profit_factor,
        "total_dollars":    total_dollars,
        "expectancy":       expectancy,
        "avg_hold_days":    avg_hold,
        "streak":           streak,
        "streak_type":      streak_type,
        "equity_curve":     equity_curve,
        "setup_breakdown":  setup_breakdown,
        "sector_breakdown": sector_breakdown,
        "quality_breakdown":quality_breakdown,
        "rs_breakdown":     rs_breakdown,
        "best5":            best5,
        "worst5":           worst5,
        "trades":           trades,  # full list for table
    }


def _trade_summary(t: dict) -> dict:
    return {
        "ticker":     t.get("ticker", "?"),
        "date_in":    t.get("date_in", ""),
        "date_out":   t.get("date_out", ""),
        "entry":      _safe(t.get("entry")),
        "exit":       _safe(t.get("exit_price")),
        "pnl_pct":    _pnl(t),
        "pnl_$":      _dollars(t),
        "setup_type": _setup(t),
        "stars":      _stars(t),
        "sector":     _sector(t),
        "reason":     t.get("exit_reason", ""),
    }


# ── lessons learned ───────────────────────────────────────────────────────────

def get_lessons(closed: List[dict], candidate_ticker: str = "") -> List[str]:
    """
    Return a list of plain-English warnings based on past trade patterns.
    If candidate_ticker is given, also check if that ticker has a losing history.
    """
    if not closed:
        return []

    lessons = []
    trades  = closed

    # ── 1. Repeated loser tickers ─────────────────────────────────────────────
    ticker_results: Dict[str, List[float]] = {}
    for t in trades:
        tk = t.get("ticker", "")
        ticker_results.setdefault(tk, []).append(_pnl(t))

    if candidate_ticker:
        past = ticker_results.get(candidate_ticker.upper(), [])
        if past:
            losses = [p for p in past if p <= 0]
            if losses:
                lessons.append(
                    f"⚠️ You've traded {candidate_ticker} before — "
                    f"{len(losses)}/{len(past)} trades were losers "
                    f"(avg {round(sum(losses)/len(losses),1)}%). Tread carefully."
                )

    # ── 2. Sector bleeding ────────────────────────────────────────────────────
    sector_pnl: Dict[str, List[float]] = {}
    for t in trades:
        sec = _sector(t)
        sector_pnl.setdefault(sec, []).append(_pnl(t))

    for sec, pnls in sector_pnl.items():
        if len(pnls) >= 3:
            wr = sum(1 for p in pnls if p > 0) / len(pnls)
            if wr < 0.35:
                lessons.append(
                    f"🔴 Sector warning — {sec}: {int(wr*100)}% win rate "
                    f"over {len(pnls)} trades. You keep losing here."
                )

    # ── 3. Low-quality setups underperforming ─────────────────────────────────
    one_star = [t for t in trades if _stars(t) == 1]
    if len(one_star) >= 5:
        wr = sum(1 for t in one_star if _pnl(t) > 0) / len(one_star)
        if wr < 0.40:
            lessons.append(
                f"📉 1-star setups: only {int(wr*100)}% win rate over "
                f"{len(one_star)} trades. Consider skipping low-conviction signals."
            )

    # ── 4. Weak RS underperforming ────────────────────────────────────────────
    weak_rs = [t for t in trades if _rs(t) < 60 and _rs(t) > 0]
    strong_rs = [t for t in trades if _rs(t) >= 80]
    if len(weak_rs) >= 4 and len(strong_rs) >= 4:
        wr_weak   = sum(1 for t in weak_rs   if _pnl(t) > 0) / len(weak_rs)
        wr_strong = sum(1 for t in strong_rs if _pnl(t) > 0) / len(strong_rs)
        if wr_strong - wr_weak > 0.25:
            lessons.append(
                f"📊 RS matters for you — RS80+ trades win {int(wr_strong*100)}% "
                f"vs only {int(wr_weak*100)}% for RS<60. Stick to high RS."
            )

    # ── 5. Losing streak warning ──────────────────────────────────────────────
    recent = trades[-5:] if len(trades) >= 5 else trades
    recent_losses = sum(1 for t in recent if _pnl(t) <= 0)
    if recent_losses >= 4:
        lessons.append(
            f"🛑 You've lost {recent_losses} of your last {len(recent)} trades. "
            "Consider cutting size or pausing until the streak breaks."
        )

    # ── 6. Holding too long (time stops) ─────────────────────────────────────
    long_losers = []
    for t in trades:
        if _pnl(t) <= 0:
            try:
                days = (date.fromisoformat(t["date_out"]) -
                        date.fromisoformat(t["date_in"])).days
                if days > 15:
                    long_losers.append(days)
            except Exception:
                pass
    if len(long_losers) >= 3:
        avg_days = round(sum(long_losers) / len(long_losers), 0)
        lessons.append(
            f"⏳ You tend to hold losers too long — avg {avg_days} days before cutting "
            f"({len(long_losers)} trades). A 10-day time stop would have saved money."
        )

    return lessons


# ── Groq context summary ──────────────────────────────────────────────────────

def get_groq_summary(closed: List[dict]) -> str:
    """
    Compact multi-line summary for the Telegram bot's system context.
    Groq uses this to give historically-informed responses.
    """
    if not closed:
        return "No closed trades on record yet."

    stats = compute_stats(closed)
    lines = [
        f"Total trades: {stats['total']} | Win rate: {stats['win_rate']}% | "
        f"Profit factor: {stats['profit_factor']} | Total P&L: ${stats['total_dollars']:+.0f}",
        f"Avg winner: +{stats['avg_win_pct']}% | Avg loser: {stats['avg_loss_pct']}% | "
        f"Avg hold: {stats['avg_hold_days']} days | "
        f"Streak: {stats['streak']} {stats['streak_type']}s in a row",
    ]

    # Setup breakdown
    sd = stats.get("setup_breakdown", {})
    if sd:
        parts = []
        for stype, v in sorted(sd.items()):
            parts.append(f"{stype}({v['win_rate']}% WR, {v['total']} trades)")
        lines.append("By setup: " + " | ".join(parts))

    # Best/worst sectors
    sec = stats.get("sector_breakdown", {})
    if sec:
        best_sec  = max(sec.items(), key=lambda x: x[1]["win_rate"])
        worst_sec = min(sec.items(), key=lambda x: x[1]["win_rate"])
        if best_sec[1]["total"] >= 3:
            lines.append(f"Best sector: {best_sec[0]} ({best_sec[1]['win_rate']}% WR)")
        if worst_sec[1]["total"] >= 3 and worst_sec[0] != best_sec[0]:
            lines.append(f"Worst sector: {worst_sec[0]} ({worst_sec[1]['win_rate']}% WR)")

    # RS insight
    rs = stats.get("rs_breakdown", {})
    if "Elite (80+)" in rs and rs["Elite (80+)"]["total"] >= 3:
        lines.append(
            f"RS80+ trades: {rs['Elite (80+)']['win_rate']}% WR "
            f"({rs['Elite (80+)']['total']} trades)"
        )

    # Lessons
    lessons = get_lessons(closed)
    if lessons:
        lines.append("PATTERNS TO WATCH: " + " | ".join(lessons[:3]))

    return "\n".join(lines)
