"""
telegram_bot.py — two-way Telegram chatbot for StockPal

Everything goes through Groq (Llama 3.3 70B).
Data is fetched based on what the message is asking about,
then handed to Groq as context so it writes ONE consistent reply.

Examples:
  "what are my positions"
  "should I hold plug"
  "scan market"
  "analyze NVDA"
  "any good setups on my watchlist"
  "what's the market doing"
  "add AAPL"
"""

import os
import sys
import re
import logging

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from dotenv import load_dotenv
load_dotenv(os.path.join(HERE, ".env"))

TOKEN         = os.getenv("TELEGRAM_BOT_TOKEN", "")
OWNER_CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID", "")).strip()
GROQ_KEY      = os.getenv("GROQ_API_KEY", "").strip()

log = logging.getLogger("telegram_bot")

# ── conversation memory (last 10 turns) ───────────────────────────────────────
_CONV_HISTORY: list = []   # [{"role": "user"/"assistant", "content": "..."}]
MAX_HISTORY = 10           # messages to keep (5 exchanges)

# ── Moomoo config ─────────────────────────────────────────────────────────────
_MM_ACC_ID = int(os.getenv("MOOMOO_CASH_ACC_ID", "0") or "0")
_MM_ENV    = "REAL" if _MM_ACC_ID else "SIMULATE"


# ── trade execution helpers ───────────────────────────────────────────────────

def _live_price(ticker: str) -> float:
    """Quick yfinance fetch for the most recent close price."""
    try:
        import yfinance as yf
        df = yf.download(ticker, period="2d", interval="1m",
                         auto_adjust=True, progress=False)
        if df is not None and not df.empty:
            return float(df["Close"].iloc[-1])
    except Exception:
        pass
    return 0.0


def _build_confirm_msg(trade: dict) -> str:
    """Build the Option-B confirmation message with live price."""
    from trade_queue import seconds_left
    ticker  = trade["ticker"]
    action  = trade["action"]
    qty     = trade["qty"]
    trigger = trade.get("trigger", "manual")
    secs    = seconds_left()

    live_p  = _live_price(ticker)
    est_val = round(live_p * qty, 2) if live_p else 0

    action_word  = "sell" if action == "sell" else "buy"
    value_label  = "proceeds" if action == "sell" else "cost"
    trigger_line = {
        "stop_hit":   f"🛑 Stop hit on <b>{ticker}</b>",
        "target_hit": f"🎯 Target hit on <b>{ticker}</b>",
        "manual":     f"📋 Trade request: <b>{action.upper()} {ticker}</b>",
    }.get(trigger, f"📋 <b>{ticker}</b>")

    pnl_line = ""
    if trade.get("pnl_pct") and action == "sell":
        pnl_line = f"P&amp;L: <b>{trade['pnl_pct']:+.1f}% (${trade['pnl_dol']:+,.0f})</b>\n"

    price_line = (
        f"Live price:  <b>${live_p:.2f}</b>\n" if live_p
        else "Live price:  unavailable\n"
    )
    est_line = (
        f"Est. {value_label}: <b>${est_val:,.2f}</b>\n" if est_val
        else ""
    )

    return (
        f"{trigger_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{pnl_line}"
        f"{price_line}"
        f"Order: <b>{action_word.upper()} {qty} shares at MARKET</b>\n"
        f"{est_line}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Reply <b>YES</b> to execute now\n"
        f"Reply <b>NO</b> to adjust the qty\n"
        f"<i>⏱ {secs}s remaining</i>"
    )


