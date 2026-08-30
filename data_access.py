"""data_access.py — Streamlit-cached data fetchers (regime, scores, quotes,
charts, company info). Extracted from app.py; depends only on the data/
signal modules, not on app.py state."""

import streamlit as st
from market_data import (get_vix, get_bars, compute_indicators, detect_regime,
                         get_company_info, get_price_change, get_batch_quotes)
from signals import score_entry, check_position


@st.cache_data(ttl=300)
def load_regime():
    vix    = get_vix()
    spy_df = get_bars("SPY", "1y", "1d")
    if spy_df is not None:
        spy_df = compute_indicators(spy_df)
    return detect_regime(spy_df, vix)


@st.cache_data(ttl=300, show_spinner=False)
def cached_score(ticker: str, regime_str: str):
    # Reuse bars the scanner already downloaded (avoids a redundant yfinance
    # fetch); score_entry falls back to its own get_bars path if not cached.
    try:
        from scanner import get_cached_bars
        _df = get_cached_bars(ticker)
    except Exception:
        _df = None
    return score_entry(ticker, {"regime": regime_str}, _df=_df)


@st.cache_data(ttl=15, show_spinner=False)
def cached_position_check(ticker, entry, stop, target1, date_in, qty):
    return check_position(ticker, entry, stop, target1, date_in, qty)


@st.cache_data(ttl=300, show_spinner=False)
def load_chart_df(ticker: str, period: str):
    df = get_bars(ticker, period, "1d")
    return compute_indicators(df) if df is not None else None


@st.cache_data(ttl=86400, show_spinner=False)
def cached_company_info(ticker: str) -> dict:
    return get_company_info(ticker)


def cached_price_change(ticker: str):
    # Always fresh — Finnhub quote with 8s in-memory cache in market_data.py
    return get_price_change(ticker)


@st.cache_data(ttl=5, show_spinner=False)
def live_quote(ticker: str) -> dict:
    """Single Finnhub quote — 5s cache. Returns price, pct, open, high, low."""
    quotes = get_batch_quotes([ticker])
    return quotes.get(ticker, {})


@st.cache_data(ttl=5, show_spinner=False)
def live_quotes_batch(tickers: tuple) -> dict:
    """Batch Finnhub quotes — 5s cache."""
    return get_batch_quotes(list(tickers))
