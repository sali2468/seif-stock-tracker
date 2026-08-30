"""
StockPal — Market Scanner
Swing | Day Trade | VCP detection across ~780 tickers.

Cache strategy (single pickle, no metadata files):
  - First run           : full 1-year download (~5-8 min, one time only)
  - Within 8 hours      : use cache as-is (instant)
  - 8 hrs – 7 days old  : incremental 5-day update (~60 sec)
  - Older than 7 days   : full re-download
"""

import concurrent.futures
import logging
import os
import pickle
import time
import numpy as np
import pandas as pd
import yfinance as yf
from dataclasses import dataclass, field
from typing import Optional, List, Tuple, Dict
from market_data import compute_indicators, get_batch_quotes
from signals import score_entry
from config import get_section

log = logging.getLogger("scanner")


# ─────────────────────────────────────────────────────────────────────────────
# UNIVERSE  (~780 liquid tickers, Russell 1000 style)
# ─────────────────────────────────────────────────────────────────────────────
UNIVERSE = {
    "Semiconductors": [
        "NVDA","AMD","INTC","QCOM","AVGO","TXN","MU","MRVL","AMAT","KLAC",
        "LRCX","NXPI","ON","MCHP","ADI","SWKS","QRVO","MPWR","WOLF","SLAB",
        "ALGM","AMBA","CRUS","SITM","MTSI","ONTO","COHU","RMBS","FORM","ACLS",
        "DIOD","VICR","ACMR","SMCI","TSM","ARM","ALAB","OLED",
    ],
    "Software": [
        "MSFT","CRM","ORCL","ADBE","NOW","SNOW","PLTR","NET","CRWD","DDOG",
        "ZS","PANW","FTNT","OKTA","TWLO","WDAY","TEAM","ZM","DOCU","VEEV",
        "PAYC","PCTY","TYL","CDNS","SNPS","ANSS","MANH","AZPN","EPAM","GLOB",
        "GDDY","GEN","RNG","MDB","GTLB","BILL","CFLT","ZI","FRSH","JAMF",
        "QTWO","BLKB","BRZE","IOT","TTD","MGNI","RAMP","FOUR","TOST","HUBS",
    ],
    "Hardware & Cloud": [
        "AAPL","IBM","CSCO","ANET","JNPR","FFIV","HPQ","HPE","DELL","WDC",
        "STX","NTAP","PSTG","ZBRA","CDW","AKAM","SOUN","BBAI","AI","LDOS","SAIC",
    ],
    "Internet & Fintech": [
        "UBER","SHOP","LYFT","DASH","EXPE","COIN","HOOD","SOFI","MSTR","AFRM",
        "UPST","SQ","PYPL","NU","ALLY","PINS","SNAP","SPOT","RBLX","MTCH",
        "DUOL","RDDT","KVYO","TTD","IAC",
    ],
    "Communication": [
        "META","GOOGL","NFLX","DIS","CMCSA","T","VZ","ROKU","EA","TTWO",
        "WBD","CHTR","PARA","FOXA","LUMN","NYT",
    ],
    "Consumer Discretionary": [
        "AMZN","TSLA","NKE","MCD","SBUX","HD","LOW","TJX","BKNG","ABNB",
        "GM","F","RIVN","ANF","GPS","FIVE","M","KSS","BOOT","MGM","CZR",
        "LVS","WYNN","TGT","DG","DLTR","OLLI","BBY","AZO","ORLY","GPC",
        "LULU","RH","WSM","EBAY","ETSY","AN","KMX","CVNA","NIO","LI","XPEV",
        "YUM","CMG","DPZ","QSR","TXRH","DRI","PENN","DKNG","GRMN",
        "RL","TPR","CROX","DECK","SKX","HLT","MAR","BURL","JWN",
    ],
    "Consumer Staples": [
        "WMT","COST","PG","KO","PEP","PM","MO","MDLZ","CL","KR","SFM",
        "CVS","WBA","TSN","HRL","CAG","CPB","K","GIS","MKC","CHD",
        "STZ","TAP","MNST","CELH","CLX","SYY",
    ],
    "Healthcare": [
        "UNH","JNJ","PFE","ABBV","MRK","LLY","TMO","DHR","VRTX","REGN",
        "BIIB","GILD","MRNA","DXCM","ISRG","AMGN","BMY","HCA","HUM","CI",
        "ELV","MDT","ABT","BSX","SYK","EW","HOLX","ALGN","IDXX","ZBH",
        "HIMS","DOCS","EXAS","INCY","NBIX","UTHR",
    ],
    "Biotech": [
        "ALNY","EDIT","BEAM","CRSP","ARVN","SRPT","RARE","FOLD","SAGE",
        "ACAD","IMVT","RPRX","ARWR","IONS","BMRN","EXEL","VKTX","SMMT",
        "RVMD","KRYS","ITCI","LGND","AGIO","IOVA",
    ],
    "Banks": [
        "JPM","BAC","GS","MS","WFC","C","USB","PNC","TFC","MTB",
        "NTRS","STT","ZION","WAL","COF","RF","KEY","CFG","HBAN","FITB",
    ],
    "Insurance & Exchanges": [
        "V","MA","AXP","BLK","SCHW","MET","PRU","GL","CB","TRV",
        "ALL","PGR","HIG","AIG","ICE","CME","CBOE","NDAQ","MSCI","SPGI",
        "MCO","VRSK","BX","KKR","APO","ARES","ARCC","DFS","SYF",
    ],
    "Energy": [
        "XOM","CVX","COP","EOG","SLB","MPC","PSX","VLO","OXY","DVN",
        "FANG","HAL","MRO","OVV","RIG","AR","EQT","CTRA","NOV","CHK",
        "SM","MTDR","NOG","WMB","KMI","ET","EPD","LNG","BKR",
    ],
    "Industrials": [
        "CAT","DE","HON","GE","MMM","EMR","ETN","PH","ROK","AME",
        "ROP","IEX","IDEX","HUBB","GNRC","TT","CARR","OTIS","GWW","FAST",
        "SNA","CGNX","TRMB","XYL","CSX","NSC","UNP","FDX","UPS","XPO",
        "SAIA","ODFL","WERN","JBHT","CHRW","EXPD",
    ],
    "Aerospace & Defense": [
        "LMT","RTX","NOC","GD","HII","BA","KTOS","AXON","DRS","CACI",
        "TDG","HEI","BWXT","LHX","RKLB","LUNR","ACHR","JOBY","ASTS",
    ],
    "Clean Energy": [
        "PLUG","BE","FSLR","ENPH","RUN","NEE","AES","BLNK","CHPT","EVGO",
    ],
    "Materials": [
        "FCX","NEM","GOLD","HL","AA","CLF","NUE","MP","WPM","PAAS",
        "MOS","CF","NTR","ADM","BG","LIN","APD","ALB","SQM","LYB",
        "DOW","DD","IP","PKG","KGC","AEM","FNV","RGLD","BTG",
    ],
    "Mining & Uranium": [
        "CCJ","UEC","DNN","UUUU","NXE","LEU",
    ],
    "Shipping & Airlines": [
        "ZIM","DAC","MATX","SBLK","DAL","UAL","AAL","LUV","ALK",
        "CCL","RCL","NCLH","JBLU","CHRW","EXPD",
    ],
    "Real Estate": [
        "AMT","PLD","EQIX","SPG","O","VICI","IRM","STAG","CCI","SBAC",
        "AVB","EQR","MAA","VTR","WELL","EXR","CUBE","PSA","LEN","DHI",
        "TOL","KBH","PHM","NVR",
    ],
    "Utilities": [
        "NEE","DUK","SO","XEL","AEP","EXC","EIX","WEC","ETR","PPL",
        "FE","CMS","AWK","SRE","PCG",
    ],
    "China ADRs": [
        "BABA","JD","PDD","BIDU","YUMC","BILI","NIO","XPEV","LI","FUTU",
    ],
    "Emerging Growth": [
        # Small/mid-cap "next NVDA" candidates — high growth potential, higher risk.
        # AI compute, data & software
        "CRWV","NBIS","APLD","CORZ","IREN","WULF","CIFR","TEM","RXRX","PATH","S","APP","U",
        # Quantum computing
        "IONQ","RGTI","QBTS","QUBT","ARQQ","LAES",
        # Nuclear / next-gen energy
        "OKLO","SMR","NNE","ASPI",
        # Space & drones
        "RDW","RCAT","UMAC","ONDS","AVAV","BKSY","PL","VSAT","EVTL","SIDU",
        # Semiconductors (small/mid growth)
        "CRDO","NVTS","INDI","POET","LSCC","POWI","AOSL",
        # Robotics & autonomous
        "SERV","RR","PONY","WRD","AUR",
        # Biotech (high-potential)
        "SDGR","ABSI","RNA","ABCL","KROS","ABVX",
        # Fintech / crypto
        "CRCL","BULL",
        # Consumer growth
        "ELF","ONON","BROS","CAVA",
    ],
    "ETFs": [
        "SPY","QQQ","IWM","GLD","SLV","TLT","XLE","XLF","XLK","XLV",
        "XLI","XLY","XLB","XLRE","XLU","XLC","ARKK","SMH","IBB",
    ],
}