def _execute_pending_trade(trade: dict) -> str:
    """Place the market order via Moomoo and update state."""
    from trade_queue import clear_pending
    ticker = trade["ticker"]
    action = trade["action"]
    qty    = trade["qty"]

    live_p = _live_price(ticker)

    # ── place order ───────────────────────────────────────────────────────────
    try:
        from moomoo_integration import MoomooTrader
        mt = MoomooTrader(env=_MM_ENV, acc_id=_MM_ACC_ID)

        if action == "sell":
            ok, msg, oid = mt.place_entry_order(
                ticker=ticker, qty=qty,
                price=live_p, order_type="MARKET",
            )
            # override: use the sell side
            try:
                from futu import TrdSide
                ctx = mt._ctx()
                from futu import OrderType
                kwargs = dict(
                    price=0.0, qty=qty,
                    code=f"US.{ticker}",
                    trd_side=TrdSide.SELL,
                    order_type=OrderType.MARKET,
                    trd_env=mt.trd_env,
                    remark="tg_sell",
                )
                if mt.acc_id:
                    kwargs["acc_id"] = mt.acc_id
                from futu import RET_OK
                ret, data = ctx.place_order(**kwargs)
                ctx.close()
                if ret == RET_OK:
                    oid = str(data["order_id"].iloc[0])
                    ok, msg = True, "Sell order placed"
                else:
                    ok, msg = False, str(data)
            except Exception as ex:
                # fallback already set above
                pass
        else:
            ok, msg, oid = mt.place_entry_order(
                ticker=ticker, qty=qty,
                price=live_p, order_type="MARKET",
            )
    except Exception as e:
        clear_pending()
        return (
            f"❌ Order failed — Moomoo unreachable\n"
            f"<i>{e}</i>\n\n"
            f"Place the order manually in Moomoo."
        )

    clear_pending()

    if not ok:
        return (
            f"❌ Order rejected by Moomoo\n"
            f"<i>{msg}</i>\n\n"
            f"Place the order manually."
        )

    # ── update local state based on trigger ──────────────────────────────────
    trigger = trade.get("trigger", "manual")
    extra_msg = ""
    try:
        from state import (
            close_position, partial_exit_position,
            update_managed_flags, update_stop, get_positions,
            add_to_position,
        )
        if trigger == "stop_hit":
            close_position(ticker, live_p, "Stop hit — Telegram sell")

        elif trigger == "t1_hit":
            partial_exit_position(ticker, qty, live_p, "T1 hit — staged exit (half)")
            # move stop to breakeven
            pos   = get_positions().get(ticker, {})
            entry = float(pos.get("entry", live_p))
            update_stop(ticker, entry)
            update_managed_flags(ticker, exit1_done=True)
            extra_msg = (
                f"\n🔒 Stop moved to breakeven (${entry:.2f})\n"
                f"Remaining shares trailing — let the winner run."
            )

        elif trigger == "t2_hit":
            partial_exit_position(ticker, qty, live_p, "T2 hit — staged exit")
            trail_stop = round(live_p * 0.95, 2)
            update_managed_flags(
                ticker, exit2_done=True, trailing=True, trail_stop=trail_stop,
            )
            update_stop(ticker, trail_stop)
            extra_msg = (
                f"\n📈 Trailing stop activated at ${trail_stop:.2f} (5% below)\n"
                f"Raises automatically as price moves up."
            )

        elif trigger == "pullback_add":
            add_to_position(ticker, qty, live_p)
            extra_msg = (
                f"\n📈 Added {qty} shares at ${live_p:.2f}\n"
                f"Avg entry updated. Stop unchanged."
            )

        elif trigger == "time_stop":
            close_position(ticker, live_p, "Time stop — 10 days flat, exited")

        elif trigger == "target_hit":
            partial_exit_position(ticker, qty, live_p, "Target hit — Telegram sell (half)")

    except Exception as _se:
        log.warning(f"State update after trade failed: {_se}")

    est_val = round(live_p * qty, 2) if live_p else 0
    return (
        f"✅ <b>{action.upper()} {qty} {ticker} — ORDER SENT</b>\n"
        f"Market order placed @ ~${live_p:.2f}\n"
        f"Est. {'proceeds' if action == 'sell' else 'cost'}: ${est_val:,.2f}\n"
        f"Order ID: <code>{oid}</code>"
        f"{extra_msg}\n\n"
        f"<i>Check Moomoo for fill confirmation.</i>"
    )


