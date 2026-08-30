"""
monitor_daemon.py  —  Veteran's Edge background alert daemon

Run this ONCE and leave it running. It checks your positions every 5 minutes
during market hours and fires Telegram alerts whether the Streamlit app is open
or not.

Usage:
    Double-click  start_monitor.bat        ← easiest
    or:  python monitor_daemon.py

The daemon writes a log to  monitor_daemon.log  so you can see what it's doing.
"""

import sys
import os
import time
import logging
from datetime import datetime, date, time as dtime

# ── path ──────────────────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")  # suppress st warnings

from dotenv import load_dotenv
load_dotenv(os.path.join(HERE, ".env"))

# ── logging ───────────────────────────────────────────────────────────────────
LOG_FILE = os.path.join(HERE, "monitor_daemon.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("daemon")

# ── single-instance lock ──────────────────────────────────────────────────────
LOCK_FILE = os.path.join(HERE, ".monitor_daemon.lock")

def _acquire_lock():
    """Ensure only one daemon instance runs. Exits if another is alive."""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as f:
                old_pid = int(f.read().strip())
            # Try sending signal 0 — raises OSError if process is dead
            os.kill(old_pid, 0)
            log.error(
                f"Another daemon is already running (PID {old_pid}). "
                f"Stop it first with stop_monitor.bat, then retry."
            )
            sys.exit(1)
        except (OSError, ValueError):
            log.warning("Stale lock file found — overwriting.")
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    log.info(f"Lock acquired (PID {os.getpid()})")

def _release_lock():
    try:
        os.remove(LOCK_FILE)
    except Exception:
        pass

# ── market hours (US Eastern) ─────────────────────────────────────────────────
try:
    import pytz
    _ET = pytz.timezone("America/New_York")
    def _now_et():
        return datetime.now(_ET)
except ImportError:
    def _now_et():                          # fallback: assume machine is in ET
        return datetime.now()

MARKET_OPEN    = dtime(9, 25)             # 5 min before open
MARKET_CLOSE   = dtime(16, 5)            # 5 min after close
BRIEFING_START = dtime(8, 28)            # start window for morning briefing
BRIEFING_END   = dtime(8, 45)            # end window (generous — survives slow wakeups)

def _is_market_hours() -> bool:
    now = _now_et()
    if now.weekday() >= 5:                 # Saturday / Sunday
        return False
    t = now.time().replace(tzinfo=None)
    return MARKET_OPEN <= t <= MARKET_CLOSE

def _is_briefing_window() -> bool:
    """True if current ET time is the 8:28–8:45 window on a weekday."""
    now = _now_et()
    if now.weekday() >= 5:
        return False
    t = now.time().replace(tzinfo=None)
    return BRIEFING_START <= t <= BRIEFING_END

def _secs_until_next_open() -> int:
    """Return seconds until next weekday market open (capped at 18 h)."""
    try:
        from datetime import timedelta
        now = _now_et()
        for days_ahead in range(1, 8):
            candidate = (now + timedelta(days=days_ahead)).replace(
                hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute,
                second=0, microsecond=0,
            )
            if candidate.weekday() < 5:
                secs = int((candidate - now).total_seconds())
                return max(60, min(secs, 64800))   # cap at 18 h
    except Exception:
        pass
    return 3600

# ── core check ────────────────────────────────────────────────────────────────
CHECK_INTERVAL_SECS = 5 * 60      # 5 minutes between checks

def run_checks():
    """Run alerts for EVERY user against their OWN positions, each routed to their
    OWN Telegram chat. Nobody ever receives another user's alerts."""
    import state as _state
    import alerts as _alerts
    try:
        from auth import all_telegram
        _tg = all_telegram()                 # {username: chat_id}
    except Exception:
        _tg = {}
    _owner  = _state._owner()
    _global = getattr(_alerts, "CHAT_ID", "")
    _users  = _state.users_with_data() or ([_owner] if _global else [])
    for _u in _users:
        _cid = _tg.get(_u) or (_global if _u == _owner else "")   # "" = no Telegram → suppress
        try:
            _state.set_active_user_override(_u)
            _alerts.set_target_chat(_cid)
            _run_checks_once()
        except Exception as _e:
            log.warning("alert checks for %s failed: %s", _u, _e)
    _state.set_active_user_override(None)
    _alerts.set_target_chat(None)


def _run_checks_once():
    """Run one full pass over the ACTIVE user's positions and options, fire due alerts."""

    # Import here so path issues surface as clear errors, not import-time noise
    from signals import check_position
    from state import (
        get_positions, get_options, update_stop, update_targets,
        mark_position_alerted, mark_option_alerted,
        update_managed_flags,
    )
    from alerts import (
        alert_stock_stop, alert_stock_target, alert_stock_raise_stop,
        alert_stock_exit_stale, alert_stock_overbought_exit,
        alert_sell_signal, alert_watch_closely,
        alert_staged_t1, alert_staged_t2, alert_time_stop,
        alert_pullback_add,
        alert_option_target, alert_option_stop, alert_option_near_expiry,
    )

    # ── Stock positions ───────────────────────────────────────────────────────
    positions = get_positions()
    if not positions:
        log.info("No open positions.")
    else:
        log.info(f"Checking {len(positions)} position(s)…")

    for ticker, pos in list(positions.items()):
        try:
            s = check_position(
                ticker, pos["entry"], pos["stop"],
                pos["target1"], pos["date_in"], pos["qty"],
            )
            if s is None:
                continue
            action = s.action
            log.debug(f"  [{ticker}] action={action}  price={s.price:.2f}  pnl={s.pnl_pct:+.2f}%")

            # ── Auto-tighten stop ─────────────────────────────────────────────
            if (
                s.suggested_stop
                and s.suggested_stop > pos["stop"] * 1.01
                and not pos.get("alerted_raise")
                and action in ("RAISE STOP", "TIGHTEN STOP", "TAKE PROFIT")
            ):
                old_stop = pos["stop"]
                update_stop(ticker, s.suggested_stop)
                pos = get_positions().get(ticker, pos)
                alert_stock_raise_stop(
                    ticker, s.price, old_stop, s.suggested_stop,
                    s.pnl_pct, s.pnl_dollars,
                )
                mark_position_alerted(ticker, "alerted_raise")
                log.info(f"  [{ticker}] Stop raised {old_stop:.2f} → {s.suggested_stop:.2f}")

            # ── Auto-raise targets ────────────────────────────────────────────
            if s.pnl_pct > 5:
                try:
                    atr_val = float(
                        str(s.indicators.get("Daily ATR", "$0")).replace("$", "").strip()
                    )
                except Exception:
                    atr_val = 0.0
                if atr_val > 0:
                    new_t1 = round(s.price + atr_val * 3.0, 2)
                    new_t2 = round(s.price + atr_val * 6.0, 2)
                    if (
                        new_t1 > pos.get("target1", 0) * 1.02
                        or new_t2 > pos.get("target2", 0) * 1.02
                    ):
                        update_targets(ticker, new_t1, new_t2)
                        log.info(f"  [{ticker}] Targets raised → T1={new_t1} T2={new_t2}")

            # ── Stop hit ──────────────────────────────────────────────────────
            if action == "EXIT NOW":
                if s.price <= pos["stop"] and not pos.get("alerted_stop"):
                    alert_stock_stop(
                        ticker, pos["entry"], s.price, pos["stop"],
                        pos["qty"], s.pnl_pct, s.pnl_dollars,
                    )
                    mark_position_alerted(ticker, "alerted_stop")
                    log.info(f"  [{ticker}] STOP HIT alert sent")

                elif "nowhere" in s.plain_reason and not pos.get("alerted_stale"):
                    alert_stock_exit_stale(ticker, s.days_held, s.pnl_pct)
                    mark_position_alerted(ticker, "alerted_stale")
                    log.info(f"  [{ticker}] STALE alert sent")

                elif (
                    "overbought" in s.plain_reason.lower()
                    and not pos.get("alerted_overbought")
                ):
                    try:
                        rsi_val = float(s.indicators.get("RSI", 0))
                    except Exception:
                        rsi_val = 0.0
                    alert_stock_overbought_exit(ticker, rsi_val, s.pnl_pct, s.pnl_dollars)
                    mark_position_alerted(ticker, "alerted_overbought")
                    log.info(f"  [{ticker}] OVERBOUGHT alert sent")

            # ── Target hit ────────────────────────────────────────────────────
            elif action == "TAKE PROFIT" and not pos.get("alerted_target"):
                alert_stock_target(
                    ticker, pos["entry"], s.price, pos["target1"],
                    pos["qty"], s.pnl_pct, s.pnl_dollars,
                )
                mark_position_alerted(ticker, "alerted_target")
                log.info(f"  [{ticker}] TARGET HIT alert sent")

            # ── Watch closely ─────────────────────────────────────────────────
            elif action == "WATCH CLOSELY" and not pos.get("alerted_watch"):
                alert_watch_closely(ticker, s.price, s.pnl_pct, s.plain_reason)
                mark_position_alerted(ticker, "alerted_watch")
                log.info(f"  [{ticker}] WATCH alert sent")

            # ── Chart analysis sell ───────────────────────────────────────────
            if action == "HOLD" and not pos.get("alerted_chart_sell"):
                try:
                    from chart_analysis import analyze_ticker
                    try:
                        _days = (
                            date.today()
                            - date.fromisoformat(pos.get("date_in", str(date.today())))
                        ).days
                    except Exception:
                        _days = 0
                    ca = analyze_ticker(ticker, position={
                        "entry":   pos["entry"],
                        "stop":    pos["stop"],
                        "target1": pos.get("target1", 0),
                        "target2": pos.get("target2", 0),
                        "qty":     pos.get("qty", 1),
                        "days_held": _days,
                    })
                    if (
                        ca and "error" not in ca
                        and ca.get("action") == "SELL"
                        and ca.get("confidence") in ("High", "Medium")
                    ):
                        alert_sell_signal(
                            ticker, s.price, s.pnl_pct,
                            ca.get("headline", "Technical deterioration"),
                            ca.get("reasoning", s.plain_reason),
                            ca.get("risks", []),
                            ca.get("suggested_stop"),
                        )
                        mark_position_alerted(ticker, "alerted_chart_sell")
                        log.info(f"  [{ticker}] CHART SELL alert sent")
                except Exception as e:
                    log.debug(f"  [{ticker}] Chart analysis skipped: {e}")

            # ── Managed staged exits (scanner positions) ──────────────────────
            if pos.get("managed"):
                qty_rem = int(pos.get("qty_remaining", pos.get("qty", 0)))
                t1      = float(pos.get("target1", float("inf")))
                t2      = float(pos.get("target2", float("inf")))

                # T1 — first target hit, exit1 not done yet
                if not pos.get("exit1_done") and s.price >= t1:
                    pnl_d = round((s.price - pos["entry"]) * qty_rem, 2)
                    alert_staged_t1(
                        ticker, pos["entry"], s.price, t1,
                        qty_rem, s.pnl_pct, pnl_d,
                    )
                    log.info(f"  [{ticker}] T1 staged exit alert sent")

                # T2 — second target hit, exit1 done, exit2 not done yet
                elif pos.get("exit1_done") and not pos.get("exit2_done") and s.price >= t2:
                    pnl_d = round((s.price - pos["entry"]) * qty_rem, 2)
                    alert_staged_t2(
                        ticker, pos["entry"], s.price, t2,
                        qty_rem, s.pnl_pct, pnl_d,
                    )
                    log.info(f"  [{ticker}] T2 staged exit alert sent")

                # Time stop — 10 trading days flat (< ±3%)
                if not pos.get("exit2_done") and abs(s.pnl_pct) < 3.0:
                    try:
                        from datetime import date as _dt_date
                        days_in = (_dt_date.today() - _dt_date.fromisoformat(
                            pos.get("date_in", str(_dt_date.today()))
                        )).days
                    except Exception:
                        days_in = 0
                    if days_in >= 10:
                        alert_time_stop(ticker, days_in, s.pnl_pct, qty_rem)
                        log.info(f"  [{ticker}] Time stop alert sent ({days_in} days flat)")

                # Pullback add — only on profitable positions not yet fully exited
                if (s.pnl_pct > 2.0
                        and not pos.get("exit2_done")
                        and qty_rem > 0):
                    try:
                        import yfinance as yf
                        import numpy as np
                        import pandas as pd
                        _pb_df = yf.download(
                            ticker, period="3mo", interval="1d",
                            auto_adjust=True, progress=False,
                        )
                        if _pb_df is not None and len(_pb_df) >= 25:
                            _cl  = _pb_df["Close"].values.astype(float)
                            _vol = _pb_df["Volume"].values.astype(float)
                            _ema20 = float(pd.Series(_cl).ewm(span=20, adjust=False).mean().iloc[-1])
                            _ema50 = float(pd.Series(_cl).ewm(span=50, adjust=False).mean().iloc[-1])
                            _price = float(_cl[-1])
                            _v_avg = float(np.mean(_vol[-20:])) if len(_vol) >= 20 else 1.0
                            _v_rec = float(np.mean(_vol[-5:]))  if len(_vol) >= 5  else _v_avg
                            _vr    = _v_rec / _v_avg if _v_avg > 0 else 1.0

                            _near_ema20  = abs(_price - _ema20) / _ema20 < 0.025
                            _above_ema50 = _price > _ema50
                            _vol_dry     = _vr < 1.0

                            if _near_ema20 and _above_ema50 and _vol_dry:
                                _stop_p = float(pos.get("stop", 0))
                                _rps    = max(_price - _stop_p, 0.01)
                                # get buying power
                                try:
                                    _mm_acc = int(os.getenv("MOOMOO_CASH_ACC_ID", "0") or "0")
                                    _mm_env = "REAL" if _mm_acc else "SIMULATE"
                                    from moomoo_integration import MoomooTrader as _MT
                                    _cash = _MT(env=_mm_env, acc_id=_mm_acc).get_buying_power()
                                except Exception:
                                    _cash = 0.0
                                _acct     = max(_cash, 10000.0)
                                _by_risk  = int(_acct * 0.01 / _rps)
                                _by_cash  = int(_cash * 0.20 / _price) if _cash > 0 else _by_risk
                                _sugg_qty = max(1, min(_by_risk, _by_cash or _by_risk, int(pos.get("qty", 1))))
                                alert_pullback_add(
                                    ticker, round(_price, 2), round(_ema20, 2),
                                    _stop_p, _sugg_qty, round(_cash, 2), s.pnl_pct,
                                )
                                log.info(f"  [{ticker}] Pullback add alert sent (EMA20=${_ema20:.2f})")
                    except Exception as _pbe:
                        log.debug(f"  [{ticker}] Pullback check failed: {_pbe}")

                # Trailing stop — raise if price moved up 1% above trail
                if pos.get("trailing") and pos.get("trail_stop"):
                    trail_new = round(s.price * 0.95, 2)   # 5% below current
                    if trail_new > float(pos["trail_stop"]):
                        update_managed_flags(ticker, trail_stop=trail_new)
                        update_stop(ticker, trail_new)
                        log.info(f"  [{ticker}] Trailing stop raised to ${trail_new:.2f}")

        except Exception as e:
            log.error(f"  [{ticker}] Error during check: {e}")

    # ── Options ───────────────────────────────────────────────────────────────
    try:
        from market_data import get_option_price, days_to_expiry
        options = get_options()
        if options:
            log.info(f"Checking {len(options)} option(s)…")
        for oid, opt in list(options.items()):
            try:
                price = get_option_price(
                    opt["underlying"], opt["opt_type"],
                    opt["strike"], opt["expiry"],
                )
                if price is None:
                    continue
                dte = days_to_expiry(opt["expiry"])

                if price >= opt["target_price"] and not opt.get("alerted_target"):
                    alert_option_target(
                        opt["underlying"], opt["opt_type"], opt["strike"],
                        opt["expiry"], opt["contracts"], opt["entry_price"],
                        price, opt["target_price"],
                    )
                    mark_option_alerted(oid, "alerted_target")
                    log.info(f"  [{oid}] OPTION TARGET alert sent")

                elif price <= opt["stop_price"] and not opt.get("alerted_stop"):
                    alert_option_stop(
                        opt["underlying"], opt["opt_type"], opt["strike"],
                        opt["expiry"], opt["contracts"], opt["entry_price"],
                        price, opt["stop_price"],
                    )
                    mark_option_alerted(oid, "alerted_stop")
                    log.info(f"  [{oid}] OPTION STOP alert sent")

                if dte <= 5 and not opt.get("alerted_expiry"):
                    alert_option_near_expiry(
                        opt["underlying"], opt["opt_type"], opt["strike"],
                        opt["expiry"], dte, price,
                    )
                    mark_option_alerted(oid, "alerted_expiry")
                    log.info(f"  [{oid}] OPTION EXPIRY alert sent")

            except Exception as e:
                log.error(f"  [{oid}] Options check error: {e}")
    except Exception:
        pass   # market_data may be unavailable during off-hours


# ── main loop ─────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("Veteran's Edge — Monitor Daemon + Bot")
    log.info(f"PID {os.getpid()} | Check interval: {CHECK_INTERVAL_SECS // 60} min")
    log.info(f"Log file: {LOG_FILE}")
    log.info("=" * 60)

    _acquire_lock()

    # ── Start Telegram chatbot in a background thread ─────────────────────────
    try:
        import threading
        from telegram_bot import run_bot
        bot_thread = threading.Thread(target=run_bot, daemon=True, name="telegram-bot")
        bot_thread.start()
        log.info("Telegram bot thread started — you can now text the bot queries")
    except Exception as e:
        log.warning(f"Could not start Telegram bot: {e}")

    try:
        while True:
            # ── Morning briefing (8:28–8:45 ET, once per day) ────────────────
            if _is_briefing_window():
                try:
                    from morning_briefing import send_morning_briefing
                    send_morning_briefing()        # no-op if already sent today
                except Exception as e:
                    log.error(f"Morning briefing error: {e}", exc_info=True)

            if _is_market_hours():
                start = time.time()
                log.info("--- Running checks ---")
                try:
                    run_checks()
                except Exception as e:
                    log.error(f"run_checks() crashed: {e}", exc_info=True)

                # ── Precompute the market scan so the web app loads instantly ──
                try:
                    from scanner import run_and_cache_scan
                    from market_data import get_regime
                    run_and_cache_scan(get_regime())
                except Exception as e:
                    log.error(f"Scan caching failed: {e}", exc_info=True)

                # ── Intraday pullback Telegram alerts — DISABLED for now ──────
                # (Pullbacks tab in the app still works; only the alerts are off.)
                # To re-enable, uncomment:
                # try:
                #     from daytrader_pullback import scan_and_alert
                #     from state import get_watchlist
                #     _wl = [(w if isinstance(w, str) else w.get("ticker", "")) for w in get_watchlist()]
                #     _wl = [t for t in dict.fromkeys(_wl) if t][:40]
                #     if _wl:
                #         _n = scan_and_alert(_wl)
                #         if _n:
                #             log.info(f"Pullback alerts sent: {_n}")
                # except Exception as e:
                #     log.error(f"Pullback scan failed: {e}", exc_info=True)

                elapsed = time.time() - start
                sleep_for = max(10, CHECK_INTERVAL_SECS - elapsed)
                log.info(f"Done in {elapsed:.1f}s. Next check in {sleep_for / 60:.1f} min.")
                time.sleep(sleep_for)
            else:
                secs = _secs_until_next_open()
                now_str = _now_et().strftime("%H:%M")
                log.info(
                    f"Market closed ({now_str} ET). "
                    f"Sleeping {secs // 3600}h {(secs % 3600) // 60}m until next open."
                )
                # Sleep in 1-hour chunks so the log shows heartbeats
                time.sleep(min(secs, 3600))

    except KeyboardInterrupt:
        log.info("Daemon stopped by user (Ctrl+C).")
    finally:
        _release_lock()
        log.info("Lock released. Daemon exited.")


if __name__ == "__main__":
    main()
