"""
daytrader_pullback.py — intraday pullback day-trading engine for Moomoo.

Long-only pullback method:
  • Record the major intraday support/resistance levels (clustered swing pivots).
  • In an uptrend, ENTER on a pullback to support — the spot where a short-seller
    would COVER (take profit).
  • TARGET the next resistance above — the spot where a short-seller would ENTER.
  • STOP just below the support level.

Detection only (no orders). Convert a signal to a Moomoo plan with to_plan()
and execute via moomoo_integration.execute_trade_plan (SIMULATE by default).
"""

import logging
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional, List
from market_data import get_bars, compute_indicators
from config import get_section

log = logging.getLogger("daytrader")

# ── Tunables ──────────────────────────────────────────────────────────────────
PIVOT_K          = 3       # bars each side to qualify a swing pivot
CLUSTER_TOL      = 0.004   # merge levels within 0.4% into one (counts touches)
NEAR_SUPPORT_PCT = 0.010   # "pulled back" = within 1.0% above a support level
MIN_RR           = 1.5     # reject setups below this reward:risk
RSI_LOW, RSI_HI  = 33, 60  # intraday bounce zone (turning up off the pullback)
STOP_ATR_MULT    = 0.5     # stop = support - 0.5*ATR (fallback: 0.3% below)


@dataclass
class Level:
    price:   float
    kind:    str    # "support" | "resistance"
    touches: int
    last_idx: int


@dataclass
class PullbackSignal:
    ticker:     str
    price:      float
    entry:      float
    stop:       float
    target:     float
    stop_pct:   float
    gain_pct:   float
    rr:         float
    support:    float
    resistance: float
    stars:      int
    score:      int = 0
    why:        str = ""
    levels:     List[Level] = field(default_factory=list)


def _pivots(highs, lows, k=PIVOT_K):
    sh, sl = [], []
    for i in range(k, len(highs) - k):
        if highs[i] >= highs[i - k:i + k + 1].max():
            sh.append((i, float(highs[i])))
        if lows[i] <= lows[i - k:i + k + 1].min():
            sl.append((i, float(lows[i])))
    return sh, sl


def _cluster(pivots, tol=CLUSTER_TOL):
    levels = []
    for idx, p in sorted(pivots, key=lambda x: x[1]):
        for lv in levels:
            if abs(p - lv["price"]) / lv["price"] <= tol:
                lv["price"] = (lv["price"] * lv["touches"] + p) / (lv["touches"] + 1)
                lv["touches"] += 1
                lv["last_idx"] = max(lv["last_idx"], idx)
                break
        else:
            levels.append({"price": p, "touches": 1, "last_idx": idx})
    return levels


def find_levels(df: pd.DataFrame):
    """Return (supports, resistances) as Level lists, most-touched first."""
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    sh, sl = _pivots(h, l)
    res = [Level(round(x["price"], 2), "resistance", x["touches"], x["last_idx"]) for x in _cluster(sh)]
    sup = [Level(round(x["price"], 2), "support",    x["touches"], x["last_idx"]) for x in _cluster(sl)]
    res.sort(key=lambda x: (x.touches, x.last_idx), reverse=True)
    sup.sort(key=lambda x: (x.touches, x.last_idx), reverse=True)
    return sup, res


def _load_intraday(ticker: str, interval: str, period: str) -> Optional[pd.DataFrame]:
    """Real-time bars from Moomoo OpenD; fall back to yfinance if OpenD is down."""
    try:
        from moomoo_integration import MoomooData
        df = MoomooData().get_intraday_bars(ticker, interval=interval, num=300)
        if df is not None and len(df) >= 40:
            return df
    except Exception:
        pass
    return get_bars(ticker, period, interval)