def _handle_manual_trade(text: str) -> str:
    """
    Parse plain-text trade commands:
      'buy 10 NVDA'  'sell PLUG'  'sell all AAPL'  'buy TSLA 5 shares'
    Sets a pending trade and returns the Option-B confirmation message.
    """
    from trade_queue import set_pending, get_pending
    t = text.strip()

    # patterns: buy/sell [qty] TICKER [qty shares]
    buy_m = re.search(
        r'\b(buy|long)\b\s*(\d+)?\s*([A-Z]{1,5})\b(?:\s+(\d+)\s*shares?)?',
        t, re.IGNORECASE,
    )
    sell_m = re.search(
        r'\b(sell|exit|close|short)\b\s*(\d+|all)?\s*([A-Z]{1,5})\b(?:\s+(\d+)\s*shares?)?',
        t, re.IGNORECASE,
    )

    match = buy_m or sell_m
    if not match:
        return ""

    action = "buy" if buy_m else "sell"
    grp    = match.groups()
    # groups: (verb, qty1, ticker, qty2)
    raw_qty = grp[1] or grp[3]

    # For sell, resolve qty from open position if "all" or not specified
    ticker = grp[2].upper()
    if action == "sell" and (raw_qty is None or str(raw_qty).lower() == "all"):
        try:
            from state import get_positions
            pos = get_positions().get(ticker, {})
            qty = int(pos.get("qty", 0)) or 1
        except Exception:
            qty = 1
    else:
        qty = int(raw_qty) if raw_qty else 1

    set_pending(action=action, ticker=ticker, qty=qty, trigger="manual")
    pending = get_pending()
    return _build_confirm_msg(pending)


# ── simple non-AI commands ────────────────────────────────────────────────────

def _help_reply() -> str:
    return (
        "things you can ask:\n\n"
        "• <b>positions</b> — what you're holding\n"
        "• <b>p&l</b> — how you're doing\n"
        "• <b>signals</b> — setups on your watchlist\n"
        "• <b>scan market</b> — top picks from the full 300-ticker universe\n"
        "• <b>analyze AAPL</b> — full tech + fundamental breakdown\n"
        "• <b>market</b> — current regime + VIX\n"
        "• <b>watchlist</b> — your list\n"
        "• <b>AAPL</b> — quick info on any ticker\n"
        "• <b>stop AAPL</b> / <b>target AAPL</b>\n"
        "• <b>add AAPL</b> / <b>remove AAPL</b>\n"
        "• <b>buy 10 NVDA</b> / <b>sell PLUG</b> — execute trades via Moomoo\n"
        "• <b>YES</b> / <b>NO</b> — confirm or adjust a pending trade\n"
        "• <b>briefing</b> — send morning briefing now\n"
        "• <b>test</b> — check alerts are working\n\n"
        "or just ask anything in plain English"
    )


# ── side-effect commands (no AI needed) ──────────────────────────────────────

def _add_ticker(ticker: str) -> str:
    from state import add_to_watchlist, get_watchlist
    ticker = ticker.upper()
    if ticker in get_watchlist():
        return f"{ticker} is already on your watchlist"
    add_to_watchlist(ticker)
    return f"added {ticker} to your watchlist"


def _remove_ticker(ticker: str) -> str:
    from state import remove_from_watchlist, get_watchlist
    ticker = ticker.upper()
    if ticker not in get_watchlist():
        return f"{ticker} isn't on your watchlist"
    remove_from_watchlist(ticker)
    return f"removed {ticker} from your watchlist"


# ── data fetchers (return dicts / lists, NOT formatted strings) ───────────────

def _fetch_portfolio() -> dict:
    """Returns raw portfolio data for Groq context."""
    from state import get_positions, get_watchlist, get_closed
    from market_data import get_current_price

    positions = get_positions()
    pos_lines = []
    total_open_pnl = 0.0
    for ticker, pos in positions.items():
        try:
            price     = get_current_price(ticker) or pos["entry"]
            pnl_pct   = (price - pos["entry"]) / pos["entry"] * 100
            pnl_dol   = (price - pos["entry"]) * pos["qty"]
            total_open_pnl += pnl_dol
            pos_lines.append(
                f"{ticker}: {pos['qty']} shares @ ${pos['entry']:.2f}, "
                f"now ${price:.2f} ({pnl_pct:+.1f}% / ${pnl_dol:+,.0f}), "
                f"stop ${pos['stop']:.2f}, target ${pos.get('target1', 0):.2f}"
            )
        except Exception:
            pos_lines.append(f"{ticker}: price unavailable")

    closed     = get_closed()
    closed_pnl = sum(t.get("pnl_dollars", 0) for t in closed)
    wins       = sum(1 for t in closed if t.get("pnl_pct", 0) > 0)
    win_rate   = round(wins / len(closed) * 100) if closed else 0

    return {
        "positions":     pos_lines,
        "total_open_pnl": total_open_pnl,
        "closed_pnl":    closed_pnl,
        "win_rate":      win_rate,
        "trade_count":   len(closed),
        "watchlist":     get_watchlist(),
        "position_keys": list(positions.keys()),
    }


