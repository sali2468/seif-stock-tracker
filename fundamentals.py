"""
Fundamental data engine — powered by Finnhub (free tier).
Covers: analyst targets, earnings dates, news sentiment,
        insider transactions, basic financials.

All data cached to avoid hammering the API.
Cache TTL: 4 hours for most data, 30 min for news.
"""

import os, time
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
_KEY = os.getenv("FINNHUB_API_KEY", "")

_MEM: dict = {}   # in-memory cache  {key: {data, ts}}

def _cached(key: str, ttl: int):
    """Return cached value if fresh, else None."""
    e = _MEM.get(key)
    if e and (time.time() - e["ts"]) < ttl:
        return e["data"]
    return None

def _store(key: str, data):
    _MEM[key] = {"data": data, "ts": time.time()}
    return data

def _fh():
    """Return a Finnhub client, or None if no key."""
    if not _KEY:
        return None
    try:
        import finnhub
        return finnhub.Client(api_key=_KEY)
    except Exception:
        return None


# ── Analyst price targets ─────────────────────────────────────────────────────

def get_analyst_targets(ticker: str) -> dict:
    """
    Returns:
      target_mean, target_high, target_low, num_analysts,
      recommendation  ('strong_buy'|'buy'|'hold'|'sell'|'strong_sell')
      rating_score    (1=strong_buy … 5=strong_sell)

    Source: yfinance (free, no API key required).
    """
    key = f"analyst_{ticker}"
    cached = _cached(key, 4 * 3600)
    if cached:
        return cached

    empty = {
        "target_mean": None, "target_high": None, "target_low": None,
        "num_analysts": 0, "recommendation": "hold", "rating_score": 3,
    }

    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info

        def _safe(k):
            v = info.get(k)
            try:
                return round(float(v), 2) if v else None
            except Exception:
                return None

        result = dict(empty)
        result["target_mean"]   = _safe("targetMeanPrice")
        result["target_high"]   = _safe("targetHighPrice")
        result["target_low"]    = _safe("targetLowPrice")
        result["num_analysts"]  = int(info.get("numberOfAnalystOpinions") or 0)

        raw_rec = (info.get("recommendationKey") or "hold").lower()
        # yfinance returns: strong_buy, buy, hold, underperform, sell, strong_sell
        rec_map = {
            "strong_buy": (1.5, "strong_buy"),
            "buy":        (2.0, "buy"),
            "hold":       (3.0, "hold"),
            "underperform": (4.0, "sell"),
            "sell":       (4.5, "sell"),
            "strong_sell":(5.0, "strong_sell"),
        }
        score_val, rec_label = rec_map.get(raw_rec, (3.0, "hold"))
        result["rating_score"]    = score_val
        result["recommendation"]  = rec_label

        return _store(key, result)
    except Exception:
        return _store(key, empty)


# ── Earnings calendar ─────────────────────────────────────────────────────────

def get_earnings_info(ticker: str) -> dict:
    """
    Returns:
      next_date       (str YYYY-MM-DD or None)
      days_until      (int, negative = already passed)
      last_surprise   (float % — positive means beat, negative means miss)
      last_eps_actual (float)
      last_eps_est    (float)
    """
    key = f"earnings_{ticker}"
    cached = _cached(key, 4 * 3600)
    if cached:
        return cached

    empty = {
        "next_date": None, "days_until": 999,
        "last_surprise": None, "last_eps_actual": None, "last_eps_est": None,
    }

    fh = _fh()
    if not fh:
        return empty

    try:
        today     = date.today()
        from_date = today.isoformat()
        to_date   = (today + timedelta(days=90)).isoformat()
        cal       = fh.earnings_calendar(symbol=ticker, _from=from_date, to=to_date)
        earnings_list = cal.get("earningsCalendar", [])

        result = dict(empty)
        if earnings_list:
            next_e = earnings_list[0]
            ndate  = next_e.get("date")
            if ndate:
                result["next_date"]  = ndate
                result["days_until"] = (date.fromisoformat(ndate) - today).days

        # Last quarter surprise
        hist = fh.company_earnings(ticker, limit=4)
        if hist:
            last = hist[0]
            actual = last.get("actual")
            est    = last.get("estimate")
            if actual is not None and est and est != 0:
                result["last_surprise"]   = round((actual - est) / abs(est) * 100, 1)
                result["last_eps_actual"] = actual
                result["last_eps_est"]    = est

        return _store(key, result)
    except Exception:
        return _store(key, empty)


# ── News sentiment ────────────────────────────────────────────────────────────