def scan_pullback(ticker: str, interval: str = "5m", period: str = "5d") -> Optional[PullbackSignal]:
    """Return a PullbackSignal if a pullback-to-support long is set up now, else None."""
    df = _load_intraday(ticker, interval, period)
    if df is None or len(df) < 40:
        return None
    df = compute_indicators(df)
    sup, res = find_levels(df)
    if not sup or not res:
        return None

    d, prev = df.iloc[-1], df.iloc[-2]
    price = float(d["close"])
    ema20 = float(d.get("ema20", np.nan))
    ema50 = float(d.get("ema50", np.nan))
    rsi   = float(d.get("rsi",   np.nan))
    rsi_p = float(prev.get("rsi", rsi))
    adx   = float(d.get("adx",   np.nan))
    atr   = float(d.get("atr",   np.nan))
    vr    = float(d.get("vol_ratio", np.nan))

    below = [s for s in sup if s.price < price]
    above = [r for r in res if r.price > price]
    if not below or not above:
        return None
    support    = max(below, key=lambda s: s.price)   # nearest support under price
    resistance = min(above, key=lambda r: r.price)   # nearest resistance above price

    hg = get_section("pullback.hard_gates", {})
    sc = get_section("pullback.scoring", {})
    near_pct = float(hg.get("near_support_pct", NEAR_SUPPORT_PCT))
    rsi_lo   = float(hg.get("rsi_low",  RSI_LOW))
    rsi_hi   = float(hg.get("rsi_high", RSI_HI))
    min_rr   = float(hg.get("min_rr",   MIN_RR))

    # ── Hard gates: uptrend + pulled back to support + in the bounce zone ─────
    prox        = (price - support.price) / price
    in_uptrend  = not np.isnan(ema20) and price >= ema20 * 0.99
    pulled_back = prox <= near_pct
    bounce_zone = not np.isnan(rsi) and rsi_lo <= rsi <= rsi_hi
    if not (in_uptrend and pulled_back and bounce_zone):
        return None

    entry  = round(price, 2)
    smult  = float(get_section("pullback.stops_targets.stop_atr_mult", STOP_ATR_MULT) or STOP_ATR_MULT)
    buf    = atr * smult if not np.isnan(atr) and atr > 0 else support.price * 0.003
    stop   = round(support.price - buf, 2)
    target = round(resistance.price * 0.999, 2)   # exit just before resistance
    risk, reward = entry - stop, target - entry
    if risk <= 0 or reward <= 0:
        return None
    rr = round(reward / risk, 2)
    if rr < min_rr:
        return None

    # ── Quality score — ranks the "best" pullback to enter ────────────────────
    score, reasons = 0, []
    tch = support.touches
    if   tch >= 4: score += int(sc.get("support_4_touch", 22)); reasons.append(f"{tch}x-tested support")
    elif tch == 3: score += int(sc.get("support_3_touch", 15)); reasons.append("3x-tested support")
    elif tch == 2: score += int(sc.get("support_2_touch",  8)); reasons.append("2x-tested support")

    if ((not np.isnan(ema20) and abs(support.price - ema20) / support.price < 0.006) or
            (not np.isnan(ema50) and abs(support.price - ema50) / support.price < 0.006)):
        score += int(sc.get("ma_confluence", 12)); reasons.append("support on a moving average")

    if not np.isnan(ema20) and not np.isnan(ema50) and ema20 > ema50:
        score += int(sc.get("ema_stack", 8)); reasons.append("EMA20>EMA50 uptrend")
    if not np.isnan(ema50) and price > ema50:
        score += int(sc.get("above_ema50", 6))
    if not np.isnan(adx):
        if   adx >= 30: score += int(sc.get("adx_strong",   10)); reasons.append(f"strong trend ADX {adx:.0f}")
        elif adx >= 20: score += int(sc.get("adx_moderate",  6))

    vol = df["volume"].values.astype(float)
    if len(vol) >= 20:
        recent, base = float(np.mean(vol[-3:])), float(np.mean(vol[-20:]))
        if base > 0 and recent < base * 0.9:
            score += int(sc.get("vol_dry_pullback", 8)); reasons.append("volume drying up on the dip")
    if not np.isnan(rsi) and 40 <= rsi <= 52:
        score += int(sc.get("rsi_reset", 8)); reasons.append(f"RSI reset to {rsi:.0f}")

    if float(d["close"]) > float(d["open"]):
        score += int(sc.get("bullish_bar", 8)); reasons.append("bullish bar off support")
    if not np.isnan(rsi) and rsi > rsi_p:
        score += int(sc.get("rsi_turning_up", 6)); reasons.append("RSI turning up")
    if not np.isnan(ema20) and price > ema20:
        score += int(sc.get("reclaim_ema20", 6))

    if not np.isnan(vr):
        if   vr >= 1.5: score += int(sc.get("vol_bounce_1_5", 10)); reasons.append(f"volume {vr:.1f}x on the bounce")
        elif vr >= 1.2: score += int(sc.get("vol_bounce_1_2",  6))

    if   rr >= 3: score += int(sc.get("rr_3", 14)); reasons.append(f"R:R {rr:.1f}:1")
    elif rr >= 2: score += int(sc.get("rr_2",  8))

    if   prox <= 0.003: score += int(sc.get("prox_tight", 6)); reasons.append("right at support")
    elif prox <= 0.006: score += int(sc.get("prox_mid",   3))

    if score < int(hg.get("min_score", 45)):
        return None

    t3, t2 = int(sc.get("three_star", 75)), int(sc.get("two_star", 55))
    stars = 3 if score >= t3 else 2 if score >= t2 else 1
    stop_pct = round(risk / entry * 100, 1)
    gain_pct = round(reward / entry * 100, 1)
    why = (f"Score {score} — " + "; ".join(reasons[:4]) + ". "
           f"Enter at support ${support.price:.2f} (where buyers step in), target resistance "
           f"${resistance.price:.2f} (where sellers appear). Stop ${stop:.2f} ({stop_pct}%), "
           f"+{gain_pct}%, R:R {rr:.1f}:1.")

    return PullbackSignal(
        ticker=ticker, price=entry, entry=entry, stop=stop, target=target,
        stop_pct=stop_pct, gain_pct=gain_pct, rr=rr,
        support=support.price, resistance=resistance.price,
        stars=stars, score=score, why=why, levels=(sup[:5] + res[:5]),
    )


