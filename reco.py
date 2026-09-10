"""reco.py — the single source of truth for what to do with a ticker.

Every surface in the app (Analyze, Portfolio, Scanner, Dashboard) calls
`recommend()` so the whole app speaks with ONE voice and never contradicts
itself. It is deterministic (rule-based, no LLM), horizon-aware, and
ownership-aware.

LONG-ONLY (halal): we only ever buy, hold, add, or exit. "Avoid" / "Sell" mean
stay out or close a long — never open a short.

Unified verdict vocabulary
--------------------------
Don't own the ticker →  BUY  ·  WAIT (for a better entry)  ·  AVOID
Own the ticker       →  HOLD ·  SELL                       ·  BUY_MORE (add on a dip/bounce)

It reuses the existing, validated technical engine (chart_analysis) for the
score/read and layers the horizon-tuned levels + the unified decision on top,
so it stays consistent with the alerts that already use chart_analysis.
"""

from typing import Optional

# ── Horizons ──────────────────────────────────────────────────────────────────
HORIZONS = ["Day Trading", "Short-term Swing", "Long-term Swing"]
DEFAULT_HORIZON = "Short-term Swing"

# Map the friendly labels (and any legacy labels) to a canonical key.
_CANON = {
    "day trading": "Day Trading", "day trade": "Day Trading", "daytrade": "Day Trading",
    "short-term swing": "Short-term Swing", "short-term": "Short-term Swing",
    "short term swing": "Short-term Swing", "swing": "Short-term Swing",
    "long-term swing": "Long-term Swing", "long-term": "Long-term Swing",
    "long term swing": "Long-term Swing", "position": "Long-term Swing",
}

# Per-horizon tuning: ATR multiples for stop/targets, the trend line that matters,
# and the RSI overbought/oversold bands (a day trade cares about tighter extremes).
_PLAN = {
    "Day Trading":      {"stop": 0.75, "t1": 1.0, "t2": 2.0, "ema": "ema20", "ob": 72, "os": 30,
                         "hold_days": "minutes to a day"},
    "Short-term Swing": {"stop": 1.5,  "t1": 2.0, "t2": 3.5, "ema": "ema20", "ob": 73, "os": 33,
                         "hold_days": "days to a few weeks"},
    "Long-term Swing":  {"stop": 2.0,  "t1": 4.0, "t2": 7.0, "ema": "ema50", "ob": 78, "os": 38,
                         "hold_days": "weeks to months"},
}

# Human-friendly label for each verdict.
LABELS = {
    "BUY":      "Buy",
    "WAIT":     "Wait for a better entry",
    "AVOID":    "Avoid for now",
    "HOLD":     "Hold",
    "SELL":     "Sell",
    "BUY_MORE": "Buy more",
}


def canonical_horizon(horizon: Optional[str]) -> str:
    if not horizon:
        return DEFAULT_HORIZON
    return _CANON.get(str(horizon).strip().lower(), DEFAULT_HORIZON)


def horizon_plan(horizon: str) -> dict:
    return _PLAN.get(canonical_horizon(horizon), _PLAN[DEFAULT_HORIZON])


# ── Technical read (deterministic) ────────────────────────────────────────────
def _read(ticker: str):
    """Return the latest indicator snapshot, or None if data is unavailable."""
    from market_data import get_bars, compute_indicators, ema200_slope
    df = get_bars(ticker, "6mo", "1d")
    if df is None or len(df) < 30:
        return None
    df = compute_indicators(df)
    d = df.iloc[-1]
    price = float(d["close"])
    atr = float(d.get("atr", 0) or 0)
    ema20 = float(d.get("ema20", price) or price)
    ema50 = float(d.get("ema50", price) or price)
    try:
        _e200 = d.get("ema200", None)
        ema200 = float(_e200) if _e200 is not None and _e200 == _e200 else None  # NaN check
    except Exception:
        ema200 = None
    macd_h = float(d.get("macd_hist", 0) or 0)
    pmh = float(df.iloc[-2].get("macd_hist", 0) or 0)
    _prev_c = float(df.iloc[-2].get("close", price) or price)
    return {
        "df": df, "price": price, "atr": atr,
        "rsi": float(d.get("rsi", 50) or 50),
        "adx": float(d.get("adx", 20) or 20),
        "ema20": ema20, "ema50": ema50, "ema200": ema200,
        "macd_dir": "building" if macd_h > pmh else "fading",
        "vol_ratio": float(d.get("vol_ratio", 1.0) or 1.0),
        "day_change": (price - _prev_c) / _prev_c * 100 if _prev_c else 0.0,
        "low_5d": float(df["low"].tail(5).min()),
        "low_10d": float(df["low"].tail(10).min()),
        "slope200": ema200_slope(df),
    }


