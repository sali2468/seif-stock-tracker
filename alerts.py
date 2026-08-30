"""
Telegram alert sender for StockPal.
Covers: stock alerts, option alerts, daily morning briefing.
"""

import os
import json
import time as _time
import requests
from datetime import datetime
from dotenv import load_dotenv

_here   = os.path.dirname(__file__)
_parent = os.path.join(_here, "..", "trading_bot")
for _env in [os.path.join(_here, ".env"), os.path.join(_parent, ".env")]:
    if os.path.exists(_env):
        load_dotenv(_env)
        break

TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID",   "")

# ── Alert deduplication — prevents duplicate Telegram messages ────────────────
# Cooldown per alert key so the same alert never fires twice within COOLDOWN_SECS.
# Works across browser tabs and rapid Streamlit reruns.
_THROTTLE_FILE = os.path.join(_here, ".alert_throttle.json")
_COOLDOWN_SECS = 600   # 10 minutes — daemon checks every 5 min; this prevents any duplicate

def _load_throttle() -> dict:
    try:
        if os.path.exists(_THROTTLE_FILE):
            return json.loads(open(_THROTTLE_FILE).read())
    except Exception:
        pass
    return {}

def _save_throttle(data: dict):
    try:
        # Prune entries older than 24h to keep file tiny
        cutoff = _time.time() - 86400
        data   = {k: v for k, v in data.items() if v > cutoff}
        open(_THROTTLE_FILE, "w").write(json.dumps(data))
    except Exception:
        pass

def _is_throttled(key: str, secs: int = None) -> bool:
    """Return True if this alert key was sent within the cooldown window.
    Pass secs to override the default COOLDOWN_SECS for this check."""
    cooldown = secs if secs is not None else _COOLDOWN_SECS
    last = _load_throttle().get(key, 0)
    return (_time.time() - last) < cooldown

def _record_alert(key: str):
    """Stamp this key as just sent."""
    data = _load_throttle()
    data[key] = _time.time()
    _save_throttle(data)


# Per-user routing: the daemon sets a target chat before checking each user so
# every alert (_send) goes to THAT user's private chat only.
#   None → legacy broadcast (owner / subscribers)
#   ""   → suppress (user has no Telegram connected — send nowhere)
#   "id" → send only to that chat_id
_target_chat = None


def set_target_chat(chat_id):
    global _target_chat
    _target_chat = None if chat_id is None else str(chat_id)


def _get_all_chat_ids() -> list:
    """Chat(s) the next _send() goes to."""
    if _target_chat is not None:
        return [_target_chat] if _target_chat else []
    try:
        from subscribers import get_all_chat_ids
        return get_all_chat_ids()
    except Exception:
        return [CHAT_ID] if CHAT_ID else []


def _send(text: str) -> bool:
    """Broadcast to owner + all active subscribers."""
    if not TOKEN:
        print(f"[ALERT - no Telegram]\n{text}")
        return False
    chat_ids = _get_all_chat_ids()
    if not chat_ids:
        print(f"[ALERT - no recipients]\n{text}")
        return False
    ok = True
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for cid in chat_ids:
        for chunk in chunks:
            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                    json={"chat_id": cid, "text": chunk, "parse_mode": "HTML"},
                    timeout=8,
                )
                if not resp.ok:
                    ok = False
            except Exception as e:
                print(f"Telegram send failed to {cid}: {e}")
                ok = False
    return ok


# ══════════════════════════════════════════════════════════════════════════════
# STOCK POSITION ALERTS  (real-time, fire once per event)
# ══════════════════════════════════════════════════════════════════════════════

def alert_stock_signal(ticker, price, stop, target1, target2,
                       stop_pct, gain_pct, rr, stars, why_buy,
                       signal_type: str = "BUY", watch_buy_at: float = 0.0) -> bool:
    star_str   = "⭐" * stars
    grade      = "strong setup" if stars == 3 else "good setup" if stars == 2 else "developing setup"
    levels     = f"stop ${stop:.2f} ({stop_pct}% risk) · target ${target1:.2f} (+{gain_pct}%) · {rr}:1 r/r"

    if signal_type == "WATCH":
        action_line = (f"📍 <b>Buy when price reaches ${watch_buy_at:.2f}</b>\n"
                       f"current price ${price:.2f} — entry timing not ideal right now")
        header = f"👁 <b>{ticker} — WATCH (wait for pullback)</b>"
    else:
        action_line = f"✅ <b>BUY</b> — enter around <b>${price:.2f}</b>"
        header = f"{star_str} <b>{ticker} — {grade}</b>"

    return _send(f"{header}\n\n{action_line}\n{levels}\n\n<i>{why_buy}</i>")


