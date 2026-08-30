"""
Market data + indicator engine for the tracker.
Pure yfinance + pure pandas/numpy — zero native dependencies, no llvmlite/numba.
"""

import warnings
warnings.filterwarnings("ignore")

from datetime import datetime
from typing import Optional
import pandas as pd
import numpy as np
import yfinance as yf


# ── Pure-python indicator implementations ─────────────────────────────────────

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _macd(close: pd.Series, fast=12, slow=26, signal=9):
    macd_line = _ema(close, fast) - _ema(close, slow)
    sig_line  = _ema(macd_line, signal)
    hist      = macd_line - sig_line
    return macd_line, sig_line, hist


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    up   = high.diff()
    down = -low.diff()
    plus_dm  = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    atr_s = _atr(high, low, close, period)
    plus_di  = 100 * pd.Series(plus_dm,  index=close.index).ewm(alpha=1/period, adjust=False).mean() / atr_s
    minus_di = 100 * pd.Series(minus_dm, index=close.index).ewm(alpha=1/period, adjust=False).mean() / atr_s
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    return dx.ewm(alpha=1/period, adjust=False).mean()


def _bbands(close: pd.Series, period: int = 20, std: float = 2.0):
    mid   = close.rolling(period).mean()
    sigma = close.rolling(period).std()
    return mid + std * sigma, mid, mid - std * sigma

# ── Option price fetching ─────────────────────────────────────────────────────

def get_option_price(underlying: str, opt_type: str,
                     strike: float, expiry: str) -> Optional[float]:
    """
    Fetch the current mid-price of an option contract via yfinance.
    opt_type = "call" or "put"
    expiry   = "YYYY-MM-DD"
    Returns price per share (multiply by 100 for dollar value per contract).
    """
    try:
        t     = yf.Ticker(underlying)
        chain = t.option_chain(expiry)
        df    = chain.calls if opt_type.lower() == "call" else chain.puts
        row   = df[df["strike"] == strike]
        if row.empty:
            # Try nearest strike
            row = df.iloc[(df["strike"] - strike).abs().argsort()[:1]]
        if row.empty:
            return None
        bid = float(row["bid"].iloc[0])
        ask = float(row["ask"].iloc[0])
        last = float(row["lastPrice"].iloc[0])
        # Use mid-price if bid/ask are valid, else lastPrice
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2, 4)
        return round(last, 4) if last > 0 else None
    except Exception:
        return None


def get_option_expiries(underlying: str) -> list:
    """Return list of available expiry date strings for a ticker."""
    try:
        return list(yf.Ticker(underlying).options)
    except Exception:
        return []


def days_to_expiry(expiry: str) -> int:
    """How many calendar days until expiry."""
    from datetime import date
    try:
        exp = date.fromisoformat(expiry)
        return (exp - date.today()).days
    except Exception:
        return 999


# ── Company info cache (24-hour file cache) ───────────────────────────────────
import json as _json
import os as _os

_INFO_CACHE_FILE = _os.path.join(_os.path.dirname(__file__), "company_cache.json")
_INFO_TTL = 86400  # 24 hours


def _load_info_cache() -> dict:
    try:
        with open(_INFO_CACHE_FILE, "r") as f:
            return _json.load(f)
    except Exception:
        return {}


def _save_info_cache(cache: dict):
    try:
        with open(_INFO_CACHE_FILE, "w") as f:
            _json.dump(cache, f, indent=2)
    except Exception:
        pass


def _first_sentences(text: str, n: int = 2) -> str:
    if not text:
        return ""
    parts = [s.strip() for s in text.replace("\n", " ").split(".") if s.strip()]
    return ". ".join(parts[:n]) + ("." if parts else "")


def search_symbols(query: str, limit: int = 10) -> list:
    """
    Search Finnhub for tickers matching a company name or partial ticker.
    Returns list of dicts: {symbol, name, type, exchange}
    """
    if not query or len(query.strip()) < 2:
        return []
    api_key = _os.getenv("FINNHUB_API_KEY", "")
    if not api_key:
        return []
    try:
        import requests as _req
        r = _req.get(
            "https://finnhub.io/api/v1/search",
            params={"q": query.strip(), "token": api_key},
            timeout=5,
        )
        r.raise_for_status()
        results = r.json().get("result", [])
        # Filter to US-listed common stocks and ETFs only, limit results
        filtered = [
            {
                "symbol":   x.get("symbol", ""),
                "name":     x.get("description", ""),
                "type":     x.get("type", ""),
                "exchange": x.get("displaySymbol", ""),
            }
            for x in results
            if x.get("type") in ("Common Stock", "ETP", "ETF", "ADR")
            and "." not in x.get("symbol", "")   # exclude non-US symbols like BRK.A
        ]
        return filtered[:limit]
    except Exception:
        return []


