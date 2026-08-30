"""
Buy / sell signal engine — Veteran's Edge
All thresholds driven by strategy_config.json.
All output in plain English.
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional
from market_data import compute_indicators, ema200_slope, get_bars, get_current_price
from datetime import date

# ── Strategy config (shared, mtime-cached — see config.py) ────────────────────
from config import get_section as _e


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class BuySignal:
    ticker:        str
    price:         float
    stop:          float
    target1:       float
    target2:       float
    stop_pct:      float
    gain_pct:      float
    rr:            float
    stars:         int
    headline:      str
    why_buy:       str
    what_to_watch: str
    warnings:        list  = field(default_factory=list)
    indicators:      dict  = field(default_factory=dict)
    rs_rank:         float = 0.0
    trend_template:  int   = 0
    signal_type:     str   = "BUY"
    watch_buy_at:    float = 0.0
    watch_reason:    str   = ""


@dataclass
class PositionStatus:
    ticker:         str
    price:          float
    entry:          float
    pnl_pct:        float
    pnl_dollars:    float
    action:         str
    action_color:   str
    plain_reason:   str
    suggested_stop: Optional[float] = None
    days_held:      int = 0
    indicators:     dict = field(default_factory=dict)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _plain_rr(rr: float) -> str:
    if rr >= 3:
        return f"Risk a little to make a lot ({rr:.1f}:1 reward vs risk)"
    elif rr >= 2:
        return f"Good reward vs risk ({rr:.1f}:1)"
    return f"Modest reward vs risk ({rr:.1f}:1)"


_CHOPPY_REGIMES = ("BULL_VOLATILE", "BEAR_QUIET", "BEAR_VOLATILE")


def _find_support_level(df: pd.DataFrame, price: float, atr: float,
                        d: pd.Series = None) -> float:
    try:
        if d is None:
            d = df.iloc[-1]
        candidates = []
        recent_low = float(df["close"].iloc[-15:].min())
        if recent_low < price * 0.995:
            candidates.append(recent_low)
        for col in ("ema20", "ema50"):
            v = float(d.get(col, np.nan))
            if not np.isnan(v) and v < price * 0.995:
                candidates.append(v)
        return round(max(candidates), 2) if candidates else round(price - atr * 1.5, 2)
    except Exception:
        return round(price - atr * 1.5, 2)


def _entry_timing_ok(df: pd.DataFrame, regime: str, d: pd.Series = None) -> tuple:
    if regime not in _CHOPPY_REGIMES:
        return True, ""
    try:
        if d is None:
            d = df.iloc[-1]
        macd_h = float(d.get("macd_hist", np.nan))
        rsi    = float(d.get("rsi",       np.nan))
        adx    = float(d.get("adx",       np.nan))
        reasons, macd_bad = [], False
        if not np.isnan(macd_h) and macd_h < 0:
            reasons.append("short-term momentum is fading (MACD below zero)")
            macd_bad = True
        if not np.isnan(adx) and adx < 22:
            reasons.append(f"trend conviction is low (ADX {adx:.0f}) — price is chopping")
        if not np.isnan(rsi) and 45 < rsi < 55:
            reasons.append(f"RSI {rsi:.0f} is mid-range — no clear momentum edge yet")
        if macd_bad or len(reasons) >= 2:
            return False, " and ".join(reasons)
    except Exception:
        pass
    return True, ""


# ── ENTRY SIGNAL ENGINE ───────────────────────────────────────────────────────

def score_entry(ticker: str, regime: dict, _df=None) -> Optional[BuySignal]:
    """
    Score a ticker for a swing trade entry.
    Returns BuySignal if criteria are met, None otherwise.
    _df: pre-loaded & indicator-enriched DataFrame (from batch download).
    """
    sc = _e("swing.entry.scoring",      {})
    hg = _e("swing.entry.hard_gates",   {})
    st = _e("swing.entry.stops_targets", {})

    if _df is not None:
        df = _df
    else:
        df = get_bars(ticker, "1y", "1d")
        if df is None:
            return None
        df = compute_indicators(df)

    if len(df) < int(hg.get("min_bars", 60)):
        return None

    d         = df.iloc[-1]
    price     = float(d["close"])
    rsi       = float(d.get("rsi",       np.nan))
    adx       = float(d.get("adx",       np.nan))
    vol_ratio = float(d.get("vol_ratio", np.nan))
    ema20     = float(d.get("ema20",     np.nan))
    ema50     = float(d.get("ema50",     np.nan))
    ema200    = float(d.get("ema200",    np.nan))
    atr       = float(d.get("atr",       np.nan))
    macd_h    = float(d.get("macd_hist", np.nan))
    macd_now  = float(d.get("macd",      np.nan))
    macd_sig  = float(d.get("macd_sig",  np.nan))
    slope200  = ema200_slope(df)

    if np.isnan(atr) or atr == 0 or np.isnan(price):
        return None

    # ── Hard gate: volume ──────────────────────────────────────────────────────
    vol_min = float(hg.get("volume_ratio_min", 1.3))
    if not np.isnan(vol_ratio) and vol_ratio < vol_min:
        return None

    score     = 0
    positives = []
    warnings  = []

    # 1. EMA alignment
    if not np.isnan(ema20) and not np.isnan(ema50) and not np.isnan(ema200):
        if price > ema20 > ema50 > ema200:
            score += int(sc.get("ema_full_stack", 25))
            positives.append("trending strongly upward on all timeframes")
        elif price > ema20 > ema50:
            score += int(sc.get("ema_partial_stack", 12))
            positives.append("trending upward short and medium term")
        elif price < ema200:
            warnings.append("Price is below its long-term average — riskier trade fighting the main trend")
            score += int(sc.get("ema_below_200", -10))

    # 2. 200 EMA slope
    if slope200 == "positive":
        score += int(sc.get("ema200_slope_positive", 15))
        positives.append("the long-term trend is still pointing up")
    elif slope200 == "negative":
        warnings.append("The long-term trend is pointing DOWN — use half your normal position size")
        score += int(sc.get("ema200_slope_negative", -10))

    # 3. RSI
    rsi_lo = float(sc.get("rsi_healthy_low",  40))
    rsi_hi = float(sc.get("rsi_healthy_high", 65))
    rsi_ob = float(sc.get("rsi_overbought_threshold", 70))
    rsi_os = float(sc.get("rsi_oversold_threshold",   35))
    if not np.isnan(rsi):
        if rsi_lo <= rsi <= rsi_hi:
            score += int(sc.get("rsi_healthy_score", 15))
            positives.append(f"momentum is healthy (RSI {rsi:.0f}) with room to run higher")
        elif rsi > rsi_ob:
            warnings.append(f"Already overbought (RSI {rsi:.0f}) — buying late, higher reversal risk")
            score += int(sc.get("rsi_overbought_score", -15))
        elif rsi < rsi_os:
            warnings.append(f"Oversold (RSI {rsi:.0f}) — could keep falling before recovering")
            score += int(sc.get("rsi_oversold_score", -5))

    # 4. ADX
    adx_s = float(sc.get("adx_strong_threshold",   30))
    adx_m = float(sc.get("adx_moderate_threshold", 25))
    if not np.isnan(adx):
        if adx >= adx_s:
            score += int(sc.get("adx_strong_score", 15))
            positives.append(f"moving with strong conviction (ADX {adx:.0f}), not a choppy setup")
        elif adx >= adx_m:
            score += int(sc.get("adx_moderate_score", 8))
            positives.append(f"showing a clear directional trend (ADX {adx:.0f})")
        else:
            warnings.append(f"Trend is weak (ADX {adx:.0f}) — may chop sideways instead of trending")

    # 5. Volume
    vol_h = float(sc.get("volume_high_threshold",     1.5))
    vol_m = float(sc.get("volume_moderate_threshold", 1.2))
    if not np.isnan(vol_ratio):
        if vol_ratio >= vol_h:
            score += int(sc.get("volume_high_score", 15))
            positives.append(f"big money is participating — volume is {vol_ratio:.1f}x above average")
        elif vol_ratio >= vol_m:
            score += int(sc.get("volume_moderate_score", 7))
            positives.append(f"above-average volume ({vol_ratio:.1f}x) showing real interest")

    # 6. MACD
    if not np.isnan(macd_h):
        if macd_h > 0:
            score += int(sc.get("macd_histogram_positive", 10))
            positives.append("momentum is accelerating in the right direction")
        else:
            warnings.append("Short-term momentum is fading — entry timing is not ideal")

    if len(df) >= 2:
        prev_m = float(df.iloc[-2].get("macd",    np.nan))
        prev_s = float(df.iloc[-2].get("macd_sig", np.nan))
        if not np.isnan(prev_m) and not np.isnan(macd_now) and not np.isnan(macd_sig):
            if prev_m < prev_s and macd_now >= macd_sig:
                score += int(sc.get("macd_fresh_cross", 10))
                positives.append("just fired a fresh buy signal on the momentum indicator")

    # 7. Regime
    r = regime.get("regime", "BULL_QUIET")
    if r == "BULL_QUIET":
        score += int(sc.get("regime_bull_quiet_bonus", 5))
        positives.append("market conditions are ideal — calm and bullish")
    elif r == "BULL_VOLATILE":
        warnings.append("Market is choppy — use a tighter stop loss than normal")
    elif r in ("BEAR_QUIET", "BEAR_VOLATILE"):
        score += int(sc.get("regime_bear_penalty", -15))
        warnings.append(f"Downtrending market ({r}) — only the highest-conviction setups, half size")
    elif r == "CRISIS":
        return None

    min_score = int(hg.get("min_score", 50))
    if score < min_score:
        return None

    # ── Fundamentals overlay ──────────────────────────────────────────────────
    snap = analyst = earnings = news = insider = {}
    try:
        from fundamentals import get_fundamental_snapshot
        snap    = get_fundamental_snapshot(ticker)
        fscore  = snap.get("fscore", 0)
        analyst = snap.get("analyst",   {})
        earnings= snap.get("earnings",  {})
        news    = snap.get("news",      {})
        insider = snap.get("insider",   {})

        score += fscore * float(sc.get("fscore_multiplier", 1.5))

        if analyst.get("recommendation") in ("strong_buy", "buy"):
            tgt = analyst.get("target_mean")
            positives.append(
                f"Wall St rates it a {analyst['recommendation'].replace('_',' ')}"
                + (f" with avg target ${tgt:.2f}" if tgt else "")
            )
        if news.get("label") == "positive":
            positives.append(f"recent news is positive ({news.get('article_count',0)} articles this week)")
        if insider.get("signal") == "buying":
            positives.append("company insiders are buying their own stock")

        dte = earnings.get("days_until", 999)
        if 0 < dte <= 7:
            warnings.append(f"⚠️ EARNINGS IN {dte} DAYS — binary event, use half size or wait")
        if analyst.get("recommendation") in ("sell", "strong_sell"):
            warnings.append("Wall St consensus is SELL — trading against institutional opinion")
        if news.get("label") == "negative":
            warnings.append("Recent news is negative — sentiment headwind on this trade")
        if insider.get("signal") == "selling":
            warnings.append("Insiders have been net sellers recently — stay alert")
    except Exception:
        snap = analyst = earnings = news = insider = {}

    # ── Stops & targets ───────────────────────────────────────────────────────
    stop_mult = float(st.get("stop_atr_mult_bull_quiet", 2.0)) if r == "BULL_QUIET" \
                else float(st.get("stop_atr_mult_other", 1.5))
    stop    = round(price - atr * stop_mult, 2)
    target1 = round(price + atr * float(st.get("target1_atr_mult", 3.0)), 2)
    atr_t2  = round(price + atr * float(st.get("target2_atr_mult", 6.0)), 2)
    a_tgt   = analyst.get("target_mean") if analyst else None
    target2 = round(min(a_tgt, atr_t2 * 1.2), 2) if a_tgt and a_tgt > price * 1.05 else atr_t2

    risk   = price - stop
    reward = target1 - price
    rr     = round(reward / risk, 1) if risk > 0 else 0

    if rr < float(hg.get("min_rr", 1.5)):
        return None

    stop_pct = round(risk   / price * 100, 1)
    gain_pct = round(reward / price * 100, 1)

    thr3 = int(_e("swing.entry.stars_thresholds.three_star", 75))
    thr2 = int(_e("swing.entry.stars_thresholds.two_star",   60))
    stars = 3 if score >= thr3 else 2 if score >= thr2 else 1

    # ── Narrative ─────────────────────────────────────────────────────────────
    star_str = "⭐" * stars
    if stars == 3:
        headline = f"{star_str}  Strong buy — technicals and fundamentals aligned"
    elif stars == 2:
        headline = f"{star_str}  Good setup — solid but not perfect"
    else:
        headline = f"{star_str}  Developing setup — smaller position recommended"

    try:
        from market_data import get_company_info
        _info       = get_company_info(ticker)
        _name       = _info.get("name") or ticker.upper()
        _short_name = (_name.replace(" Corporation","").replace(" Inc.","").replace(" Inc","")
                            .replace(" Corp.","").replace(" Corp","").replace(" Ltd.","")
                            .replace(" Ltd","").replace(", LLC","").strip())
    except Exception:
        _short_name = ticker.upper()

    _fins    = snap.get("financials", {}) if snap else {}
    _rg      = _fins.get("revenue_growth_yoy")
    _pm      = _fins.get("profit_margin")
    _surp    = earnings.get("last_surprise") if earnings else None
    _tgt     = analyst.get("target_mean")    if analyst  else None
    _nana    = analyst.get("num_analysts", 0) if analyst  else 0
    _rec     = analyst.get("recommendation", "") if analyst else ""
    _ins_net = insider.get("net_shares", 0) if insider else 0
    dte      = earnings.get("days_until", 999) if earnings else 999

    _lead_parts = []
    if _tgt and _nana >= 3 and _tgt > price:
        upside = (_tgt - price) / price * 100
        if _rec in ("strong_buy", "buy"):
            _lead_parts.append((3, f"{_nana} Wall St analysts rate {_short_name} a {_rec.replace('_',' ')} with an average target of ${_tgt:.2f} — {upside:.0f}% upside."))
        elif upside >= 15:
            _lead_parts.append((2, f"Analysts have a consensus target of ${_tgt:.2f} on {_short_name} ({upside:.0f}% above current price)."))
    if _rg is not None and _rg >= 15:
        _lead_parts.append((2, f"{_short_name} is growing revenue at {_rg:.0f}% year-over-year" + (f" with a {_pm*100:.0f}% profit margin" if _pm and _pm > 0.1 else "") + " — fundamentals back the chart."))
    if _surp is not None and _surp >= 8:
        _lead_parts.append((2, f"{_short_name} beat earnings estimates by {_surp:.0f}% last quarter."))
    if insider.get("signal") == "buying" and abs(_ins_net) > 5000:
        _lead_parts.append((2, f"Company insiders net bought {abs(_ins_net):,.0f} shares of {_short_name} recently — management is putting their own money in."))
    if not _lead_parts:
        if not np.isnan(ema20) and not np.isnan(ema50) and not np.isnan(ema200):
            if price > ema20 > ema50 > ema200:
                _lead_parts.append((1, f"{_short_name} is in a confirmed uptrend on every timeframe — daily, weekly, and long-term moving averages all stacked bullishly."))
            elif price > ema20 > ema50:
                _lead_parts.append((1, f"{_short_name} is trending up short and medium term with price above both 20- and 50-day averages."))
    _lead_parts.sort(key=lambda x: x[0], reverse=True)
    _lead = _lead_parts[0][1] if _lead_parts else f"{_short_name} meets the entry criteria."

    _tech = []
    if not np.isnan(adx):
        if adx >= 35: _tech.append((3, f"Trend strength is very high (ADX {adx:.0f}) — strong directional move, not chop."))
        elif adx >= 28: _tech.append((2, f"The trend has real conviction (ADX {adx:.0f}), price moving with purpose."))
    if not np.isnan(vol_ratio) and vol_ratio >= 1.5:
        _tech.append((3, f"Volume is {vol_ratio:.1f}x above average — institutions actively participating."))
    elif not np.isnan(vol_ratio) and vol_ratio >= 1.2:
        _tech.append((2, f"Above-average volume ({vol_ratio:.1f}x) confirms real buying interest."))
    if len(df) >= 2:
        pm = float(df.iloc[-2].get("macd", np.nan))
        ps = float(df.iloc[-2].get("macd_sig", np.nan))
        if not np.isnan(pm) and not np.isnan(macd_now) and not np.isnan(macd_sig):
            if pm < ps and macd_now >= macd_sig:
                _tech.append((4, "The MACD just crossed bullish — a fresh momentum buy signal."))
    if not np.isnan(rsi) and 45 <= rsi <= 62:
        _tech.append((2, f"RSI {rsi:.0f} — healthy momentum with room to run before overbought."))
    elif not np.isnan(rsi) and 62 < rsi <= 68:
        _tech.append((1, f"RSI {rsi:.0f} — strong momentum, watch for first signs of pullback."))
    if not np.isnan(macd_h) and macd_h > 0 and not any("MACD just crossed" in p[1] for p in _tech):
        _tech.append((1, "Short-term momentum is positive (MACD histogram above zero)."))
    _tech.sort(key=lambda x: x[0], reverse=True)

    if not np.isnan(adx) and adx >= 30 and not np.isnan(rsi) and rsi < 65:
        _risk = f"{_plain_rr(rr)}. Your stop is ${stop:.2f} — only {stop_pct}% below entry."
    else:
        _risk = f"Stop is {stop_pct}% below entry at ${stop:.2f}. {_plain_rr(rr)}."

    why = " ".join([_lead] + [p[1] for p in _tech[:2]] + [_risk])

    if 0 < dte <= 21:
        what = (f"⚠️ Earnings in {dte} days — decide if you'll hold through or exit before. "
                f"Exit if price drops below ${stop:.2f}. First target ${target1:.2f}.")
    else:
        what = (f"Exit immediately if price drops below ${stop:.2f}. "
                f"First profit target ${target1:.2f} — sell half there, trail stop on the rest.")

    ind = {
        "RSI":            round(rsi, 1)       if not np.isnan(rsi)       else "—",
        "Trend Strength": round(adx, 1)       if not np.isnan(adx)       else "—",
        "Volume":         f"{vol_ratio:.1f}x" if not np.isnan(vol_ratio) else "—",
        "200 EMA Trend":  slope200.upper(),
        "Daily ATR":      f"${atr:.2f}",
        "Analyst Target": (f"${_tgt:.2f} ({_nana} analysts)" if _tgt else "—"),
        "Wall St Rating": (_rec.replace("_"," ").title() if _rec else "—"),
        "News Sentiment": (news.get("label","—").title() if news else "—"),
        "Earnings":       (f"in {dte}d" if 0 < dte <= 60 else "—"),
    }

    _timing_ok, _timing_reason = _entry_timing_ok(df, r, d)
    _watch_kwargs: dict = {}
    if not _timing_ok:
        _wba = _find_support_level(df, price, atr, d)
        headline = f"👁️ Watch — good setup, wait for pullback to ${_wba:.2f}"
        _watch_kwargs = dict(
            signal_type  = "WATCH",
            watch_buy_at = _wba,
            watch_reason = (
                f"Market is {r.replace('_',' ').lower()} and {_timing_reason}. "
                f"The setup is valid but timing improves on a pullback. "
                f"Consider entering when price reaches ${_wba:.2f} (nearest support). "
                f"Set a price alert at ${_wba:.2f} and re-evaluate then."
            ),
        )

    return BuySignal(
        ticker=ticker, price=price, stop=stop,
        target1=target1, target2=target2,
        stop_pct=stop_pct, gain_pct=gain_pct,
        rr=rr, stars=stars, headline=headline,
        why_buy=why, what_to_watch=what,
        warnings=warnings, indicators=ind,
        **_watch_kwargs,
    )


# ── EXIT MANAGEMENT ENGINE ────────────────────────────────────────────────────

def check_position(ticker: str, entry: float, stop: float,
                   target1: float, date_in: str, qty: int) -> Optional[PositionStatus]:
    ex  = _e("swing.exit", {})
    wc  = ex.get("watch_closely",  {})
    ts  = ex.get("trailing_stops", {})
    tp  = ex.get("take_profit",    {})
    st  = ex.get("stale_trade",    {})
    ob  = ex.get("overbought",     {})
    tst = ex.get("tighten_stop",   {})

    price = get_current_price(ticker)
    if price is None:
        return None

    df = get_bars(ticker, "6mo", "1d")
    if df is None or len(df) < 20:
        return None

    df   = compute_indicators(df)
    d    = df.iloc[-1]
    rsi  = float(d.get("rsi",       np.nan))
    macd_h = float(d.get("macd_hist", np.nan))
    atr    = float(d.get("atr",       np.nan))
    ema20  = float(d.get("ema20",     np.nan))

    pnl_pct     = round((price - entry) / entry * 100, 2)
    pnl_dollars = round((price - entry) * qty, 2)

    try:
        days = (date.today() - date.fromisoformat(date_in)).days
    except Exception:
        days = 0

    risk_per_share = entry - stop
    r_multiple     = (price - entry) / risk_per_share if risk_per_share > 0 else 0

    ind = {
        "RSI":             round(rsi, 1)    if not np.isnan(rsi)    else "—",
        "Momentum (MACD)": "↑ Positive"    if (not np.isnan(macd_h) and macd_h > 0) else "↓ Fading",
        "Daily ATR":       f"${atr:.2f}"   if not np.isnan(atr)    else "—",
        "R Multiple":      f"{r_multiple:+.1f}R",
        "Days Held":       days,
    }

    # ── EXIT — stop hit ───────────────────────────────────────────────────────
    if price <= stop:
        return PositionStatus(
            ticker, price, entry, pnl_pct, pnl_dollars,
            "EXIT NOW", "#FF3333",
            f"Stop hit. Price fell to ${price:.2f}, below your stop of ${stop:.2f}. "
            f"Exit now — this is what stops are for. P&L: {pnl_pct:+.2f}% (${pnl_dollars:+.2f}).",
            days_held=days, indicators=ind
        )

    # ── EXIT — stale trade (O'Neil 2-week rule) ────────────────────────────────
    stale_days = int(st.get("days", 14))
    stale_move = float(st.get("min_move_pct", 1.0))
    if days >= stale_days and abs(pnl_pct) < stale_move:
        return PositionStatus(
            ticker, price, entry, pnl_pct, pnl_dollars,
            "EXIT NOW", "#FF3333",
            f"Held {ticker} for {days} days and it's gone nowhere ({pnl_pct:+.2f}%). "
            f"Dead money — exit and redeploy into a working setup.",
            days_held=days, indicators=ind
        )

    # ── EXIT — overbought + momentum rolling over ─────────────────────────────
    rsi_ob = float(ob.get("rsi_threshold", 72))
    if not np.isnan(rsi) and rsi > rsi_ob and len(df) >= 2:
        prev_h = float(df.iloc[-2].get("macd_hist", macd_h))
        if not np.isnan(macd_h) and macd_h < prev_h:
            return PositionStatus(
                ticker, price, entry, pnl_pct, pnl_dollars,
                "EXIT NOW", "#FF3333",
                f"{ticker} is overbought (RSI {rsi:.0f}) and momentum is rolling over. "
                f"You're up {pnl_pct:+.2f}% (${pnl_dollars:+.2f}) — lock it in before it reverses. "
                f"The easy money has been made.",
                days_held=days, indicators=ind
            )

    # ── TAKE PROFIT — T1 hit (unconditional partial exit — Minervini 2R rule) ──
    if price >= target1:
        partial = int(tp.get("partial_sell_pct", 50))
        # Calculate break-even stop for the remaining shares
        be_stop = round(entry * 1.005, 2)
        if not np.isnan(macd_h) and len(df) >= 2:
            prev_h = float(df.iloc[-2].get("macd_hist", macd_h))
            if macd_h < prev_h:
                # Momentum fading at target — exit more aggressively
                return PositionStatus(
                    ticker, price, entry, pnl_pct, pnl_dollars,
                    "TAKE PROFIT", "#FF8C00",
                    f"🎯 {ticker} hit your first target ${target1:.2f}! "
                    f"You're up {pnl_pct:+.2f}% (${pnl_dollars:+.2f}). "
                    f"Sell {partial}% now — momentum is fading, don't give it back. "
                    f"Move your stop to ${be_stop:.2f} (break-even) on the rest.",
                    suggested_stop=be_stop, days_held=days, indicators=ind
                )
        return PositionStatus(
            ticker, price, entry, pnl_pct, pnl_dollars,
            "TAKE PROFIT", "#FF8C00",
            f"🎯 {ticker} hit your first target ${target1:.2f}! "
            f"Up {pnl_pct:+.2f}% (${pnl_dollars:+.2f}). "
            f"Take {partial}% off the table now. Move stop to break-even ${be_stop:.2f} on the rest "
            f"— the remaining position is now risk-free.",
            suggested_stop=be_stop, days_held=days, indicators=ind
        )

    # ── TIGHTEN STOP — RSI getting stretched ─────────────────────────────────
    rsi_ts = float(tst.get("rsi_threshold", 65))
    if not np.isnan(rsi) and rsi_ts < rsi <= rsi_ob:
        new_stop = round(price - atr * float(tst.get("atr_mult", 1.5)), 2) if not np.isnan(atr) else None
        if new_stop and new_stop > stop:
            return PositionStatus(
                ticker, price, entry, pnl_pct, pnl_dollars,
                "TIGHTEN STOP", "#FFA500",
                f"{ticker} is getting stretched (RSI {rsi:.0f}). Up {pnl_pct:+.2f}% (${pnl_dollars:+.2f}). "
                f"Tighten your stop to ${new_stop:.2f} to protect gains in case it pulls back.",
                suggested_stop=new_stop, days_held=days, indicators=ind
            )

    # ── WATCH CLOSELY — approaching stop ─────────────────────────────────────
    stop_dist = (price - stop) / price * 100
    if stop_dist < float(wc.get("stop_dist_pct", 2.0)):
        return PositionStatus(
            ticker, price, entry, pnl_pct, pnl_dollars,
            "WATCH CLOSELY", "#FFA500",
            f"{ticker} is within {stop_dist:.1f}% of your stop. "
            f"If it drops below ${stop:.2f} — exit immediately, no hesitation.",
            days_held=days, indicators=ind
        )

    # ── WATCH CLOSELY — MACD momentum collapse ────────────────────────────────
    if pnl_pct <= 2 and not np.isnan(macd_h) and len(df) >= 2:
        prev_h = float(df.iloc[-2].get("macd_hist", macd_h))
        if prev_h > 0 and macd_h > 0 and prev_h != 0:
            fade = (prev_h - macd_h) / abs(prev_h) * 100
            if fade > float(wc.get("macd_fade_pct", 30)):
                return PositionStatus(
                    ticker, price, entry, pnl_pct, pnl_dollars,
                    "WATCH CLOSELY", "#FFA500",
                    f"⚠️ {ticker} is {pnl_pct:+.2f}% and momentum dropped {fade:.0f}% in one day. "
                    f"Trade is not developing as expected. Consider exiting before stop ${stop:.2f} is hit.",
                    days_held=days, indicators=ind
                )

    # ── WATCH CLOSELY — <1 ATR cushion to stop ───────────────────────────────
    if pnl_pct < 0 and not np.isnan(atr) and atr > 0:
        cushion = (price - stop) / atr
        if cushion < float(wc.get("atr_cushion_min", 1.0)):
            return PositionStatus(
                ticker, price, entry, pnl_pct, pnl_dollars,
                "WATCH CLOSELY", "#FFA500",
                f"⚠️ {ticker} is {pnl_pct:+.2f}% with only {cushion:.1f} ATR of cushion to stop ${stop:.2f}. "
                f"One volatile day stops you out. Tighten stop or reduce size.",
                days_held=days, indicators=ind
            )

    # ── RAISE STOP — progressive trailing (Minervini method) ──────────────────
    # Phase 1: move to break-even once up 1R
    be_r = float(ts.get("breakeven_after_r", 1.0))
    if r_multiple >= be_r and stop < entry:
        be_stop = round(entry * 1.005, 2)
        return PositionStatus(
            ticker, price, entry, pnl_pct, pnl_dollars,
            "RAISE STOP", "#00BFFF",
            f"✅ {ticker} is up {r_multiple:.1f}R ({pnl_pct:+.2f}%, ${pnl_dollars:+.2f}). "
            f"Move your stop to break-even at ${be_stop:.2f} — this trade is now risk-free.",
            suggested_stop=be_stop, days_held=days, indicators=ind
        )

    # Phase 2: trail to EMA21 once up 4%+
    trail_pct = float(ts.get("trail_ema21_after_pct", 4.0))
    if pnl_pct > trail_pct and not np.isnan(ema20):
        trail = round(max(stop, ema20 * float(ts.get("ema21_trail_buffer", 0.985))), 2)
        if trail > stop:
            return PositionStatus(
                ticker, price, entry, pnl_pct, pnl_dollars,
                "RAISE STOP", "#00BFFF",
                f"✅ {ticker} is up {pnl_pct:+.2f}% (${pnl_dollars:+.2f}). "
                f"Trail your stop to ${trail:.2f} (just below the 20-day EMA) to lock in progress.",
                suggested_stop=trail, days_held=days, indicators=ind
            )

    # ── HOLD ──────────────────────────────────────────────────────────────────
    days_str = f"{days} day{'s' if days != 1 else ''}"
    if pnl_pct >= 0:
        msg   = (f"✅ {ticker} is up {pnl_pct:+.2f}% (${pnl_dollars:+.2f}) in {days_str}. "
                 f"Trend intact — hold and let it develop. Stop at ${stop:.2f}.")
        color = "#00CC66"
    elif pnl_pct >= -3.0:
        msg   = (f"🟡 {ticker} is slightly underwater — {pnl_pct:.2f}% (${pnl_dollars:+.2f}) in {days_str}. "
                 f"Still within normal pullback range, above stop ${stop:.2f}. Watch closely.")
        color = "#FFA500"
    else:
        msg   = (f"⚠️ {ticker} is down {pnl_pct:.2f}% (${pnl_dollars:+.2f}) in {days_str}. "
                 f"Above stop ${stop:.2f} but not performing. Consider tightening stop or reducing size.")
        color = "#FF8C00"

    return PositionStatus(
        ticker, price, entry, pnl_pct, pnl_dollars,
        "HOLD", color, msg, days_held=days, indicators=ind
    )