ALL_TICKERS = list(dict.fromkeys(t for sec in UNIVERSE.values() for t in sec))


# ─────────────────────────────────────────────────────────────────────────────
# CACHE  (single pickle — no metadata file, uses file mtime)
# ─────────────────────────────────────────────────────────────────────────────
_CACHE_FILE    = r"C:\TradeCache\.scan_cache.pkl"   # local drive — not synced by OneDrive
_INC_TTL_HRS   = 8    # incremental update after 8 hours
_FULL_TTL_DAYS = 7    # full re-download after 7 days


def cache_info() -> dict:
    """Cache status for the UI label."""
    if not os.path.exists(_CACHE_FILE):
        return {"exists": False, "label": "No cache — first scan downloads everything (~5 min)"}

    age_hrs  = (time.time() - os.path.getmtime(_CACHE_FILE)) / 3600
    size_mb  = os.path.getsize(_CACHE_FILE) / 1_048_576

    if age_hrs < 1:
        age_str = f"{age_hrs*60:.0f} min old"
    elif age_hrs < 24:
        age_str = f"{age_hrs:.1f}h old"
    else:
        age_str = f"{age_hrs/24:.1f}d old"

    next_inc  = max(0, _INC_TTL_HRS  - age_hrs)
    next_full = max(0, _FULL_TTL_DAYS * 24 - age_hrs)

    return {
        "exists":   True,
        "age_hrs":  round(age_hrs, 1),
        "size_mb":  round(size_mb, 1),
        "label":    f"data {age_str} · {size_mb:.0f} MB · next update in {next_inc:.1f}h",
    }


def _load_cache() -> Optional[Dict]:
    if not os.path.exists(_CACHE_FILE):
        return None
    try:
        with open(_CACHE_FILE, "rb") as f:
            return pickle.load(f)
    except Exception as e:
        log.warning("Cache load failed (%s) — will re-download", e)
        return None


def _save_cache(bar_map: Dict) -> None:
    try:
        with open(_CACHE_FILE, "wb") as f:
            pickle.dump(bar_map, f)
    except Exception:
        pass


def invalidate_cache() -> None:
    """Wipe caches — next scan does a full re-download and recompute."""
    for f in (_CACHE_FILE, _RESULT_FILE):
        try:
            os.remove(f)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# PRECOMPUTED SCAN RESULT  (daemon writes it, the web app reads it instantly)
# ─────────────────────────────────────────────────────────────────────────────
_RESULT_FILE    = r"C:\TradeCache\.scan_result.pkl"
_RESULT_VERSION = 1


def save_scan_result(regime_str: str, swing, day, vcp, momentum) -> None:
    """Persist a completed scan atomically so the app can load it with no compute."""
    try:
        os.makedirs(os.path.dirname(_RESULT_FILE), exist_ok=True)
        payload = {
            "version": _RESULT_VERSION,
            "ts":      time.time(),
            "regime":  regime_str,
            "swing":   swing, "day": day, "vcp": vcp, "momentum": momentum,
        }
        tmp = _RESULT_FILE + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(payload, f)
        os.replace(tmp, _RESULT_FILE)   # atomic on Windows + POSIX
    except Exception as e:
        log.warning("Could not save scan result: %s", e)


def load_scan_result(max_age_s: float = 600):
    """
    Return (regime_str, swing, day, vcp, momentum) from the daemon-written result
    if it exists and is fresher than max_age_s. Returns None otherwise — the caller
    then falls back to running the scan itself, so this is always safe.
    """
    try:
        if not os.path.exists(_RESULT_FILE):
            return None
        if (time.time() - os.path.getmtime(_RESULT_FILE)) > max_age_s:
            return None
        with open(_RESULT_FILE, "rb") as f:
            p = pickle.load(f)
        if p.get("version") != _RESULT_VERSION:
            return None
        return p["regime"], p["swing"], p["day"], p["vcp"], p["momentum"]
    except Exception as e:
        log.warning("Could not load scan result: %s", e)
        return None


def scan_result_age_s():
    """Seconds since the daemon last wrote a scan result, or None if none exists."""
    try:
        return time.time() - os.path.getmtime(_RESULT_FILE)
    except OSError:
        return None


# In-memory memo of the loaded bar_map so single-ticker lookups don't re-read
# the multi-MB pickle every call. Reloads only when the file's mtime changes.
_bars_mem: Dict = {"mtime": 0.0, "map": None}


