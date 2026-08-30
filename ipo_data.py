"""
ipo_data.py — IPO calendar + company detail fetcher.
Uses Finnhub API. Prices are fetched lazily (not on initial load).
"""

import os
import requests
import logging
from datetime import date, timedelta

log = logging.getLogger("ipo_data")

FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "")
BASE_URL    = "https://finnhub.io/api/v1"

MONTHS = [
    "January","February","March","April","May","June",
    "July","August","September","October","November","December",
]


def _fh(endpoint: str, params: dict) -> dict:
    params["token"] = FINNHUB_KEY
    try:
        r = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"Finnhub {endpoint}: {e}")
        return {}


# ── Calendar ──────────────────────────────────────────────────────────────────

def get_ipo_year(year: int = None) -> dict:
    """
    Fetch all IPOs for the given year. Returns dict keyed by month number (1-12),
    each value is a list of IPO dicts. Fast — no per-ticker price calls.
    """
    if year is None:
        year = date.today().year

    data = _fh("calendar/ipo", {
        "from": f"{year}-01-01",
        "to":   f"{year}-12-31",
    })
    ipos = data.get("ipoCalendar", [])

    by_month = {m: [] for m in range(1, 13)}

    for ipo in ipos:
        status = (ipo.get("status") or "").lower()
        if status == "withdrawn":
            continue
        date_str = ipo.get("date", "")
        try:
            d = date.fromisoformat(date_str)
        except Exception:
            continue

        price_str = ipo.get("price") or ""
        lo, hi, mid = _parse_price_range(price_str)
        shares    = ipo.get("numberOfShares") or 0
        total_val = ipo.get("totalSharesValue") or 0
        today     = date.today()

        entry = {
            "date":        date_str,
            "name":        ipo.get("name") or "—",
            "ticker":      ipo.get("symbol") or "",
            "exchange":    ipo.get("exchange") or "",
            "price_range": price_str or "TBD",
            "price_low":   lo,
            "price_high":  hi,
            "price_mid":   mid,
            "shares":      _fmt_shares(shares),
            "shares_raw":  shares,
            "mkt_cap":     _fmt_val(total_val),
            "mkt_cap_raw": total_val,
            "status":      status or "expected",
            "is_past":     d < today,
            "days_away":   (d - today).days,
        }
        by_month[d.month].append(entry)

    # Sort each month by date
    for m in by_month:
        by_month[m].sort(key=lambda x: x["date"])

    return by_month


# ── Company detail (lazy — called only when user clicks an IPO) ───────────────

def get_ipo_details(ticker: str, price_mid: float = 0.0) -> dict:
    """
    Full detail for one IPO company. Called lazily when user expands a row.
    Returns: profile, quote, news, financials, sentiment.
    """
    if not ticker:
        return {}

    # Run all requests (profile is the most important)
    profile  = _fh("stock/profile2",  {"symbol": ticker})
    quote    = _fh("quote",           {"symbol": ticker})
    metrics  = _fh("stock/metric",    {"symbol": ticker, "metric": "all"})
    today    = date.today()
    news_raw = _fh("company-news",    {
        "symbol": ticker,
        "from":   (today - timedelta(days=60)).isoformat(),
        "to":     today.isoformat(),
    })

    # ── Quote ────────────────────────────────────────────────────────────────
    cur_price  = float(quote.get("c") or 0)
    prev_close = float(quote.get("pc") or cur_price)
    day_pct    = round((cur_price - prev_close) / prev_close * 100, 2) if prev_close else 0
    ipo_return = round((cur_price - price_mid) / price_mid * 100, 1) if (price_mid and cur_price) else None

    # ── Key metrics ──────────────────────────────────────────────────────────
    m = metrics.get("metric") or {}
    fin_metrics = {
        "52w High":        _fmt_price(m.get("52WeekHigh")),
        "52w Low":         _fmt_price(m.get("52WeekLow")),
        "P/E (TTM)":       _fmt_num(m.get("peBasicExclExtraTTM")),
        "EPS (TTM)":       _fmt_price(m.get("epsBasicExclExtraItemsTTM")),
        "Revenue/Share":   _fmt_price(m.get("revenuePerShareTTM")),
        "Net Margin":      _fmt_pct(m.get("netProfitMarginTTM")),
        "ROE":             _fmt_pct(m.get("roeTTM")),
        "Debt/Equity":     _fmt_num(m.get("totalDebt/totalEquityAnnual")),
        "Current Ratio":   _fmt_num(m.get("currentRatioAnnual")),
        "Beta":            _fmt_num(m.get("beta")),
    }

    # ── News (top 6) ─────────────────────────────────────────────────────────
    news = []
    if isinstance(news_raw, list):
        for n in news_raw[:6]:
            news.append({
                "headline": n.get("headline", ""),
                "source":   n.get("source", ""),
                "url":      n.get("url", ""),
                "datetime": n.get("datetime", 0),
                "summary":  n.get("summary", ""),
            })

    return {
        "profile":     profile,
        "cur_price":   cur_price,
        "day_pct":     day_pct,
        "ipo_return":  ipo_return,
        "fin_metrics": fin_metrics,
        "news":        news,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_price_range(s: str):
    try:
        s = str(s).replace("$", "").strip()
        if "-" in s:
            lo, hi = s.split("-", 1)
            lo, hi = float(lo.strip()), float(hi.strip())
            return lo, hi, round((lo + hi) / 2, 2)
        v = float(s)
        return v, v, v
    except Exception:
        return 0.0, 0.0, 0.0


def _fmt_shares(n):
    if not n: return "—"
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000:     return f"{n/1_000:.0f}K"
    return str(n)


def _fmt_val(v):
    if not v: return "—"
    if v >= 1e9:  return f"${v/1e9:.1f}B"
    if v >= 1e6:  return f"${v/1e6:.0f}M"
    return f"${v:,.0f}"


def _fmt_price(v):
    if v is None: return "—"
    try: return f"${float(v):.2f}"
    except: return "—"


def _fmt_num(v):
    if v is None: return "—"
    try: return f"{float(v):.2f}"
    except: return "—"


def _fmt_pct(v):
    if v is None: return "—"
    try: return f"{float(v)*100:.1f}%"
    except: return "—"