def _analyst(ticker: str):
    try:
        from fundamentals import get_analyst_targets
        a = get_analyst_targets(ticker) or {}
        return (a.get("target_mean"), a.get("target_high"),
                a.get("num_analysts", 0) or 0, a.get("recommendation", "hold"))
    except Exception:
        return (None, None, 0, "hold")


def _levels(read: dict, ref_price: float, plan: dict, is_long: bool, analyst) -> dict:
    """Horizon-tuned stop + two targets off a reference price (long-only)."""
    atr = read["atr"]
    df = read["df"]
    tmean, thigh, n_an, _rec = analyst
    # Stop: ATR-based (per horizon), tightened to just under the nearest support.
    atr_stop = ref_price - plan["stop"] * atr
    below = [x for x in df["low"].tail(20).values if x < ref_price]
    stop = (max(below) * 0.995) if (below and max(below) > atr_stop) else atr_stop
    stop = round(max(stop, 0.01), 2)
    # Targets: Long-term leans on analyst price targets; others use ATR multiples.
    src = "atr"
    if is_long and tmean and n_an > 0 and tmean > ref_price:
        t1 = round(tmean, 2)
        t2 = round(thigh, 2) if (thigh and thigh > tmean) else round(ref_price + plan["t2"] * atr, 2)
        src = "analyst"
    else:
        t1 = round(ref_price + plan["t1"] * atr, 2)
        t2 = round(ref_price + plan["t2"] * atr, 2)
    risk, reward = ref_price - stop, t1 - ref_price
    rr = round(reward / risk, 1) if risk > 0 else 0.0
    return {"stop": stop, "target1": t1, "target2": t2, "rr": rr, "target_source": src}


def _style_score(horizon: str, read: dict, snap: dict, cur: float) -> int:
    """Horizon-SPECIFIC opportunity score. Each style weighs different things, so the
    SAME ticker can score well as a day trade and poorly as a long-term hold (or vice
    versa) — that divergence is correct, not a contradiction. Same ticker + same style
    is always identical (deterministic), which is the consistency that matters.

      Day Trading      → intraday momentum, volume, short trend; ignores fundamentals.
      Short-term Swing → 20/50 trend structure + momentum + a little fundamentals.
      Long-term Swing  → big trend (200d) + business quality + analyst upside; patient
                         with short-term overbought.
    """
    rsi = read["rsi"]; adx = read["adx"]
    ema20, ema50, ema200 = read["ema20"], read["ema50"], read["ema200"]
    mom_up = read["macd_dir"] == "building"
    vr = read["vol_ratio"]; dchg = read["day_change"]; slope = read["slope200"]
    above20 = cur > ema20
    above50 = cur > ema50
    above200 = (ema200 is not None) and cur > ema200
    fin = (snap or {}).get("financials", {}) or {}
    a = (snap or {}).get("analyst", {}) or {}
    rg = fin.get("revenue_growth_yoy"); pm = fin.get("profit_margin")
    tm = a.get("target_mean"); n_an = a.get("num_analysts", 0) or 0
    upside = ((tm - cur) / cur * 100) if (tm and cur) else None
    dte = ((snap or {}).get("earnings", {}) or {}).get("days_until", 999) or 999
    s = 0

    if horizon == "Day Trading":
        s += 2 if mom_up else -2
        s += 2 if above20 else -2
        if adx >= 25:
            s += 1
        if vr >= 1.3 and dchg > 0:
            s += 2          # volume-backed push higher today
        elif vr >= 1.3 and dchg < 0:
            s -= 1          # heavy selling today
        if rsi >= 78:
            s -= 2          # too hot to chase intraday
        elif 45 <= rsi <= 68:
            s += 1
        elif rsi < 35:
            s -= 1          # falling knife
    elif horizon == "Long-term Swing":
        if above200 and slope == "positive":
            s += 3
        elif above200:
            s += 1
        else:
            s -= 3
        if above50:
            s += 1
        if rg is not None:
            s += 2 if rg >= 15 else 1 if rg > 0 else -2
        if pm is not None and pm > 0:
            s += 1
        if upside is not None:
            s += 2 if upside >= 12 else -2 if upside <= -5 else 0
        if n_an >= 5 and (a.get("recommendation", "hold") in ("strong_buy", "buy")):
            s += 1
        # long-term tolerates short-term overbought → no RSI penalty
    else:  # Short-term Swing
        if above20 and above50:
            s += 2
        elif not above50:
            s -= 2
        s += 2 if mom_up else -1
        if adx >= 22:
            s += 1
        if rsi >= 74:
            s -= 2
        elif 40 <= rsi <= 58 and mom_up:
            s += 1          # healthy pullback resuming
        fs = (snap or {}).get("fscore", 0) or 0
        s += 1 if fs >= 3 else -1 if fs <= -3 else 0
        if 0 < dte <= 4:
            s -= 1          # earnings imminent = swing risk
    return s