def get_cached_bars(ticker: str) -> Optional[pd.DataFrame]:
    """
    Return indicator-enriched daily bars for one ticker straight from the scan
    cache — no yfinance download. Lets the Dashboard / Watchlist / Analyze pages
    reuse the data the scanner already fetched. Returns None if the ticker isn't
    cached (caller then falls back to its own get_bars path).
    """
    try:
        mtime = os.path.getmtime(_CACHE_FILE)
    except OSError:
        return None
    if _bars_mem["map"] is None or mtime != _bars_mem["mtime"]:
        _bars_mem["map"]   = _load_cache() or {}
        _bars_mem["mtime"] = mtime
    raw = _bars_mem["map"].get(ticker)
    if raw is None or len(raw) < 60:
        return None
    try:
        return compute_indicators(raw)
    except Exception as e:
        log.debug("get_cached_bars(%s) indicator calc failed: %s", ticker, e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# DOWNLOAD HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _download_chunk(tickers: list, period: str = "1y") -> Dict[str, pd.DataFrame]:
    """Download one chunk of tickers in a single yfinance call."""
    if not tickers:
        return {}
    try:
        raw = yf.download(
            tickers,
            period=period,
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        log.warning("yfinance download failed for %d tickers: %s", len(tickers), e)
        return {}

    result = {}

    # Single ticker: flat DataFrame
    if len(tickers) == 1:
        try:
            df = raw.copy()
            df.columns = [str(c).lower() for c in df.columns]
            df = df[["open", "high", "low", "close", "volume"]].dropna()
            if len(df) >= 60:
                result[tickers[0]] = df
        except Exception:
            pass
        return result

    # Multiple tickers: yfinance returns MultiIndex columns
    # Level-0 may be Price or Ticker depending on yfinance version — detect it
    try:
        lvl0 = raw.columns.get_level_values(0).unique().tolist()
        price_types = {"Open", "High", "Low", "Close", "Volume",
                       "open", "high", "low", "close", "volume"}

        if any(str(v) in price_types for v in lvl0):
            # New yfinance format: (Price, Ticker) — swap levels
            raw = raw.swaplevel(axis=1)

        for ticker in tickers:
            try:
                df = raw[ticker].copy()
                df.columns = [str(c).lower() for c in df.columns]
                df = df[["open", "high", "low", "close", "volume"]].dropna()
                if len(df) >= 60:
                    result[ticker] = df
            except Exception:
                continue
    except Exception:
        pass

    return result


def _download_parallel(tickers: list, period: str = "1y", chunk_size: int = 300) -> Dict[str, pd.DataFrame]:
    """Split tickers into chunks and download concurrently."""
    chunks = [tickers[i: i + chunk_size] for i in range(0, len(tickers), chunk_size)]
    result: Dict[str, pd.DataFrame] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(chunks), 4)) as ex:
        futs = [ex.submit(_download_chunk, chunk, period) for chunk in chunks]
        for fut in concurrent.futures.as_completed(futs):
            try:
                result.update(fut.result())
            except Exception:
                pass
    return result


def _incremental_update(bar_map: Dict) -> Dict:
    """
    Fetch last 5 days for all cached tickers and append to stored history.
    Takes ~60 sec vs ~5 min for a full download.
    """
    recent = _download_parallel(list(bar_map.keys()), period="5d")
    raw_cols = ["open", "high", "low", "close", "volume"]

    for ticker, new_df in recent.items():
        if ticker not in bar_map:
            continue
        old_df = bar_map[ticker]
        # Strip any indicator columns before merging
        old_raw = old_df[[c for c in raw_cols if c in old_df.columns]]
        new_raw = new_df[[c for c in raw_cols if c in new_df.columns]]
        merged  = pd.concat([old_raw, new_raw])
        merged  = merged[~merged.index.duplicated(keep="last")].sort_index().tail(260)
        if len(merged) >= 60:
            bar_map[ticker] = merged

    return bar_map


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL DATACLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class VCPSignal:
    ticker:          str
    price:           float
    pivot_high:      float
    contractions:    List[float]
    num_pivots:      int
    tightness_pct:   float
    vol_dry_up:      bool
    prior_trend_pct: float
    base_days:       int
    stars:           int
    stop:            float
    target:          float
    why:             str
    sector:          str  = ""
    rs_rank:         float = 0.0
    trend_template:  int   = 0


@dataclass
class DayTradeSignal:
    ticker:    str
    price:     float
    sector:    str
    atr:       float
    atr_pct:   float
    vol_ratio: float
    rsi:       float
    adx:       float
    gap_pct:   float
    trend:     str
    stars:     int
    entry:     float
    stop:      float
    target:    float
    stop_pct:  float
    gain_pct:  float
    headline:  str
    why:       str
    catalysts: list = field(default_factory=list)


@dataclass
class MomentumReversalSignal:
    ticker:              str
    price:               float
    sector:              str
    stop:                float
    target1:             float
    target2:             float
    stop_pct:            float
    gain_pct:            float
    rr:                  float
    stars:               int
    why:                 str
    rsi:                 float
    adx:                 float
    vol_ratio:           float
    macd_hist:           float
    macd_cross_days_ago: int
    above_200ma:         bool
    setup_type:          str = "reclaim"   # reclaim | deep_bottom | catalyst
    catalyst:            str = ""


# ─────────────────────────────────────────────────────────────────────────────
# SCORING HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _sector_of(ticker: str) -> str:
    for sector, tickers in UNIVERSE.items():
        if ticker in tickers:
            return sector
    return "Other"


def _rs_raw(df: pd.DataFrame, spy_raw: float) -> float:
    """IBD-style weighted RS minus SPY."""
    try:
        c = df["close"]
        n = len(c)
        def perf(d): return float(c.iloc[-1] / c.iloc[-(d+1)] - 1) if n > d else 0.0
        raw = 0.4 * perf(63) + 0.2 * perf(126) + 0.2 * perf(189) + 0.2 * perf(252)
        return raw - spy_raw
    except Exception:
        return 0.0


def _rs_rank(score: float, all_scores: list) -> int:
    """0-99 percentile vs all tickers."""
    if not all_scores:
        return 50
    beaten = sum(1 for v in all_scores if v < score)
    return min(99, int(beaten / len(all_scores) * 100))


def _trend_template(df: pd.DataFrame) -> int:
    """Minervini Trend Template — returns 0-8 criteria passed."""
    try:
        c  = df["close"]
        n  = len(c)
        if n < 50:
            return 0
        p      = float(c.iloc[-1])
        sma50  = float(c.rolling(50).mean().iloc[-1])
        sma150 = float(c.rolling(150).mean().iloc[-1]) if n >= 150 else float("nan")
        sma200 = float(c.rolling(200).mean().iloc[-1]) if n >= 200 else float("nan")
        sma200_ago = float(c.rolling(200).mean().iloc[-21]) if n >= 221 else sma200
        hi52 = float(c.tail(252).max())
        lo52 = float(c.tail(252).min())

        checks = [
            not np.isnan(sma200) and p > sma200,
            not np.isnan(sma200) and sma200 > sma200_ago,
            not np.isnan(sma150) and not np.isnan(sma200) and sma150 > sma200,
            not np.isnan(sma150) and sma50 > sma150,
            not np.isnan(sma200) and sma50 > sma200,
            p > sma50,
            p >= hi52 * 0.75,
            p >= lo52 * 1.30,
        ]
        return sum(checks)
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC HELPERS — used by morning_briefing.py and external callers
# ─────────────────────────────────────────────────────────────────────────────

def _batch_download(tickers: list, period: str = "1y") -> Dict[str, pd.DataFrame]:
    """Public alias for _download_parallel — downloads and caches OHLCV data."""
    return _download_parallel(tickers, period=period)


def _rs_raw_score(df: pd.DataFrame) -> float:
    """IBD-style raw RS score for a single ticker (no SPY adjustment)."""
    return _rs_raw(df, 0.0)


def _rs_percentile(ticker: str, score: float, score_map: dict) -> int:
    """0-99 percentile rank of score vs all values in score_map dict."""
    all_scores = list(score_map.values())
    return _rs_rank(score, all_scores)


def _trend_template_check(df: pd.DataFrame):
    """Returns (criteria_passed: int, passes_template: bool) for Minervini TT."""
    tt = _trend_template(df)
    return tt, tt >= 6