def _fetch_regime() -> dict:
    from market_data import detect_regime, get_vix, get_bars, compute_indicators
    try:
        vix    = get_vix()
        spy_df = get_bars("SPY", "1y", "1d")
        if spy_df is not None:
            spy_df = compute_indicators(spy_df)
        regime = detect_regime(spy_df, vix)
        return regime
    except Exception:
        return {"regime": "BULL_QUIET", "vix": 0, "spy": 0, "above_200": True}


def _fetch_watchlist_signals(regime_str: str, position_keys: list) -> list:
    from state import get_watchlist
    from signals import score_entry
    from market_data import get_bars_batch, compute_indicators
    wl        = get_watchlist()
    open_set  = set(position_keys)
    scan_list = [t for t in wl if t not in open_set][:20]
    if not scan_list:
        return []
    try:
        batch = get_bars_batch(scan_list, "1y", "1d")
    except Exception:
        batch = {}
    sigs = []
    for t in scan_list:
        try:
            df = batch.get(t)
            if df is not None:
                df = compute_indicators(df)
            s = score_entry(t, {"regime": regime_str}, _df=df)
            if s:
                sigs.append(s)
        except Exception:
            pass
    sigs.sort(key=lambda x: x.stars, reverse=True)
    return sigs


def _fetch_universe_scan(regime: dict, exclude: set) -> dict:
    """Returns {'swing': [...], 'vcp': [...]} — top picks from both lists."""
    from scanner import run_daily_scan
    try:
        swing_sigs, _, vcp_sigs = run_daily_scan(regime)
        fresh_sw  = [s for s in swing_sigs if s.ticker not in exclude]
        fresh_vcp = [s for s in vcp_sigs   if s.ticker not in exclude]
        fresh_sw.sort( key=lambda x: (x.trend_template, x.rs_rank, x.stars), reverse=True)
        fresh_vcp.sort(key=lambda x: (x.stars, x.rs_rank), reverse=True)
        return {"swing": fresh_sw[:8], "vcp": fresh_vcp[:5]}
    except Exception as e:
        log.error(f"Universe scan error: {e}")
        return {"swing": [], "vcp": []}


def _fetch_analysis(ticker: str, positions: dict) -> dict:
    from chart_analysis import analyze_ticker
    ticker  = ticker.upper()
    pos_ctx = None
    if ticker in positions:
        from state import get_positions
        pos = get_positions().get(ticker, {})
        from datetime import date
        try:
            days_held = (date.today() - date.fromisoformat(
                pos.get("date_in", str(date.today())))).days
        except Exception:
            days_held = 0
        pos_ctx = {
            "entry":     pos.get("entry", 0),
            "stop":      pos.get("stop", 0),
            "target1":   pos.get("target1", 0),
            "qty":       pos.get("qty", 1),
            "days_held": days_held,
        }
    return analyze_ticker(ticker, position=pos_ctx)


# ── intent classifier (fast Groq call, no data needed) ───────────────────────