def get_news_sentiment(ticker: str) -> dict:
    """
    Returns:
      score          (-1.0 very negative … +1.0 very positive)
      label          ('positive'|'neutral'|'negative')
      article_count  (how many articles in last 7 days)
      headlines      (list of up to 5 recent headline strings)
    """
    key = f"news_{ticker}"
    cached = _cached(key, 1800)   # 30-min cache for news
    if cached:
        return cached

    empty = {"score": 0.0, "label": "neutral", "article_count": 0, "headlines": []}

    fh = _fh()
    if not fh:
        return empty

    try:
        today    = date.today()
        from_d   = (today - timedelta(days=7)).isoformat()
        to_d     = today.isoformat()
        articles = fh.company_news(ticker, _from=from_d, to=to_d)

        if not articles:
            return _store(key, empty)

        headlines = [a.get("headline", "") for a in articles[:10] if a.get("headline")]

        # Compute sentiment from headline keywords (free-tier compatible)
        _bull = ["beat","surge","soar","rally","upgrade","buy","strong","record",
                 "profit","growth","raised","above","exceeds","positive","bullish",
                 "wins","awarded","partnership","deal","launch","breakthrough"]
        _bear = ["miss","fall","drop","cut","downgrade","sell","weak","loss","below",
                 "warns","concern","risk","decline","layoff","probe","investigation",
                 "lawsuit","bearish","crash","volatile","disappoints","negative"]

        bull_n = sum(1 for h in headlines for w in _bull if w in h.lower())
        bear_n = sum(1 for h in headlines for w in _bear if w in h.lower())
        total  = bull_n + bear_n or 1
        score  = (bull_n - bear_n) / total
        score  = max(-1.0, min(1.0, score))
        label  = "positive" if score > 0.1 else "negative" if score < -0.1 else "neutral"

        result = {
            "score":         round(score, 3),
            "label":         label,
            "article_count": len(articles),
            "headlines":     headlines[:5],
        }
        return _store(key, result)
    except Exception:
        return _store(key, empty)


# ── Insider transactions ──────────────────────────────────────────────────────

def get_insider_signal(ticker: str) -> dict:
    """
    Returns:
      net_shares     (positive = net buying, negative = net selling, last 90 days)
      signal         ('buying'|'selling'|'neutral')
      transactions   (count in last 90 days)
      summary        (plain English string)
    """
    key = f"insider_{ticker}"
    cached = _cached(key, 12 * 3600)  # 12-hour cache
    if cached:
        return cached

    empty = {"net_shares": 0, "signal": "neutral", "transactions": 0, "summary": "No recent insider activity."}

    fh = _fh()
    if not fh:
        return empty

    try:
        data   = fh.stock_insider_transactions(ticker)
        txns   = data.get("data", []) if data else []
        cutoff = (date.today() - timedelta(days=90)).isoformat()
        recent = [t for t in txns if t.get("transactionDate", "") >= cutoff]

        if not recent:
            return _store(key, empty)

        net = sum(
            t.get("share", 0) if t.get("transactionCode") in ("P", "A") else -t.get("share", 0)
            for t in recent
        )

        signal = "buying" if net > 5000 else "selling" if net < -5000 else "neutral"

        if signal == "buying":
            summary = f"Insiders net bought {abs(net):,.0f} shares in last 90 days — management is putting their own money in."
        elif signal == "selling":
            summary = f"Insiders net sold {abs(net):,.0f} shares in last 90 days — worth noting but not always bearish."
        else:
            summary = "Insider activity is minimal — no strong signal either way."

        result = {
            "net_shares":   int(net),
            "signal":       signal,
            "transactions": len(recent),
            "summary":      summary,
        }
        return _store(key, result)
    except Exception:
        return _store(key, empty)


# ── Basic financials ──────────────────────────────────────────────────────────

def get_basic_financials(ticker: str) -> dict:
    """
    Returns key financial health metrics:
      pe_ratio, revenue_growth_yoy, profit_margin,
      debt_equity, roe (return on equity), market_cap_b (billions)
    """
    key = f"fins_{ticker}"
    cached = _cached(key, 6 * 3600)
    if cached:
        return cached

    empty = {
        "pe_ratio": None, "revenue_growth_yoy": None,
        "profit_margin": None, "debt_equity": None,
        "roe": None, "market_cap_b": None,
    }

    fh = _fh()
    if not fh:
        return empty

    try:
        data   = fh.company_basic_financials(ticker, "all")
        metric = data.get("metric", {})

        def _g(k):
            v = metric.get(k)
            return round(float(v), 2) if v is not None else None

        result = {
            "pe_ratio":           _g("peBasicExclExtraTTM"),
            "revenue_growth_yoy": _g("revenueGrowthTTMYoy"),
            "profit_margin":      _g("netProfitMarginTTM"),
            "debt_equity":        _g("totalDebt/totalEquityAnnual"),
            "roe":                _g("roeRfy"),
            "market_cap_b":       _g("marketCapitalization"),
        }
        return _store(key, result)
    except Exception:
        return _store(key, empty)