def _hold_estimate(horizon: str, cur: float, t1: float, atr: float) -> str:
    """Plain-English 'how long to hold it' guide, matched to the style."""
    if horizon == "Day Trading":
        return "Minutes to a few hours — a same-day trade; plan to close it before the close, not hold overnight."
    if horizon == "Long-term Swing":
        return "Several weeks to months — long-term targets track the ~12-month analyst view; give the thesis room to play out."
    # Short-term Swing — estimate trading days to the first target at a realistic pace.
    if atr > 0 and t1 > cur:
        days = int(round((t1 - cur) / (0.4 * atr)))
        days = max(2, min(days, 20))
        wks = days / 5
        span = f"~{days} trading days" + (f" (about {wks:.0f} week{'s' if wks >= 1.5 else ''})" if days >= 5 else "")
        return f"A few days to ~3 weeks — roughly {span} to reach the first target if it moves at its usual pace."
    return "A few days to a few weeks."


def _horizon_word(h: str) -> str:
    return {"Day Trading": "day-trade", "Short-term Swing": "short-term swing",
            "Long-term Swing": "long-term"}.get(h, "swing")


def _custom_reason(name, ticker, verdict, horizon, f, snap, lv) -> str:
    """Compose a reason that is specific to THIS company — its analyst ratings,
    news, revenue trajectory, insider activity and earnings timing — not just a
    generic momentum line. Plain-language, deterministic (so it never contradicts
    the badge or another page)."""
    a = (snap or {}).get("analyst", {}) or {}
    nw = (snap or {}).get("news", {}) or {}
    e = (snap or {}).get("earnings", {}) or {}
    fin = (snap or {}).get("financials", {}) or {}
    ins = (snap or {}).get("insider", {}) or {}
    who = f"{name} ({ticker})" if name and name != ticker else ticker
    hw = _horizon_word(horizon)
    cur = f["current"]; stop = lv["stop"]; t1 = lv["target1"]; rr = lv["rr"]

    # Plain technical phrase, framed for the chosen style.
    if horizon == "Day Trading":
        if f["momentum_up"] and f.get("vol_up"):
            tech = "momentum is building today on heavier-than-usual volume"
        elif f["momentum_up"]:
            tech = "intraday momentum is turning up"
        else:
            tech = "intraday momentum has stalled"
        if f["overbought"]:
            tech += f", though RSI at {f['rsi']} is hot (it has run up fast)"
    elif horizon == "Long-term Swing":
        if f.get("above_200") and f.get("slope_pos"):
            tech = "it's in a healthy long-term uptrend, above a rising 200-day average price"
        elif f.get("above_200"):
            tech = "it's above its 200-day average price, but the long-term trend is flattening"
        else:
            tech = "it's below its 200-day average price — the long-term trend is down"
    else:  # Short-term Swing
        if f["trend_up"] and f["momentum_up"]:
            tech = "it's above its 20- and 50-day average prices with momentum building"
        elif f["trend_up"]:
            tech = "it's holding its trend but momentum has cooled"
        else:
            tech = "it's lost its trend and momentum is weak"
        if f["overbought"]:
            tech += f", and RSI at {f['rsi']} shows it has run up fast (overbought)"

    parts = []
    if verdict == "BUY":
        parts.append(f"{who} looks like a solid {hw} buy right now — {tech}.")
    elif verdict == "WAIT":
        parts.append(f"{who} has a setup worth watching, but this isn't the best entry yet — {tech}.")
    elif verdict == "AVOID":
        parts.append(f"{who} isn't worth buying here — {tech}.")
    elif verdict == "HOLD":
        _pl = ""
        if f.get("pnl_pct") is not None:
            _pl = f" You're {'up' if f['pnl_pct'] >= 0 else 'down'} {abs(f['pnl_pct']):.1f}% on it and"
        parts.append(f"Keep holding {who} —{_pl} {tech}.")
    elif verdict == "SELL":
        parts.append(f"It's time to sell {who} — {tech}, so the reason you're in the trade no longer holds.")
    elif verdict == "BUY_MORE":
        parts.append(f"{who} pulled back and is bouncing while its uptrend is still intact — a spot where adding can pay off ({tech}).")

    # Analyst consensus + price target (company-specific).
    tm = a.get("target_mean"); na = a.get("num_analysts", 0) or 0
    rec = (a.get("recommendation") or "hold").replace("_", " ")
    if tm and na > 0 and cur:
        up = (tm - cur) / cur * 100
        _bull_verdict = verdict in ("BUY", "HOLD", "BUY_MORE")
        if up >= 3 and _bull_verdict:
            parts.append(f"Wall Street backs it too — {na} analysts rate it {rec}, average price target ${tm:.2f} ({up:+.0f}% above today's ${cur:.2f}).")
        elif up >= 3:
            parts.append(f"That said, Wall Street is more upbeat — {na} analysts rate it {rec} with an average target of ${tm:.2f} ({up:+.0f}% above today), so keep it on your radar for when the chart improves.")
        elif up <= -3:
            parts.append(f"Wall Street is cautious, too — {na} analysts' average target of ${tm:.2f} sits {up:.0f}% below today's price.")
        else:
            parts.append(f"Analysts see it near fair value ({na} of them, average target ${tm:.2f}).")

    # News sentiment this week.
    ac = nw.get("article_count", 0) or 0; lbl = nw.get("label", "neutral")
    if ac >= 2 and lbl == "positive":
        parts.append(f"Recent news has been mostly positive ({ac} stories this week).")
    elif ac >= 2 and lbl == "negative":
        parts.append(f"Keep an eye on the headlines — recent news has skewed negative ({ac} stories this week).")

    # Company trajectory (revenue growth + profitability), else insider buying.
    rg = fin.get("revenue_growth_yoy"); pm = fin.get("profit_margin")
    if rg is not None:
        if rg >= 15:
            _t = f"The business is growing fast — revenue is up {rg:.0f}% over the past year"
        elif rg > 0:
            _t = f"The business is growing — revenue is up {rg:.0f}% over the past year"
        else:
            _t = f"Revenue has been shrinking ({rg:.0f}% over the past year), a caution flag"
        if pm is not None and pm > 0:
            _t += f", and it's profitable (about {pm:.0f}% profit margin)"
        elif pm is not None and pm <= 0:
            _t += ", and it isn't profitable yet"
        parts.append(_t + ".")
    elif ins.get("signal") == "buying":
        parts.append("Company insiders have been buying recently — a vote of confidence.")

    # Earnings timing.
    dte = e.get("days_until", 999) or 999
    if 0 < dte <= 21:
        _nd = e.get("next_date") or ""
        parts.append(f"Heads up: earnings are about {dte} days away{(' (' + _nd + ')') if _nd else ''}, which can move the price sharply.")

    # What to actually do, tied to the levels.
    if verdict == "BUY":
        parts.append(f"If you buy, a stop at ${stop:.2f} caps the downside while you aim for ${t1:.2f} (~{rr}:1 reward-to-risk).")
    elif verdict == "WAIT":
        parts.append(f"Add it to your watchlist and wait for a dip toward ${stop:.2f}–${cur:.2f} or a clean push higher.")
    elif verdict == "AVOID":
        parts.append("Better to wait for the trend to turn back up before putting money in.")
    elif verdict == "HOLD":
        parts.append(f"Let it work toward ${t1:.2f} and trust your safety exit (stop) at ${stop:.2f}.")
    elif verdict == "SELL":
        parts.append(f"Protect your money — exit near ${cur:.2f} and reassess.")
    elif verdict == "BUY_MORE":
        parts.append(f"Adding near ${cur:.2f} while keeping the same stop at ${stop:.2f} keeps your risk defined.")

    return " ".join(p for p in parts if p)