def detect_vcp(ticker: str, df: pd.DataFrame) -> Optional[VCPSignal]:
    """Detect Volatility Contraction Pattern — config-driven via strategy_config.json vcp section."""
    # Config loaded once and cached in config.py (no per-ticker disk I/O)
    _det = get_section("vcp.detection",        {})
    _sc  = get_section("vcp.scoring",          {})
    _thr = get_section("vcp.stars_thresholds", {})
    _stt = get_section("vcp.stops_targets",    {})

    try:
        if df is None or len(df) < 100:
            return None

        close = df["close"].values.astype(float)
        high_ = df["high"].values.astype(float)
        low_  = df["low"].values.astype(float)
        vol   = df["volume"].values.astype(float)
        n     = len(close)
        price = close[-1]

        # Prior uptrend required
        min_prior = float(_det.get("min_prior_trend_pct", 25))
        pw        = min(252, n - 60)
        prior_lo  = float(np.min(close[max(0, n - pw): max(0, n - 55)]))
        prior_pct = (price - prior_lo) / prior_lo * 100 if prior_lo > 0 else 0
        if prior_pct < min_prior:
            return None

        # Base window
        max_base = int(_det.get("max_base_days", 65))
        min_base = int(_det.get("min_base_days", 20))
        bs  = max(0, n - max_base)
        bh, bl, bc, bv = high_[bs:], low_[bs:], close[bs:], vol[bs:]
        nb  = len(bc)
        if nb < min_base:
            return None

        near_piv = float(_det.get("near_pivot_threshold", 0.90))
        pivot_high = float(np.max(bh))
        if price < pivot_high * near_piv:
            return None

        # Swing highs / lows
        lk = 5
        s_highs, s_lows = [], []
        for i in range(lk, nb - lk):
            if bh[i] >= float(np.max(bh[i-lk: i+lk+1])):
                s_highs.append((i, float(bh[i])))
            if bl[i] <= float(np.min(bl[i-lk: i+lk+1])):
                s_lows.append((i, float(bl[i])))

        if len(s_highs) < 2 or len(s_lows) < 2:
            return None

        # Contraction pairs
        d_min = float(_det.get("contraction_depth_min", 3.0))
        d_max = float(_det.get("contraction_depth_max", 45.0))
        contractions, used = [], set()
        for hi_i, hi_p in s_highs:
            nxt = [(li, lp) for li, lp in s_lows if li > hi_i and li not in used]
            if not nxt:
                continue
            li_i, li_p = nxt[0]
            depth = (hi_p - li_p) / hi_p * 100
            if d_min <= depth <= d_max:
                contractions.append((hi_i, hi_p, li_i, li_p, depth))
                used.add(li_i)

        min_contractions = int(_det.get("min_contractions", 2))
        if len(contractions) < min_contractions:
            return None

        depths = [c[4] for c in contractions]
        dec    = sum(1 for i in range(1, len(depths)) if depths[i] < depths[i-1])
        if dec < len(depths) - 1 or depths[-1] >= depths[0] * 0.80:
            return None

        vol_thr    = float(_det.get("vol_dry_up_threshold", 0.80))
        avg_vol    = float(np.mean(bv))
        recent_vol = float(np.mean(bv[-15:])) if nb >= 15 else avg_vol
        vol_dry_up = recent_vol < avg_vol * vol_thr
        tight_h    = float(np.max(bh[-15:])) if nb >= 15 else float(np.max(bh))
        tight_l    = float(np.min(bl[-15:])) if nb >= 15 else float(np.min(bl))
        tight_pct  = (tight_h - tight_l) / tight_l * 100 if tight_l > 0 else 99.0

        nc = len(contractions)
        # Scoring from config
        sc_piv3 = int(_sc.get("pivots_3plus", 3))
        sc_piv2 = int(_sc.get("pivots_2",     1))
        d_tight = float(_sc.get("last_depth_tight_max", 6.0))
        d_mid   = float(_sc.get("last_depth_mid_max",  10.0))
        d_loose = float(_sc.get("last_depth_loose_max",15.0))
        t_tight = float(_sc.get("tightness_tight_max",  5.0))
        t_mid   = float(_sc.get("tightness_mid_max",    8.0))
        s_prior = float(_sc.get("strong_prior_trend_pct",50))

        score = (sc_piv3 if nc >= 3 else sc_piv2) + \
                (int(_sc.get("last_depth_tight_score", 3)) if depths[-1] < d_tight else
                 int(_sc.get("last_depth_mid_score",   2)) if depths[-1] < d_mid else
                 int(_sc.get("last_depth_loose_score", 1)) if depths[-1] < d_loose else 0) + \
                (int(_sc.get("vol_dry_up_score", 2)) if vol_dry_up else 0) + \
                (int(_sc.get("tightness_tight_score", 2)) if tight_pct < t_tight else
                 int(_sc.get("tightness_mid_score",   1)) if tight_pct < t_mid   else 0) + \
                (int(_sc.get("strong_prior_trend_score", 1)) if prior_pct > s_prior else 0)

        if score < int(_det.get("min_score", 4)):
            return None

        t3    = int(_thr.get("three_star", 8))
        t2    = int(_thr.get("two_star",   6))
        stars = 3 if score >= t3 else 2 if score >= t2 else 1

        last_lo     = contractions[-1][3]
        sb          = float(_stt.get("stop_buffer_below_pivot", 0.98))
        te          = float(_stt.get("target_extension", 1.0))
        stop        = round(last_lo * sb, 2)
        target      = round(pivot_high + (pivot_high - last_lo) * te, 2)
        depths_str  = " → ".join(f"{d:.1f}%" for d in depths)
        why = (
            f"{nc}-pivot VCP over {nb} days. Contractions: {depths_str}. "
            f"Last pullback {depths[-1]:.1f}%, base range {tight_pct:.1f}%. "
            + ("Volume dried up — sellers exhausted. " if vol_dry_up else "")
            + f"Entry above ${pivot_high:.2f}. Prior uptrend +{prior_pct:.0f}%."
        )

        return VCPSignal(
            ticker=ticker, price=round(price, 2), pivot_high=round(pivot_high, 2),
            contractions=[round(d, 1) for d in depths], num_pivots=nc,
            tightness_pct=round(tight_pct, 1), vol_dry_up=vol_dry_up,
            prior_trend_pct=round(prior_pct, 1), base_days=nb,
            stars=stars, stop=stop, target=target, why=why,
            sector=_sector_of(ticker),
        )
    except Exception:
        return None