def get_company_info(ticker: str) -> dict:
    """
    Return {name, description, sector, industry} for a ticker.
    Cached to disk for 24 hours — fast on repeat calls.
    Falls back gracefully if yfinance has no data.
    """
    cache = _load_info_cache()
    entry = cache.get(ticker.upper(), {})

    if entry:
        age = datetime.now().timestamp() - entry.get("ts", 0)
        if age < _INFO_TTL:
            return entry

    result = {
        "name":        ticker.upper(),
        "description": "",
        "sector":      "",
        "industry":    "",
        "ts":          datetime.now().timestamp(),
    }
    try:
        info = yf.Ticker(ticker).info
        result["name"]        = info.get("longName") or info.get("shortName") or ticker.upper()
        result["sector"]      = info.get("sector",   "")
        result["industry"]    = info.get("industry", "")
        raw_desc = info.get("longBusinessSummary", "")
        result["description"] = _first_sentences(raw_desc, 2)
    except Exception:
        pass

    cache[ticker.upper()] = result
    _save_info_cache(cache)
    return result


# ── Live price source: Finnhub (real-time, no delay) ─────────────────────────
import os as _os
from dotenv import load_dotenv as _load_dotenv
_load_dotenv(_os.path.join(_os.path.dirname(__file__), ".env"))

_FINNHUB_KEY = _os.getenv("FINNHUB_API_KEY", "")
_finnhub_client = None

def _get_finnhub():
    global _finnhub_client
    if _FINNHUB_KEY and _finnhub_client is None:
        try:
            import finnhub
            _finnhub_client = finnhub.Client(api_key=_FINNHUB_KEY)
        except Exception:
            pass
    return _finnhub_client


# ── Price cache (short TTL for live prices, longer for bars) ─────────────────
_price_cache: dict = {}   # ticker → {price, ts}
_PRICE_TTL = 8            # seconds — refresh live price every 8 seconds

_cache: dict = {}
_CACHE_TTL_INTRADAY = 30   # seconds for 1m/5m/15m bars
_CACHE_TTL_DAILY    = 300  # seconds for daily bars


def _cache_key(ticker, period, interval):
    return f"{ticker}_{period}_{interval}"


def _bars_ttl(interval: str) -> int:
    return _CACHE_TTL_INTRADAY if interval in ("1m","2m","5m","15m","30m") else _CACHE_TTL_DAILY


def _is_fresh(key, ttl: int):
    if key not in _cache:
        return False
    return (datetime.now() - _cache[key]["ts"]).seconds < ttl


def get_bars(ticker: str, period: str = "1y", interval: str = "1d") -> Optional[pd.DataFrame]:
    key = _cache_key(ticker, period, interval)
    ttl = _bars_ttl(interval)
    if _is_fresh(key, ttl):
        return _cache[key]["df"]

    try:
        df = yf.download(ticker, period=period, interval=interval,
                         auto_adjust=True, progress=False, multi_level_index=False)
        if df is None or df.empty:
            return None
        df.columns = [c.lower() for c in df.columns]
        df = df[["open","high","low","close","volume"]].dropna()
        _cache[key] = {"df": df, "ts": datetime.now()}
        return df
    except Exception:
        return None


def get_bars_batch(tickers: list, period: str = "1y", interval: str = "1d") -> dict:
    """
    Download OHLCV data for multiple tickers in ONE yfinance request.
    Returns {ticker: df} — same format as get_bars() but ~10x faster for large lists.
    Also populates the individual _cache entries so get_bars() calls benefit too.
    """
    if not tickers:
        return {}

    ttl = _bars_ttl(interval)
    result  = {}
    missing = []

    # Return cached tickers without hitting the network
    for t in tickers:
        key = _cache_key(t, period, interval)
        if _is_fresh(key, ttl):
            result[t] = _cache[key]["df"]
        else:
            missing.append(t)

    if not missing:
        return result

    try:
        # Single request for all missing tickers
        raw = yf.download(
            missing, period=period, interval=interval,
            auto_adjust=True, progress=False, group_by="ticker",
        )
        now = datetime.now()

        if len(missing) == 1:
            # yfinance returns flat columns for a single ticker
            t = missing[0]
            try:
                df = raw.copy()
                df.columns = [c.lower() for c in df.columns]
                df = df[["open","high","low","close","volume"]].dropna()
                if not df.empty and len(df) >= 60:
                    _cache[_cache_key(t, period, interval)] = {"df": df, "ts": now}
                    result[t] = df
            except Exception:
                pass
        else:
            # Multi-ticker: top-level columns are ticker symbols
            for t in missing:
                try:
                    df = raw[t].copy()
                    df.columns = [c.lower() for c in df.columns]
                    df = df[["open","high","low","close","volume"]].dropna()
                    if not df.empty and len(df) >= 60:
                        _cache[_cache_key(t, period, interval)] = {"df": df, "ts": now}
                        result[t] = df
                except Exception:
                    pass
    except Exception:
        # Fall back to individual downloads if batch fails
        for t in missing:
            df = get_bars(t, period, interval)
            if df is not None:
                result[t] = df

    return result


