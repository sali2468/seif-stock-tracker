"""swing_engine.py — multi-factor swing-trade scoring.

Takes the technical candidates the scanner already finds (pullback / reversal /
swing setups) and enriches each with a CONSISTENT set of factors:

    technical setup · market regime · insider buying · analyst ratings · financials/trajectory

…to produce one composite 0-100 score, a ⭐ star rating, and a plain-English list
of WHY it made the list. The slow-moving factors (analyst / insider / fundamentals)
are cached with a long TTL so the recommendations stay consistent day-to-day
instead of churning — which was the whole point of the redesign.
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta

import requests

log = logging.getLogger("swing_engine")

_KEY  = os.getenv("FINNHUB_API_KEY", "")
_BASE = "https://finnhub.io/api/v1"
_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".swing_factors_cache.json")
_CACHE_TTL  = 18 * 3600            # 18h — analyst/insider/fundamentals move slowly

# Blend weights (sum = 1.0)
_W = {"technical": 0.35, "analyst": 0.20, "trajectory": 0.20, "insider": 0.15, "market": 0.10}

# ── factor cache (file-based, TTL) ────────────────────────────────────────────
_cache = None


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(_CACHE_FILE) as f:
                _cache = json.load(f)
        except Exception:
            _cache = {}
    return _cache


def _save_cache() -> None:
    try:
        with open(_CACHE_FILE, "w") as f:
            json.dump(_cache, f)
    except Exception:
        pass


def _fh(path: str, **params):
    params["token"] = _KEY
    r = requests.get(f"{_BASE}/{path}", params=params, timeout=12)
    r.raise_for_status()
    return r.json()


def _factors(ticker: str) -> dict:
    """Fetch (cached) the slow-moving factor data: analyst recs, insider txns, metrics."""
    c = _load_cache()
    key = ticker.upper()
    rec = c.get(key)
    if rec and (time.time() - rec.get("_ts", 0) < _CACHE_TTL):
        return rec
    data = {"_ts": time.time(), "analyst": None, "insider": [], "metric": {}}
    if _KEY:
        try:
            recs = _fh("stock/recommendation", symbol=ticker)
            data["analyst"] = recs[0] if recs else None
        except Exception as e:
            log.debug("analyst fetch %s: %s", ticker, e)
        try:
            ins = _fh("stock/insider-transactions", symbol=ticker)
            data["insider"] = (ins or {}).get("data", []) or []
        except Exception as e:
            log.debug("insider fetch %s: %s", ticker, e)
        try:
            m = _fh("stock/metric", symbol=ticker, metric="all")
            data["metric"] = (m or {}).get("metric", {}) or {}
        except Exception as e:
            log.debug("metric fetch %s: %s", ticker, e)
    c[key] = data
    _save_cache()
    return data


def _first(m: dict, *keys):
    for k in keys:
        v = m.get(k)
        if v is not None:
            return v
    return None


# ── individual factor scores (each 0-100) ─────────────────────────────────────

def _analyst_score(a):
    if not a:
        return 50, None
    sb, b = a.get("strongBuy", 0) or 0, a.get("buy", 0) or 0
    h, s, ss = a.get("hold", 0) or 0, a.get("sell", 0) or 0, a.get("strongSell", 0) or 0
    total = sb + b + h + s + ss
    if total == 0:
        return 50, None
    score = (sb * 1.0 + b * 0.75 + h * 0.5 + s * 0.25 + ss * 0.0) / total * 100
    bull = sb + b
    reason = f"{bull}/{total} analysts rate Buy+" if bull >= max(1, total * 0.5) else None
    return round(score), reason


def _insider_score(ins):
    """Score recent OPEN-MARKET purchases (code P) vs sales (code S). Grants/gifts/options ignored."""
    if not ins:
        return 50, None
    cutoff = (datetime.utcnow() - timedelta(days=180)).strftime("%Y-%m-%d")
    buys = sells = 0
    buy_sh = sell_sh = 0
    for t in ins:
        d = t.get("transactionDate") or ""
        if d < cutoff:
            continue
        code = (t.get("transactionCode") or "").upper()
        chg = t.get("change") or 0
        if code == "P" and chg > 0:
            buys += 1; buy_sh += chg
        elif code == "S" and chg < 0:
            sells += 1; sell_sh += -chg
    if buys == 0 and sells == 0:
        return 50, None
    # Insider BUYING is the strong bullish signal — insiders rarely buy unless confident.
    if buys > 0 and buy_sh >= sell_sh:
        return min(95, 68 + buys * 4), f"insiders buying ({buys} open-market purchase{'s' if buys != 1 else ''})"
    # Open-market SELLING at large caps is mostly noise (scheduled 10b5-1 plans) — keep it a
    # soft, quiet factor: mild ding only when persistent, and never a scary headline reason.
    if sells >= 8 and buys == 0:
        return 45, None
    return 50, None


def _trajectory_score(m):
    """Growth + profitability + relative strength from fundamentals."""
    if not m:
        return 50, []
    score = 50.0
    reasons = []
    rg = _first(m, "revenueGrowthTTMYoy", "revenueGrowthQuarterlyYoy", "revenueGrowth3Y")
    if rg is not None:
        if rg >= 20:   score += 15; reasons.append(f"revenue +{rg:.0f}%")
        elif rg >= 5:  score += 8;  reasons.append(f"revenue +{rg:.0f}%")
        elif rg < 0:   score -= 12; reasons.append(f"revenue {rg:.0f}%")
    eg = _first(m, "epsGrowthTTMYoy", "epsGrowthQuarterlyYoy", "epsGrowth3Y")
    if eg is not None:
        if eg >= 20:   score += 12; reasons.append(f"EPS +{eg:.0f}%")
        elif eg >= 5:  score += 6
        elif eg < 0:   score -= 8
    npm = _first(m, "netProfitMarginTTM", "netProfitMarginAnnual")
    if npm is not None and npm >= 10:
        score += 6; reasons.append(f"net margin {npm:.0f}%")
    rel = _first(m, "priceRelativeToS&P50052Week")
    perf = _first(m, "52WeekPriceReturnDaily")
    if (rel is not None and rel > 0) or (perf is not None and perf > 0):
        score += 6; reasons.append("outperforming over 52w")
    elif perf is not None and perf < -20:
        score -= 6
    return max(0, min(100, round(score))), reasons


def _market_score(regime):
    r = (regime or "").upper()
    if "BULL" in r and "QUIET" in r: return 78, "calm bull market"
    if "BULL" in r:                  return 66, "bullish market"
    if "BEAR" in r:                  return 30, "bearish market — caution"
    return 50, None


# ── composite ─────────────────────────────────────────────────────────────────

def score_swing(ticker: str, technical: float, technical_reason: str = "",
                regime: str = "") -> dict:
    """Blend the technical setup (0-100) with market + analyst + insider + fundamentals.
    Returns {ticker, score, stars, factors, reasons}."""
    f = _factors(ticker)
    a_s, a_r = _analyst_score(f.get("analyst"))
    i_s, i_r = _insider_score(f.get("insider"))
    t_s, t_rs = _trajectory_score(f.get("metric"))
    m_s, m_r = _market_score(regime)
    tech = max(0.0, min(100.0, float(technical)))

    composite = (tech * _W["technical"] + a_s * _W["analyst"] + t_s * _W["trajectory"]
                 + i_s * _W["insider"] + m_s * _W["market"])
    stars = 3 if composite >= 72 else 2 if composite >= 56 else 1

    reasons = [r for r in ([technical_reason, a_r, i_r] + t_rs + [m_r]) if r]
    return {
        "ticker": ticker.upper(),
        "score":  round(composite),
        "stars":  stars,
        "factors": {"technical": round(tech), "analyst": a_s, "trajectory": t_s,
                    "insider": i_s, "market": m_s},
        "reasons": reasons[:5],
    }


def rank_swing(candidates: list, regime: str = "") -> list:
    """candidates: list of (ticker, technical_0_100, technical_reason).
    Returns enriched dicts sorted best→worst (score desc, then stars)."""
    seen, out = set(), []
    for tk, tech, why in candidates:
        u = (tk or "").upper()
        if not u or u in seen:
            continue
        seen.add(u)
        try:
            out.append(score_swing(u, tech, why or "", regime))
        except Exception as e:
            log.debug("score_swing %s: %s", u, e)
    out.sort(key=lambda d: (d["score"], d["stars"]), reverse=True)
    return out