def recommend(ticker: str, horizon: str, position: Optional[dict] = None,
              company_name: Optional[str] = None) -> dict:
    """The single source of truth. Returns a unified recommendation dict.

    Keys: owns, horizon, verdict, label, confidence, current, atr, rsi, stop,
    target1, target2, rr, reason, options (other actions to consider),
    plus base headline/reasoning/risks/catalysts for the detail panel.
    """
    horizon = canonical_horizon(horizon)
    plan = _PLAN[horizon]
    owns = bool(position)
    is_long = horizon == "Long-term Swing"

    read = _read(ticker)
    if not read:
        return {"error": "no_data", "owns": owns, "horizon": horizon,
                "verdict": "WAIT" if not owns else "HOLD",
                "label": LABELS["WAIT" if not owns else "HOLD"],
                "confidence": "Low", "reason": f"Not enough chart data for {ticker} yet.",
                "options": [], "risks": [], "catalysts": []}

    price = read["price"]
    rsi = read["rsi"]
    key_ema = read[plan["ema"]] or price
    analyst = _analyst(ticker)

    # Base technical read from the existing (validated, plain-language) engine.
    try:
        from chart_analysis import analyze_ticker as _ca
        base = _ca(ticker, position=position) or {}
    except Exception:
        base = {}
    # Company-specific fundamentals (analyst / news / trajectory / earnings / insider).
    # Internally cached ~4-6h in fundamentals.py, so this is cheap on repeat calls.
    try:
        from fundamentals import get_fundamental_snapshot
        snap = get_fundamental_snapshot(ticker) or {}
    except Exception:
        snap = {}

    lv = _levels(read, price, plan, is_long, analyst)
    stop, t1, rr = lv["stop"], lv["target1"], lv["rr"]

    overbought = rsi >= plan["ob"]
    below_trend = price < key_ema
    momentum_up = read["macd_dir"] == "building"
    extended = read["atr"] > 0 and (price - key_ema) / read["atr"] > 2.6  # stretched above the trend line
    # Long-term buyers tolerate short-term overbought/extension; day & swing don't.
    good_entry = (rr >= 1.2) if is_long else ((not overbought) and (rr >= 1.3) and (not extended))

    # Style-weighted opportunity score → the SAME ticker can score very differently
    # across day / short / long (intended). Confidence follows the score's strength.
    s = _style_score(horizon, read, snap, price)
    conf = "High" if (s >= 5 or s <= -4) else "Medium" if (s >= 2 or s <= -2) else "Low"

    result = {
        "owns": owns, "horizon": horizon, "confidence": conf, "style_score": s,
        "current": round(price, 2), "atr": round(read["atr"], 2), "rsi": round(rsi),
        "stop": stop, "target1": t1, "target2": lv["target2"], "rr": rr,
        "target_source": lv["target_source"],
        "hold": _hold_estimate(horizon, price, t1, read["atr"]),
        "headline": base.get("headline", ""), "reasoning": base.get("reasoning", ""),
        "risks": base.get("risks", [])[:4], "catalysts": base.get("catalysts", [])[:4],
        "analyst_target": base.get("analyst_target"), "earnings_days": base.get("earnings_days", 999),
    }

    pnl_pct = None
    if not owns:
        # ── Not held → BUY / WAIT / AVOID (style-weighted) ───────────────────
        if s >= 4 and good_entry:
            verdict = "BUY"
        elif s >= 0:
            verdict = "WAIT"
        else:
            verdict = "AVOID"
        options = [v for v in ("BUY", "WAIT", "AVOID") if v != verdict]
    else:
        # ── Held → HOLD / SELL / BUY_MORE (style-weighted) ───────────────────
        entry = float(position.get("entry", price) or price)
        p_stop = float(position.get("stop", stop) or stop)
        p_t1 = float(position.get("target1", t1) or t1)
        pnl_pct = (price - entry) / entry * 100 if entry else 0.0
        broken = (price <= p_stop) or (s <= -3)
        # A "bounce back" add: still constructive for the style, pulled back and now
        # turning up, room left to the target, and not deeply underwater.
        near_pullback = (rsi <= 55) and (price <= key_ema * 1.03 or price <= read["low_10d"] * 1.06)
        bounce_add = (not broken) and (s >= 0) and (not below_trend) and momentum_up and near_pullback \
            and (price < p_t1) and (pnl_pct > -8) and (rsi >= plan["os"])
        if broken:
            verdict = "SELL"
        elif bounce_add:
            verdict = "BUY_MORE"
        else:
            verdict = "HOLD"
        options = [v for v in ("HOLD", "SELL", "BUY_MORE") if v != verdict]

    _above_200 = (read["ema200"] is not None) and (price > read["ema200"])
    facts = {
        "current": price, "rsi": round(rsi),
        "trend_up": not below_trend, "momentum_up": momentum_up,
        "overbought": overbought, "extended": extended, "rr": rr, "pnl_pct": pnl_pct,
        "vol_up": read["vol_ratio"] >= 1.3 and read["day_change"] > 0,
        "above_200": _above_200, "slope_pos": read["slope200"] == "positive",
    }
    result["verdict"] = verdict
    result["label"] = LABELS[verdict]
    result["reason"] = _custom_reason(company_name or ticker, ticker, verdict, horizon, facts, snap, lv)
    result["options"] = [{"verdict": v, "label": LABELS[v]} for v in options]
    return result