def alert_stock_stop(ticker, entry, current, stop, qty, pnl_pct, pnl_dollars) -> bool:
    _key = f"stop_{ticker}"
    if _is_throttled(_key): return False
    _record_alert(_key)
    # Queue a pending sell trade so user can reply YES to execute
    try:
        from trade_queue import set_pending
        set_pending(
            action="sell", ticker=ticker, qty=qty, trigger="stop_hit",
            entry=entry, stop=stop, pnl_pct=pnl_pct, pnl_dol=pnl_dollars,
        )
    except Exception:
        pass
    return _send(
        f"🛑 <b>{ticker} stop hit</b>\n\n"
        f"price dropped to ${current:.2f}, stop was ${stop:.2f}\n"
        f"bought at ${entry:.2f} — {qty} shares\n"
        f"<b>{pnl_pct:+.1f}% (${pnl_dollars:+,.0f})</b>\n\n"
        f"Reply <b>YES</b> to sell {qty} shares at market now\n"
        f"Reply <b>NO</b> to adjust qty first\n"
        f"<i>⏱ Expires in 60 seconds</i>"
    )


def alert_stock_target(ticker, entry, current, target1, qty, pnl_pct, pnl_dollars) -> bool:
    _key = f"target_{ticker}"
    if _is_throttled(_key): return False
    _record_alert(_key)
    # Queue a pending sell trade (half position at target)
    try:
        from trade_queue import set_pending
        sell_qty = max(1, qty // 2)
        set_pending(
            action="sell", ticker=ticker, qty=sell_qty, trigger="target_hit",
            entry=entry, pnl_pct=pnl_pct, pnl_dol=pnl_dollars,
        )
    except Exception:
        pass
    sell_qty = max(1, qty // 2)
    return _send(
        f"🎯 <b>{ticker} hit target</b>\n\n"
        f"at ${current:.2f} — up <b>{pnl_pct:+.1f}% (${pnl_dollars:+,.0f})</b>\n"
        f"{qty} shares, bought at ${entry:.2f}\n\n"
        f"Reply <b>YES</b> to sell {sell_qty} shares (half) at market now\n"
        f"Reply <b>NO</b> to adjust qty first\n"
        f"<i>⏱ Expires in 60 seconds</i>"
    )


def alert_staged_t1(ticker, entry, current, target1, qty_remaining, pnl_pct, pnl_dollars) -> bool:
    """Target 1 hit on a managed scanner position — sell half, move stop to breakeven."""
    _key = f"staged_t1_{ticker}"
    if _is_throttled(_key): return False
    _record_alert(_key)
    sell_qty = max(1, qty_remaining // 2)
    try:
        from trade_queue import set_pending
        set_pending(
            action="sell", ticker=ticker, qty=sell_qty, trigger="t1_hit",
            entry=entry, pnl_pct=pnl_pct, pnl_dol=pnl_dollars,
        )
    except Exception:
        pass
    return _send(
        f"🎯 <b>{ticker} — Target 1 hit!</b>\n\n"
        f"Price ${current:.2f} · up <b>{pnl_pct:+.1f}% (${pnl_dollars:+,.0f})</b>\n"
        f"Entry ${entry:.2f} · {qty_remaining} shares remaining\n\n"
        f"Plan: sell <b>{sell_qty} shares</b> (half), move stop to breakeven on the rest\n\n"
        f"Reply <b>YES</b> to execute now\n"
        f"Reply <b>NO</b> to adjust qty\n"
        f"<i>⏱ Expires in 60 seconds</i>"
    )


def alert_staged_t2(ticker, entry, current, target2, qty_remaining, pnl_pct, pnl_dollars) -> bool:
    """Target 2 hit — sell another half of remaining, activate trailing stop on the rest."""
    _key = f"staged_t2_{ticker}"
    if _is_throttled(_key): return False
    _record_alert(_key)
    sell_qty = max(1, qty_remaining // 2)
    keep_qty = qty_remaining - sell_qty
    try:
        from trade_queue import set_pending
        set_pending(
            action="sell", ticker=ticker, qty=sell_qty, trigger="t2_hit",
            entry=entry, pnl_pct=pnl_pct, pnl_dol=pnl_dollars,
        )
    except Exception:
        pass
    trail_stop = round(current * 0.95, 2)
    return _send(
        f"🎯🎯 <b>{ticker} — Target 2 hit!</b>\n\n"
        f"Price ${current:.2f} · up <b>{pnl_pct:+.1f}% (${pnl_dollars:+,.0f})</b>\n"
        f"{qty_remaining} shares remaining\n\n"
        f"Plan: sell <b>{sell_qty} shares</b> now, "
        f"trail remaining {keep_qty} with stop at ${trail_stop:.2f} (5%)\n\n"
        f"Reply <b>YES</b> to execute\n"
        f"Reply <b>NO</b> to adjust qty\n"
        f"<i>⏱ Expires in 60 seconds</i>"
    )


def alert_time_stop(ticker, days_held, pnl_pct, qty_remaining) -> bool:
    """Position flat after 10 days — prompt to exit and free up capital."""
    _key = f"time_stop_{ticker}"
    if _is_throttled(_key): return False
    _record_alert(_key)
    try:
        from trade_queue import set_pending
        set_pending(
            action="sell", ticker=ticker, qty=qty_remaining,
            trigger="time_stop", pnl_pct=pnl_pct,
        )
    except Exception:
        pass
    direction = "up" if pnl_pct > 0 else "down"
    return _send(
        f"⏰ <b>{ticker} — time stop ({days_held} days)</b>\n\n"
        f"Price has barely moved — {direction} only <b>{abs(pnl_pct):.1f}%</b> "
        f"after {days_held} trading days\n"
        f"Capital tied up here could be working harder elsewhere\n\n"
        f"Reply <b>YES</b> to sell {qty_remaining} shares at market\n"
        f"Reply <b>NO</b> to keep holding\n"
        f"<i>⏱ Expires in 60 seconds</i>"
    )


def alert_pullback_add(
    ticker, current_price, ema20, stop,
    suggested_qty, available_cash, pnl_pct,
) -> bool:
    """
    Pullback to EMA20 on a winning managed position — suggest adding shares.
    Throttled to once per 4 hours per ticker so it doesn't spam.
    """
    _key = f"pullback_add_{ticker}"
    if _is_throttled(_key, secs=14400): return False   # 4-hour cooldown
    _record_alert(_key)
    risk_per = round(current_price - stop, 2)
    est_cost = round(current_price * suggested_qty, 2)
    try:
        from trade_queue import set_pending
        set_pending(
            action="buy", ticker=ticker, qty=suggested_qty,
            trigger="pullback_add", entry=current_price, stop=stop,
        )
    except Exception:
        pass
    cash_line = f"Available cash: ${available_cash:,.0f}\n" if available_cash > 0 else ""
    return _send(
        f"📉 <b>{ticker} — pullback add zone</b>\n\n"
        f"Price ${current_price:.2f} is back at EMA20 (${ema20:.2f})\n"
        f"Trend still intact — volume drying up on the dip\n"
        f"Position already up {pnl_pct:+.1f}% — adding to a winner\n\n"
        f"{cash_line}"
        f"Risk/share: ${risk_per:.2f} · Stop: ${stop:.2f}\n"
        f"Suggested add: <b>{suggested_qty} shares</b> (~${est_cost:,.0f})\n\n"
        f"Reply <b>YES</b> to buy {suggested_qty} shares at market\n"
        f"Reply <b>NO</b> to adjust qty\n"
        f"<i>⏱ Expires in 60 seconds</i>"
    )


def alert_stock_raise_stop(ticker, current, old_stop, new_stop, pnl_pct, pnl_dollars) -> bool:
    _key = f"raise_{ticker}_{round(new_stop, 2)}"
    if _is_throttled(_key): return False
    _record_alert(_key)
    locked = round(new_stop - old_stop, 2)
    return _send(
        f"🔼 <b>{ticker} stop raised to ${new_stop:.2f}</b>\n\n"
        f"was ${old_stop:.2f}, locked in +${locked:.2f}/share more\n"
        f"up {pnl_pct:+.1f}% (${pnl_dollars:+,.0f}) at ${current:.2f}\n\n"
        f"no action needed"
    )


def alert_stock_exit_stale(ticker, days, pnl_pct) -> bool:
    _key = f"stale_{ticker}"
    if _is_throttled(_key): return False
    _record_alert(_key)
    return _send(
        f"⏳ <b>{ticker} — going nowhere</b>\n\n"
        f"{days} days held, {pnl_pct:+.1f}% — nothing happening\n\n"
        f"free up the money for a better trade"
    )


def alert_sell_signal(ticker: str, price: float, pnl_pct: float,
                      headline: str, reasoning: str,
                      risks: list, suggested_stop) -> bool:
    """Chart analysis flipped to SELL — fire immediately."""
    _key = f"sell_{ticker}"
    if _is_throttled(_key): return False
    _record_alert(_key)
    stop_line = f"\nsuggested stop: ${suggested_stop:.2f}" if suggested_stop else ""
    return _send(
        f"🔴 <b>{ticker} — chart turned bearish</b>\n\n"
        f"${price:.2f} | {pnl_pct:+.1f}%\n\n"
        f"<b>{headline}</b>\n"
        f"<i>{reasoning[:200]}</i>"
        f"{stop_line}\n\n"
        f"open the app and decide"
    )


def alert_watch_closely(ticker: str, price: float, pnl_pct: float,
                        reason: str) -> bool:
    """Position needs close attention — not yet a full sell, but deteriorating."""
    _key = f"watch_{ticker}"
    if _is_throttled(_key): return False
    _record_alert(_key)
    return _send(
        f"👀 <b>{ticker} — keep an eye on this</b>\n\n"
        f"${price:.2f} | {pnl_pct:+.1f}%\n"
        f"<i>{reason}</i>\n\n"
        f"tighten your stop"
    )


def alert_price_drop(ticker: str, price: float, drop_pct: float,
                     stop: float, pnl_pct: float) -> bool:
    """Single-session drop larger than 1 ATR — heads-up."""
    cushion = round((price - stop) / price * 100, 1)
    return _send(
        f"📉 <b>{ticker} dropping fast</b>\n\n"
        f"down {drop_pct:.1f}% today to ${price:.2f} ({pnl_pct:+.1f}% overall)\n"
        f"stop at ${stop:.2f} — {cushion}% cushion left\n\n"
        f"check the chart before close"
    )


def alert_stock_overbought_exit(ticker, rsi, pnl_pct, pnl_dollars) -> bool:
    _key = f"overbought_{ticker}"
    if _is_throttled(_key): return False
    _record_alert(_key)
    return _send(
        f"🔴 <b>{ticker} — take profit</b>\n\n"
        f"RSI at {rsi:.0f}, overbought\n"
        f"up {pnl_pct:+.1f}% (${pnl_dollars:+,.0f})\n\n"
        f"lock it in"
    )


# ══════════════════════════════════════════════════════════════════════════════
# OPTION ALERTS
# ══════════════════════════════════════════════════════════════════════════════

def alert_option_target(underlying, opt_type, strike, expiry,
                        contracts, entry, current, target) -> bool:
    pnl_per = round((current - entry) * 100, 2)
    pnl_tot = round(pnl_per * contracts, 2)
    pnl_pct = round((current - entry) / entry * 100, 1)
    return _send(
        f"🎯 <b>OPTION TARGET HIT — {underlying} {opt_type.upper()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 {underlying} ${strike:.0f} {opt_type.upper()} exp {expiry}\n"
        f"📦 {contracts} contract{'s' if contracts > 1 else ''}\n"
        f"💵 Entry: ${entry:.2f}  →  Now: ${current:.2f}  ({pnl_pct:+.1f}%)\n"
        f"💰 P&L:   ${pnl_tot:+,.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Consider selling all or part now and locking in the gain.</i>"
    )


def alert_option_stop(underlying, opt_type, strike, expiry,
                      contracts, entry, current, stop) -> bool:
    pnl_tot = round((current - entry) * 100 * contracts, 2)
    pnl_pct = round((current - entry) / entry * 100, 1)
    return _send(
        f"🛑 <b>OPTION STOP HIT — {underlying} {opt_type.upper()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 {underlying} ${strike:.0f} {opt_type.upper()} exp {expiry}\n"
        f"📦 {contracts} contract{'s' if contracts > 1 else ''}\n"
        f"💵 Entry: ${entry:.2f}  →  Now: ${current:.2f}  ({pnl_pct:+.1f}%)\n"
        f"💸 P&L:   ${pnl_tot:+,.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Stop hit — exit now to protect remaining capital.</i>"
    )


def alert_option_near_expiry(underlying, opt_type, strike, expiry, days_left, current) -> bool:
    return _send(
        f"⏰ <b>OPTION EXPIRING — {underlying} {opt_type.upper()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 {underlying} ${strike:.0f} {opt_type.upper()} exp {expiry}\n"
        f"📅 Days left: <b>{days_left}</b>\n"
        f"💵 Current:   ${current:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Only {days_left} day{'s' if days_left != 1 else ''} to expiry. "
        f"Decide: close, roll, or let expire.</i>"
    )


# ══════════════════════════════════════════════════════════════════════════════
# MORNING BRIEFING  (comprehensive — positions + signals + analysis + tip)
# ══════════════════════════════════════════════════════════════════════════════

# Rotating daily reminders — one fires each day based on weekday
_DAILY_TIPS = [
    "The best traders don't predict — they react. Let the chart tell you what to do, not your feelings.",
    "A small loss today keeps the account alive for tomorrow. Never let a loser become a disaster.",
    "Size matters more than timing. A half-size position on a good setup beats a full-size bet on a bad one.",
    "When the market gives you a gift (a target hit), say thank you and take at least half off.",
    "Boredom is a trading killer. 'I need to be in a trade' is how you lose money in a sideways market.",
    "Your stop loss is decided BEFORE you enter — not when price is falling and fear kicks in.",
    "The trend is your friend until it ends. Trade with the 200-day EMA direction, not against it.",
]


def _regime_label(r: str) -> str:
    return {
        "BULL_QUIET":    "🟢 Calm Bull Market",
        "BULL_VOLATILE": "🟡 Choppy Bull Market",
        "BEAR_QUIET":    "🟠 Quiet Bear Market",
        "BEAR_VOLATILE": "🔴 Volatile Bear Market",
        "CRISIS":        "💀 Market Crisis",
    }.get(r, r)


def _regime_advice(r: str) -> str:
    return {
        "BULL_QUIET":    "Ideal conditions. Full size, lean long.",
        "BULL_VOLATILE": "Trend up but bumpy. Smaller size, tighter stops.",
        "BEAR_QUIET":    "Trend down. Only A+ setups, half size.",
        "BEAR_VOLATILE": "High danger. Minimal trades. Protect capital.",
        "CRISIS":        "Stay in cash. No new longs. Wait for the storm to pass.",
    }.get(r, "")


def alert_morning_briefing() -> bool:
    """
    Full morning briefing — sent automatically at 8:30 AM.
    Pulls live data: market regime, open positions with chart analysis,
    top buy signals from watchlist, and a daily tip.
    """
    try:
        from market_data import get_bars, compute_indicators, get_vix, detect_regime, get_current_price
        from signals import check_position, score_entry
        from state import get_positions, get_watchlist, get_options
        from chart_analysis import analyze_ticker
        from market_data import get_option_price, days_to_expiry

        now = datetime.now()
        lines = []

        # ── Header ─────────────────────────────────────────────────────────────
        lines += [
            f"🌅 <b>StockPal — Morning Briefing</b>",
            f"<i>{now.strftime('%A, %B %d %Y')} · {now.strftime('%I:%M %p')}</i>",
            f"━━━━━━━━━━━━━━━━━━━━",
            "",
        ]

        # ── Market regime ───────────────────────────────────────────────────────
        try:
            vix    = get_vix()
            spy_df = get_bars("SPY", "1y", "1d")
            if spy_df is not None:
                spy_df = compute_indicators(spy_df)
            regime = detect_regime(spy_df, vix)
            rl     = _regime_label(regime["regime"])
            ra     = _regime_advice(regime["regime"])
            lines += [
                f"📊 <b>Market Conditions</b>",
                f"{rl}",
                f"<i>{ra}</i>",
                f"VIX: <b>{regime['vix']:.1f}</b>  |  SPY: <b>${regime['spy']:.2f}</b>  |  "
                f"200MA: {'✅ Above' if regime['above_200'] else '⚠️ Below'}",
                "",
            ]
        except Exception:
            regime = {"regime": "BULL_QUIET"}
            lines += ["📊 Market data unavailable", ""]

        # ── Open stock positions ────────────────────────────────────────────────
        positions = get_positions()
        if positions:
            open_pnl = 0.0
            urgent   = []
            pos_lines = []

            for ticker, pos in positions.items():
                try:
                    price   = get_current_price(ticker) or pos["entry"]
                    pnl_pct = (price - pos["entry"]) / pos["entry"] * 100
                    pnl_dol = (price - pos["entry"]) * pos["qty"]
                    open_pnl += pnl_dol

                    # Signal check (upgraded rules)
                    s = check_position(ticker, pos["entry"], pos["stop"],
                                       pos["target1"], pos["date_in"], pos["qty"])
                    action = s.action if s else "HOLD"

                    # Chart analysis for deeper verdict
                    try:
                        from datetime import date
                        days_held = (date.today() - date.fromisoformat(
                            pos.get("date_in", str(date.today())))).days
                    except Exception:
                        days_held = 0

                    ca = analyze_ticker(ticker, position={
                        "entry": pos["entry"], "stop": pos["stop"],
                        "target1": pos.get("target1", 0), "target2": pos.get("target2", 0),
                        "qty": pos.get("qty", 1), "days_held": days_held,
                    })
                    ca_action = ca.get("action", "HOLD") if ca and "error" not in ca else "—"
                    ca_head   = ca.get("headline", "") if ca and "error" not in ca else ""

                    # Merged verdict — most conservative wins
                    hard = {"EXIT NOW", "TAKE PROFIT", "TIGHTEN STOP", "WATCH CLOSELY"}
                    if action in hard:
                        final_action = action
                        urgent.append(ticker)
                    elif ca_action == "SELL":
                        final_action = "WATCH CLOSELY ⚠️"
                        urgent.append(ticker)
                    else:
                        final_action = action

                    pnl_icon = "✅" if pnl_pct >= 0 else "🔻"
                    action_icon = {
                        "EXIT NOW": "🚨", "TAKE PROFIT": "🎯",
                        "TIGHTEN STOP": "🔼", "RAISE STOP": "🔼",
                        "WATCH CLOSELY": "⚠️", "WATCH CLOSELY ⚠️": "⚠️",
                        "HOLD": "✅",
                    }.get(final_action, "📋")

                    block = (
                        f"{pnl_icon} <b>{ticker}</b>  ${price:.2f}  "
                        f"({pnl_pct:+.2f}%  /  ${pnl_dol:+.2f})\n"
                        f"   Entry ${pos['entry']:.2f}  ·  Stop ${pos['stop']:.2f}  ·  "
                        f"T1 ${pos['target1']:.2f}\n"
                        f"   {action_icon} <b>{final_action}</b>"
                    )
                    if ca_head and ca_action in ("SELL", "WATCH"):
                        block += f"\n   <i>⚡ {ca_head}</i>"

                    pos_lines.append(block)
                except Exception:
                    pos_lines.append(f"❓ <b>{ticker}</b> — could not fetch data")

            lines.append(f"💼 <b>Your Positions ({len(positions)} open)</b>")
            lines += pos_lines
            pnl_icon = "💰" if open_pnl >= 0 else "💸"
            lines.append(f"\n{pnl_icon} <b>Unrealized P&L: ${open_pnl:+.2f}</b>")

            if urgent:
                lines.append(
                    f"\n🚨 <b>Needs attention today: {', '.join(urgent)}</b>"
                )
            lines.append("")
        else:
            lines += ["💼 <b>Positions</b>", "No open positions.", ""]

        # ── Open options summary ────────────────────────────────────────────────
        opts = get_options()
        if opts:
            opt_urgent = []
            for oid, opt in opts.items():
                try:
                    op = get_option_price(opt["underlying"], opt["opt_type"],
                                         opt["strike"], opt["expiry"])
                    dte = days_to_expiry(opt["expiry"])
                    if op and (op <= opt["stop_price"] or op >= opt["target_price"] or dte <= 3):
                        opt_urgent.append(
                            f"  {opt['underlying']} ${opt['strike']:.0f} "
                            f"{opt['opt_type'].upper()} — {'STOP HIT' if op <= opt['stop_price'] else 'TARGET HIT' if op >= opt['target_price'] else f'{dte}d to expiry'}"
                        )
                except Exception:
                    pass
            if opt_urgent:
                lines += ["🎯 <b>Options needing action:</b>"] + opt_urgent + [""]

        # ── Top buy signals ─────────────────────────────────────────────────────
        lines.append(f"📈 <b>Top Setups Today</b>")
        try:
            if regime.get("regime") == "CRISIS":
                lines.append("🚫 Market in CRISIS — no new trades.")
            else:
                wl       = get_watchlist()
                open_set = set(positions.keys())
                sigs     = []
                for t in wl:
                    if t in open_set:
                        continue
                    try:
                        sig = score_entry(t, {"regime": regime.get("regime", "BULL_QUIET")})
                        if sig:
                            sigs.append(sig)
                    except Exception:
                        pass
                sigs.sort(key=lambda x: x.stars, reverse=True)

                if not sigs:
                    lines.append("📭 No clean setups right now. Patience pays.")
                else:
                    for sig in sigs[:5]:
                        lines.append(
                            f"{'⭐' * sig.stars} <b>{sig.ticker}</b>  ${sig.price:.2f}\n"
                            f"   🛑 ${sig.stop:.2f}  🎯 ${sig.target1:.2f}  ⚖️ {sig.rr}:1\n"
                            f"   <i>{sig.why_buy[:100]}{'...' if len(sig.why_buy) > 100 else ''}</i>"
                        )
                    if len(sigs) > 5:
                        others = ", ".join(s.ticker for s in sigs[5:10])
                        lines.append(f"<i>Also on radar: {others}</i>")
        except Exception as e:
            lines.append(f"<i>Could not run scan: {e}</i>")

        lines.append("")

        # ── Daily tip ───────────────────────────────────────────────────────────
        tip = _DAILY_TIPS[now.weekday() % len(_DAILY_TIPS)]
        lines += [
            "━━━━━━━━━━━━━━━━━━━━",
            f"💡 <i>{tip}</i>",
            "━━━━━━━━━━━━━━━━━━━━",
            "<i>Open the app to act on any of these.</i>",
        ]

        return _send("\n".join(lines))

    except Exception as e:
        return _send(f"🌅 <b>Morning Briefing</b>\n\n❌ Error building briefing: {e}")


def alert_daily_briefing(regime_label: str, regime_desc: str,
                         buy_signals: list,
                         exit_count: int, raise_count: int) -> bool:
    """Legacy manual briefing — kept for the Dashboard 'Briefing' button."""
    lines = [
        f"🌅 <b>StockPal — Daily Briefing</b>",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📊 Market: <b>{regime_label}</b>",
        f"<i>{regime_desc}</i>",
        "",
    ]
    if exit_count:
        lines.append(f"🚨 <b>{exit_count} position{'s' if exit_count > 1 else ''} need action — open the app now!</b>")
        lines.append("")
    if raise_count:
        lines.append(f"🔼 <b>{raise_count} stop raise{'s' if raise_count > 1 else ''} suggested</b>")
        lines.append("")
    if buy_signals:
        lines.append(f"📈 <b>Top trade idea{'s' if len(buy_signals) > 1 else ''} today:</b>")
        for sig in buy_signals[:5]:
            lines.append(
                f"  {'⭐' * sig.stars} <b>{sig.ticker}</b> — "
                f"${sig.price:.2f}  |  Stop ${sig.stop:.2f}  |  "
                f"Target ${sig.target1:.2f}  |  {sig.rr}:1 R:R"
            )
        lines.append("")
    else:
        lines.append("📭 No clean buy signals today. Patience pays.")
        lines.append("")
    lines += [
        "━━━━━━━━━━━━━━━━━━━━",
        "<i>Open the tracker app to act on any of these.</i>",
    ]
    return _send("\n".join(lines))


def test_alert() -> bool:
    return _send(
        "✅ alerts are working\n\n"
        "you'll get texts for:\n"
        "• new buy signals\n"
        "• stop losses hit\n"
        "• targets reached\n"
        "• stop raises\n"
        "• overbought exits\n"
        "• morning briefing\n\n"
        "you can also text back any question about your trades"
    )