def _classify_intent(query: str) -> dict:
    """
    Ask Groq to classify what data the query needs.
    Returns e.g. {"universe_scan": True, "watchlist_scan": False, "analyze": "NVDA"}
    Uses 50 tokens — very fast.
    """
    try:
        from groq import Groq
        client = Groq(api_key=GROQ_KEY)
        resp   = client.chat.completions.create(
            model="llama3-8b-8192",   # smallest/fastest model for classification
            max_tokens=60,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Classify what data a trading bot needs to answer this question. "
                        "Reply with ONLY a JSON object, no explanation. Keys:\n"
                        "  universe_scan: true if user wants stocks from the full market (not their watchlist)\n"
                        "  watchlist_scan: true if user wants signals/setups from their watchlist\n"
                        "  analyze: ticker symbol string if user wants a deep analysis of a specific stock, else null\n"
                        "  portfolio: true if user asks about their positions, P&L, or holdings\n"
                        "Example: {\"universe_scan\":true,\"watchlist_scan\":false,\"analyze\":null,\"portfolio\":false}"
                    ),
                },
                {"role": "user", "content": query},
            ],
        )
        import json
        raw = resp.choices[0].message.content.strip()
        # strip markdown fences if present
        raw = re.sub(r"```(?:json)?", "", raw).strip().strip("`")
        return json.loads(raw)
    except Exception as e:
        log.debug(f"Intent classification failed: {e}")
        return {"universe_scan": False, "watchlist_scan": False, "analyze": None, "portfolio": False}


# ── single Groq reply — all data fed as context ───────────────────────────────