def scan_and_alert(tickers, interval: str = "5m", cooldown: int = 1800) -> int:
    """Scan tickers for pullback setups; fire throttled Telegram alerts. Returns # sent."""
    from alerts import _send, _is_throttled, _record_alert
    sent = 0
    for t in tickers:
        try:
            sig = scan_pullback(t, interval=interval)
        except Exception:
            continue
        if not sig:
            continue
        key = f"pullback_{t}"
        if _is_throttled(key, secs=cooldown):
            continue
        msg = (
            f"🎯 <b>PULLBACK — {t}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{'⭐'*sig.stars}  ${sig.price:.2f}\n"
            f"Enter @ support   ${sig.entry:.2f}  (where buyers step in)\n"
            f"🛑 Stop           ${sig.stop:.2f}  ({sig.stop_pct}%)\n"
            f"🎯 Target @ res   ${sig.target:.2f}  (+{sig.gain_pct}%)\n"
            f"R:R               {sig.rr:.1f}:1\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>{sig.why}</i>"
        )
        if _send(msg):
            _record_alert(key)
            sent += 1
    return sent


def to_plan(sig: PullbackSignal, qty: int, env: str = "SIMULATE") -> dict:
    """Map a signal to the moomoo_integration execute_trade_plan() format (single target)."""
    return {
        "plan_id":       f"pb_{sig.ticker}",
        "ticker":        sig.ticker,
        "qty":           int(qty),
        "qty_remaining": int(qty),
        "entry_price":   sig.entry,
        "stop_price":    sig.stop,
        "target1_price": sig.target,
        "target2_price": sig.target,   # single target for day trades
        "atr":           round((sig.entry - sig.stop) / max(STOP_ATR_MULT, 0.1), 2),
        "trd_env":       env,
        "status":        "pending",
        "orders":        {"add_order_ids": []},
    }