def _score_day(ticker: str, df: pd.DataFrame) -> Optional[DayTradeSignal]:
    """Score day-trade potential — config-driven via strategy_config.json day_trade section."""
    if df is None or len(df) < 30:
        return None

    # Config loaded once and cached in config.py (no per-ticker disk I/O)
    _hg  = get_section("day_trade.entry.hard_gates",      {})
    _sc  = get_section("day_trade.entry.scoring",         {})
    _thr = get_section("day_trade.entry.stars_thresholds", {})
    _st  = get_section("day_trade.entry.stops_targets",    {})

    d, prev = df.iloc[-1], df.iloc[-2]
    price     = float(d["close"])
    atr       = float(d.get("atr",       np.nan))
    vol_ratio = float(d.get("vol_ratio", np.nan))
    rsi       = float(d.get("rsi",       np.nan))
    adx       = float(d.get("adx",       np.nan))
    ema20     = float(d.get("ema20",     np.nan))
    ema50     = float(d.get("ema50",     np.nan))
    macd_h    = float(d.get("macd_hist", np.nan))

    if any(np.isnan(x) for x in [price, atr, rsi]) or atr == 0:
        return None

    gap_pct = (float(d["open"]) - float(prev["close"])) / float(prev["close"]) * 100
    atr_pct = atr / price * 100

    # Hard gates
    if atr_pct < float(_hg.get("min_atr_pct", 2.0)):
        return None
    if not np.isnan(vol_ratio) and vol_ratio < float(_hg.get("min_volume_ratio", 1.5)):
        return None

    score, catalysts, warnings = 0, [], []

    # ATR range scoring
    ah = float(_sc.get("atr_pct_high_threshold", 5.0))
    am = float(_sc.get("atr_pct_mid_threshold",  3.0))
    al = float(_sc.get("atr_pct_low_threshold",  2.0))
    if   atr_pct >= ah: score += int(_sc.get("atr_pct_high_score", 35)); catalysts.append(f"moves {atr_pct:.1f}%/day — great range")
    elif atr_pct >= am: score += int(_sc.get("atr_pct_mid_score",  25)); catalysts.append(f"solid {atr_pct:.1f}% daily range")
    elif atr_pct >= al: score += int(_sc.get("atr_pct_low_score",  12)); catalysts.append(f"moderate {atr_pct:.1f}% daily range")

    # Volume scoring
    if not np.isnan(vol_ratio):
        vs  = float(_sc.get("volume_surge_threshold",    3.0))
        vst = float(_sc.get("volume_strong_threshold",   2.0))
        va  = float(_sc.get("volume_above_avg_threshold",1.5))
        vb  = float(_sc.get("volume_below_cutoff",       0.8))
        if   vol_ratio >= vs:  score += int(_sc.get("volume_surge_score",    35)); catalysts.append(f"volume {vol_ratio:.1f}x normal — catalyst likely")
        elif vol_ratio >= vst: score += int(_sc.get("volume_strong_score",   25)); catalysts.append(f"strong volume {vol_ratio:.1f}x normal")
        elif vol_ratio >= va:  score += int(_sc.get("volume_above_avg_score",15)); catalysts.append(f"above-avg volume {vol_ratio:.1f}x")
        elif vol_ratio < vb:   return None

    # ADX scoring
    if not np.isnan(adx):
        ads = float(_sc.get("adx_strong_threshold",   35))
        adm = float(_sc.get("adx_moderate_threshold", 25))
        adw = float(_sc.get("adx_weak_threshold",     20))
        if   adx >= ads: score += int(_sc.get("adx_strong_score",   20)); catalysts.append(f"strong trend ADX {adx:.0f}")
        elif adx >= adm: score += int(_sc.get("adx_moderate_score", 12)); catalysts.append(f"solid trend ADX {adx:.0f}")
        elif adx >= adw: score += int(_sc.get("adx_weak_score",      6))
        else: warnings.append("Choppy — trade smaller, exit fast")

    # RSI scoring
    if not np.isnan(rsi):
        rml = float(_sc.get("rsi_momentum_low",        50))
        rmh = float(_sc.get("rsi_momentum_high",       72))
        rob = float(_sc.get("rsi_overbought_threshold",72))
        rrl = float(_sc.get("rsi_recovering_low",      35))
        rrh = float(_sc.get("rsi_recovering_high",     50))
        if   rml <= rsi <= rmh: score += int(_sc.get("rsi_momentum_score",   15)); catalysts.append(f"momentum building RSI {rsi:.0f}")
        elif rsi > rob:         score += int(_sc.get("rsi_overbought_score",  5)); warnings.append(f"Overbought RSI {rsi:.0f} — scalps only")
        elif rrl <= rsi < rrh:  score += int(_sc.get("rsi_recovering_score",  8)); catalysts.append(f"momentum recovering RSI {rsi:.0f}")

    # Gap scoring
    gus = float(_sc.get("gap_up_strong_threshold", 3.0))
    gum = float(_sc.get("gap_up_mild_threshold",   1.5))
    gd  = float(_sc.get("gap_down_threshold",      3.0))
    if   gap_pct >=  gus: score += int(_sc.get("gap_up_strong_score", 20)); catalysts.append(f"gapped up {gap_pct:.1f}%")
    elif gap_pct >=  gum: score += int(_sc.get("gap_up_mild_score",   10)); catalysts.append(f"opened {gap_pct:.1f}% higher")
    elif gap_pct <= -gd:  score += int(_sc.get("gap_down_score",      15)); catalysts.append(f"gapped down {abs(gap_pct):.1f}%")

    # EMA alignment
    if not np.isnan(ema20) and not np.isnan(ema50):
        if price > ema20 > ema50:
            score += int(_sc.get("ema_alignment_score", 10)); catalysts.append("above key MAs")

    # MACD
    if not np.isnan(macd_h) and macd_h > 0:
        score += int(_sc.get("macd_positive_score", 8))

    if score < int(_hg.get("min_score", 50)):
        return None

    trend  = "up"   if (not np.isnan(ema20) and price > ema20) else \
             "down" if (not np.isnan(ema20) and price < ema20) else "flat"
    # Halal: LONG-ONLY. Never recommend shorting. A downtrend (or an unclear/flat
    # setup, whose target would sit below entry) is dropped — every day-trade pick
    # is an upside/LONG trade the user profits from by buying, never by shorting.
    if trend != "up":
        return None
    sm     = float(_st.get("stop_atr_mult",   0.75))
    tm     = float(_st.get("target_atr_mult", 1.5))
    entry  = round(price, 2)
    stop   = round(price - atr * sm, 2)   # long-only: stop below entry
    target = round(price + atr * tm, 2)   # long-only: target above entry
    sp     = round(abs(entry - stop)   / entry * 100, 1)
    gp     = round(abs(target - entry) / entry * 100, 1)
    t3 = int(_thr.get("three_star", 80))
    t2 = int(_thr.get("two_star",   65))
    stars  = 3 if score >= t3 else 2 if score >= t2 else 1
    dir_   = "LONG"   # halal: long-only — non-uptrends were dropped above
    why    = f"{ticker}: {catalysts[0] if catalysts else ''}. Entry ${entry}, stop ${stop} ({sp}% risk), target ${target} (+{gp}%)."
    if warnings: why += f" ⚠️ {warnings[0]}"

    return DayTradeSignal(
        ticker=ticker, price=price, sector=_sector_of(ticker),
        atr=atr, atr_pct=round(atr_pct, 1),
        vol_ratio=round(vol_ratio, 1) if not np.isnan(vol_ratio) else 0,
        rsi=round(rsi, 1), adx=round(adx, 1) if not np.isnan(adx) else 0,
        gap_pct=round(gap_pct, 1), trend=trend,
        stars=stars, entry=entry, stop=stop, target=target,
        stop_pct=sp, gain_pct=gp,
        headline=f"{'⭐'*stars}  {dir_} — {catalysts[0] if catalysts else 'Active'}",
        why=why, catalysts=catalysts,
    )