def get_batch_quotes(tickers: list, fallback: bool = True) -> dict:
    """
    Fetch live quotes for multiple tickers in parallel using Finnhub.
    Returns {ticker: {"price": float, "prev_close": float, "open": float,
                       "high": float, "low": float, "pct": float}}

    fallback=True  → any ticker Finnhub can't serve is retried via yfinance
                     (one slow request per ticker — fine for small lists).
    fallback=False → skip the yfinance retry. Use this for large bulk fetches
                     (e.g. the 780-ticker scan overlay): when Finnhub is rate-
                     limited, hundreds of yfinance fallbacks would crawl, and a
                     missing live quote simply leaves that ticker on its daily
                     close — which is acceptable for scoring.
    """
    import concurrent.futures

    def _fetch_one(t):
        fh = _get_finnhub()
        if fh:
            try:
                q = fh.quote(t)
                c = q.get("c", 0)
                pc = q.get("pc", 0)
                if c and c > 0:
                    pct = (c - pc) / pc * 100 if pc else 0
                    result = {
                        "price":      round(c, 4),
                        "prev_close": round(pc, 4),
                        "open":       round(q.get("o", c), 4),
                        "high":       round(q.get("h", c), 4),
                        "low":        round(q.get("l", c), 4),
                        "pct":        round(pct, 2),
                        "volume":     int(q.get("v", 0)),   # today's traded volume so far
                    }
                    _price_cache[t] = {"price": result["price"], "ts": datetime.now()}
                    return t, result
            except Exception:
                pass
        # yfinance fallback (skipped for bulk fetches — see fallback arg)
        if fallback:
            try:
                p = yf.Ticker(t).fast_info.last_price
                if p:
                    p = round(float(p), 4)
                    _price_cache[t] = {"price": p, "ts": datetime.now()}
                    return t, {"price": p, "prev_close": p, "open": p,
                               "high": p, "low": p, "pct": 0.0}
            except Exception:
                pass
        return t, None

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(tickers), 10)) as ex:
        futures = {ex.submit(_fetch_one, t): t for t in tickers}
        for f in concurrent.futures.as_completed(futures):
            t, data = f.result()
            if data:
                results[t] = data
    return results


def get_avg_daily_volume(ticker: str) -> int:
    """
    Returns 20-day average daily volume.
    Uses cached bars from get_bars() — instant if the scanner already ran.
    """
    df = get_bars(ticker, "1y", "1d")
    if df is None or df.empty or "volume" not in df.columns:
        return 0
    tail = df["volume"].tail(20).replace(0, np.nan).dropna()
    return int(tail.mean()) if len(tail) > 0 else 0


def get_current_price(ticker: str) -> Optional[float]:
    """
    Real-time price with minimal delay.
    Priority:
      1. Finnhub (if API key set) — real-time, no delay
      2. yfinance fast_info       — ~1-2 second delay
      3. Last bar close           — fallback only
    Caches for 8 seconds to avoid hammering APIs.
    """
    # Check cache first
    cached = _price_cache.get(ticker)
    if cached and (datetime.now() - cached["ts"]).seconds < _PRICE_TTL:
        return cached["price"]

    price = None

    # ── 1. Finnhub real-time quote ────────────────────────────────────────────
    fh = _get_finnhub()
    if fh:
        try:
            q = fh.quote(ticker)
            # q = {"c": current, "h": high, "l": low, "o": open, "pc": prev_close}
            p = q.get("c", 0)
            if p and p > 0:
                price = round(float(p), 4)
        except Exception:
            pass

    # ── 2. yfinance fast_info (fallback) ─────────────────────────────────────
    if price is None:
        try:
            p = yf.Ticker(ticker).fast_info.last_price
            if p:
                price = round(float(p), 4)
        except Exception:
            pass

    # ── 3. Last bar close (last resort) ──────────────────────────────────────
    if price is None:
        df = get_bars(ticker, "5d", "1d")
        if df is not None and not df.empty:
            price = round(float(df["close"].iloc[-1]), 4)

    if price:
        _price_cache[ticker] = {"price": price, "ts": datetime.now()}
    return price


