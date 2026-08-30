"""
morning_briefing.py — auto-fires at 8:30 AM ET every trading day.

Called by monitor_daemon.py — do NOT run this directly.
Sends a structured Telegram message covering:
  • Market regime + VIX
  • Open positions (P&L, distance to stop/target)
  • Top scanner picks from watchlist (fast — no full universe scan)
  • Any VCP setups on the watchlist
  • Performance lessons (if history flags patterns)
  • Earnings this week on watchlist tickers
"""

import logging
import os
from datetime import date, datetime

log = logging.getLogger("daemon")

HERE = os.path.dirname(os.path.abspath(__file__))
_SENT_FILE = os.path.join(HERE, ".briefing_sent_date")  # stores last sent date


# ── guard: only send once per calendar day ────────────────────────────────────

def _already_sent_today() -> bool:
    try:
        with open(_SENT_FILE) as f:
            return f.read().strip() == date.today().isoformat()
    except Exception:
        return False


def _mark_sent():
    with open(_SENT_FILE, "w") as f:
        f.write(date.today().isoformat())


# ── helpers ───────────────────────────────────────────────────────────────────

def _pct_color_emoji(pct: float) -> str:
    if pct >= 3:
        return "🟢"
    if pct >= 0:
        return "🟡"
    if pct >= -3:
        return "🟠"
    return "🔴"