def _score_momentum_reversal(ticker: str, df: pd.DataFrame) -> Optional[MomentumReversalSignal]:
    """
    Detect a bullish turnaround. Three ways to qualify:
      • reclaim     — Stage 1→2: MACD crossed above zero, near/above the 200-day MA
      • deep_bottom — well BELOW the 200-day MA but reversing off an oversold low,
                      with several confirmations (higher low, RSI curling up, EMA20 reclaim)
      • catalyst    — a recent gap-up on heavy volume (news proxy), even before a clean cross
    Built for beaten-down names starting to turn and small-cap "next NVDA" runners.
    """
    if df is None or len(df) < 60:
        return None

    # Config loaded once and cached in config.py (no per-ticker disk I/O)
    _hg  = get_section("momentum_reversal.hard_gates",   {})
    _sc  = get_section("momentum_reversal.scoring",      {})
    _st  = get_section("momentum_reversal.stops_targets", {})
    _db  = _hg.get("deep_bottom", {})
    _cat = _hg.get("catalyst",    {})

    d         = df.iloc[-1]
    price     = float(d["close"])
    rsi       = float(d.get("rsi",       np.nan))
    adx       = float(d.get("adx",       np.nan))
    vol_ratio = float(d.get("vol_ratio", np.nan))
    ema200    = float(d.get("ema200",    np.nan))
    ema50     = float(d.get("ema50",     np.nan))
    ema20     = float(d.get("ema20",     np.nan))
    atr       = float(d.get("atr",       np.nan))
    macd_h    = float(d.get("macd_hist", np.nan))

    if np.isnan(atr) or atr == 0 or np.isnan(price) or np.isnan(macd_h) or np.isnan(rsi):
        return None

    # ── MACD zero-line cross: histogram was negative, crossed to positive ──────
    hist = df["macd_hist"].dropna().values
    macd_cross_days_ago = None
    if len(hist) >= 6:
        for i in range(len(hist) - 1, max(0, len(hist) - 21), -1):
            if hist[i] >= 0 and hist[i - 1] < 0:
                macd_cross_days_ago = (len(hist) - 1) - i
                break
    has_cross = macd_cross_days_ago is not None and macd_cross_days_ago <= int(_hg.get("max_cross_age_days", 15))
    if has_cross:
        # Confirm it was meaningfully negative before the cross (not a flat zero hover)
        pci = len(hist) - 1 - macd_cross_days_ago - 1
        if pci >= 0:
            pre = hist[max(0, pci - 4): pci + 1]
            if len(pre) > 0 and float(np.min(pre)) > -0.05:
                has_cross = False

    # ── Catalyst: a recent gap-up on heavy volume (proxy for a news catalyst) ──
    catalyst, catalyst_desc = False, ""
    if _cat.get("enabled", True):
        look = int(_cat.get("lookback_days", 3))
        gthr = float(_cat.get("min_gap_pct", 5.0))
        vthr = float(_cat.get("min_vol_ratio", 2.0))
        o_arr, c_arr = df["open"].values, df["close"].values
        vr_arr = df["vol_ratio"].values
        n = len(df)
        for j in range(max(1, n - look), n):
            prev_c = c_arr[j - 1]
            gp = (o_arr[j] - prev_c) / prev_c * 100 if prev_c else 0.0
            vj = float(vr_arr[j]) if not np.isnan(vr_arr[j]) else 0.0
            if gp >= gthr and vj >= vthr:
                when = "today" if (n - 1 - j) == 0 else f"{n-1-j}d ago"
                catalyst, catalyst_desc = True, f"gapped up {gp:.0f}% on {vj:.1f}x volume {when} — a catalyst hit"
                break

    # ── 200-MA position & reversal-confirmation signals ───────────────────────
    above_200ma = not np.isnan(ema200) and price >= ema200
    deep_bottom = (not np.isnan(ema200)) and price < ema200 * float(_db.get("below_200ma_frac", 0.95))

    rsi_series = df["rsi"].dropna().values
    rsi_prior  = float(rsi_series[-6]) if len(rsi_series) >= 6 else rsi
    ob_look    = int(_db.get("oversold_lookback", 15))
    rsi_low    = float(df["rsi"].tail(ob_look).min())
    was_oversold    = rsi_low < float(_db.get("oversold_rsi", 32))
    rsi_rising      = rsi > rsi_prior + 2
    low_10          = float(df["low"].tail(10).min())
    low_prior       = float(df["low"].iloc[-30:-10].min()) if len(df) >= 30 else low_10
    higher_low      = low_10 >= low_prior * 0.99
    reclaimed_ema20 = (not np.isnan(ema20)) and price > ema20

    # A confirmed deep bottom needs several reversal signs (avoids falling knives)
    deep_confirms = sum([was_oversold, rsi_rising, higher_low, reclaimed_ema20, catalyst])
    deep_ok = deep_bottom and _db.get("enabled", True) and deep_confirms >= int(_db.get("min_confirmations", 3))

    # ── Core trigger: reversing (MACD cross) OR catalyst OR a confirmed bottom ─
    if not (has_cross or catalyst or deep_ok):
        return None
    # Any deep-bottom entry must clear the confirmation bar, however it triggered
    if deep_bottom and not deep_ok:
        return None

    # ── RSI + volume gates ────────────────────────────────────────────────────
    if rsi < float(_hg.get("min_rsi", 40)):
        return None
    # Volume gate skipped for catalyst plays — the gap already confirmed a surge
    if not catalyst and not np.isnan(vol_ratio) and vol_ratio < float(_hg.get("min_volume_ratio", 1.2)):
        return None

    setup_type = "deep_bottom" if deep_bottom else "reclaim"
    if catalyst and not has_cross:
        setup_type = "catalyst"

    # ── Scoring ───────────────────────────────────────────────────────────────
    score, positives = 0, []

    if has_cross:
        if macd_cross_days_ago == 0:
            score += int(_sc.get("macd_cross_today",  30)); positives.append("MACD just crossed above zero today — selling pressure has stopped")
        elif macd_cross_days_ago <= 3:
            score += int(_sc.get("macd_cross_recent", 25)); positives.append(f"MACD crossed above zero {macd_cross_days_ago} day{'s' if macd_cross_days_ago>1 else ''} ago — momentum is flipping")
        elif macd_cross_days_ago <= 7:
            score += int(_sc.get("macd_cross_week",   18)); positives.append(f"MACD turned positive {macd_cross_days_ago} days ago — negative momentum has ended")
        else:
            score += int(_sc.get("macd_cross_old",    10)); positives.append("MACD has recently crossed back above zero")

    # RSI level
    if rsi >= 60:
        score += int(_sc.get("rsi_strong",    20)); positives.append(f"RSI {rsi:.0f} — buyers firmly in control, momentum building fast")
    elif rsi >= 50:
        score += int(_sc.get("rsi_reclaimed", 15)); positives.append(f"RSI reclaimed 50 ({rsi:.0f}) — the balance of power has shifted to buyers")
    else:
        score += int(_sc.get("rsi_near",       8))

    # 200-MA position
    if above_200ma:
        score += int(_sc.get("above_200ma", 20)); positives.append("price has reclaimed its 200-day moving average — the long-term tide is turning")
    elif not deep_bottom:
        score += int(_sc.get("near_200ma",  10)); positives.append("price is approaching its 200-day moving average from below")

    # Volume
    if not np.isnan(vol_ratio):
        if vol_ratio >= 2.0:
            score += int(_sc.get("volume_surge",  20)); positives.append(f"volume is {vol_ratio:.1f}x normal — institutions actively buying this reversal")
        elif vol_ratio >= 1.5:
            score += int(_sc.get("volume_strong", 15)); positives.append(f"strong volume {vol_ratio:.1f}x confirms real buying interest, not a fake-out")
        elif vol_ratio >= 1.2:
            score += int(_sc.get("volume_above",   8)); positives.append(f"above-average volume {vol_ratio:.1f}x shows the reversal has participation")

    # EMA20 vs EMA50 (golden cross forming?)
    if not np.isnan(ema20) and not np.isnan(ema50):
        if ema20 > ema50:
            score += int(_sc.get("ema_golden",      15)); positives.append("20-day average crossed above 50-day — a golden cross is forming")
        elif ema20 > ema50 * 0.98:
            score += int(_sc.get("ema_near_golden",  8)); positives.append("20-day average is nearly at the 50-day — golden cross imminent")

    # ADX (new trend starting)
    if not np.isnan(adx) and adx >= 25:
        score += int(_sc.get("adx_trending", 10)); positives.append(f"trend direction gaining conviction (ADX {adx:.0f})")

    # NEW — bottoming-reversal signals
    if was_oversold and rsi_rising:
        score += int(_sc.get("oversold_bounce", 12)); positives.append(f"bouncing out of oversold (RSI bottomed at {rsi_low:.0f}, now curling up)")
    if higher_low:
        score += int(_sc.get("higher_low", 10)); positives.append("holding a higher low — the downtrend is losing its grip")
    if reclaimed_ema20 and deep_bottom:
        score += int(_sc.get("reclaimed_ema20", 8)); positives.append("reclaimed the 20-day average — short-term trend flipped up")
    if catalyst:
        score += int(_sc.get("catalyst_gap", 20)); positives.append(catalyst_desc)

    if score < int(_hg.get("min_score", 45)):
        return None

    t3    = int(_sc.get("three_star", 80))
    t2    = int(_sc.get("two_star",   60))
    stars = 3 if score >= t3 else 2 if score >= t2 else 1

    if deep_bottom:
        stop_m = float(_st.get("deep_bottom_stop_atr_mult",    2.0))
        tgt1_m = float(_st.get("deep_bottom_target1_atr_mult", 4.0))
        tgt2_m = float(_st.get("deep_bottom_target2_atr_mult", 7.5))
    else:
        stop_m = float(_st.get("stop_atr_mult",    1.5))
        tgt1_m = float(_st.get("target1_atr_mult", 3.0))
        tgt2_m = float(_st.get("target2_atr_mult", 6.0))
    stop    = round(price - atr * stop_m,  2)
    target1 = round(price + atr * tgt1_m, 2)
    target2 = round(price + atr * tgt2_m, 2)

    risk     = price - stop
    reward   = target1 - price
    rr       = round(reward / risk, 1) if risk > 0 else 0
    stop_pct = round(risk   / price * 100, 1)
    gain_pct = round(reward / price * 100, 1)

    if rr < float(_hg.get("min_rr", 1.5)):
        return None

    _prefix = {"deep_bottom": "Deep-downtrend reversal — ",
               "catalyst":    "Catalyst play — "}.get(setup_type, "")
    _why = _prefix + " ".join(p + "." for p in positives[:3])
    _why += f" Stop ${stop:.2f} ({stop_pct}% risk), first target ${target1:.2f} (+{gain_pct}%), R:R {rr:.1f}:1."

    return MomentumReversalSignal(
        ticker=ticker, price=round(price, 2), sector=_sector_of(ticker),
        stop=stop, target1=target1, target2=target2,
        stop_pct=stop_pct, gain_pct=gain_pct, rr=rr,
        stars=stars, why=_why,
        rsi=round(rsi, 1)       if not np.isnan(rsi)       else 0.0,
        adx=round(adx, 1)       if not np.isnan(adx)       else 0.0,
        vol_ratio=round(vol_ratio, 1) if not np.isnan(vol_ratio) else 0.0,
        macd_hist=round(macd_h, 4),
        macd_cross_days_ago=macd_cross_days_ago if macd_cross_days_ago is not None else 99,
        above_200ma=above_200ma,
        setup_type=setup_type, catalyst=catalyst_desc,
    )