# ── Combined fundamental snapshot ────────────────────────────────────────────

def get_fundamental_snapshot(ticker: str) -> dict:
    """
    One call returns everything. Used by signals.py and chart_analysis.py.
    """
    analyst  = get_analyst_targets(ticker)
    earnings = get_earnings_info(ticker)
    news     = get_news_sentiment(ticker)
    insider  = get_insider_signal(ticker)
    fins     = get_basic_financials(ticker)

    # Composite fundamental score  (-10 … +10)
    fscore = 0

    # Analyst recommendation
    r = analyst.get("rating_score", 3)
    if r <= 1.5:   fscore += 3
    elif r <= 2.5: fscore += 2
    elif r >= 4.5: fscore -= 3
    elif r >= 3.5: fscore -= 1

    # News sentiment
    ns = news.get("score", 0)
    if ns > 0.3:    fscore += 2
    elif ns > 0.1:  fscore += 1
    elif ns < -0.3: fscore -= 2
    elif ns < -0.1: fscore -= 1

    # Insider signal
    if insider.get("signal") == "buying":   fscore += 2
    elif insider.get("signal") == "selling": fscore -= 1

    # Revenue growth
    rg = fins.get("revenue_growth_yoy")
    if rg is not None:
        if rg > 20:   fscore += 2
        elif rg > 10: fscore += 1
        elif rg < 0:  fscore -= 2

    # Earnings surprise
    surprise = earnings.get("last_surprise")
    if surprise is not None:
        if surprise > 10:  fscore += 1
        elif surprise < -10: fscore -= 1

    fscore = max(-10, min(10, fscore))

    return {
        "analyst":      analyst,
        "earnings":     earnings,
        "news":         news,
        "insider":      insider,
        "financials":   fins,
        "fscore":       fscore,   # -10 (very bearish) … +10 (very bullish)
    }


def fundamental_summary_text(snap: dict, price: float) -> str:
    """Returns a concise plain-English paragraph for use in alerts/analysis."""
    lines = []

    # Analyst target
    a = snap["analyst"]
    if a["target_mean"] and a["num_analysts"] > 0:
        upside = round((a["target_mean"] - price) / price * 100, 1)
        rec    = a["recommendation"].replace("_", " ").title()
        lines.append(
            f"Wall St ({a['num_analysts']} analysts): <b>{rec}</b>, "
            f"avg target <b>${a['target_mean']:.2f}</b> ({upside:+.1f}% from here)"
        )

    # Earnings
    e = snap["earnings"]
    if e["next_date"]:
        dte = e["days_until"]
        if dte <= 0:
            lines.append(f"⚠️ Earnings were <b>very recently</b> — results may already be priced in")
        elif dte <= 7:
            lines.append(f"🚨 <b>Earnings in {dte} days</b> ({e['next_date']}) — high binary risk, consider reducing size")
        elif dte <= 21:
            lines.append(f"📅 Earnings in {dte} days ({e['next_date']}) — position before then or reduce ahead of it")
    if e["last_surprise"] is not None:
        icon = "✅" if e["last_surprise"] > 0 else "❌"
        lines.append(f"{icon} Last earnings: {e['last_surprise']:+.1f}% surprise vs estimates")

    # News
    n = snap["news"]
    if n["article_count"] > 0:
        icon = "📰✅" if n["label"] == "positive" else "📰❌" if n["label"] == "negative" else "📰"
        lines.append(f"{icon} News ({n['article_count']} articles, 7d): <b>{n['label']}</b> sentiment")
        if n["headlines"]:
            lines.append(f"   <i>\"{n['headlines'][0][:90]}\"</i>")

    # Insider
    ins = snap["insider"]
    if ins["signal"] != "neutral":
        icon = "🟢" if ins["signal"] == "buying" else "🔴"
        lines.append(f"{icon} Insiders: {ins['summary']}")

    # Financials
    fins = snap["financials"]
    fin_parts = []
    if fins["pe_ratio"]:
        fin_parts.append(f"P/E {fins['pe_ratio']:.1f}")
    if fins["revenue_growth_yoy"] is not None:
        fin_parts.append(f"revenue growth {fins['revenue_growth_yoy']:+.1f}%/yr")
    if fins["profit_margin"] is not None:
        fin_parts.append(f"margin {fins['profit_margin']:.1f}%")
    if fin_parts:
        lines.append(f"📊 Financials: {', '.join(fin_parts)}")

    return "\n".join(lines) if lines else "No fundamental data available."