def get_price_change(ticker: str):
    """
    Returns (price, change_pct, change_dollar).
    Uses live price + previous daily close for the change calculation.
    """
    try:
        price = get_current_price(ticker)
        if price is None:
            return None, 0.0, 0.0

        # Get previous close
        fh = _get_finnhub()
        if fh:
            try:
                q    = fh.quote(ticker)
                prev = q.get("pc", 0)
                if prev and prev > 0:
                    chg = price - prev
                    pct = chg / prev * 100
                    return round(price, 2), round(pct, 2), round(chg, 2)
            except Exception:
                pass

        # Fallback: use daily bars
        df = get_bars(ticker, "5d", "1d")
        if df is not None and len(df) >= 2:
            prev = float(df["close"].iloc[-2])
            chg  = price - prev
            pct  = chg / prev * 100
            return round(price, 2), round(pct, 2), round(chg, 2)

    except Exception:
        pass
    p = get_current_price(ticker)
    return (p, 0.0, 0.0) if p else (None, 0.0, 0.0)


def get_vix() -> float:
    try:
        fh = _get_finnhub()
        if fh:
            q = fh.quote("VIX")
            v = q.get("c", 0)
            if v and v > 0:
                return round(float(v), 2)
    except Exception:
        pass
    try:
        df = get_bars("^VIX", "5d", "1d")
        if df is not None:
            return round(float(df["close"].iloc[-1]), 2)
    except Exception:
        pass
    return 20.0


def clear_price_cache(ticker: str = None):
    """Force-refresh price on next call. Pass ticker=None to clear all."""
    if ticker:
        _price_cache.pop(ticker, None)
        for k in list(_cache.keys()):
            if k.startswith(ticker):
                del _cache[k]
    else:
        _price_cache.clear()
        _cache.clear()


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add all indicators using pure pandas/numpy — no native libs needed."""
    df = df.copy()
    n  = len(df)

    # EMAs
    df["ema20"]  = _ema(df["close"], 20)
    df["ema50"]  = _ema(df["close"], 50)
    df["ema200"] = _ema(df["close"], 200) if n >= 200 else np.nan

    # RSI
    df["rsi"] = _rsi(df["close"], 14)

    # MACD
    df["macd"], df["macd_sig"], df["macd_hist"] = _macd(df["close"])

    # ATR
    df["atr"] = _atr(df["high"], df["low"], df["close"], 14)

    # ADX
    df["adx"] = _adx(df["high"], df["low"], df["close"], 14)

    # Bollinger Bands
    df["bb_upper"], df["bb_mid"], df["bb_lower"] = _bbands(df["close"], 20, 2.0)

    # Volume
    df["vol_ma20"]  = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma20"]

    return df


def ema200_slope(df: pd.DataFrame, lookback: int = 10) -> str:
    """Returns 'positive', 'negative', or 'flat'."""
    try:
        e = df["ema200"].dropna()
        if len(e) < lookback:
            return "flat"
        slope = (e.iloc[-1] - e.iloc[-lookback]) / lookback
        pct   = slope / e.iloc[-1] * 100
        if pct > 0.03:
            return "positive"
        elif pct < -0.03:
            return "negative"
        return "flat"
    except Exception:
        return "flat"


def get_regime() -> dict:
    """Convenience wrapper — fetches SPY + VIX then returns detect_regime dict."""
    try:
        vix    = get_vix()
        df_spy = get_bars("SPY", "1y", "1d")
        if df_spy is not None:
            df_spy = compute_indicators(df_spy)
        return detect_regime(df_spy, vix)
    except Exception:
        return {"regime": "BULL_QUIET", "color": "#00AA00", "vix": 0,
                "spy": 0, "ema200": 0, "above_200": True}


def detect_regime(df_spy: pd.DataFrame, vix: float) -> dict:
    """Detect market regime from SPY + VIX."""
    try:
        d = df_spy.iloc[-1]
        price  = float(d["close"])
        ema200 = float(d["ema200"]) if not pd.isna(d.get("ema200", np.nan)) else price
        above  = price > ema200

        if vix >= 40:
            regime, color = "CRISIS", "#8B0000"
        elif above and vix < 20:
            regime, color = "BULL_QUIET", "#00AA00"
        elif above and vix < 30:
            regime, color = "BULL_VOLATILE", "#FFA500"
        elif not above and vix < 30:
            regime, color = "BEAR_QUIET", "#FF6600"
        else:
            regime, color = "BEAR_VOLATILE", "#CC0000"

        return {"regime": regime, "color": color, "vix": vix,
                "spy": round(price, 2), "ema200": round(ema200, 2), "above_200": above}
    except Exception:
        return {"regime": "BULL_QUIET", "color": "#00AA00", "vix": vix,
                "spy": 0, "ema200": 0, "above_200": True}