def _groq_reply(query: str) -> str:
    if not GROQ_KEY:
        return "groq key not set — check .env"

    from groq import Groq

    # ── 1. Classify intent (fast — 50 tokens) ────────────────────────────────
    intent = _classify_intent(query)
    log.info(f"Bot intent: {intent}")

    # ── 2. Always fetch base portfolio + regime ───────────────────────────────
    portfolio  = _fetch_portfolio()
    regime     = _fetch_regime()
    regime_str = regime.get("regime", "BULL_QUIET")

    # ── performance summary (lessons + history for Groq) ─────────────────────
    try:
        from state import get_closed as _get_closed
        from performance import get_groq_summary, get_lessons
        _closed_trades = _get_closed()
        _perf_summary  = get_groq_summary(_closed_trades) if _closed_trades else "No closed trades yet."
        # check for lessons relevant to any ticker mentioned in the query
        _ticker_in_query = re.search(r'\b([A-Z]{2,5})\b', query.upper())
        _candidate = _ticker_in_query.group(1) if _ticker_in_query else ""
        _lessons = get_lessons(_closed_trades, candidate_ticker=_candidate)
    except Exception:
        _perf_summary = ""
        _lessons = []

    context_blocks = [
        "=== PORTFOLIO ===",
        "Open positions: " + (", ".join(portfolio["positions"]) or "none"),
        f"Open P&L: ${portfolio['total_open_pnl']:+,.0f}",
        f"Closed P&L: ${portfolio['closed_pnl']:+,.0f} | Win rate: {portfolio['win_rate']}% ({portfolio['trade_count']} trades)",
        f"Watchlist: {', '.join(portfolio['watchlist'][:20])}",
        "",
        "=== MARKET ===",
        f"Regime: {regime_str} | VIX: {regime.get('vix', 0):.1f} | "
        f"SPY: ${regime.get('spy', 0):.2f} | "
        f"200MA: {'above' if regime.get('above_200') else 'below'}",
    ]

    if _perf_summary:
        context_blocks += ["", "=== TRADING HISTORY & PERFORMANCE ===", _perf_summary]

    if _lessons:
        context_blocks += ["", "=== LESSONS FROM PAST TRADES (share these if relevant) ==="] + _lessons

    # ── 3. Full universe scan (with analysis validation on top picks) ─────────
    if intent.get("universe_scan"):
        log.info("Bot: running universe scan")
        exclude = set(portfolio["position_keys"]) | set(portfolio["watchlist"])
        scan_results = _fetch_universe_scan(regime, exclude)
        pos_keys_set = {k: True for k in portfolio["position_keys"]}

        swing_list = scan_results.get("swing", [])
        vcp_list   = scan_results.get("vcp", [])

        if swing_list:
            from scanner import UNIVERSE
            lines = []
            for s in swing_list:
                sector = next((sec for sec, tks in UNIVERSE.items() if s.ticker in tks), "")
                try:
                    ca = _fetch_analysis(s.ticker, pos_keys_set)
                    verdict = ca.get("action", "?") if "error" not in ca else "?"
                    conf    = ca.get("confidence", "") if "error" not in ca else ""
                    analysis_note = f"analysis={verdict}({conf})"
                except Exception:
                    analysis_note = "analysis=unavailable"
                rs = getattr(s, "rs_rank", 0)
                tt = getattr(s, "trend_template", 0)
                lines.append(
                    f"{s.ticker} ({'⭐'*s.stars}) ${s.price:.2f}"
                    + (f" [{sector}]" if sector else "")
                    + f" | RS {rs:.0f} | TT {tt}/8"
                    + f" | stop ${s.stop:.2f} | target ${s.target1:.2f} | {s.rr}:1"
                    + f" | {analysis_note}"
                    + f" | {s.why_buy[:80]}"
                )
            context_blocks += ["", "=== FULL MARKET SCAN — SWING SETUPS (outside watchlist) ===",
                                "Note: only recommend tickers where analysis=BUY or WATCH, not SELL."] + lines
        else:
            context_blocks += ["", "=== FULL MARKET SCAN ===", "No clean swing setups found right now."]

        if vcp_list:
            vcp_lines = []
            for v in vcp_list:
                ct_str = " → ".join(f"{d}%" for d in v.contractions)
                rs = getattr(v, "rs_rank", 0)
                tt = getattr(v, "trend_template", 0)
                vcp_lines.append(
                    f"{v.ticker} ({'⭐'*v.stars}) ${v.price:.2f}"
                    + f" | {v.num_pivots}-pivot VCP | contractions: {ct_str}"
                    + f" | pivot breakout ${v.pivot_high:.2f}"
                    + f" | stop ${v.stop:.2f} | target ${v.target:.2f}"
                    + f" | tight {v.tightness_pct}% | {'vol dry-up ✓' if v.vol_dry_up else 'vol ok'}"
                    + f" | RS {rs:.0f} | TT {tt}/8"
                )
            context_blocks += ["", "=== VCP SETUPS (Volatility Contraction Patterns) ===",
                                "VCPs = coiled springs. Buy on breakout above pivot high, not before."] + vcp_lines

    # ── 4. Ticker analysis ────────────────────────────────────────────────────
    analyze_ticker_sym = intent.get("analyze")
    # also catch explicit pattern the classifier might miss
    if not analyze_ticker_sym:
        am = re.search(
            r'\b(?:analyze|analysis|deep dive|deep-dive|breakdown|tell me about)\s+([A-Za-z]{1,5})\b'
            r'|\b([A-Za-z]{1,5})\s+(?:analysis|analyze|deep dive|breakdown)\b',
            query, re.IGNORECASE,
        )
        if am:
            analyze_ticker_sym = am.group(1) or am.group(2)

    if analyze_ticker_sym:
        ticker = analyze_ticker_sym.upper()
        log.info(f"Bot: analyzing {ticker}")
        ca = _fetch_analysis(ticker, {k: True for k in portfolio["position_keys"]})
        if "error" not in ca:
            context_blocks += [
                "", f"=== ANALYSIS: {ticker} ===",
                f"Action: {ca['action']} ({ca['confidence']} confidence) | Score: {ca.get('score', 0):+d}",
                f"Headline: {ca['headline']}",
                f"Reasoning: {ca['reasoning'][:300]}",
                "Catalysts: " + " | ".join(ca.get("catalysts", [])[:3]),
                "Risks: " + " | ".join(ca.get("risks", [])[:3]),
                f"Suggested stop: ${ca.get('suggested_stop', 'N/A')}",
                f"Outlook: {ca.get('outlook', '')}",
                f"Earnings in: {ca.get('earnings_days', 999)} days",
                f"Fundamentals: {ca.get('fundamentals', '')[:200]}",
            ]
        else:
            context_blocks += ["", f"=== ANALYSIS: {ticker} ===", f"Failed: {ca.get('message')}"]

    # ── 5. Watchlist signals ──────────────────────────────────────────────────
    if intent.get("watchlist_scan"):
        log.info("Bot: running watchlist scan")
        sigs = _fetch_watchlist_signals(regime_str, portfolio["position_keys"])
        if sigs:
            lines = [
                f"{s.ticker} ({'⭐'*s.stars}) ${s.price:.2f} | stop ${s.stop:.2f} | "
                f"target ${s.target1:.2f} | {s.rr}:1 | {s.why_buy[:100]}"
                for s in sigs[:6]
            ]
            context_blocks += ["", "=== WATCHLIST SIGNALS ==="] + lines
        else:
            context_blocks += ["", "=== WATCHLIST SIGNALS ===", "No signals right now."]

    # ── 6. Main Groq answer (with conversation history) ──────────────────────
    full_context = "\n".join(context_blocks)

    system_msg = {
        "role": "system",
        "content": (
            "You are a trading assistant texting a day trader. "
            "Be direct, short, casual — like a smart friend who knows markets. "
            "No fluff. Max 8 lines.\n\n"
            "FORMATTING: Use HTML tags only — <b>TICKER</b> for bold. "
            "NEVER use markdown asterisks like **this** — they will not render.\n\n"
            "RS = Relative Strength rank vs SPY (0–99). 80+ = strong, 60–79 = decent, <60 = weak. "
            "TT = Minervini Trend Template score (0–8 criteria). 6+ = strong uptrend, 8/8 = textbook setup. "
            "Prioritize tickers with RS 70+ AND TT 6+ when giving recommendations.\n\n"
            "PRIORITY RULE: If a full analysis (=== ANALYSIS ===) is provided for a ticker, "
            "that is the final word. If the analysis says SELL or avoid, do NOT recommend "
            "that ticker even if it appeared in the scan results. "
            "Scan results are a starting shortlist — the analysis overrides them.\n\n"
            "PERFORMANCE RULE: If TRADING HISTORY is provided, use it to give personalized advice. "
            "Mention if a setup type is historically weak for this trader. "
            "If LESSONS FROM PAST TRADES are provided and relevant, surface them naturally — "
            "e.g. 'heads up, you've lost on this ticker before' or 'your VCPs have been working well lately'. "
            "Don't lecture — one line is enough.\n\n"
            "Never make up prices or numbers — only use what's in the data. "
            "You remember what was said earlier in the conversation.\n\n"
            + full_context
        ),
    }

    # build messages: system + recent history + current message
    messages = [system_msg] + list(_CONV_HISTORY) + [{"role": "user", "content": query}]

    try:
        client = Groq(api_key=GROQ_KEY)
        resp   = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=500,
            messages=messages,
        )
        reply = resp.choices[0].message.content.strip()
        # convert any leftover markdown bold to HTML (Telegram uses parse_mode=HTML)
        reply = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', reply)
        reply = re.sub(r'\*(.+?)\*',     r'<i>\1</i>', reply)
        return reply
    except Exception as e:
        log.error(f"Groq failed: {e}")
        return "something went wrong — try again"