def _build_briefing() -> str:
    """Assemble the full briefing message. Returns HTML-formatted string."""
    from state import get_positions, get_closed, get_watchlist
    from market_data import get_regime

    lines = []

    today_str = datetime.now().strftime("%A, %b %d")
    lines.append(f"☀️ <b>Morning Briefing — {today_str}</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    # ── 1. Market regime ─────────────────────────────────────────────────────
    try:
        regime = get_regime()
        regime_str = regime.get("regime", "UNKNOWN")
        vix        = regime.get("vix", 0)
        spy        = regime.get("spy", 0)
        above_200  = regime.get("above_200", False)

        regime_emoji = {
            "BULL_QUIET":   "🐂 Bull (quiet)",
            "BULL_VOLATILE":"🐂⚡ Bull (volatile)",
            "BEAR_QUIET":   "🐻 Bear (quiet)",
            "BEAR_VOLATILE":"🐻⚡ Bear (volatile)",
        }.get(regime_str, regime_str)

        lines.append(
            f"\n🌍 <b>MARKET</b>\n"
            f"{regime_emoji} · VIX {vix:.1f} · SPY ${spy:.2f} "
            f"({'above' if above_200 else 'below'} 200MA)"
        )
        if regime_str.startswith("BEAR"):
            lines.append("⚠️ Bear market — reduce size, no new aggressive longs.")
        elif "VOLATILE" in regime_str:
            lines.append("⚡ Volatile market — widen stops or reduce size.")
    except Exception as e:
        log.warning(f"Briefing: regime fetch failed: {e}")
        lines.append("\n🌍 <b>MARKET</b>\nUnavailable")

    # ── 2. Open positions ─────────────────────────────────────────────────────
    try:
        import yfinance as yf
        positions = get_positions()
        if positions:
            lines.append("\n💼 <b>YOUR POSITIONS</b>")
            tickers = list(positions.keys())
            prices  = {}
            try:
                raw = yf.download(tickers, period="2d", interval="1d",
                                  auto_adjust=True, progress=False,
                                  group_by="ticker", threads=True)
                for tk in tickers:
                    try:
                        if len(tickers) == 1:
                            prices[tk] = float(raw["Close"].iloc[-1])
                        else:
                            prices[tk] = float(raw[tk]["Close"].iloc[-1])
                    except Exception:
                        pass
            except Exception:
                pass

            for ticker, pos in positions.items():
                entry  = float(pos.get("entry", 0))
                stop   = float(pos.get("stop", entry))
                t1     = float(pos.get("target1", entry))
                qty    = int(pos.get("qty", 1))
                price  = prices.get(ticker, entry)

                pnl_pct  = (price - entry) / entry * 100 if entry else 0
                pnl_dol  = (price - entry) * qty
                to_stop  = (price - stop)  / price * 100
                to_t1    = (t1 - price)    / price * 100

                em = _pct_color_emoji(pnl_pct)
                lines.append(
                    f"{em} <b>{ticker}</b> ${price:.2f} · "
                    f"P&L <b>{pnl_pct:+.1f}%</b> (${pnl_dol:+.0f}) · "
                    f"Stop {to_stop:.1f}% away · Target {to_t1:.1f}% away"
                )
        else:
            lines.append("\n💼 <b>POSITIONS</b>\nNo open positions.")
    except Exception as e:
        log.warning(f"Briefing: positions fetch failed: {e}")
        lines.append("\n💼 <b>POSITIONS</b>\nUnavailable")

    # ── 3. Top scanner picks from watchlist ───────────────────────────────────
    try:
        from scanner import _batch_download, _rs_raw_score, _rs_percentile, _trend_template_check
        from signals import score_entry

        watchlist = get_watchlist()
        if watchlist:
            regime_obj = get_regime()
            regime_key = regime_obj.get("regime", "BULL_QUIET")

            bar_map = _batch_download(watchlist + ["SPY"], period="1y")
            spy_df  = bar_map.get("SPY")
            spy_rs  = _rs_raw_score(spy_df) if spy_df is not None else 0.0

            rs_raw_map = {
                tk: _rs_raw_score(df) - spy_rs
                for tk, df in bar_map.items() if tk != "SPY"
            }

            swing_hits = []
            vcp_hits   = []

            from scanner import detect_vcp
            for tk, df in bar_map.items():
                if tk == "SPY":
                    continue
                try:
                    price = float(df["close"].iloc[-1])
                    if price < 2 or price > 500:
                        continue

                    rs   = _rs_percentile(tk, rs_raw_map.get(tk, 0), rs_raw_map)
                    tt,_ = _trend_template_check(df)

                    sw = score_entry(tk, {"regime": regime_key}, _df=df)
                    if sw:
                        sw.rs_rank        = rs
                        sw.trend_template = tt
                        swing_hits.append(sw)

                    vcp = detect_vcp(tk, df)
                    if vcp:
                        vcp.rs_rank        = rs
                        vcp.trend_template = tt
                        vcp_hits.append(vcp)
                except Exception:
                    continue

            # Sort and take top 3 swing + top 2 VCP
            swing_hits.sort(
                key=lambda s: (
                    2 if s.trend_template >= 6 else 1 if s.trend_template >= 4 else 0,
                    2 if s.rs_rank >= 80 else 1 if s.rs_rank >= 60 else 0,
                    s.stars, s.rr,
                ),
                reverse=True,
            )
            vcp_hits.sort(key=lambda v: (v.stars, v.rs_rank), reverse=True)

            if swing_hits:
                lines.append("\n📡 <b>TOP WATCHLIST SETUPS</b>")
                for s in swing_hits[:3]:
                    lines.append(
                        f"{'⭐'*s.stars} <b>{s.ticker}</b> ${s.price:.2f} · "
                        f"RS {s.rs_rank:.0f} · TT {s.trend_template}/8 · "
                        f"Stop ${s.stop:.2f} · T1 ${s.target1:.2f} · {s.rr}:1"
                    )
            else:
                lines.append("\n📡 <b>TOP SETUPS</b>\nNo clean setups on your watchlist today.")

            if vcp_hits:
                lines.append("\n🔭 <b>VCP SETUPS</b>")
                for v in vcp_hits[:2]:
                    ct_str = " → ".join(f"{d}%" for d in v.contractions)
                    lines.append(
                        f"{'⭐'*v.stars} <b>{v.ticker}</b> ${v.price:.2f} · "
                        f"{v.num_pivots}-pivot · {ct_str} · "
                        f"Pivot ${v.pivot_high:.2f} · RS {v.rs_rank:.0f}"
                    )
    except Exception as e:
        log.warning(f"Briefing: scanner section failed: {e}")
        lines.append("\n📡 <b>SETUPS</b>\nScanner unavailable.")

    # ── 4. Performance lessons ────────────────────────────────────────────────
    try:
        from performance import get_lessons, compute_stats
        closed = get_closed()
        if closed:
            stats   = compute_stats(closed)
            lessons = get_lessons(closed)
            lines.append(
                f"\n📊 <b>YOUR EDGE</b> — {stats['total']} trades · "
                f"{stats['win_rate']}% WR · PF {stats['profit_factor']}x"
            )
            if lessons:
                lines.append("⚠️ " + lessons[0])   # top lesson only — don't spam
    except Exception as e:
        log.warning(f"Briefing: performance section failed: {e}")

    # ── 5. Earnings this week on watchlist ────────────────────────────────────
    try:
        from fundamentals import get_fundamental_snapshot
        watchlist = get_watchlist()
        earning_soon = []
        # Check a sample of watchlist tickers (checking all is too slow)
        check_list = watchlist[:40]
        for tk in check_list:
            try:
                snap = get_fundamental_snapshot(tk)
                ea   = snap.get("earnings", {})
                dte  = ea.get("days_until")
                if dte is not None and 0 <= int(dte) <= 5:
                    earning_soon.append((tk, int(dte)))
            except Exception:
                continue
        if earning_soon:
            lines.append("\n📅 <b>EARNINGS THIS WEEK</b> (watchlist)")
            for tk, dte in sorted(earning_soon, key=lambda x: x[1]):
                when = "today" if dte == 0 else f"in {dte}d"
                lines.append(f"• <b>{tk}</b> reports {when}")
    except Exception as e:
        log.warning(f"Briefing: earnings section failed: {e}")

    # ── footer ────────────────────────────────────────────────────────────────
    lines.append("\n━━━━━━━━━━━━━━━━━━━━")
    lines.append("Good luck today. Trade the plan. 🎯")

    return "\n".join(lines)


# ── public entry point ────────────────────────────────────────────────────────

def send_morning_briefing(force: bool = False) -> bool:
    """
    Build and send the morning briefing via Telegram.
    Returns True if sent, False if skipped (already sent today) or failed.
    Set force=True to bypass the already-sent guard (for testing).
    """
    if not force and _already_sent_today():
        log.info("Morning briefing: already sent today — skipping.")
        return False

    log.info("Morning briefing: building…")
    try:
        from alerts import _send
        msg = _build_briefing()
        ok  = _send(msg)
        if ok:
            _mark_sent()
            log.info("Morning briefing: sent successfully.")
        else:
            log.warning("Morning briefing: _send() returned False.")
        return ok
    except Exception as e:
        log.error(f"Morning briefing: failed — {e}", exc_info=True)
        return False
