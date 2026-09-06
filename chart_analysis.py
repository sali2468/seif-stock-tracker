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
                f"The price ${price:.2f} is above both its 20-day average of ${ema20:.2f} and its "
                f"50-day average of ${ema50:.2f} (average-price lines that show the recent trend) — "
                f"the short- and medium-term trend is pointing up"
            )
        elif above_20 and not above_50:
            score += 0
            notes.append(
                f"The price is above its 20-day average (${ema20:.2f}) but still below its 50-day "
                f"average (${ema50:.2f}) — the trend is recovering but not yet confirmed"
            )
        else:
            score -= 2
            risks.append(
                f"The price ${price:.2f} is below both its 20-day average (${ema20:.2f}) and its "
                f"50-day average (${ema50:.2f}) — the recent uptrend is broken"
            )

        # — 200d EMA (long-term regime) ————————————————————————————————————
        if ema200:
            if above_200 and slope == "positive":
                score += 2
                catalysts.append(
                    f"The price is above a rising 200-day average (${ema200:.2f}) — this line tracks "
                    f"the long-term trend, and it shows the long-term uptrend is intact"
                )
            elif above_200 and slope == "flat":
                score += 1
                notes.append(f"The price is above its 200-day average (${ema200:.2f}, the long-term trend line), but that line is flat — long-term direction is neutral")
            elif above_200 and slope == "negative":
                score += 0
                risks.append(
                    f"The price is above its 200-day average (${ema200:.2f}, the long-term trend line), but that line is declining — the long-term trend is weakening"
                )
            else:
                score -= 2
                risks.append(
                    f"The price is below its 200-day average (${ema200:.2f}, the long-term trend line) and that line is {slope} — the stock is in a long-term downtrend"
                )

        # — MACD histogram (momentum) ——————————————————————————————————————
        # Also flag rapid fades (like RKLB: -55% in 2 days)
        rapid_fade = macd_dir == "fading" and macd_h > 0 and macd_fade_pct > 30
        if macd_dir == "building" and macd_h > 0:
            score += 2
            catalysts.append(
                "Momentum is building and buyers are gaining strength — momentum (the MACD reading) "
                "measures whether buying pressure is speeding up or slowing down"
            )
        elif macd_dir == "building" and macd_h < 0:
            score += 1
            catalysts.append(
                "Momentum is still negative but starting to turn up — a possible early shift from "
                "sellers to buyers, worth watching to see if it holds (this is the MACD reading, which "
                "tracks whether buying pressure is speeding up or slowing down)"
            )
        elif rapid_fade:
            score -= 3
            risks.append(
                f"Momentum collapsed about {macd_fade_pct:.0f}% in a single day — a sudden loss of "
                f"buying pressure that often comes just before a reversal (this is the MACD reading)"
            )
        elif macd_dir == "fading" and macd_h > 0:
            score -= 1
            risks.append(
                "Momentum is fading — buyers are losing steam even though the move is still positive "
                "(this is the MACD reading, which tracks whether buying pressure is speeding up or slowing down)"
            )
        else:
            score -= 1
            risks.append(
                "Momentum is negative and still weakening — sellers are in control right now (this is the MACD reading)"
            )

        # — RSI ————————————————————————————————————————————————————————————
        if rsi > 80:
            score -= 3
            risks.append(
                f"RSI is {rsi:.0f} — extremely high (RSI is a 0-100 speed gauge for the price). "
                f"The stock has run up very fast and is likely due for a pullback"
            )
        elif rsi > 72:
            score -= 2
            risks.append(
                f"RSI is {rsi:.0f} — high, or 'overbought' (RSI is a 0-100 speed gauge for the price). "
                f"After a fast run-up like this, the price often stalls or dips"
            )
        elif rsi > 60:
            score += 1
            catalysts.append(f"RSI is {rsi:.0f} — strong but not overheated (RSI is a 0-100 speed gauge for the price); there is still room to move higher")
        elif rsi >= 40:
            score += 0
            notes.append(f"RSI is {rsi:.0f} — a neutral, middle-of-the-road reading (RSI is a 0-100 speed gauge for the price)")
        elif rsi >= 30:
            score += 1
            catalysts.append(
                f"RSI is {rsi:.0f} — low and recovering (RSI is a 0-100 speed gauge for the price); "
                f"the stock may be setting up for a bounce"
            )
        else:
            score -= 1
            risks.append(f"RSI is {rsi:.0f} — very low, or 'oversold' (RSI is a 0-100 speed gauge for the price); the downtrend may still be picking up speed")

        # — ADX (trend strength — amplifies the direction) ————————————————
        if adx > 35:
            score += 1  # strong trend in force — rewards aligned setups
            notes.append(f"Trend strength (ADX) is {adx:.0f} — a very strong, decisive trend is in play (ADX is a 0-100 gauge of how strong the trend is, not which direction)")
        elif adx > 25:
            notes.append(f"Trend strength (ADX) is {adx:.0f} — a solid, steady trend (ADX is a 0-100 gauge of how strong the trend is, not which direction)")
        elif adx < 15:
            score -= 1
            risks.append(
                f"Trend strength (ADX) is only {adx:.0f} — the trend is very weak and the price is mostly "
                f"chopping sideways (ADX is a 0-100 gauge of how strong the trend is)"
            )

        # — Volume ——————————————————————————————————————————————————————————
        if vr > 2.0:
            if day_chg > 0:
                score += 2
                catalysts.append(
                    f"Trading volume is {vr:.1f}x a normal day while the price rose — that heavy buying "
                    f"often means big institutions (funds) are stepping in (volume = how many shares changed hands)"
                )
            else:
                score -= 2
                risks.append(
                    f"Trading volume is {vr:.1f}x a normal day while the price fell — heavy selling that "
                    f"suggests big holders are getting out (volume = how many shares changed hands)"
                )
        elif vr > 1.4:
            if day_chg > 0:
                score += 1
                catalysts.append(
                    f"Trading volume is above average ({vr:.1f}x a normal day) on an up day, which helps "
                    f"confirm the move higher is real"
                )
            else:
                score -= 1
                risks.append(
                    f"Trading volume is above average ({vr:.1f}x a normal day) on a down day — a sign that "
                    f"sellers are active"
                )
        elif vr < 0.6:
            risks.append(f"Trading volume is light ({vr:.1f}x a normal day) — few people are behind today's move, so it may not hold")

        # — Gap analysis ———————————————————————————————————————————————————
        if gap_pct < -2.0:
            score -= 1
            risks.append(f"The stock opened {gap_pct:.1f}% lower than yesterday's close (a 'gap down') — a sign of selling pressure overnight")
        elif gap_pct > 2.0 and above_20:
            score += 1
            catalysts.append(f"The stock opened {gap_pct:.1f}% higher than yesterday's close (a 'gap up') — a sign of buying strength this morning")

        # — Proximity to recent lows (stop-hunt risk) ——————————————————————
        near_low_5d = price < low_5d * 1.015
        if near_low_5d:
            score -= 1
            risks.append(
                f"The price ${price:.2f} is right near its lowest point of the last 5 days (${low_5d:.2f}) — "
                f"that level has been acting as a floor, and dropping below it would be a warning sign"
            )

        # — Position-specific checks ———————————————————————————————————————
        if position and pnl_pct is not None:
            if cushion_atr is not None and cushion_atr < 0.8 and pnl_pct < 0:
                score -= 3
                risks.append(
                    f"Your safety exit (stop) at ${position['stop']:.2f} sits very close to the price on a "
                    f"trade that's already down — less than one normal day's price swing away, so you could "
                    f"easily be forced out"
                )
            elif cushion_atr is not None and cushion_atr < 1.0:
                score -= 1
                risks.append(
                    "Your safety exit (stop) is less than one normal day's price swing below the current "
                    "price — a single volatile day could trigger it and close the trade"
                )
            if pnl_pct > 15 and above_20 and above_50:
                score += 1
                catalysts.append(
                    f"You're up {pnl_pct:.1f}% and the price is still above its 20-day and 50-day average "
                    f"lines — the trend is working in your favor, so it may pay to let the winner keep running"
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
                    f"Wall Street analysts rate it {rec.replace('_',' ')} — the professionals are "
                    f"leaning against this stock right now"
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
                    f"🚨 EARNINGS IN {earnings_days} DAYS ({snap['earnings']['next_date']}) — earnings is "
                    f"the company's quarterly report card, and the price can jump or drop sharply right after it"
                )
            elif 0 < earnings_days <= 21:
                risks.append(
                    f"Earnings (the company's quarterly report) is due in {earnings_days} days — "
                    f"decide before then whether you want to hold through that risk"
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
                headline = f"Strong buy setup on {ticker} — several signals agree it looks good right now"
            else:
                headline = f"Decent buy on {ticker} — conditions mostly look good, but keep your risk small"
        elif action == "HOLD":
            if confidence == "High":
                headline = f"{ticker} is doing well — the trend is healthy, so hold and give it room"
            elif confidence == "Medium":
                headline = f"{ticker} is holding up but momentum is mixed — stay in, but watch your safety exit (stop)"
            else:
                headline = f"{ticker} is flashing warning signs — tighten your safety exit (stop) and watch it closely"
        elif action == "SELL":
            if position:
                headline = f"Time to exit {ticker} — the setup is falling apart, so protect your money now"
            else:
                headline = f"Avoid {ticker} for now — the setup is broken; wait for it to improve"
        else:  # WATCH
            headline = f"{ticker} is worth watching — wait for a clearer, more confident signal before buying"

        # ── REASONING (specific numbers, plain English) ───────────────────────
        ema_desc = (
            "above both its 20-day and 50-day average price — the short- and medium-term trend is up"
            if above_20 and above_50 else
            "above its 20-day average price but still below its 50-day average — only a partial recovery so far"
            if above_20 else
            "below both its 20-day and 50-day average price — the recent trend is broken"
        )
        macd_5_str = str(macd_5d)
        long_term = (
            f"above its {'rising ' if slope == 'positive' else slope + ' '}200-day average price (${ema200:.2f}), the line that tracks the long-term trend"
            if ema200 and above_200 else
            f"below its 200-day average price (${ema200:.2f}), the line that tracks the long-term trend"
            if ema200 else
            "missing a 200-day average (less than 200 days of history), so the long-term trend is unclear"
        )

        pos_sentence = ""
        if position and pnl_pct is not None:
            pos_sentence = (
                f" You are {pnl_pct:+.2f}% versus your buy price of ${position['entry']:.2f}, "
                f"with a safety exit (your stop) set at ${position['stop']:.2f} — "
                f"that's {cushion_pct:.1f}% below the current price."
            )

        _rsi_plain = ("high, meaning it has run up quickly and may pause or dip" if rsi > 70
                      else "low, meaning it has dropped hard and may be due for a bounce" if rsi < 30
                      else "in a healthy middle range")
        reasoning = (
            f"{ticker} is trading at ${price:.2f} ({day_chg:+.2f}% today) and is {ema_desc}. "
            f"Its momentum is {macd_dir} — momentum (often called MACD) simply means whether buying pressure is speeding up or slowing down. "
            f"RSI is {rsi:.0f}, which is {_rsi_plain} (RSI is a 0-100 speed gauge for the price). "
            f"The trend strength reading (ADX) is {adx:.0f}, which points to {'a strong, decisive' if adx > 25 else 'a weak, choppy'} trend. "
            f"Today's trading volume is {vr:.1f}x a normal day. "
            f"Overall, the stock is {long_term}."
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
            catalysts.append(f"Recent price floors to watch: the 5-day low at ${low_5d:.2f} and the 10-day low at ${low_10d:.2f} — levels where buyers have stepped in before")
        if not risks:
            risks.append(
                f"This stock typically swings about ${atr:.2f} up or down per day ({atr_pct:.1f}% of its price) — "
                f"keep your position small enough that one normal day won't hurt too much"
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