# ── main query router ─────────────────────────────────────────────────────────

def process_query(text: str) -> str:
    t  = text.strip()
    tl = t.lower().strip()

    # ── 1. Pending trade YES / NO / adjustment ────────────────────────────────
    from trade_queue import get_pending, clear_pending, update_pending
    pending = get_pending()
    if pending:
        state = pending.get("state", "confirm")

        if state == "confirm":
            if tl in ("yes", "y", "yep", "yeah", "confirm", "execute", "do it", "go", "ok"):
                return _execute_pending_trade(pending)

            if tl in ("no", "n", "nope", "cancel", "nevermind", "never mind", "stop", "dismiss"):
                update_pending(state="adjust")
                return (
                    f"Got it. How many shares instead?\n"
                    f"Reply with a number (e.g. <b>10</b>) or <b>cancel</b> to dismiss."
                )

        elif state == "adjust":
            if tl in ("cancel", "dismiss", "never mind", "stop", "nope", "no"):
                clear_pending()
                return "Trade cancelled."

            qty_match = re.search(r'\b(\d+)\b', t)
            if qty_match:
                new_qty = int(qty_match.group(1))
                update_pending(qty=new_qty, state="confirm")
                updated = get_pending()
                return _build_confirm_msg(updated)

            return (
                "Didn't catch that. Reply with a number of shares (e.g. <b>15</b>) "
                "or <b>cancel</b>."
            )

    # ── 2. Help ───────────────────────────────────────────────────────────────
    if any(w in tl for w in ("help", "commands", "what can you")):
        return _help_reply()

    # ── 3. Test alert ─────────────────────────────────────────────────────────
    if tl in ("test", "ping", "check"):
        from alerts import test_alert
        test_alert()
        return "sent a test message"

    # ── 4. Add/remove watchlist ───────────────────────────────────────────────
    m = re.search(r'\badd\s+([A-Za-z]{1,5})\b', t, re.IGNORECASE)
    if m:
        return _add_ticker(m.group(1))

    m = re.search(r'\b(?:remove|delete)\s+([A-Za-z]{1,5})\b', t, re.IGNORECASE)
    if m:
        return _remove_ticker(m.group(1))

    # ── 5. Briefing ───────────────────────────────────────────────────────────
    if any(w in tl for w in ("brief", "morning", "send briefing")):
        try:
            from morning_briefing import send_morning_briefing
            ok = send_morning_briefing(force=True)
        except Exception:
            ok = False
        return "sent the morning briefing" if ok else "couldn't send it — check logs"

    # ── 6. Manual trade commands: "buy 10 NVDA" / "sell PLUG" ────────────────
    _trade_re = re.compile(
        r'\b(buy|sell|long|exit|close)\b.{0,20}\b[A-Z]{2,5}\b',
        re.IGNORECASE,
    )
    if _trade_re.search(t):
        result = _handle_manual_trade(t)
        if result:
            return result

    # ── 7. Everything else → Groq ─────────────────────────────────────────────
    return _groq_reply(t)


