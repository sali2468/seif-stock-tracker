"""
chart_analysis.py — Rule-based live stock analysis
Uses the exact same indicator data as the scanner (RSI, MACD, ADX, ATR,
EMAs, volume) to produce a plain-English BUY / HOLD / SELL / WATCH verdict.
Zero API calls — runs instantly off the chart data.
"""

import numpy as np
from typing import Optional


def analyze_ticker(ticker: str, position: Optional[dict] = None) -> dict:
    """
    Full technical analysis for a ticker.

    Parameters
    ----------
    ticker   : stock symbol
    position : optional dict with keys entry, stop, target1, target2, qty, days_held

    Returns
    -------
    dict with keys:
        action, confidence, headline, reasoning, risks, catalysts,
        suggested_stop, outlook, score
    OR  {"error": ..., "message": ...} on failure.
    """
    try:
        from market_data import get_bars, compute_indicators, ema200_slope
    except ImportError:
        return {"error": "import", "message": "market_data module not found."}

    try:
        df = get_bars(ticker, "6mo", "1d")
        if df is None or len(df) < 20:
            return {"error": "no_data", "message": f"Not enough chart data for {ticker}."}

        df   = compute_indicators(df)
        d    = df.iloc[-1]
        prev = df.iloc[-2]

        # ── Raw values ────────────────────────────────────────────────────────
        price   = float(d["close"])
        prev_c  = float(prev["close"])
        rsi     = float(d.get("rsi",       50))
        adx     = float(d.get("adx",       20))
        atr     = float(d.get("atr",        0))
        macd_h  = float(d.get("macd_hist",  0))
        pmh     = float(prev.get("macd_hist", 0))
        ema20   = float(d.get("ema20",  price))
        ema50   = float(d.get("ema50",  price))
        _e200   = d.get("ema200", np.nan)
        ema200  = float(_e200) if not np.isnan(float(_e200 if _e200 is not None else np.nan)) else None
        vr      = float(d.get("vol_ratio", 1.0))
        slope   = ema200_slope(df)

        macd_5d      = [round(float(x), 4) for x in df["macd_hist"].tail(5).tolist()]
        macd_dir     = "building" if macd_h > pmh else "fading"
        macd_fade_pct = round((pmh - macd_h) / abs(pmh) * 100, 1) if pmh != 0 else 0

        day_chg   = (price - prev_c) / prev_c * 100
        atr_pct   = atr / price * 100 if price else 0
        low_5d    = float(df["low"].tail(5).min())
        low_10d   = float(df["low"].tail(10).min())
        high_20d  = float(df["high"].tail(20).max())
        today_open = float(d.get("open", price))
        gap_pct   = (today_open - prev_c) / prev_c * 100

        above_20  = price > ema20
        above_50  = price > ema50
        above_200 = (price > ema200) if ema200 else None

        # Position maths
        pnl_pct       = None
        cushion_atr   = None
        cushion_pct   = None
        if position:
            entry = position.get("entry", price)
            stop  = position.get("stop",  price * 0.95)
            pnl_pct     = (price - entry) / entry * 100
            cushion_pct = (price - stop) / price * 100
            cushion_atr = (price - stop) / atr if atr else 0

        # ── SCORING ───────────────────────────────────────────────────────────
        # +/- points build up; final score → action + confidence
        score    = 0
        catalysts = []
        risks     = []
        notes     = []   # neutral observations for reasoning

        # — EMA structure (trend direction) ——————————————————————————————————
        if above_20 and above_50:
            score += 2
            catalysts.append(
                f"Price ${price:.2f} above EMA20 (${ema20:.2f}) and EMA50 (${ema50:.2f}) — "
                f"bullish short & medium-term structure"
            )
        elif above_20 and not above_50:
            score += 0
            notes.append(
                f"Price above EMA20 (${ema20:.2f}) but below EMA50 (${ema50:.2f}) — "
                f"mixed structure, trend recovering but not confirmed"
            )
        else:
            score -= 2
            risks.append(
                f"Price ${price:.2f} below EMA20 (${ema20:.2f}) and EMA50 (${ema50:.2f}) — "
                f"short-term trend is broken"
            )

        # — 200d EMA (long-term regime) ————————————————————————————————————
        if ema200:
            if above_200 and slope == "positive":
                score += 2
                catalysts.append(
                    f"Above a rising 200d EMA (${ema200:.2f}) — long-term uptrend intact"
                )
            elif above_200 and slope == "flat":
                score += 1
                notes.append(f"Above 200d EMA (${ema200:.2f}) but slope is flat — neutral long-term")
            elif above_200 and slope == "negative":
                score += 0
                risks.append(
                    f"Above 200d EMA (${ema200:.2f}) but it's declining — long-term trend weakening"
                )
            else:
                score -= 2
                risks.append(
                    f"Below 200d EMA (${ema200:.2f}) with a {slope} slope — in a long-term downtrend"
                )

        # — MACD histogram (momentum) ——————————————————————————————————————
        # Also flag rapid fades (like RKLB: -55% in 2 days)
        rapid_fade = macd_dir == "fading" and macd_h > 0 and macd_fade_pct > 30
        if macd_dir == "building" and macd_h > 0:
            score += 2
            catalysts.append(
                f"MACD histogram building ({macd_h:.4f} vs {pmh:.4f}) — "
                f"momentum is accelerating"
            )
        elif macd_dir == "building" and macd_h < 0:
            score += 1
            catalysts.append(
                f"MACD turning up from negative ({macd_h:.4f}) — "
                f"potential momentum shift, watch for cross above zero"
            )
        elif rapid_fade:
            score -= 3
            risks.append(
                f"MACD histogram dropped {macd_fade_pct:.0f}% in one day "
                f"({pmh:.4f} → {macd_h:.4f}) — sharp momentum loss, high reversal risk"
            )
        elif macd_dir == "fading" and macd_h > 0:
            score -= 1
            risks.append(
                f"MACD histogram fading ({pmh:.4f} → {macd_h:.4f}) — "
                f"bullish momentum is weakening"
            )
        else:
            score -= 1
            risks.append(
                f"MACD negative and fading ({macd_h:.4f}) — bearish momentum"
            )

        # — RSI ————————————————————————————————————————————————————————————
        if rsi > 80:
            score -= 3
            risks.append(
                f"RSI {rsi:.0f} — extremely overbought, reversal probability is high"
            )
        elif rsi > 72:
            score -= 2
            risks.append(
                f"RSI {rsi:.0f} — overbought, momentum typically stalls or reverses here"
            )
        elif rsi > 60:
            score += 1
            catalysts.append(f"RSI {rsi:.0f} — healthy momentum, not yet extended")
        elif rsi >= 40:
            score += 0
            notes.append(f"RSI {rsi:.0f} — neutral zone")
        elif rsi >= 30:
            score += 1
            catalysts.append(
                f"RSI {rsi:.0f} — recovering from oversold, potential bounce setup"
            )
        else:
            score -= 1
            risks.append(f"RSI {rsi:.0f} — deeply oversold, downtrend may be accelerating")

        # — ADX (trend strength — amplifies the direction) ————————————————
        if adx > 35:
            score += 1  # strong trend in force — rewards aligned setups
            notes.append(f"ADX {adx:.0f} — very strong trend in play, go with it")
        elif adx > 25:
            notes.append(f"ADX {adx:.0f} — solid trending conditions")
        elif adx < 15:
            score -= 1
            risks.append(
                f"ADX {adx:.0f} — trend is very weak, choppy range-bound conditions"
            )

        # — Volume ——————————————————————————————————————————————————————————
        if vr > 2.0:
            if day_chg > 0:
                score += 2
                catalysts.append(
                    f"Volume {vr:.1f}x average on an up day — strong institutional buying"
                )
            else:
                score -= 2
                risks.append(
                    f"Volume {vr:.1f}x average on a down day — heavy distribution, sellers in control"
                )
        elif vr > 1.4:
            if day_chg > 0:
                score += 1
                catalysts.append(
                    f"Above-average volume ({vr:.1f}x) confirms the upside move"
                )
            else:
                score -= 1
                risks.append(
                    f"Above-average volume ({vr:.1f}x) on a down day — distribution signal"
                )
        elif vr < 0.6:
            risks.append(f"Volume only {vr:.1f}x average — weak conviction in today's move")

        # — Gap analysis ———————————————————————————————————————————————————
        if gap_pct < -2.0:
            score -= 1
            risks.append(f"Gapped down {gap_pct:.1f}% at open — overnight selling pressure")
        elif gap_pct > 2.0 and above_20:
            score += 1
            catalysts.append(f"Gapped up {gap_pct:.1f}% at open — bullish morning strength")

        # — Proximity to recent lows (stop-hunt risk) ——————————————————————
        near_low_5d = price < low_5d * 1.015
        if near_low_5d:
            score -= 1
            risks.append(
                f"Price ${price:.2f} near the 5-day low (${low_5d:.2f}) — "
                f"testing recent support, break would be bearish"
            )

        # — Position-specific checks ———————————————————————————————————————
        if position and pnl_pct is not None:
            if cushion_atr is not None and cushion_atr < 0.8 and pnl_pct < 0:
                score -= 3
                risks.append(
                    f"Stop is only {cushion_atr:.1f} ATR away on a losing trade "
                    f"(${position['stop']:.2f}) — very little cushion, exit risk is high"
                )
            elif cushion_atr is not None and cushion_atr < 1.0:
                score -= 1
                risks.append(
                    f"Stop only {cushion_atr:.1f} ATR below price — "
                    f"a single volatile day could stop you out"
                )
            if pnl_pct > 15 and above_20 and above_50:
                score += 1
                catalysts.append(
                    f"Trade up {pnl_pct:.1f}% with EMA structure still intact — "
                    f"trend is working, let winners run"
                )

        # ── FUNDAMENTALS overlay ──────────────────────────────────────────────
        fund_text = ""
        analyst_target = None
        earnings_days  = 999
        try:
            from fundamentals import get_fundamental_snapshot, fundamental_summary_text
            snap          = get_fundamental_snapshot(ticker)
            fscore        = snap.get("fscore", 0)
            analyst_target = snap["analyst"].get("target_mean")
            earnings_days  = snap["earnings"].get("days_until", 999)

            # Blend into score: fscore is -10…+10, weight it at ~40% of a full point each
            score += round(fscore * 0.4)

            # Catalyst / risk additions
            rec = snap["analyst"].get("recommendation", "hold")
            if rec in ("strong_buy", "buy") and analyst_target:
                upside = (analyst_target - price) / price * 100
                catalysts.append(
                    f"Wall St rates it {rec.replace('_',' ')} — "
                    f"{snap['analyst']['num_analysts']} analysts, avg target "
                    f"${analyst_target:.2f} ({upside:+.1f}% upside)"
                )
            elif rec in ("sell", "strong_sell"):
                risks.append(
                    f"Wall St consensus is {rec.replace('_',' ')} — "
                    f"institutional opinion is against this trade"
                )

            if snap["news"].get("label") == "positive":
                catalysts.append(
                    f"News sentiment positive — "
                    f"{snap['news']['article_count']} bullish articles this week"
                )
            elif snap["news"].get("label") == "negative":
                risks.append(
                    f"News sentiment negative — "
                    f"{snap['news']['article_count']} bearish articles this week"
                )
                if snap["news"]["headlines"]:
                    risks.append(f"\"{snap['news']['headlines'][0][:80]}\"")

            if snap["insider"].get("signal") == "buying":
                catalysts.append(snap["insider"]["summary"])
            elif snap["insider"].get("signal") == "selling":
                risks.append(snap["insider"]["summary"])

            if 0 < earnings_days <= 7:
                risks.append(
                    f"🚨 EARNINGS IN {earnings_days} DAYS ({snap['earnings']['next_date']}) — "
                    f"binary event, price could gap either way"
                )
            elif 0 < earnings_days <= 21:
                risks.append(
                    f"Earnings in {earnings_days} days — "
                    f"decide before then whether to hold through"
                )

            fund_text = fundamental_summary_text(snap, price)
        except Exception:
            snap = {}

        # ── DECISION ──────────────────────────────────────────────────────────
        if position and pnl_pct is not None:
            # Holding a position → HOLD or SELL
            if score <= -4:
                action, confidence = "SELL", "High"
            elif score <= -2:
                action, confidence = "SELL", "Medium"
            elif score >= 3:
                action, confidence = "HOLD", "High"
            elif score >= 1:
                action, confidence = "HOLD", "Medium"
            else:
                action, confidence = "HOLD", "Low"

            # Override: deep loss + near stop always SELL
            if pnl_pct < -8 and cushion_atr is not None and cushion_atr < 1.0:
                action, confidence = "SELL", "High"
        else:
            # No position → BUY / WATCH / avoid
            if score >= 5:
                action, confidence = "BUY", "High"
            elif score >= 3:
                action, confidence = "BUY", "Medium"
            elif score >= 1:
                action, confidence = "WATCH", "Medium"
            elif score >= -1:
                action, confidence = "WATCH", "Low"
            else:
                action, confidence = "SELL", "High"   # flat → avoid / stay out (never short)

        # ── HEADLINE ─────────────────────────────────────────────────────────
        if action == "BUY":
            if confidence == "High":
                headline = f"Strong buy setup — multiple indicators aligned on {ticker}"
            else:
                headline = f"Decent entry on {ticker} — conditions mostly favorable, manage risk"
        elif action == "HOLD":
            if confidence == "High":
                headline = f"{ticker} is behaving well — structure intact, hold and let it develop"
            elif confidence == "Medium":
                headline = f"{ticker} holding but momentum mixed — stay in, watch your stop"
            else:
                headline = f"{ticker} showing warning signs — tighten stop and watch closely"
        elif action == "SELL":
            if position:
                headline = f"Exit signal on {ticker} — technical deterioration, protect capital now"
            else:
                headline = f"Avoid {ticker} here — setup is broken, wait for better conditions"
        else:  # WATCH
            headline = f"{ticker} on the radar — wait for cleaner confirmation before pulling the trigger"

        # ── REASONING (specific numbers, plain English) ───────────────────────
        ema_desc = (
            "above both EMA20 and EMA50 — short/medium trend bullish"
            if above_20 and above_50 else
            "above EMA20 but still below EMA50 — partial recovery"
            if above_20 else
            "below EMA20 and EMA50 — trend structure broken"
        )
        macd_5_str = str(macd_5d)
        long_term = (
            f"above its {'rising' if slope == 'positive' else slope} 200d EMA (${ema200:.2f})"
            if ema200 and above_200 else
            f"below its 200d EMA (${ema200:.2f})"
            if ema200 else
            "no 200d EMA data (< 200 trading days)"
        )

        pos_sentence = ""
        if position and pnl_pct is not None:
            pos_sentence = (
                f" Position is {pnl_pct:+.2f}% from entry ${position['entry']:.2f}, "
                f"with stop at ${position['stop']:.2f} "
                f"({cushion_pct:.1f}% cushion, {cushion_atr:.1f} ATR units of room)."
            )

        reasoning = (
            f"{ticker} is trading at ${price:.2f} ({day_chg:+.2f}% today), {ema_desc}. "
            f"MACD histogram is {macd_dir} at {macd_h:.4f} — 5-day sequence {macd_5_str}. "
            f"RSI at {rsi:.0f} is {'overbought' if rsi > 70 else 'oversold' if rsi < 30 else 'in a healthy range'}. "
            f"ADX at {adx:.0f} signals {'a strong' if adx > 25 else 'a weak'} trend. "
            f"Volume is {vr:.1f}x the 20-day average. "
            f"The stock is {long_term}."
            f"{pos_sentence}"
        )

        # ── SUGGESTED STOP ───────────────────────────────────────────────────
        if action in ("BUY", "HOLD", "WATCH"):
            # 1.5 ATR below price, but never below 5-day low
            raw_stop = round(price - atr * 1.5, 2)
            suggested_stop = max(raw_stop, round(low_5d * 0.995, 2))
        else:
            suggested_stop = None

        # ── OUTLOOK ──────────────────────────────────────────────────────────
        lt = "Bullish" if (ema200 and above_200 and slope == "positive") else \
             "Bearish" if (ema200 and (not above_200 or slope == "negative")) else "Neutral"
        st = "Bullish" if score >= 2 else "Bearish" if score <= -2 else "Neutral"
        outlook = f"Long-term {lt} · Short-term {st}"

        # ── FALLBACKS (always show at least 1 catalyst + 1 risk) ─────────────
        if not catalysts:
            catalysts.append(f"Support at 5-day low ${low_5d:.2f} and 10-day low ${low_10d:.2f}")
        if not risks:
            risks.append(
                f"ATR ${atr:.2f} ({atr_pct:.1f}% daily range) — "
                f"size position accordingly to limit single-day impact"
            )

        return {
            "action":          action,
            "confidence":      confidence,
            "headline":        headline,
            "reasoning":       reasoning,
            "risks":           risks[:4],
            "catalysts":       catalysts[:4],
            "suggested_stop":  suggested_stop,
            "outlook":         outlook,
            "score":           score,
            "fundamentals":    fund_text,
            "analyst_target":  analyst_target,
            "earnings_days":   earnings_days,
        }

    except Exception as e:
        return {"error": "analysis_error", "message": str(e)}