# ─────────────────────────────────────────────────────────────────────────────
# LIVE DATA OVERLAY  (replaces today's bar with real-time Finnhub data)
# ─────────────────────────────────────────────────────────────────────────────

# Floor on the session fraction used for volume projection. Early in the day the
# extrapolation "volume_so_far / fraction_elapsed" is extremely noisy — a small
# opening burst implies an absurd full-day total, producing fake volume surges
# and junk day-trade signals. Flooring the fraction at ~0.35 (≈11:50 AM ET) caps
# the inflation to ~2.85× so-far volume until the extrapolation becomes reliable.
_MIN_PROJECTION_FRAC = 0.35


def _market_fraction_elapsed() -> float:
    """Fraction of the 6.5-hour US trading day elapsed (0.0 pre-open → 1.0 post-close)."""
    from datetime import datetime
    now   = datetime.now()
    open_ = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    if now <= open_:  return 0.0
    if now >= close:  return 1.0
    return (now - open_).total_seconds() / (close - open_).total_seconds()


def _overlay_live_bars(bar_map: Dict, progress_cb=None) -> Dict:
    """
    Batch-fetch live Finnhub quotes for every ticker and overlay them as
    today's OHLCV bar so that scoring uses real-time data, not yesterday's close.

    Volume is projected to end-of-day based on how much of the session has passed,
    which gives an apples-to-apples comparison against the 20-day average.
    """
    tickers = [t for t in bar_map if t != "SPY"]
    if not tickers:
        return bar_map

    if progress_cb:
        progress_cb(38, f"Fetching live quotes for {len(tickers)} tickers…")

    # Batch via Finnhub (parallel, already rate-limited inside get_batch_quotes)
    CHUNK = 200
    quotes: Dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        # fallback=False: under Finnhub rate-limiting, skip slow per-ticker
        # yfinance retries — those tickers just keep their daily-close bar.
        futs = [ex.submit(get_batch_quotes, tickers[i: i + CHUNK], False)
                for i in range(0, len(tickers), CHUNK)]
        for fut in concurrent.futures.as_completed(futs):
            try:
                quotes.update(fut.result())
            except Exception as e:
                log.warning("Live quote batch failed: %s", e)

    frac = _market_fraction_elapsed()
    today = pd.Timestamp.now().normalize()

    for ticker, q in quotes.items():
        if ticker not in bar_map:
            continue
        price  = q.get("price",  0)
        open_  = q.get("open",   price)
        high   = q.get("high",   price)
        low    = q.get("low",    price)
        vol    = q.get("volume", 0)
        if not price or price <= 0:
            continue

        # Project intraday volume to a full-day equivalent for a fair comparison
        # against the 20-day average. Floor the fraction (see _MIN_PROJECTION_FRAC)
        # so early-session bursts don't extrapolate into fake volume surges.
        proj_vol = int(vol / max(frac, _MIN_PROJECTION_FRAC)) if frac > 0 else vol

        df = bar_map[ticker]
        raw_cols = ["open", "high", "low", "close", "volume"]

        # Either update today's existing bar or append a new one
        if len(df) > 0 and df.index[-1].normalize() == today:
            for col, val in zip(raw_cols, [open_, high, low, price, proj_vol]):
                df.at[df.index[-1], col] = val
            bar_map[ticker] = df[raw_cols]
        else:
            new_row = pd.DataFrame(
                [[open_, high, low, price, proj_vol]],
                columns=raw_cols,
                index=[today],
            )
            bar_map[ticker] = pd.concat(
                [df[[c for c in raw_cols if c in df.columns]], new_row]
            )

    if progress_cb:
        progress_cb(42, "Live quotes applied. Computing indicators…")

    return bar_map


# ─────────────────────────────────────────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────────────────────────────────────────