# ── bot runner ────────────────────────────────────────────────────────────────

def run_bot():
    """Start the Telegram bot polling. Called from monitor_daemon.py."""
    if not TOKEN or not OWNER_CHAT_ID:
        log.warning("Telegram token or chat ID missing — bot disabled")
        return

    try:
        from telegram import Update
        from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
        import asyncio
    except ImportError:
        log.error("python-telegram-bot not installed — bot disabled")
        return

    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        global _CONV_HISTORY
        if str(update.effective_chat.id) != OWNER_CHAT_ID:
            return
        user_text = (update.message.text or "").strip()
        if not user_text:
            return
        log.info(f"Bot received: {user_text!r}")
        try:
            reply = process_query(user_text)
        except Exception as e:
            log.error(f"Bot query error: {e}", exc_info=True)
            reply = f"something went wrong: {e}"

        # record to conversation memory (skip side-effect-only replies)
        _CONV_HISTORY.append({"role": "user",      "content": user_text})
        _CONV_HISTORY.append({"role": "assistant",  "content": reply})
        # keep only the last MAX_HISTORY messages
        if len(_CONV_HISTORY) > MAX_HISTORY:
            _CONV_HISTORY = _CONV_HISTORY[-MAX_HISTORY:]

        await update.message.reply_text(reply, parse_mode="HTML")

    async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # /start <token> — link ANY user's Telegram to their app account (per-user alerts)
        args = context.args or []
        token = args[0].strip() if args else ""
        chat_id = str(update.effective_chat.id)
        if token:
            try:
                import telegram_connect, auth
                user = telegram_connect.resolve_pending(token)
                if user:
                    auth.set_telegram(user, chat_id)
                    log.info("Telegram linked: %s -> chat %s", user, chat_id)
                    await update.message.reply_text(
                        "✅ <b>Connected!</b> Your StockPal alerts will come to this chat.",
                        parse_mode="HTML")
                    return
            except Exception as e:
                log.warning("start-connect failed: %s", e)
        await update.message.reply_text(
            "👋 Welcome to StockPal. Open the app → <b>🔔 Telegram Alerts</b> and tap the "
            "connect link there to link your account.", parse_mode="HTML")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app  = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Telegram bot polling started")
    app.run_polling(stop_signals=None, close_loop=False, drop_pending_updates=True)