def run_daily_scan(
    regime: dict,
    price_min: float = 2.0,
    price_max: float = 500.0,
    progress_cb=None,
    live: bool = True,
) -> Tuple[List, List, List]:
    """
    Returns (swing_signals, day_signals, vcp_signals).
    Loads from cache when available; downloads only when needed.
    """
    regime_str = regime.get("regime", "BULL_QUIET")

    # ── 1. Load / refresh data ────────────────────────────────────────────────
    bar_map  = _load_cache()
    age_hrs  = (time.time() - os.path.getmtime(_CACHE_FILE)) / 3600 if bar_map else float("inf")

    if bar_map is None:
        if progress_cb: progress_cb(5, f"First run — downloading {len(ALL_TICKERS)} tickers (one-time, ~5 min)…")
        raw = _download_parallel(list(set(ALL_TICKERS + ["SPY"])), period="1y")
        bar_map = raw
        _save_cache(bar_map)
        if progress_cb: progress_cb(35, f"Downloaded {len(bar_map)} tickers. Scoring…")

    elif age_hrs >= _FULL_TTL_DAYS * 24:
        if progress_cb: progress_cb(5, f"Weekly refresh — re-downloading {len(ALL_TICKERS)} tickers…")
        raw = _download_parallel(list(set(ALL_TICKERS + ["SPY"])), period="1y")
        bar_map = raw
        _save_cache(bar_map)
        if progress_cb: progress_cb(35, f"Downloaded {len(bar_map)} tickers. Scoring…")

    elif age_hrs >= _INC_TTL_HRS:
        if progress_cb: progress_cb(5, f"Daily update — fetching last 5 days for {len(bar_map)} tickers…")
        bar_map = _incremental_update(bar_map)
        _save_cache(bar_map)
        if progress_cb: progress_cb(35, f"Updated. Scoring {len(bar_map)} tickers…")

    else:
        if progress_cb: progress_cb(35, f"Cache is fresh ({age_hrs*60:.0f} min old). Fetching live quotes…")

    # ── 1b. Backfill any universe tickers missing from the cache ──────────────
    # (e.g. newly added names) so universe edits take effect on the next scan
    # without waiting for the weekly full re-download.
    if bar_map:
        missing = [t for t in dict.fromkeys(ALL_TICKERS + ["SPY"]) if t not in bar_map]
        if missing:
            if progress_cb: progress_cb(32, f"Fetching {len(missing)} newly added tickers…")
            fresh = _download_parallel(missing, period="1y")
            if fresh:
                bar_map.update(fresh)
                _save_cache(bar_map)

    # ── 2. Overlay live Finnhub data on today's bar ───────────────────────────
    if live:
        bar_map = _overlay_live_bars(bar_map, progress_cb)

    # ── 3. RS scores & Trend Template (need only close prices — raw bars) ──────
    # Indicator computation is deferred into the parallel scoring stage below so
    # it runs across 4 threads instead of one serial pass over ~780 tickers.
    spy_df  = bar_map.get("SPY")
    spy_raw = _rs_raw(spy_df, 0.0) if spy_df is not None else 0.0

    rs_scores = {t: _rs_raw(df, spy_raw) for t, df in bar_map.items() if t != "SPY"}
    all_rs    = list(rs_scores.values())

    tt_scores = {t: _trend_template(df) for t, df in bar_map.items() if t != "SPY"}

    if progress_cb: progress_cb(48, "Scoring signals…")

    # ── 5. Score every ticker ─────────────────────────────────────────────────
    # Split into 4 chunks and score each chunk in its own thread.
    # Each thread runs its chunk serially (avoids thread-safety issues with
    # score_entry/pandas) while 4 chunks run concurrently → ~3-4× speedup.
    swing_signals, day_signals, vcp_signals, momentum_signals = [], [], [], []

    tickers_to_score = [(t, df) for t, df in bar_map.items() if t != "SPY"]

    # Swing hard gates (config cached in config.py — read-only, thread-safe)
    _sw_gates = get_section("swing.entry.hard_gates", {})
    _rs_gate  = float(_sw_gates.get("rs_rank_min", 0))
    _tt_gate  = int(_sw_gates.get("trend_template_min", 0))

    def _score_chunk(chunk):
        """Score a slice of tickers serially — thread-safe, no shared writes.
        Indicators are computed here (inside the worker) so the cost spreads
        across the 4 chunk threads, and price-filtered tickers skip it entirely."""
        sw_list, dt_list, vcp_list, mr_list = [], [], [], []
        for ticker, raw_df in chunk:
            try:
                price = float(raw_df["close"].iloc[-1])
                if not (price_min <= price <= price_max):
                    continue
                df = compute_indicators(raw_df)   # deferred from the serial pass
                rs = _rs_rank(rs_scores.get(ticker, 0.0), all_rs)
                tt = tt_scores.get(ticker, 0)

                sw = score_entry(ticker, {"regime": regime_str}, _df=df)
                if sw:
                    if rs < _rs_gate or tt < _tt_gate:
                        pass
                    else:
                        sw.rs_rank = rs; sw.trend_template = tt
                        sw_list.append(sw)

                dt = _score_day(ticker, df)
                if dt:
                    dt_list.append(dt)

                if _sector_of(ticker) != "ETFs":
                    vcp = detect_vcp(ticker, df)
                    if vcp:
                        vcp.rs_rank = rs; vcp.trend_template = tt
                        vcp_list.append(vcp)

                    mr = _score_momentum_reversal(ticker, df)
                    if mr:
                        mr_list.append(mr)
            except Exception as e:
                log.debug("Scoring failed for %s: %s", ticker, e)
                continue
        return sw_list, dt_list, vcp_list, mr_list

    n = len(tickers_to_score)
    chunk_size = max(1, n // 4)
    chunks = [tickers_to_score[i: i + chunk_size] for i in range(0, n, chunk_size)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        for sw_l, dt_l, vcp_l, mr_l in ex.map(_score_chunk, chunks):
            swing_signals.extend(sw_l)
            day_signals.extend(dt_l)
            vcp_signals.extend(vcp_l)
            momentum_signals.extend(mr_l)

    # ── 6. Sort ───────────────────────────────────────────────────────────────
    def swing_key(s):
        return (2 if s.trend_template >= 6 else 1 if s.trend_template >= 4 else 0,
                2 if s.rs_rank >= 80    else 1 if s.rs_rank >= 60    else 0,
                s.stars, s.rr)

    swing_signals.sort(   key=swing_key,                                    reverse=True)
    day_signals.sort(     key=lambda x: (x.stars, x.vol_ratio),             reverse=True)
    vcp_signals.sort(     key=lambda x: (x.stars, x.rs_rank),               reverse=True)
    momentum_signals.sort(key=lambda x: (x.stars, x.rr, -x.macd_cross_days_ago), reverse=True)

    if progress_cb: progress_cb(100, "Done!")
    return swing_signals, day_signals, vcp_signals, momentum_signals


def run_and_cache_scan(regime, progress_cb=None):
    """
    Run a full scan across the widest price range and persist the result for the
    web app to load instantly. Called by the background daemon on its cycle.
    Scoring is price-range-independent, so the app filters the stored signals by
    each user's price filter post-hoc — one daemon scan serves every user.
    """
    regime_str = regime.get("regime", "BULL_QUIET") if isinstance(regime, dict) else str(regime)
    sw, dt, vcp, mom = run_daily_scan(
        {"regime": regime_str}, price_min=1.0, price_max=1e9, progress_cb=progress_cb, live=False
    )
    save_scan_result(regime_str, sw, dt, vcp, mom)
    log.info("Scan cached for web app: swing=%d day=%d vcp=%d momentum=%d",
             len(sw), len(dt), len(vcp), len(mom))
    return sw, dt, vcp, mom
