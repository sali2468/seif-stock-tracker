"""
StockPal — Trading Dashboard
Run: streamlit run app.py
"""

import os
import streamlit as st

# Streamlit Cloud provides secrets via st.secrets; the app reads os.getenv().
# Bridge scalar secrets into the environment so every getenv() keeps working in
# the cloud. No-op locally (no secrets.toml) and never overrides an existing var.
try:
    for _sk, _sv in st.secrets.items():
        if isinstance(_sv, (str, int, float, bool)):
            os.environ.setdefault(_sk, str(_sv))
except Exception:
    pass

import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import numpy as np
import time as _time
from datetime import date, datetime

from log_setup import setup_logging
setup_logging()   # configure app-wide logging once (writes to veterans_edge.log)

st.set_page_config(
    page_title="StockPal",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ══════════════════════════════════════════════════════════════════════════════
# THEME — inject first so the login page is styled too
# ══════════════════════════════════════════════════════════════════════════════
from styles import get_css
st.session_state.setdefault("theme", "dark")
st.markdown(get_css(st.session_state["theme"]), unsafe_allow_html=True)

# ── Auth gate — accounts + "stay logged in" cookie (15-min idle) ──────────────
from datetime import timedelta as _timedelta
from auth import require_login, read_token, make_token, current_user, is_admin
try:
    import extra_streamlit_components as _stx
    _cm = _stx.CookieManager(key="ve_cm")
    _cookies = _cm.get_all(key="ve_cm_all") or {}
except Exception:
    _cm, _cookies = None, {}

# Restore session from a valid cookie (survives page refresh)
if not st.session_state.get("authenticated") and _cookies.get("ve_auth"):
    _ur = read_token(_cookies["ve_auth"])
    if _ur:
        st.session_state.update(authenticated=True, username=_ur[0], role=_ur[1])

if not require_login():
    st.stop()

# Refresh the sliding 15-min cookie (throttled to avoid write loops)
if _cm is not None and (_time.time() - st.session_state.get("_cookie_ts", 0) > 45):
    try:
        _cm.set("ve_auth",
                make_token(current_user(), st.session_state.get("role", "user")),
                expires_at=datetime.now() + _timedelta(minutes=15),
                key=f"ve_set_{int(_time.time())}")
        st.session_state["_cookie_ts"] = _time.time()
    except Exception:
        pass

# (sidebar auto-expand removed — top nav bar handles navigation when sidebar is hidden)

# ── Imports ───────────────────────────────────────────────────────────────────
from market_data import (
    get_bars_batch, compute_indicators, ema200_slope,
    get_option_price, days_to_expiry, get_price_change,
)
from signals import score_entry, PositionStatus
from state import (
    get_positions, get_closed, get_watchlist,
    add_position, close_position, update_stop, update_targets,
    add_to_watchlist, remove_from_watchlist, set_watchlist, load_sp500,
    add_option, get_options, get_closed_options,
    close_option, mark_option_alerted, mark_position_alerted,
)
from alerts import (
    alert_stock_signal, alert_stock_stop, alert_stock_target,
    alert_stock_raise_stop, alert_stock_exit_stale,
    alert_stock_overbought_exit, alert_daily_briefing,
    alert_sell_signal, alert_watch_closely,
    alert_option_target, alert_option_stop, alert_option_near_expiry,
    test_alert, TOKEN, CHAT_ID,
)
from chart_analysis import analyze_ticker as chart_analyze
from market_data import get_batch_quotes

# ── Moomoo / Futu integration (soft import — ok if futu-api not installed) ───
try:
    from moomoo_integration import (
        MoomooTrader, execute_trade_plan,
        validate_trade, FUTU_AVAILABLE,
    )
    from moomoo_state import (
        get_active_plans, add_plan, create_plan_dict,
    )
    import os as _mm_os
    _MM_HOST   = _mm_os.getenv("MOOMOO_HOST", "127.0.0.1")
    _MM_PORT   = int(_mm_os.getenv("MOOMOO_PORT", "11111"))
    _MM_ACC_ID = int(_mm_os.getenv("MOOMOO_CASH_ACC_ID", "0"))
    MOOMOO_READY = True
except Exception:
    FUTU_AVAILABLE = False
    MOOMOO_READY   = False
    _MM_HOST, _MM_PORT, _MM_ACC_ID = "127.0.0.1", 11111, 0


# ══════════════════════════════════════════════════════════════════════════════
# LIVE MARKET BAR — SPY / QQQ / IWM / VIX via WebSocket, shown on every page
# ══════════════════════════════════════════════════════════════════════════════
def _iframe_head_css() -> str:
    """Theme-aware CSS prelude for content rendered inside a components.html
    iframe. CSS variables and the app's @import font do NOT cross the iframe
    boundary, so each iframe re-declares its :root tokens + the Geist font,
    keyed off the current theme so colors AND font switch with light/dark mode."""
    light = st.session_state.get("theme", "dark") == "light"
    t = {
        "bg":      "#ffffff"         if light else "#0d0f10",
        "surface": "#f6f7f9"         if light else "rgba(255,255,255,.035)",
        "border":  "rgba(0,0,0,.10)" if light else "rgba(255,255,255,.09)",
        "fg":      "#0c1013"         if light else "#f5f7f8",
        "muted":   "#4b5560"         if light else "#9ca3a8",
        "faint":   "#5c646e"         if light else "#6b7280",
        "dim":     "#6d7681"         if light else "#4b5257",
        "pos":     "#00a406"         if light else "#00c805",
        "neg":     "#e5352b"         if light else "#ff5000",
    }
    return ("@import url('https://fonts.googleapis.com/css2?"
            "family=Geist:wght@400;500;600;700;800&family=Geist+Mono:wght@400;500;600&display=swap');"
            ":root{" + "".join(f"--{k}:{v};" for k, v in t.items()) + "}")


def live_market_bar():
    """Top-of-page market pulse: SPY QQQ IWM + VIX. Updates on every trade tick."""
    import json as _json, os as _os
    api_key = _os.getenv("FINNHUB_API_KEY", "")

    # Seed values from Finnhub REST so something shows before first WS tick
    market_tickers = ["SPY", "QQQ", "IWM"]
    seed = {}
    try:
        quotes = get_batch_quotes(market_tickers)
        for t in market_tickers:
            q = quotes.get(t, {})
            seed[t] = {"price": q.get("price", 0), "pct": q.get("pct", 0)}
    except Exception:
        for t in market_tickers:
            seed[t] = {"price": 0, "pct": 0}

    vix_val = 0.0
    try:
        from market_data import get_vix
        vix_val = get_vix()
    except Exception:
        pass

    seed_json = _json.dumps(seed)

    html = f"""<!DOCTYPE html><html><head><style>
    {_iframe_head_css()}
    *{{margin:0;padding:0;box-sizing:border-box;}}
    body{{background:transparent;font-family:'Geist',ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;overflow:visible;}}
    #bar{{display:flex;align-items:center;gap:18px;padding:4px 0 2px;flex-wrap:nowrap;}}
    .mkt{{display:inline-flex;align-items:center;gap:7px;
          background:var(--surface);border:1px solid var(--border);
          border-radius:8px;padding:5px 12px;}}
    .sym{{font-weight:700;font-size:.82rem;color:var(--faint);letter-spacing:.04em;}}
    .prc{{font-weight:800;font-size:.92rem;color:var(--fg);transition:color .15s;}}
    .chg{{font-size:.78rem;font-weight:600;}}
    .vix{{display:inline-flex;align-items:center;gap:7px;
          background:var(--surface);border:1px solid var(--border);
          border-radius:8px;padding:5px 12px;}}
    .vix-val{{font-weight:800;font-size:.92rem;}}
    .dot{{width:6px;height:6px;border-radius:50%;background:var(--pos);
          animation:pulse 1.4s ease-in-out infinite;margin-right:2px;}}
    @keyframes pulse{{0%,100%{{opacity:1;}}50%{{opacity:.3;}}}}
    .flash-u{{color:var(--pos)!important;}} .flash-d{{color:var(--neg)!important;}}
    </style></head><body>
    <div id="bar">
      <div style="display:flex;align-items:center;gap:4px">
        <div class="dot"></div>
        <span style="color:var(--dim);font-size:.68rem;font-weight:700;letter-spacing:.06em">LIVE MARKET</span>
      </div>
    </div>
    <script>
    const API_KEY = '{api_key}';
    const seed    = {seed_json};
    const mTickers = {_json.dumps(market_tickers)};
    const prices  = {{}};
    const pcts    = {{}};

    const bar = document.getElementById('bar');

    // Build market chips
    mTickers.forEach(sym => {{
      const s = seed[sym] || {{}};
      prices[sym] = s.price || 0;
      pcts[sym]   = s.pct   || 0;
      const el = document.createElement('div');
      el.className = 'mkt'; el.id = 'mkt-'+sym;
      el.innerHTML = chipHTML(sym, prices[sym], pcts[sym]);
      bar.appendChild(el);
    }});

    // VIX chip (static REST, no WS for indices)
    const vixEl = document.createElement('div');
    vixEl.className = 'vix'; vixEl.id = 'vix-chip';
    const vv = {vix_val};
    const vixColor = vv >= 30 ? 'var(--neg)' : vv >= 20 ? '#f59e0b' : 'var(--pos)';
    vixEl.innerHTML = `<span class="sym">VIX</span><span class="vix-val" style="color:${{vixColor}}">${{vv.toFixed(1)}}</span>`;
    bar.appendChild(vixEl);

    function chipHTML(sym, price, pct) {{
      const c = pct >= 0 ? 'var(--pos)' : 'var(--neg)';
      const a = pct >= 0 ? '▲' : '▼';
      return `<span class="sym">${{sym}}</span>
              <span class="prc" id="mp-${{sym}}">${{price > 0 ? '$'+price.toFixed(2) : '—'}}</span>
              <span class="chg" id="mc-${{sym}}" style="color:${{c}}">${{a}} ${{Math.abs(pct).toFixed(2)}}%</span>`;
    }}

    function updateMarket(sym, price) {{
      const prev = prices[sym] || price;
      const pc   = prev > 0 ? prev : price;
      // estimate prev close from current pct
      const prevClose = price / (1 + (pcts[sym]||0)/100);
      const pct  = prevClose > 0 ? (price - prevClose) / prevClose * 100 : 0;
      prices[sym] = price; pcts[sym] = pct;

      const pEl = document.getElementById('mp-'+sym);
      const cEl = document.getElementById('mc-'+sym);
      if (!pEl) return;
      const flash = price > prev ? 'flash-u' : 'flash-d';
      pEl.classList.add(flash); setTimeout(()=>pEl.classList.remove(flash), 350);
      pEl.textContent = '$' + price.toFixed(2);
      const col = pct >= 0 ? 'var(--pos)' : 'var(--neg)';
      cEl.style.color = col;
      cEl.textContent = (pct>=0?'▲':'▼') + ' ' + Math.abs(pct).toFixed(2) + '%';
    }}

    if (API_KEY) {{
      function connect() {{
        const ws = new WebSocket('wss://ws.finnhub.io?token='+API_KEY);
        ws.onopen = () => mTickers.forEach(s => ws.send(JSON.stringify({{type:'subscribe',symbol:s}})));
        ws.onmessage = evt => {{
          try {{
            const msg = JSON.parse(evt.data);
            if (msg.type==='trade' && msg.data) {{
              const latest={{}};
              msg.data.forEach(t=>{{latest[t.s]=t.p;}});
              Object.entries(latest).forEach(([s,p])=>{{if(mTickers.includes(s))updateMarket(s,p);}});
            }}
          }} catch(e) {{}}
        }};
        ws.onerror = ()=>ws.close();
        ws.onclose = ()=>setTimeout(connect,3000);
      }}
      connect();
    }}
    </script></body></html>"""

    import streamlit.components.v1 as components
    # Pull the iframe flush: negative margin cancels Streamlit's block-container padding
    st.markdown('<div style="margin:-1rem -2.5rem 0;padding:0">', unsafe_allow_html=True)
    components.html(html, height=48, scrolling=False)
    st.markdown('</div>', unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# SINGLE TICKER LIVE PRICE — Finnhub WebSocket for Analyze page
# ══════════════════════════════════════════════════════════════════════════════
def live_single_ticker_bar(ticker: str, seed_price: float, seed_pct: float):
    """Live price badge for one ticker — used on the Analyze page."""
    import os as _os
    api_key = _os.getenv("FINNHUB_API_KEY", "")
    chg_col  = "var(--pos)" if seed_pct >= 0 else "var(--neg)"
    chg_icon = "▲" if seed_pct >= 0 else "▼"
    safe_pct = round(seed_pct, 4)

    html = f"""<!DOCTYPE html><html><head><style>
    {_iframe_head_css()}
    *{{margin:0;padding:0;box-sizing:border-box;}}
    body{{background:transparent;font-family:'Geist',ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;overflow:visible;}}
    #wrap{{display:flex;flex-direction:column;justify-content:center;padding:4px 0 6px;}}
    #prc{{font-size:2.4rem;font-weight:800;color:var(--fg);transition:color .15s;line-height:1.1;}}
    #chg{{font-size:1.1rem;font-weight:700;margin-top:2px;}}
    #lbl{{color:var(--dim);font-size:.72rem;display:flex;align-items:center;gap:5px;margin-top:6px;}}
    .dot{{width:7px;height:7px;border-radius:50%;background:var(--pos);animation:pulse 1.4s ease-in-out infinite;flex-shrink:0;}}
    @keyframes pulse{{0%,100%{{opacity:1;}}50%{{opacity:.3;}}}}
    .flash-u{{color:var(--pos)!important;}} .flash-d{{color:var(--neg)!important;}}
    </style></head><body>
    <div id="wrap">
      <div id="prc">${seed_price:.2f}</div>
      <div id="chg" style="color:{chg_col}">{chg_icon} {abs(seed_pct):.2f}%</div>
      <div id="lbl"><div class="dot"></div><span>Live · Finnhub WebSocket</span></div>
    </div>
    <script>
    const API_KEY  = '{api_key}';
    const SYM      = '{ticker}';
    let   price    = {seed_price:.4f};
    const prevClose = price / (1 + {safe_pct}/100);

    function update(p) {{
      const prev = price; price = p;
      const pct  = prevClose > 0 ? (price - prevClose)/prevClose*100 : 0;
      const pEl  = document.getElementById('prc');
      const cEl  = document.getElementById('chg');
      const fl   = p > prev ? 'flash-u' : 'flash-d';
      pEl.classList.add(fl); setTimeout(()=>pEl.classList.remove(fl), 350);
      pEl.textContent = '$' + price.toFixed(2);
      const col = pct >= 0 ? 'var(--pos)' : 'var(--neg)';
      cEl.style.color = col;
      cEl.textContent = (pct>=0?'▲':'▼') + ' ' + Math.abs(pct).toFixed(2) + '%';
    }}

    if (API_KEY) {{
      function connect() {{
        const ws = new WebSocket('wss://ws.finnhub.io?token=' + API_KEY);
        ws.onopen    = () => ws.send(JSON.stringify({{type:'subscribe', symbol:SYM}}));
        ws.onmessage = evt => {{
          try {{
            const msg = JSON.parse(evt.data);
            if (msg.type === 'trade' && msg.data) {{
              const latest = {{}};
              msg.data.forEach(t => {{ latest[t.s] = t.p; }});
              if (latest[SYM] !== undefined) update(latest[SYM]);
            }}
          }} catch(e) {{}}
        }};
        ws.onerror = () => ws.close();
        ws.onclose = () => setTimeout(connect, 3000);
      }}
      connect();
    }}
    </script></body></html>"""

    import streamlit.components.v1 as components
    components.html(html, height=110, scrolling=False)


# ══════════════════════════════════════════════════════════════════════════════
# TRULY LIVE PRICE TICKER — Finnhub WebSocket, prices update on every trade tick
# No Python polling. Browser connects directly; DOM updates instantly.
# ══════════════════════════════════════════════════════════════════════════════
def live_ticker_strip():
    """
    Renders an HTML/JS component that:
    1. Opens a WebSocket to wss://ws.finnhub.io
    2. Subscribes to every open position ticker
    3. Updates price, P&L, and stop distance in the DOM on every trade tick
    No re-render, no polling — truly live.
    """
    import json as _json
    import os as _os

    positions = get_positions()
    if not positions:
        return

    api_key = _os.getenv("FINNHUB_API_KEY", "")

    # Build initial snapshot (used to seed values before first WS tick arrives)
    pos_data = {}
    for ticker, pos in positions.items():
        price, pct, _ = get_price_change(ticker)
        if price is None:
            price = pos["entry"]
            pct   = 0.0
        pos_data[ticker] = {
            "entry":     pos["entry"],
            "stop":      pos["stop"],
            "target1":   pos.get("target1", 0),
            "qty":       pos.get("qty", 1),
            "price":     price,
            "day_pct":   pct,
        }

    pos_json = _json.dumps(pos_data)
    height   = 88  # chip row + padding — enough for single-row wrap

    html = f"""
<!DOCTYPE html>
<html>
<head>
<style>
  {_iframe_head_css()}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:transparent; font-family:'Geist',ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; overflow:visible; }}
  #ticker-wrap {{
    display:flex; flex-wrap:wrap; align-items:center;
    gap:6px; padding:6px 0 8px;
  }}
  .live-dot {{
    display:inline-flex; align-items:center; gap:5px;
    color:var(--dim); font-size:.7rem; font-weight:600;
    letter-spacing:.06em; text-transform:uppercase; margin-right:4px;
  }}
  .pulse {{
    width:7px; height:7px; border-radius:50%; background:var(--pos);
    animation: pulse 1.4s ease-in-out infinite;
  }}
  @keyframes pulse {{
    0%,100% {{ opacity:1; transform:scale(1); }}
    50%      {{ opacity:.4; transform:scale(.75); }}
  }}
  .chip {{
    display:inline-flex; align-items:center; gap:10px;
    background:var(--surface); border:1px solid var(--border);
    border-radius:10px; padding:8px 14px;
  }}
  .sym  {{ font-weight:800; font-size:.95rem; color:var(--fg); }}
  .prc  {{ font-size:1.05rem; font-weight:700; color:var(--fg);
           transition: color .15s; }}
  .pnl  {{ font-size:.8rem; font-weight:600; }}
  .stp  {{ font-size:.75rem; }}
  .flash-up   {{ color:var(--pos) !important; }}
  .flash-down {{ color:var(--neg) !important; }}
</style>
</head>
<body>
<div id="ticker-wrap">
  <div class="live-dot"><div class="pulse"></div> LIVE</div>
</div>

<script>
const API_KEY   = '{api_key}';
const positions = {pos_json};
const tickers   = Object.keys(positions);
const prices    = {{}};   // symbol → last price
const dayClose  = {{}};   // symbol → previous close (for day % calc)

// ── Build chips from initial snapshot ───────────────────────────────────────
const wrap = document.getElementById('ticker-wrap');

tickers.forEach(sym => {{
  const p   = positions[sym];
  prices[sym]   = p.price;
  // Estimate prev close from day_pct:  prev = price / (1 + day_pct/100)
  dayClose[sym] = p.price / (1 + p.day_pct / 100);

  const chip = document.createElement('div');
  chip.className = 'chip';
  chip.id = 'chip-' + sym;
  chip.innerHTML = chipHTML(sym, p.price, p.entry, p.stop, p.day_pct);
  wrap.appendChild(chip);
}});

function pnlPct(sym, price)  {{ return (price - positions[sym].entry) / positions[sym].entry * 100; }}
function dayPct(sym, price)  {{ return (price - dayClose[sym]) / dayClose[sym] * 100; }}
function stopDist(sym, price){{ return (price - positions[sym].stop)  / price * 100; }}

function pnlColor(v)  {{ return v >= 0 ? 'var(--pos)' : 'var(--neg)'; }}
function stopColor(d) {{ return d < 3 ? 'var(--neg)' : d < 6 ? '#f59e0b' : 'var(--faint)'; }}
function arrow(v)     {{ return v >= 0 ? '▲' : '▼'; }}

function chipHTML(sym, price, entry, stop, dp) {{
  const pnl  = pnlPct(sym, price);
  const day  = typeof dp !== 'undefined' ? dp : dayPct(sym, price);
  const dist = stopDist(sym, price);
  return `
    <span class="sym">${{sym}}</span>
    <span class="prc" id="prc-${{sym}}">${{price.toFixed(2)}}</span>
    <span class="pnl" id="pnl-${{sym}}" style="color:${{pnlColor(pnl)}}">
      ${{arrow(day)}} ${{Math.abs(day).toFixed(2)}}% today &nbsp;|&nbsp; P&L ${{pnl >= 0 ? '+' : ''}}${{pnl.toFixed(2)}}%
    </span>
    <span class="stp" id="stp-${{sym}}" style="color:${{stopColor(dist)}}">
      Stop ${{dist.toFixed(1)}}% away
    </span>`;
}}

// ── Flash animation on price change ─────────────────────────────────────────
function flashPrice(sym, newPrice) {{
  const el = document.getElementById('prc-' + sym);
  if (!el) return;
  const cls = newPrice > prices[sym] ? 'flash-up' : 'flash-down';
  el.classList.add(cls);
  setTimeout(() => el.classList.remove(cls), 400);
}}

// ── Update DOM without re-creating chip ─────────────────────────────────────
function updateChip(sym, price) {{
  const prc = document.getElementById('prc-' + sym);
  const pnl = document.getElementById('pnl-' + sym);
  const stp = document.getElementById('stp-' + sym);
  if (!prc) return;

  flashPrice(sym, price);
  prices[sym] = price;

  const pnlV  = pnlPct(sym, price);
  const dayV  = dayPct(sym, price);
  const distV = stopDist(sym, price);

  prc.textContent = '$' + price.toFixed(2);
  pnl.style.color = pnlColor(pnlV);
  pnl.innerHTML   = arrow(dayV) + ' ' + Math.abs(dayV).toFixed(2) + '% today &nbsp;|&nbsp; P&L '
                  + (pnlV >= 0 ? '+' : '') + pnlV.toFixed(2) + '%';
  stp.style.color = stopColor(distV);
  stp.textContent = 'Stop ' + distV.toFixed(1) + '% away';
}}

// ── Finnhub WebSocket ────────────────────────────────────────────────────────
if (API_KEY) {{
  function connect() {{
    const ws = new WebSocket('wss://ws.finnhub.io?token=' + API_KEY);

    ws.onopen = () => {{
      tickers.forEach(sym => {{
        ws.send(JSON.stringify({{type:'subscribe', symbol:sym}}));
      }});
    }};

    ws.onmessage = (evt) => {{
      try {{
        const msg = JSON.parse(evt.data);
        if (msg.type === 'trade' && msg.data) {{
          // Take the last trade for each symbol in this batch
          const latest = {{}};
          msg.data.forEach(t => {{ latest[t.s] = t.p; }});
          Object.entries(latest).forEach(([sym, price]) => {{
            if (positions[sym]) updateChip(sym, price);
          }});
        }}
      }} catch(e) {{}}
    }};

    ws.onerror  = () => ws.close();
    ws.onclose  = () => setTimeout(connect, 3000);  // auto-reconnect
  }}
  connect();
}} else {{
  // No WS key — fallback REST poll every 5s
  setInterval(() => {{
    tickers.forEach(sym => {{
      fetch('https://finnhub.io/api/v1/quote?symbol=' + sym + '&token=' + API_KEY)
        .then(r => r.json())
        .then(d => {{ if (d.c) updateChip(sym, d.c); }})
        .catch(() => {{}});
    }});
  }}, 5000);
}}
</script>
</body>
</html>
"""
    import streamlit.components.v1 as components
    components.html(html, height=height, scrolling=False)

# ══════════════════════════════════════════════════════════════════════════════
# CACHED FETCHERS
# ══════════════════════════════════════════════════════════════════════════════
from data_access import (
    load_regime,
    cached_score,
    cached_position_check,
    load_chart_df,
    cached_company_info,
    cached_price_change,
    live_quotes_batch,
)


# ══════════════════════════════════════════════════════════════════════════════
# MOOMOO HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _parse_atr(atr_str: str) -> float:
    """Convert signal indicator string like '$1.23' → float 1.23."""
    try:
        return float(str(atr_str).replace("$", "").strip())
    except Exception:
        return 0.0


def _render_moomoo_panel(ticker: str, entry: float, stop: float,
                          target1: float, target2: float,
                          atr: float, key_suffix: str):
    """
    Renders the 'Send to Moomoo' control panel inside an expander.
    Call this directly after the action-button row of any signal card.
    """
    if not MOOMOO_READY:
        st.error("Moomoo module failed to load. Check that `moomoo_integration.py` exists.")
        return

    st.markdown('<div class="mm-panel">', unsafe_allow_html=True)

    # ── Connection / env row ─────────────────────────────────────────────────
    _mc1, _mc2 = st.columns([3, 1])
    _mc1.markdown(
        f'<p style="color:var(--accent);font-weight:700;font-size:.9rem;margin:0">'
        f'🚀 Send <b>{ticker}</b> to Moomoo</p>'
        f'<p style="color:var(--faint);font-size:.78rem;margin:2px 0 12px">'
        f'FutuOpenD: {_MM_HOST}:{_MM_PORT}</p>',
        unsafe_allow_html=True,
    )
    _env_sel = _mc2.selectbox("Mode", ["SIMULATE", "REAL"],
                               key=f"mm_env_{key_suffix}",
                               help="SIMULATE = paper trading, REAL = live money")

    if _env_sel == "REAL":
        st.warning("⚠️ **REAL mode** — this will place live orders with real money!")

    # ── Account buying power ──────────────────────────────────────────────────
    # Only fetched when user clicks "Refresh Balance" — never auto-connects
    # (auto-connect blocks the render thread when FutuOpenD isn't running,
    #  which freezes the entire scanner card loop)
    _bp_cache_key     = f"mm_bp_{key_suffix}"
    _bp_env_cache_key = f"mm_bpenv_{key_suffix}"
    _bp = float(st.session_state.get(_bp_cache_key, 0.0))

    # Manual refresh button
    _conn_col, _bal_col = st.columns([1, 3])
    with _conn_col:
        if st.button("🔄 Refresh Balance", key=f"mm_conn_{key_suffix}", use_container_width=True):
            _mt = MoomooTrader(host=_MM_HOST, port=_MM_PORT, env=_env_sel,
                               acc_id=_MM_ACC_ID if _env_sel == "REAL" else 0)
            _ok, _msg, _info = _mt.get_account_info()
            if _ok:
                _bp = float(_info.get("cash", 0))
                st.session_state[_bp_cache_key]     = _bp
                st.session_state[_bp_env_cache_key] = _env_sel
            else:
                st.error(f"❌ Cannot connect: {_msg}")
    with _bal_col:
        if _bp > 0:
            st.markdown(
                f'<div style="background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.25);'
                f'border-radius:8px;padding:6px 14px;margin-top:4px">'
                f'<span style="color:#86efac;font-size:.8rem;font-weight:700">AVAILABLE CASH</span>'
                f'<span style="color:var(--fg);font-size:1rem;font-weight:700;margin-left:10px">'
                f'${_bp:,.2f}</span>'
                f'<span style="color:var(--faint);font-size:.75rem;margin-left:8px">({_env_sel})</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
        elif not FUTU_AVAILABLE:
            st.info("futu-api not installed — balance unavailable", icon="📦")
        else:
            st.caption("Balance unavailable — check that FutuOpenD is running")

    st.markdown('<div class="thin-div" style="margin:10px 0"></div>', unsafe_allow_html=True)

    # ── Sizing ───────────────────────────────────────────────────────────────
    _risk_pct_def  = 1.0
    _rps           = max(0.0001, entry - stop)          # risk per share
    _acct          = _bp if _bp > 0 else 2248.0         # account size for risk calc
    # Suggested qty by risk %, capped by what cash can actually buy
    _max_by_cash   = int(_bp / entry) if (_bp > 0 and entry > 0) else 9999
    _by_risk       = max(1, int((_acct * _risk_pct_def / 100) / _rps))
    _suggested_qty = min(_by_risk, _max_by_cash) if _bp > 0 else _by_risk

    _sz1, _sz2, _sz3 = st.columns(3)
    _qty   = _sz1.number_input("Shares (50 % entry)", min_value=1, step=1,
                                value=max(1, _suggested_qty),
                                key=f"mm_qty_{key_suffix}",
                                help="50% of full position now; bot adds the rest on pullbacks")
    _ep    = _sz2.number_input("Entry price $", value=float(entry), format="%.2f",
                                key=f"mm_ep_{key_suffix}")
    _rskp  = _sz3.number_input("Risk % of acct", value=_risk_pct_def, min_value=0.1,
                                max_value=5.0, step=0.1,
                                key=f"mm_rsk_{key_suffix}",
                                help="Max % of account balance to risk on this trade")

    # ── Risk + Cash validator ─────────────────────────────────────────────────
    if MOOMOO_READY:
        _val  = validate_trade(_ep, stop, target1, target2, _acct, _rskp,
                               cash_balance=_bp)   # pass live cash for sufficiency check

        _vr_col = "var(--pos)" if _val["ok"] else "var(--neg)"
        _vr_ico = "✅" if _val["ok"] else "❌"

        _order_cost  = _qty * _ep
        _cash_ok     = (_bp <= 0) or (_order_cost <= _bp)   # ok if no live cash OR cost fits
        _cost_pct    = (_order_cost / _bp * 100) if _bp > 0 else 0
        _cost_color  = "var(--pos)" if _cash_ok else "var(--neg)"

        st.markdown(
            f'<div style="background:rgba(15,23,42,.8);border:1px solid {_vr_col}33;'
            f'border-left:3px solid {_vr_col};border-radius:8px;padding:10px 14px;margin:8px 0">'
            f'<span style="color:{_vr_col};font-weight:700;font-size:.85rem">{_vr_ico} Pre-trade check</span><br>'
            f'<span style="color:var(--muted);font-size:.8rem">'
            f'R:R to T2 <b>{_val["rr2"]}:1</b> &nbsp;·&nbsp; '
            f'Risk ${_val["risk_dollars"]:,.0f} &nbsp;·&nbsp; '
            f'${_ep - stop:.2f}/share &nbsp;·&nbsp; '
            f'Suggested qty: <b>{_val["suggested_qty"]}</b>'
            f'</span>'
            + (
                f'<br><span style="color:{_cost_color};font-size:.8rem">'
                f'{"✅" if _cash_ok else "❌"} Order cost: <b>${_order_cost:,.2f}</b>'
                f'{f" ({_cost_pct:.0f}% of ${_bp:,.2f} cash)" if _bp > 0 else ""}'
                f'</span>'
            )
            + f'</div>',
            unsafe_allow_html=True,
        )
        # ── Cash insufficiency hard error ─────────────────────────────────────
        if _bp > 0 and _order_cost > _bp:
            st.error(
                f"❌ Not enough cash — {_qty} × ${_ep:.2f} = **${_order_cost:,.2f}** "
                f"but your {_env_sel} account only has **${_bp:,.2f}**.\n\n"
                f"Reduce shares to **{int(_bp // _ep)}** or less."
            )
        elif _bp > 0 and _order_cost > _bp * 0.90:
            st.warning(
                f"⚠️ This order uses {_cost_pct:.0f}% of your available cash "
                f"(${_order_cost:,.2f} of ${_bp:,.2f}). Little room for other trades."
            )
        for _e in _val["errors"]:
            st.error(_e)
        for _w in _val["warnings"]:
            st.warning(_w)

    # ── Stop / target preview ─────────────────────────────────────────────────
    st.markdown(
        f'<div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0">'
        f'<span class="pill">🛑 Stop <b>${stop:.2f}</b></span>'
        f'<span class="pill">🎯 T1 (2R) <b>${target1:.2f}</b></span>'
        f'<span class="pill">🎯 T2 (3R) <b>${target2:.2f}</b></span>'
        f'<span class="pill">ATR <b>${atr:.2f}</b></span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── Execute button ────────────────────────────────────────────────────────
    # Block if: futu not installed, R/R check fails, or order cost > available cash
    _cash_ok      = (_bp <= 0) or ((_qty * _ep) <= _bp)
    _can_execute  = FUTU_AVAILABLE and (_val["ok"] if MOOMOO_READY else False) and _cash_ok

    if not FUTU_AVAILABLE:
        st.info("📦 Install futu-api to enable live order execution:\n```\npip install futu-api\n```")

    _ex_disabled = not _can_execute
    if st.button(
        f"🚀 Execute Trade Plan ({_env_sel})",
        key=f"mm_exec_{key_suffix}",
        type="primary",
        use_container_width=True,
        disabled=_ex_disabled,
    ):
        # ── Live cash check immediately before placing (belt-and-suspenders) ──
        _pre_mt   = MoomooTrader(host=_MM_HOST, port=_MM_PORT, env=_env_sel,
                                 acc_id=_MM_ACC_ID if _env_sel == "REAL" else 0)
        _pre_ok, _pre_msg, _pre_info = _pre_mt.get_account_info()
        if _pre_ok:
            _live_cash = float(_pre_info.get("cash", 0))
            # Update cached balance
            st.session_state[f"mm_bp_{key_suffix}"] = _live_cash
            _order_total = _qty * _ep
            if _order_total > _live_cash:
                st.error(
                    f"❌ Blocked — order would cost **${_order_total:,.2f}** "
                    f"but your {_env_sel} account only has **${_live_cash:,.2f}** cash. "
                    f"Max shares at ${_ep:.2f}: **{int(_live_cash // _ep)}**."
                )
                st.stop()
        elif _env_sel == "REAL":
            st.error(f"❌ Could not verify account balance before placing order: {_pre_msg}")
            st.stop()

        # Build plan
        _ema_approx = entry * 0.99   # rough stand-in; monitor uses live data
        _plan = create_plan_dict(
            ticker=ticker, entry_price=_ep, qty=_qty,
            stop_price=stop, target1_price=target1, target2_price=target2,
            atr=atr if atr > 0 else _rps, ema21=_ema_approx,
            trd_env=_env_sel, notes="",
        )
        with st.spinner("Placing orders with Moomoo…"):
            _ok2, _msg2, _plan2 = execute_trade_plan(
                _plan, host=_MM_HOST, port=_MM_PORT,
                acc_id=_MM_ACC_ID if _env_sel == "REAL" else 0,
            )
        if _ok2:
            add_plan(_plan2)
            # Refresh cached balance after trade
            _post_ok, _, _post_info = _pre_mt.get_account_info()
            if _post_ok:
                st.session_state[f"mm_bp_{key_suffix}"] = float(_post_info.get("cash", 0))
            st.success(
                f"Trade plan for {ticker} is live!  "
                f"Entry: {_plan2['orders']['entry_order_id']}  "
                f"Stop: {_plan2['orders']['stop_order_id']}  "
                f"TP1: {_plan2['orders']['target1_order_id']}  "
                f"TP2: {_plan2['orders']['target2_order_id']}  "
                f"Monitor it on the Moomoo tab."
            )
        else:
            st.error(f"❌ Failed: {_msg2}")

    st.markdown('</div>', unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════
from ui_components import (
    _page_header,
    _metric_tile,
    _star_color,
    _sig_type_badge,
    _watch_banner_html,
    _ipo_status_badge,
    _ipo_countdown,
    regime_plain,
    action_badge,
    sector_badge,
)


def _merged_action(pos_status, chart: dict) -> tuple:
    """
    Return (action, color, reason) — always the more conservative of the two
    systems.  chart_analyze can escalate a HOLD to WATCH CLOSELY but cannot
    override a hard EXIT / TAKE PROFIT that check_position already issued.
    """
    if not chart or "error" in chart:
        return pos_status.action, pos_status.action_color, pos_status.plain_reason

    hard_actions = {"EXIT NOW", "TAKE PROFIT", "TIGHTEN STOP", "WATCH CLOSELY", "RAISE STOP"}
    if pos_status.action in hard_actions:
        return pos_status.action, pos_status.action_color, pos_status.plain_reason

    # check_position said HOLD — see if chart analysis disagrees
    c_action = chart.get("action", "HOLD")
    c_conf   = chart.get("confidence", "Low")
    c_head   = chart.get("headline", "")

    if c_action == "SELL" and c_conf in ("High", "Medium"):
        return (
            "WATCH CLOSELY", "#FFA500",
            pos_status.plain_reason +
            f"  ⚠️ Chart analysis also flags: {c_head}",
        )
    return pos_status.action, pos_status.action_color, pos_status.plain_reason


def _show_ai_result(result: dict):
    """Render a styled Claude AI analysis result card."""
    if not result:
        return

    if "error" in result:
        err = result["error"]
        if err == "no_key":
            st.warning(
                "🔑 **Anthropic API key missing.**  "
                "Add `ANTHROPIC_API_KEY=sk-ant-...` to your `.env` file and restart the app to enable AI analysis."
            )
        elif err == "no_package":
            st.warning("📦 Run `pip install anthropic` then restart.")
        else:
            st.error(f"AI error: {result.get('message', err)}")
        return

    action   = result.get("action",     "UNKNOWN")
    conf     = result.get("confidence", "")
    head     = result.get("headline",   "")
    reason   = result.get("reasoning",  "")
    risks    = result.get("risks",      [])
    cats     = result.get("catalysts",  [])
    sug_stop = result.get("suggested_stop")
    outlook  = result.get("outlook",    "")

    action_colors = {
        "BUY":   "var(--pos)",
        "HOLD":  "#f59e0b",
        "SELL":  "var(--neg)",
        "WATCH": "var(--accent)",
    }
    ac = action_colors.get(action, "var(--faint)")

    # Build risk / catalyst columns
    risk_html = ""
    if risks:
        items = "".join(f'<div style="color:var(--muted);font-size:.82rem;margin:4px 0">• {r}</div>' for r in risks)
        risk_html = (
            f'<div style="flex:1;min-width:180px">'
            f'<div style="color:var(--neg);font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">⚠️ Risks</div>'
            f'{items}</div>'
        )

    cat_html = ""
    if cats:
        items = "".join(f'<div style="color:var(--muted);font-size:.82rem;margin:4px 0">• {c}</div>' for c in cats)
        cat_html = (
            f'<div style="flex:1;min-width:180px">'
            f'<div style="color:var(--pos);font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">🚀 Catalysts</div>'
            f'{items}</div>'
        )

    stop_html = (
        f'<div style="margin-top:14px;padding-top:10px;border-top:1px solid rgba(255,255,255,.06);'
        f'color:var(--accent);font-size:.85rem;font-weight:600">'
        f'💡 Suggested stop: <b>${sug_stop:.2f}</b></div>'
    ) if sug_stop else ""

    outlook_html = (
        f'<div style="color:var(--faint);font-size:.78rem;margin-top:2px">'
        f'Outlook: <span style="color:var(--muted)">{outlook}</span></div>'
    ) if outlook else ""

    two_col = f'<div style="display:flex;gap:24px;flex-wrap:wrap;margin-top:14px">{risk_html}{cat_html}</div>' if (risk_html or cat_html) else ""

    st.markdown(f"""
    <div class="ai-card">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:10px">
            <div>
                <div style="font-size:.7rem;font-weight:700;color:var(--accent);text-transform:uppercase;letter-spacing:.6px">📊 Live Chart Analysis</div>
                <div style="margin-top:8px;display:flex;align-items:center;gap:10px">
                    <span style="background:{ac}22;color:{ac};border:1px solid {ac}55;
                                 border-radius:8px;padding:5px 18px;font-weight:800;
                                 font-size:1rem;letter-spacing:.5px">{action}</span>
                    <span style="color:var(--accent);font-size:.75rem;font-weight:600;
                                 background:rgba(139,92,246,.12);border:1px solid rgba(139,92,246,.25);
                                 border-radius:5px;padding:3px 9px">{conf} Confidence</span>
                </div>
            </div>
            <div style="text-align:right">{outlook_html}</div>
        </div>
        <div style="font-size:1.05rem;font-weight:700;color:var(--fg);margin:12px 0 8px;line-height:1.45">{head}</div>
        <div style="color:var(--muted);font-size:.88rem;line-height:1.72">{reason}</div>
        <hr style="border:none;border-top:1px solid rgba(255,255,255,.06);margin:14px 0 0">
        {two_col}
        {stop_html}
    </div>
    """, unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# MONITORS — run silently on every page load
# ══════════════════════════════════════════════════════════════════════════════
def _run_stock_monitor():
    """
    Runs on every page load.
    • Auto-tightens stops and raises targets
    • Fires Telegram alerts for every major state change
    • Includes chart-analysis SELL signals (not just basic stop/target)
    """
    from signals import check_position as _check
    from chart_analysis import analyze_ticker as _ca

    for ticker, pos in list(get_positions().items()):
        try:
            s = _check(ticker, pos["entry"], pos["stop"],
                       pos["target1"], pos["date_in"], pos["qty"])
            if s is None:
                continue
            action = s.action

            # ── AUTO-TIGHTEN STOP ─────────────────────────────────────────────
            if s.suggested_stop and s.suggested_stop > pos["stop"] * 1.01:
                if action in ("RAISE STOP", "TIGHTEN STOP", "TAKE PROFIT"):
                    old_stop = pos["stop"]
                    update_stop(ticker, s.suggested_stop)
                    pos = get_positions().get(ticker, pos)
                    alert_stock_raise_stop(
                        ticker, s.price, old_stop, s.suggested_stop,
                        s.pnl_pct, s.pnl_dollars
                    )

            # ── AUTO-RAISE TARGETS ────────────────────────────────────────────
            if s.pnl_pct > 5:
                try:
                    atr_val = float(
                        str(s.indicators.get("Daily ATR", "$0")).replace("$", "").strip()
                    )
                except Exception:
                    atr_val = 0.0
                if atr_val > 0:
                    new_t1 = round(s.price + atr_val * 3.0, 2)
                    new_t2 = round(s.price + atr_val * 6.0, 2)
                    if (new_t1 > pos.get("target1", 0) * 1.02 or
                            new_t2 > pos.get("target2", 0) * 1.02):
                        update_targets(ticker, new_t1, new_t2)

            # ── STOP HIT ──────────────────────────────────────────────────────
            if action == "EXIT NOW":
                if s.price <= pos["stop"] and not pos.get("alerted_stop"):
                    alert_stock_stop(ticker, pos["entry"], s.price, pos["stop"],
                                     pos["qty"], s.pnl_pct, s.pnl_dollars)
                    mark_position_alerted(ticker, "alerted_stop")
                elif "nowhere" in s.plain_reason and not pos.get("alerted_stale"):
                    alert_stock_exit_stale(ticker, s.days_held, s.pnl_pct)
                    mark_position_alerted(ticker, "alerted_stale")
                elif "overbought" in s.plain_reason.lower() and not pos.get("alerted_overbought"):
                    try:
                        rsi_val = float(s.indicators.get("RSI", 0))
                    except Exception:
                        rsi_val = 0
                    alert_stock_overbought_exit(ticker, rsi_val, s.pnl_pct, s.pnl_dollars)
                    mark_position_alerted(ticker, "alerted_overbought")

            # ── TARGET HIT ────────────────────────────────────────────────────
            elif action == "TAKE PROFIT" and not pos.get("alerted_target"):
                alert_stock_target(ticker, pos["entry"], s.price, pos["target1"],
                                   pos["qty"], s.pnl_pct, s.pnl_dollars)
                mark_position_alerted(ticker, "alerted_target")

            # ── WATCH CLOSELY ─────────────────────────────────────────────────
            elif action == "WATCH CLOSELY" and not pos.get("alerted_watch"):
                alert_watch_closely(ticker, s.price, s.pnl_pct, s.plain_reason)
                mark_position_alerted(ticker, "alerted_watch")

            # ── CHART ANALYSIS SELL SIGNAL ────────────────────────────────────
            # Runs when check_position still says HOLD but chart says SELL
            if action == "HOLD" and not pos.get("alerted_chart_sell"):
                try:
                    _days = (date.today() - date.fromisoformat(
                        pos.get("date_in", str(date.today())))).days
                except Exception:
                    _days = 0
                ca = _ca(ticker, position={
                    "entry": pos["entry"], "stop": pos["stop"],
                    "target1": pos.get("target1", 0), "target2": pos.get("target2", 0),
                    "qty": pos.get("qty", 1), "days_held": _days,
                })
                if (ca and "error" not in ca
                        and ca.get("action") == "SELL"
                        and ca.get("confidence") in ("High", "Medium")):
                    alert_sell_signal(
                        ticker, s.price, s.pnl_pct,
                        ca.get("headline", "Technical deterioration"),
                        ca.get("reasoning", s.plain_reason),
                        ca.get("risks", []),
                        ca.get("suggested_stop"),
                    )
                    mark_position_alerted(ticker, "alerted_chart_sell")

        except Exception:
            pass


def _run_option_monitor():
    for oid, opt in get_options().items():
        try:
            price = get_option_price(opt["underlying"], opt["opt_type"],
                                     opt["strike"], opt["expiry"])
            if price is None:
                continue
            dte = days_to_expiry(opt["expiry"])
            if price >= opt["target_price"] and not opt.get("alerted_target"):
                alert_option_target(opt["underlying"], opt["opt_type"], opt["strike"],
                                    opt["expiry"], opt["contracts"], opt["entry_price"],
                                    price, opt["target_price"])
                mark_option_alerted(oid, "alerted_target")
            elif price <= opt["stop_price"] and not opt.get("alerted_stop"):
                alert_option_stop(opt["underlying"], opt["opt_type"], opt["strike"],
                                  opt["expiry"], opt["contracts"], opt["entry_price"],
                                  price, opt["stop_price"])
                mark_option_alerted(oid, "alerted_stop")
            if dte <= 5 and not opt.get("alerted_expiry"):
                alert_option_near_expiry(opt["underlying"], opt["opt_type"], opt["strike"],
                                         opt["expiry"], dte, price)
                mark_option_alerted(oid, "alerted_expiry")
        except Exception:
            pass


_run_stock_monitor()
_run_option_monitor()


def _daemon_running() -> bool:
    """Check if monitor_daemon.py is running via its lock file (Windows-safe)."""
    lock = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".monitor_daemon.lock")
    if not os.path.exists(lock):
        return False
    try:
        pid = int(open(lock).read().strip())
        # Windows-safe: check via tasklist instead of os.kill(pid,0) which fails on Windows
        import subprocess
        out = subprocess.check_output(
            f'tasklist /FI "PID eq {pid}" /NH', shell=True, stderr=subprocess.DEVNULL
        ).decode()
        return str(pid) in out
    except Exception:
        return False


# ── Page navigation stored in session state so it survives sidebar collapse ───
_PAGES = ["🏠  Dashboard", "❓  Guide", "📡  Scanner", "💼  Portfolio", "👁️  Watchlist", "🔎  Analyze", "🏦  Broker", "📅  IPOs", "⚙️  Strategy"]
if "page" not in st.session_state:
    st.session_state["page"] = _PAGES[0]


def _refresh():
    """Clear all caches and rerun."""
    st.cache_data.clear()
    try:
        _ssc_invalidate_all()
    except NameError:
        pass
    st.rerun()


def _parse_tickers(raw: str) -> list:
    """Split a comma-or-space-separated ticker string into a clean uppercase list."""
    return [t.strip().upper() for t in raw.replace(",", " ").split() if t.strip()]


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    # ── Brand ─────────────────────────────────────────────────────────────────
    st.markdown(
        '<div style="padding:12px 4px 16px">'
        '<span class="brand-title">StockPal</span>'
        '<span class="brand-pro">PRO</span>'
        '<div class="brand-sub" style="margin-top:3px">AI Trading Dashboard</div>'
        '</div>',
        unsafe_allow_html=True,
    )

    # ── Theme toggle (dark ↔ light) ───────────────────────────────────────────
    if st.button("☀️  Light mode" if st.session_state.get("theme") == "dark" else "🌙  Dark mode",
                 key="theme_toggle", use_container_width=True):
        st.session_state["theme"] = "light" if st.session_state.get("theme") == "dark" else "dark"
        st.rerun()

    # ── Account ───────────────────────────────────────────────────────────────
    from auth import current_user, is_admin as _is_admin, logout as _logout, list_users, set_role
    st.caption(f"👤 {current_user()}" + ("  ·  👑 admin" if _is_admin() else ""))
    if st.button("Log out", key="logout_btn", use_container_width=True):
        try:
            if _cm is not None:
                _cm.delete("ve_auth", key="ve_del")
        except Exception:
            pass
        _logout(); st.rerun()
    if _is_admin():
        with st.expander("👑 Admin · users"):
            for _usr in list_users():
                _ar1, _ar2 = st.columns([2, 1])
                _ar1.caption(f"{_usr['username']}  ·  {_usr['email'] or '—'}")
                _nr = _ar2.selectbox("role", ["user", "admin"],
                    index=0 if _usr["role"] == "user" else 1,
                    key=f"role_{_usr['username']}", label_visibility="collapsed")
                if _nr != _usr["role"]:
                    set_role(_usr["username"], _nr); st.rerun()

    # ── Telegram alerts (each user's own private channel) ─────────────────────
    try:
        import telegram_connect as _tgc
        from auth import get_telegram as _get_tg, set_telegram as _set_tg, clear_telegram as _clr_tg
        if _tgc.enabled():
            _my_tg = _get_tg(current_user())
            with st.expander("🔔 Telegram Alerts" + ("  ·  ✅" if _my_tg else "")):
                st.caption("Get your trade alerts on Telegram. They go **only** to your own private "
                           "chat with the bot — no one else can ever see them.")
                if _my_tg:
                    st.success("✅ Connected — your alerts come here.")
                    _tc1, _tc2 = st.columns(2)
                    if _tc1.button("Send test", key="tg_test", use_container_width=True):
                        _ok = _tgc.send_to(_my_tg, "✅ <b>StockPal</b> — your alerts are connected!")
                        st.toast("📲 Sent — check Telegram!" if _ok else "❌ Failed")
                    if _tc2.button("Disconnect", key="tg_disc", use_container_width=True):
                        _clr_tg(current_user()); st.rerun()
                else:
                    if "_tg_token" not in st.session_state:
                        st.session_state["_tg_token"] = _tgc.new_token()
                        _tgc.create_pending(st.session_state["_tg_token"], current_user())
                    _tok = st.session_state["_tg_token"]
                    _link = _tgc.connect_link(_tok)
                    if _link:
                        st.markdown(f"**1.** Open the bot → [**@{_tgc.bot_username()}**]({_link})")
                        st.markdown("**2.** Tap **Start** in Telegram")
                        st.markdown("**3.** Come back and click verify ↓")
                        if st.button("✅ I've started the bot — verify", key="tg_verify",
                                     type="primary", use_container_width=True):
                            if _get_tg(current_user()):
                                st.session_state.pop("_tg_token", None)
                                st.success("🎉 Connected!"); st.rerun()
                            else:
                                st.warning("Not linked yet — make sure you tapped **Start** in the bot, then try again.")
                    else:
                        st.info("Telegram bot unavailable right now.")
    except Exception:
        pass

    # ── Portfolio snapshot ────────────────────────────────────────────────────
    _pos  = get_positions()
    _opts = get_options()
    _wl   = get_watchlist()
    _snap_items = [
        ("Stocks",    len(_pos),  "var(--accent)"),
        ("Options",   len(_opts), "var(--accent)"),
        ("Watchlist", len(_wl),   "var(--accent)"),
    ]
    st.markdown(
        '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:14px">'
        + "".join(
            f'<div style="background:var(--surface);border:1px solid rgba(255,255,255,.06);'
            f'border-radius:9px;padding:8px 4px;text-align:center">'
            f'<div style="color:{c};font-size:1.15rem;font-weight:800">{v}</div>'
            f'<div style="color:var(--dim);font-size:.63rem;text-transform:uppercase;letter-spacing:.5px;margin-top:1px">{l}</div>'
            f'</div>'
            for l, v, c in _snap_items
        )
        + '</div>',
        unsafe_allow_html=True,
    )

    # ── Navigation ────────────────────────────────────────────────────────────
    _sidebar_page = st.radio(
        "nav", _PAGES,
        index=(_PAGES.index(st.session_state["page"]) if st.session_state.get("page") in _PAGES else 0),
        label_visibility="collapsed",
    )
    st.session_state["page"] = _sidebar_page

    # ── System status ─────────────────────────────────────────────────────────
    _daemon_ok  = _daemon_running()
    _tg_ok      = bool(TOKEN and CHAT_ID)
    _mm_active  = len(get_active_plans()) if MOOMOO_READY else 0

    def _status_row(icon, label, ok, detail=""):
        col  = "var(--pos)" if ok else "var(--faint)"
        dot  = "⬤" if ok else "○"
        return (
            f'<div style="display:flex;align-items:center;gap:7px;padding:4px 0">'
            f'<span style="color:{col};font-size:.6rem">{dot}</span>'
            f'<span style="color:{"var(--muted)" if ok else "var(--faint)"};font-size:.78rem">{label}'
            + (f'<span style="color:var(--dim)"> · {detail}</span>' if detail else "")
            + f'</span></div>'
        )

    # Owner-only: infrastructure status + alert controls (the owner's Telegram,
    # daemon, and broker) must never be shown to or triggerable by testers.
    if is_admin():
        st.markdown(
            '<div style="background:var(--surface);border:1px solid rgba(255,255,255,.05);'
            'border-radius:9px;padding:10px 12px;margin:12px 0">'
            + _status_row("📲", "Telegram", _tg_ok, "connected" if _tg_ok else "not configured")
            + _status_row("🔔", "Alert daemon", _daemon_ok, "running" if _daemon_ok else "stopped")
            + _status_row("🚀", "Moomoo", FUTU_AVAILABLE, f"{_mm_active} active" if FUTU_AVAILABLE else "install futu-api")
            + '</div>',
            unsafe_allow_html=True,
        )

        if _tg_ok:
            if st.button("📲 Test Alert", use_container_width=True, key="sb_tg_test"):
                st.toast("✅ Sent!" if test_alert() else "❌ Failed")

        if _mm_active > 0:
            if st.button(f"🏦 Broker Plans ({_mm_active})", use_container_width=True, key="sb_mm_plans"):
                st.session_state["page"] = "🏦  Broker"
                st.rerun()

    st.divider()

    # Add Trade
    with st.expander("➕  Log a New Trade"):
        with st.form("add_pos", clear_on_submit=True):
            t_   = st.text_input("Ticker", placeholder="NVDA").upper().strip()
            ep_  = st.number_input("Entry price $",  min_value=0.01, format="%.2f")
            sp_  = st.number_input("Stop loss $",    min_value=0.01, format="%.2f")
            t1_  = st.number_input("Target 1 $",     min_value=0.01, format="%.2f")
            t2_  = st.number_input("Target 2 $",     min_value=0.01, format="%.2f")
            q_   = st.number_input("Shares",         min_value=0.0, step=1.0, value=1.0, format="%.4f",
                                    help="Fractional shares allowed — e.g. 1.5 or 0.25")
            n_   = st.text_input("Notes (optional)")
            if st.form_submit_button("Save Trade", type="primary"):
                if t_ and ep_ > 0 and sp_ > 0 and t1_ > 0 and q_ > 0:
                    add_position(t_, ep_, sp_, t1_, t2_ or round(t1_ * 1.08, 2), round(float(q_), 6), n_)
                    st.success(f"✅ {t_} added!")
                    _refresh()
                else:
                    st.error("Fill in ticker, entry, stop, and target 1")

    # Watchlist quick-add — full management lives on the Watchlist page
    with st.expander("👁️  Watchlist"):
        _wl_add = st.text_input("Add tickers", placeholder="CRWD, MSTR, SOFI", key="wl_add")
        if st.button("Add", use_container_width=True, key="wl_sb_add") and _wl_add.strip():
            for x in _parse_tickers(_wl_add):
                add_to_watchlist(x)
            _refresh()
        if st.button("Manage Watchlist →", use_container_width=True, key="wl_goto"):
            st.session_state["page"] = "👁️  Watchlist"
            st.rerun()


# ── Page routing — driven by the sidebar radio above ─────────────────────────
page = st.session_state["page"]

# Prime the scanner once per session (from the daemon's precomputed result) so
# the Scanner page is already loaded when opened — no live scan on app open.
if "_scan_raw" not in st.session_state:
    try:
        from scanner import load_scan_result as _lsr0
        _pre0 = _lsr0(max_age_s=6 * 3600)
        if _pre0 is not None:
            _, _a0, _b0, _c0, _d0 = _pre0
            st.session_state["_scan_raw"] = (_a0, _b0, _c0, _d0)
            st.session_state["_scan_ts"] = _time.time()
    except Exception:
        pass

# ── Session cache helpers ─────────────────────────────────────────────────────
import time as _ssc_time

def _ssc_fresh(key: str, ttl: float) -> bool:
    return (key in st.session_state
            and _ssc_time.time() - st.session_state.get(f"__ts_{key}", 0) < ttl)

def _ssc_get(key: str):
    return st.session_state.get(key)

def _ssc_set(key: str, value) -> None:
    st.session_state[key] = value
    st.session_state[f"__ts_{key}"] = _ssc_time.time()

def _ssc_invalidate_all() -> None:
    drop = [k for k in st.session_state if k.startswith("__ts_")
            or k in ("db_data", "sc_regime", "hist_closed")
            or k.startswith("an_data_")]
    for k in drop:
        del st.session_state[k]


# ── Shared action helpers (used across Dashboard, Scanner, Analyze) ───────────

def _kpi_card(label: str, value: str, color: str = "var(--fg)", sub: str = "") -> str:
    """KPI card HTML used in the Performance tab."""
    return (
        f'<div style="background:var(--surface);border:1px solid var(--border);'
        f'border-radius:12px;padding:18px 20px;text-align:center">'
        f'<div style="color:var(--faint);font-size:.72rem;font-weight:700;letter-spacing:.08em;margin-bottom:6px">{label}</div>'
        f'<div style="color:{color};font-size:1.5rem;font-weight:800;line-height:1;font-variant-numeric:tabular-nums">{value}</div>'
        + (f'<div style="color:var(--faint);font-size:.72rem;margin-top:5px">{sub}</div>' if sub else "")
        + '</div>'
    )


def _do_add(ticker, entry, stop, t1, t2, qty, setup_type="swing", **kwargs):
    """Add position, show confirmation, refresh."""
    add_position(ticker, entry, stop, t1, t2, round(float(qty), 6), setup_type=setup_type, **kwargs)
    st.success(f"✅ {ticker} added to portfolio!")
    _refresh()


@st.cache_data(ttl=120, show_spinner=False)
def _entry_reco(ticker: str, entry: float) -> dict:
    """Fast, deterministic entry recommendation for the Add-Position form.

    From the ticker's recent data + the entry price the USER typed, suggests a
    stop and two targets (ATR + nearest support/resistance) and gives a plain-English
    verdict on that entry price vs the live price and trend. LONG-ONLY (halal):
    stop below entry, targets above. No AI call, so it returns instantly.
    """
    try:
        from market_data import get_bars, compute_indicators, get_current_price
        import numpy as _np
        df = get_bars(ticker, "6mo", "1d")
        if df is None or len(df) < 30 or entry <= 0:
            return {}
        df = compute_indicators(df)
        last  = df.iloc[-1]
        atr   = float(last.get("atr", 0) or 0)
        if atr <= 0:
            return {}
        cur   = get_current_price(ticker) or float(last["close"])
        rsi   = float(last.get("rsi", 50) or 50)
        ema20 = float(last.get("ema20", 0) or 0)
        ema50 = float(last.get("ema50", 0) or 0)

        # Stop: 1.5 ATR below entry, but tightened to just under the nearest recent
        # support if one sits closer than that (better risk). Targets: ATR-based off
        # the entry, so they stay sensible even for a limit far from the live price.
        win      = df.tail(20)
        atr_stop = entry - 1.5 * atr
        below    = [x for x in win["low"].values if x < entry]
        if below and max(below) > atr_stop:
            stop = max(below) * 0.995          # just under nearest support (tighter)
        else:
            stop = atr_stop
        stop = round(max(stop, 0.01), 2)
        t1   = round(entry + 2.0 * atr, 2)
        t2   = round(entry + 3.5 * atr, 2)
        risk, reward = entry - stop, t1 - entry
        rr   = round(reward / risk, 1) if risk > 0 else 0.0

        gap = (entry - cur) / cur * 100 if cur else 0.0
        notes = []
        if gap > 2:
            verdict = "Chasing"
            notes.append(f"Your entry ${entry:.2f} is {gap:.1f}% ABOVE the live price ${cur:.2f} — you'd be paying up. Consider a limit order nearer ${cur:.2f}.")
        elif gap < -2:
            verdict = "Patient"
            notes.append(f"Your entry ${entry:.2f} is {abs(gap):.1f}% BELOW the live price ${cur:.2f} — a patient limit; it only fills if price dips to you.")
        else:
            verdict = "Fair fill"
            notes.append(f"Your entry ${entry:.2f} is right around the live price ${cur:.2f} — a fair fill.")

        if ema20 and ema50 and entry > ema20 > ema50:
            notes.append("Trend is up (price above the 20- and 50-day averages) — momentum favors the long.")
        elif ema20 and entry < ema20:
            notes.append("Price is below its 20-day average — trend is weak here; keep the stop tight.")
        if rsi >= 70:
            notes.append(f"RSI {rsi:.0f} is overbought — entering here risks a snapback.")
        elif rsi <= 35:
            notes.append(f"RSI {rsi:.0f} is oversold — a bounce entry; confirm it's actually turning up.")
        if 0 < rr < 1.5:
            notes.append(f"Risk:reward is only {rr}:1 at this entry — thin. A lower entry or nearer target improves it.")

        return {"current": round(cur, 2), "atr": round(atr, 2), "rsi": round(rsi),
                "stop": stop, "target1": t1, "target2": t2, "rr": rr,
                "verdict": verdict, "notes": notes}
    except Exception:
        return {}


def _goto_analyze(ticker: str):
    """Jump to the Analyze page with this ticker prefilled (Deep Dive)."""
    st.session_state["dd_ticker"] = ticker
    st.session_state["page"] = "🔎  Analyze"
    st.rerun()


def _robinhood_chart(df, ticker: str, pos: dict, theme: str = "dark"):
    """Clean Robinhood-style price line: coloured by net change, minimal axes,
    area fill, position levels. Real hex (Plotly can't read CSS vars)."""
    c = df["close"].dropna()
    if len(c) < 2:
        return None
    up = c.iloc[-1] >= c.iloc[0]
    if theme == "light":
        pos_c, neg_c, txt, base = "#00a406", "#e5352b", "#79828c", "#0e86c9"
        fill = "rgba(0,164,6,.08)" if up else "rgba(229,53,43,.08)"
    else:
        pos_c, neg_c, txt, base = "#00c805", "#ff5000", "#9ca3a8", "#22a3e6"
        fill = "rgba(0,200,5,.10)" if up else "rgba(255,80,0,.10)"
    line_c = pos_c if up else neg_c
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=c.index, y=c.values, mode="lines",
        line=dict(color=line_c, width=2), fill="tozeroy", fillcolor=fill,
        hovertemplate="$%{y:.2f}<extra></extra>"))
    for _ly, _lc, _ll in [(pos.get("entry"), txt, "Entry"), (pos.get("stop"), neg_c, "Stop"),
                          (pos.get("target1"), pos_c, "T1"), (pos.get("target2"), base, "T2")]:
        if _ly:
            fig.add_hline(y=_ly, line_dash="dash", line_color=_lc, line_width=1,
                          annotation_text=f"{_ll} ${_ly:.2f}", annotation_font_color=_lc,
                          annotation_font_size=10, annotation_position="right")
    ylo, yhi = float(c.min()), float(c.max())
    pad = (yhi - ylo) * 0.08 or 1.0
    fig.update_layout(
        height=360, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=0, r=64, t=10, b=0), showlegend=False, hovermode="x",
        font=dict(color=txt, size=11),
        xaxis=dict(showgrid=False, zeroline=False, showspikes=True, spikethickness=1,
                   spikecolor=txt, spikedash="dot", spikemode="across"),
        yaxis=dict(showgrid=False, zeroline=False, side="right",
                   range=[ylo - pad, yhi + pad], tickprefix="$"),
    )
    return fig


def _do_alert(ticker, price, stop, t1, t2, stop_pct, gain_pct, rr, stars, why,
              signal_type: str = "BUY", watch_buy_at: float = 0.0):
    """Send Telegram signal alert — WATCH or BUY."""
    ok = alert_stock_signal(ticker, price, stop, t1, t2, stop_pct, gain_pct, rr, stars, why,
                            signal_type=signal_type, watch_buy_at=watch_buy_at)
    st.toast("📲 Sent!" if ok else "❌ Failed — check Telegram config in .env")


# ── Shared Deep-Dive components ───────────────────────────────────────────────

def _fund_row(label: str, value: str, color: str = "var(--muted)") -> str:
    """One row in the fundamentals table."""
    return (
        f'<div style="display:flex;justify-content:space-between;'
        f'border-bottom:1px solid rgba(255,255,255,.04);padding:5px 0">'
        f'<span style="color:var(--faint);font-size:.8rem">{label}</span>'
        f'<span style="color:{color};font-size:.8rem;font-weight:600">{value}</span>'
        f'</div>'
    )


def _render_fund_section(snap: dict, live_price: float) -> None:
    """Render fundamentals + company profile block (left column of deep dive)."""
    an  = snap.get("analyst",    {})
    ea  = snap.get("earnings",   {})
    ins = snap.get("insider",    {})
    fi  = snap.get("financials", {})

    rec     = an.get("recommendation", "—").replace("_", " ").title()
    rec_col = ("var(--pos)" if rec.lower() in ("buy", "strong buy")
               else "var(--neg)" if rec.lower() in ("sell", "strong sell") else "var(--muted)")
    tgt     = an.get("target_mean")
    tgt_str = f"${tgt:.2f} ({(tgt - live_price) / live_price * 100:+.1f}%)" if tgt else "—"
    tlo, thi = an.get("target_low"), an.get("target_high")
    tgt_rng = f"${tlo:.0f} – ${thi:.0f}" if tlo and thi else "—"
    dte     = ea.get("days_until", 999)
    earn_str= (f"⚠️ In {dte} days!" if 0 < dte <= 7 else f"In {dte} days" if 0 < dte <= 60 else "None soon")
    earn_col= "var(--neg)" if 0 < dte <= 7 else "#f59e0b" if 0 < dte <= 21 else "var(--faint)"
    surp    = ea.get("last_surprise")
    rg      = fi.get("revenue_growth_yoy")
    pm      = fi.get("profit_margin")
    pe      = fi.get("pe_ratio")
    ins_sig = ins.get("signal", "—").title()
    ins_col = "var(--pos)" if ins.get("signal") == "buying" else "var(--neg)" if ins.get("signal") == "selling" else "var(--faint)"

    st.markdown('<p style="color:var(--muted);font-size:.72rem;font-weight:700;letter-spacing:.08em;margin:8px 0 8px">FUNDAMENTALS</p>', unsafe_allow_html=True)
    st.markdown(
        _fund_row("Wall St Rating", f"{rec} ({an.get('num_analysts', 0)} analysts)", rec_col)
        + _fund_row("Analyst Target", tgt_str)
        + _fund_row("Target Range", tgt_rng)
        + _fund_row("Next Earnings", earn_str, earn_col)
        + _fund_row("Last EPS Surprise", f"{surp:+.1f}%" if surp is not None else "—",
                    "var(--pos)" if surp and surp > 0 else "var(--neg)" if surp and surp < 0 else "var(--faint)")
        + _fund_row("Revenue Growth YoY", f"{rg:.1f}%" if rg is not None else "—",
                    "var(--pos)" if rg and rg > 10 else "var(--neg)" if rg and rg < 0 else "var(--muted)")
        + _fund_row("Profit Margin", f"{pm * 100:.1f}%" if pm else "—")
        + _fund_row("P/E Ratio", str(pe) if pe else "—")
        + _fund_row("Insider Activity", ins_sig, ins_col),
        unsafe_allow_html=True,
    )


def _render_news_section(ticker: str, max_items: int = 6) -> None:
    """Render recent news headlines (right column of deep dive)."""
    st.markdown('<p style="color:var(--muted);font-size:.72rem;font-weight:700;letter-spacing:.08em;margin:0 0 8px">RECENT NEWS</p>', unsafe_allow_html=True)
    try:
        from ai_analysis import get_ticker_news as _gtn
        lines = [l for l in _gtn(ticker, max_items=max_items).split("\n") if l.strip()]
        html, i = "", 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("•"):
                parts = line[1:].strip()
                date_part, title = "", parts
                if parts.startswith("["):
                    eb = parts.find("]")
                    if eb > 0:
                        date_part = parts[1:eb]
                        title = parts[eb + 2:].strip()
                summary = ""
                if i + 1 < len(lines) and not lines[i + 1].startswith("•"):
                    summary = lines[i + 1].strip(); i += 1
                html += (
                    f'<div style="margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid rgba(255,255,255,.04)">'
                    f'<span style="color:var(--dim);font-size:.7rem">{date_part}</span> '
                    f'<span style="color:var(--fg);font-size:.82rem;font-weight:500">{title}</span>'
                    + (f'<br><span style="color:var(--faint);font-size:.75rem;line-height:1.5">{summary}</span>' if summary else "")
                    + '</div>'
                )
            i += 1
        st.markdown(html if html else '<p style="color:var(--faint);font-size:.82rem">No recent news found.</p>', unsafe_allow_html=True)
    except Exception:
        st.markdown('<p style="color:var(--faint);font-size:.82rem">News unavailable.</p>', unsafe_allow_html=True)


def _render_ai_verdict(ai: dict, refresh_key: str) -> None:
    """Render AI ENTER/WAIT/PASS verdict block with risks, catalysts, tip, refresh button."""
    if "error" in ai:
        st.warning(f"AI unavailable: {ai.get('message', '')}")
        return

    verdict   = ai.get("verdict",    "—")
    conf      = ai.get("confidence", "—")
    headline  = ai.get("headline",   "")
    reasoning = ai.get("reasoning",  "")
    risks     = ai.get("risks",      [])
    catalysts = ai.get("catalysts",  [])
    tip       = ai.get("trade_tip",  "")

    colors = {
        "ENTER": ("var(--pos)", "rgba(34,197,94,.12)",  "rgba(34,197,94,.3)"),
        "WAIT":  ("#f59e0b", "rgba(245,158,11,.12)", "rgba(245,158,11,.3)"),
        "PASS":  ("var(--neg)", "rgba(239,68,68,.12)",  "rgba(239,68,68,.3)"),
    }
    vc, vbg, vbdr = colors.get(verdict, ("var(--muted)", "rgba(148,163,184,.1)", "rgba(148,163,184,.2)"))
    cc = "var(--pos)" if conf == "High" else "#f59e0b" if conf == "Medium" else "var(--neg)"

    st.markdown(
        f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">'
        f'<span style="background:{vbg};border:1px solid {vbdr};color:{vc};font-weight:800;'
        f'font-size:1rem;padding:4px 14px;border-radius:8px;letter-spacing:.04em">{verdict}</span>'
        f'<span style="color:{cc};font-size:.75rem;font-weight:600">{conf} confidence</span></div>'
        f'<p style="color:var(--fg);font-weight:600;font-size:.95rem;margin:0 0 10px;line-height:1.5">{headline}</p>'
        f'<p style="color:var(--muted);font-size:.83rem;line-height:1.7;margin:0 0 14px">{reasoning}</p>',
        unsafe_allow_html=True,
    )
    rc1, rc2 = st.columns(2)
    with rc1:
        if risks:
            st.markdown('<p style="color:var(--neg);font-size:.72rem;font-weight:700;letter-spacing:.08em;margin:0 0 6px">RISKS</p>', unsafe_allow_html=True)
            for r in risks:
                st.markdown(f'<p style="color:#fca5a5;font-size:.8rem;margin:0 0 4px">⚠️ {r}</p>', unsafe_allow_html=True)
    with rc2:
        if catalysts:
            st.markdown('<p style="color:var(--pos);font-size:.72rem;font-weight:700;letter-spacing:.08em;margin:0 0 6px">CATALYSTS</p>', unsafe_allow_html=True)
            for c in catalysts:
                st.markdown(f'<p style="color:#86efac;font-size:.8rem;margin:0 0 4px">✅ {c}</p>', unsafe_allow_html=True)
    if tip:
        st.markdown(
            f'<div style="margin-top:12px;background:rgba(59,130,246,.08);border-left:3px solid var(--accent);border-radius:6px;padding:8px 14px">'
            f'<span style="color:var(--accent);font-size:.72rem;font-weight:700;letter-spacing:.08em">💡 TRADE TIP</span><br>'
            f'<span style="color:var(--accent);font-size:.82rem">{tip}</span></div>',
            unsafe_allow_html=True,
        )
    if st.button("🔄 Re-run AI Analysis", key=refresh_key, use_container_width=True):
        st.session_state.pop(refresh_key.replace("_refresh", ""), None)
        st.rerun()


def _deep_dive_panel(ticker: str, snap: dict, co: dict, live_price: float,
                     sig_ctx: dict | None, cache_key: str, ai_type: str = "entry") -> None:
    """
    Full deep dive panel: company profile + fundamentals | news + AI verdict.
    ai_type: "entry" (scanner/analyze) or "position" (portfolio).
    sig_ctx: signal dict for entry, position dict for position AI.
    """
    st.markdown(
        '<div style="background:rgba(15,23,42,.85);border:1px solid var(--border);'
        'border-radius:12px;padding:20px 22px;margin:0 0 12px">',
        unsafe_allow_html=True,
    )

    c1, c2 = st.columns(2, gap="large")
    with c1:
        st.markdown('<p style="color:var(--muted);font-size:.72rem;font-weight:700;letter-spacing:.08em;margin:0 0 8px">COMPANY PROFILE</p>', unsafe_allow_html=True)
        st.markdown(
            f'<p style="color:var(--fg);font-weight:700;font-size:1rem;margin:0 0 2px">{co.get("name", ticker)}</p>'
            f'<p style="color:var(--faint);font-size:.78rem;margin:0 0 8px">{co.get("sector","")}'
            f'{" · " + co.get("industry","") if co.get("industry") else ""}</p>'
            + (f'<p style="color:var(--muted);font-size:.8rem;line-height:1.6;margin:0 0 14px">{co.get("description","")}</p>' if co.get("description") else ""),
            unsafe_allow_html=True,
        )
        _render_fund_section(snap, live_price)

    with c2:
        _render_news_section(ticker)

    # AI verdict section
    st.markdown(
        '<hr style="border:none;border-top:1px solid rgba(255,255,255,.07);margin:16px 0 14px">'
        f'<p style="color:var(--muted);font-size:.72rem;font-weight:700;letter-spacing:.08em;margin:0 0 12px">'
        f'🤖 AI {"ENTRY" if ai_type == "entry" else "POSITION MANAGEMENT"} ANALYSIS</p>',
        unsafe_allow_html=True,
    )

    if cache_key not in st.session_state:
        with st.spinner(f"Analyzing {ticker}…"):
            try:
                if ai_type == "entry":
                    from ai_analysis import analyze_entry as _ae
                    st.session_state[cache_key] = _ae(ticker, signal=sig_ctx, company_info=co, fundamentals=snap)
                else:
                    from ai_analysis import analyze_position as _ap
                    st.session_state[cache_key] = _ap(ticker, position=sig_ctx, company_info=co, fundamentals=snap)
            except Exception as e:
                st.session_state[cache_key] = {"error": True, "message": str(e)}

    _render_ai_verdict(st.session_state.get(cache_key, {}), f"{cache_key}_refresh")
    st.markdown('</div>', unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
# PAGE — DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════
if page == "🏠  Dashboard":
    live_market_bar()
    # ── Session cache (60 s TTL) — return visits show instantly ──────────────
    if not _ssc_fresh("db_data", 60):
        _db = {
            "regime":    load_regime(),
            "positions": get_positions(),
            "wl":        get_watchlist(),
            "closed":    get_closed(),
        }
        _ssc_set("db_data", _db)
    else:
        _db = _ssc_get("db_data")

    regime    = _db["regime"]
    positions = _db["positions"]
    wl        = _db["wl"]
    rc, rl, rd = regime_plain(regime["regime"])
    now       = datetime.now()
    greeting  = "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 17 else "Good evening"

    # Header
    hc1, hc2 = st.columns([5, 1])
    hc1.markdown(f'<div class="page-title">{greeting} 👋</div>', unsafe_allow_html=True)
    hc1.markdown(f'<div class="page-sub">{now.strftime("%A, %B %d %Y")} · Prices update live via Finnhub WebSocket</div>', unsafe_allow_html=True)
    if TOKEN and CHAT_ID and is_admin():
        if hc2.button("📲 Send Briefing", use_container_width=True,
                      help="Send full morning briefing to Telegram now"):
            try:
                from morning_briefing import send_morning_briefing
                _mb_ok = send_morning_briefing(force=True)
                st.toast("☀️ Briefing sent!" if _mb_ok else "❌ Failed — check logs")
            except Exception as _mb_e:
                st.toast(f"❌ Error: {_mb_e}")

    # ── Live price ticker ─────────────────────────────────────────────────────
    live_ticker_strip()

    # Regime banner
    st.markdown(f"""
    <div class="regime-banner" style="background:{rc}12;border-color:{rc}30">
        <div>
            <div style="font-size:1.1rem;font-weight:700;color:{rc}">{rl}</div>
            <div style="color:var(--dim);font-size:.85rem;margin-top:3px">{rd}</div>
        </div>
        <div style="display:flex;gap:28px;flex-wrap:wrap">
            <div class="stat"><span class="stat-label">VIX (Fear)</span><span class="stat-value">{regime["vix"]:.1f}</span></div>
            <div class="stat"><span class="stat-label">SPY</span><span class="stat-value">${regime["spy"]:.2f}</span></div>
            <div class="stat"><span class="stat-label">200-Day MA</span><span class="stat-value">${regime["ema200"]:.2f}</span></div>
            <div class="stat"><span class="stat-label">SPY vs 200MA</span><span class="stat-value">{"✅ Above" if regime["above_200"] else "⚠️ Below"}</span></div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Summary metrics
    closed_trades = _db["closed"]
    wins_  = [t for t in closed_trades if t.get("pnl_pct", 0) > 0]
    wr_    = round(len(wins_) / len(closed_trades) * 100) if closed_trades else 0
    total_ = sum(t.get("pnl_dollars", 0) for t in closed_trades)

    dm1, dm2, dm3, dm4 = st.columns(4)
    dm1.metric("Open Positions",  len(positions))
    dm2.metric("Closed P&L",      f"${total_:+,.2f}")
    dm3.metric("Win Rate",         f"{wr_:.0f}%",
               f"{len(wins_)}W / {len(closed_trades)-len(wins_)}L" if closed_trades else "No history")
    dm4.metric("Watchlist Size",  f"{len(wl)} tickers")

    # ── Position alerts (re-checks every 30 s for signals, prices are live above) ──
    @st.fragment(run_every=30)
    def _position_alerts():
        positions_ = get_positions()
        exit_items, watch_items, raise_items, hold_items = [], [], [], []
        if not positions_:
            st.session_state["_briefing_exit_cnt"]  = 0
            st.session_state["_briefing_raise_cnt"] = 0
            st.info("No open positions. Add one via the sidebar → **Add Trade**.")
            return

        for ticker, pos in positions_.items():
            s = cached_position_check(ticker, pos["entry"], pos["stop"],
                                      pos["target1"], pos["date_in"], pos["qty"])
            if s is None:
                continue
            _db_chart = st.session_state.get(f"ca_{ticker}", {})
            _db_action, _db_color, _db_reason = _merged_action(s, _db_chart)
            class _S: pass
            _ms = _S(); _ms.__dict__.update(s.__dict__)
            _ms.action = _db_action; _ms.action_color = _db_color; _ms.plain_reason = _db_reason

            if _ms.action in ("EXIT NOW", "TAKE PROFIT"):
                exit_items.append((ticker, pos, _ms))
            elif _ms.action in ("TIGHTEN STOP", "WATCH CLOSELY"):
                watch_items.append((ticker, pos, _ms))
            elif _ms.action == "RAISE STOP":
                raise_items.append((ticker, pos, _ms))
            else:
                hold_items.append((ticker, pos, _ms))

        # Store counts so the briefing button (outside this fragment) can read them
        st.session_state["_briefing_exit_cnt"]  = len(exit_items)
        st.session_state["_briefing_raise_cnt"] = len(raise_items)

        if exit_items:
            st.markdown(f'<div style="margin:24px 0 8px;font-size:1.05rem;font-weight:700;color:var(--neg)">🚨 Action Required — {len(exit_items)} Position{"s" if len(exit_items)>1 else ""}</div>', unsafe_allow_html=True)
            for ticker, pos, s in exit_items:
                card_cls = "card-exit" if s.action == "EXIT NOW" else "card-profit"
                st.markdown(f"""
                <div class="card {card_cls}">
                    {action_badge(s.action, s.action_color)}&nbsp;&nbsp;
                    <span class="ticker-big">{ticker}</span>
                    <span class="price-tag">${s.price:.2f} &nbsp;·&nbsp;
                    <span style="color:{'var(--pos)' if s.pnl_pct>=0 else 'var(--neg)'}">{s.pnl_pct:+.2f}%</span></span>
                    <div class="thin-div" style="margin:10px 0"></div>
                    <p style="color:var(--fg);margin:0;line-height:1.65">{s.plain_reason}</p>
                </div>
                """, unsafe_allow_html=True)
                ca, cb = st.columns([3, 1])
                exit_p = ca.number_input(f"Exit {ticker} at $", value=float(s.price), format="%.2f", key=f"ep_{ticker}")
                if cb.button(f"Close {ticker}", key=f"cl_{ticker}", type="primary"):
                    close_position(ticker, exit_p, s.plain_reason[:80])
                    st.success(f"✅ {ticker} closed at ${exit_p:.2f}")
                    _refresh()

        if watch_items:
            st.markdown(f'<div style="margin:24px 0 8px;font-size:1.05rem;font-weight:700;color:#f59e0b">⚠️ Watch Closely — {len(watch_items)} Position{"s" if len(watch_items)>1 else ""}</div>', unsafe_allow_html=True)
            for ticker, pos, s in watch_items:
                st.markdown(f"""
                <div class="card card-watch">
                    {action_badge(s.action, s.action_color)}&nbsp;&nbsp;
                    <span class="ticker-big">{ticker}</span>
                    <span class="price-tag">${s.price:.2f}</span>
                    <div class="thin-div" style="margin:10px 0"></div>
                    <p style="color:var(--fg);margin:0;line-height:1.65">{s.plain_reason}</p>
                </div>
                """, unsafe_allow_html=True)

        for label, color, cls, items in [
            ("🔼 Stops Auto-Tightened", "var(--accent)", "card-raise", raise_items),
            ("✅ Holding", "var(--pos)", "card-hold", hold_items),
        ]:
            if items:
                st.markdown(f'<div style="margin:24px 0 8px;font-size:1.05rem;font-weight:700;color:{color}">{label} — {len(items)}</div>', unsafe_allow_html=True)
                for ticker, pos, s in items:
                    st.markdown(f"""
                    <div class="card {cls}">
                        {action_badge(s.action, s.action_color)}&nbsp;&nbsp;
                        <span class="ticker-big">{ticker}</span>
                        <span class="price-tag">${s.price:.2f} &nbsp;·&nbsp;
                        <span style="color:{'var(--pos)' if s.pnl_pct>=0 else 'var(--neg)'}">{s.pnl_pct:+.2f}%</span></span>
                        <div class="thin-div" style="margin:10px 0"></div>
                        <p style="color:var(--fg);margin:0;line-height:1.65">{s.plain_reason}</p>
                    </div>
                    """, unsafe_allow_html=True)

    _position_alerts()


    # ── Today's Picks ─────────────────────────────────────────────────────────
    st.markdown("---")
    # ── Today's Reversals — the app's focus: buy the bottom, sell the highs ────
    try:
        from scanner import load_scan_result as _lsr
        _revr = _lsr(max_age_s=1800)
        _rev_sigs = [s for s in (_revr[4] if _revr else []) if s.ticker not in positions][:5]
    except Exception:
        _rev_sigs = []
    st.markdown('<div style="font-size:1.1rem;font-weight:700;color:var(--fg);margin-bottom:2px">🔄 Today\'s Reversals</div>', unsafe_allow_html=True)
    st.markdown('<div style="color:var(--dim);font-size:.82rem;margin-bottom:12px">Bottoms & turnarounds — buy the low, sell into resistance</div>', unsafe_allow_html=True)
    if not _rev_sigs:
        st.caption("No reversal setups from the last scan — open 📡 Scanner → Reversals for the full list.")
    else:
        for _rs in _rev_sigs:
            _rco = cached_company_info(_rs.ticker)
            _edge = "var(--neg)" if getattr(_rs, "setup_type", "") == "deep_bottom" else "var(--border-strong)"
            st.markdown(
                f'<div class="card" style="border-left-color:{_edge}">'
                f'<div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px">'
                f'<div><span style="color:#fbbf24">{"⭐"*_rs.stars}</span> <span class="ticker-big">{_rs.ticker}</span> '
                f'<span class="price-tag">${_rs.price:.2f}</span> '
                f'<span style="color:var(--faint);font-size:.85rem">{_rco.get("name", _rs.ticker)}</span> {sector_badge(_rs.sector)}</div>'
                f'<div style="color:var(--faint);font-size:.82rem">Stop {_rs.stop_pct}% &nbsp;·&nbsp; +{_rs.gain_pct}% &nbsp;·&nbsp; R:R {_rs.rr}:1</div></div>'
                f'<p style="color:var(--muted);font-size:.88rem;line-height:1.55;margin:8px 0 4px">{_rs.why[:200]}</p>'
                f'</div>', unsafe_allow_html=True)
            _rc1, _rc2, _rc3 = st.columns([4, 1, 1])
            if _rc2.button("➕ Add", key=f"dbr_a_{_rs.ticker}", type="primary", use_container_width=True):
                _do_add(_rs.ticker, _rs.price, _rs.stop, _rs.target1, _rs.target2, 1,
                        setup_type="swing", stars=_rs.stars, sector=_rs.sector)
            if _rc3.button("📊", key=f"dbr_d_{_rs.ticker}", use_container_width=True, help="Deep Dive"):
                _goto_analyze(_rs.ticker)
        st.markdown("---")

    ph1, ph2 = st.columns([5, 1])
    ph1.markdown('<div style="font-size:1.1rem;font-weight:700;color:var(--fg);margin-bottom:2px">📈 Swing Picks</div>', unsafe_allow_html=True)
    ph1.markdown('<div style="color:var(--dim);font-size:.82rem;margin-bottom:14px">Best setups from your watchlist right now — sorted by conviction</div>', unsafe_allow_html=True)
    if ph2.button("🔄 Refresh", use_container_width=True):
        _refresh()

    if regime["regime"] == "CRISIS":
        st.session_state.pop("_send_briefing", None)   # discard stale flag
        st.error("🚫 Market in CRISIS mode — stay in cash, no new trades.")
    else:
        open_tickers  = list(positions.keys())

        # Fast: reuse the daily scan's already-scored swing setups instead of
        # re-scoring the whole watchlist live, then re-rank with the multi-factor
        # swing engine so this preview matches the 🌟 Swing Picks tab.
        try:
            from scanner import load_scan_result as _lsr_sw
            _cached_sw = _lsr_sw(max_age_s=6 * 3600)
            signals = [s for s in (_cached_sw[1] if _cached_sw else []) if s.ticker not in open_tickers]
        except Exception:
            signals = []

        if signals:
            try:
                from swing_engine import score_swing
                _reg_e = regime["regime"] if isinstance(regime, dict) else str(regime)
                signals = signals[:15]   # enrich only the top handful (bounds Finnhub calls)
                for _sig in signals:
                    try:
                        _tech = {1: 55, 2: 70, 3: 85}.get(int(getattr(_sig, "stars", 2) or 2), 65)
                        _er = score_swing(_sig.ticker, _tech, getattr(_sig, "why_buy", ""), _reg_e)
                        _sig.stars = _er["stars"]
                        _sig._eng_score = _er["score"]
                    except Exception:
                        pass
            except Exception:
                pass

        signals.sort(key=lambda x: (getattr(x, "_eng_score", 0), x.stars), reverse=True)

        if st.session_state.pop("_send_briefing", False):
            try:
                _, rl2, rd2 = regime_plain(regime["regime"])
                _exit_cnt  = st.session_state.get("_briefing_exit_cnt",  0)
                _raise_cnt = st.session_state.get("_briefing_raise_cnt", 0)
                ok = alert_daily_briefing(rl2, rd2, signals, _exit_cnt, _raise_cnt)
                st.toast("📲 Briefing sent!" if ok else "❌ Send failed — check Telegram token in .env")
            except Exception as _be:
                st.error(f"❌ Briefing error: {_be}")

        if not signals:
            st.warning("No clean buy signals right now. Market may be extended — check back after the next refresh.")
        else:
            _db_buys, _db_watches = [], []
            for _s in signals:
                (_db_watches if _s.signal_type == "WATCH" else _db_buys).append(_s)
            _db_show   = _db_buys[:10]
            _db_watch_show = _db_watches[:5]

            st.success(
                f"**{len(_db_buys)} buy signal{'s' if len(_db_buys)!=1 else ''}** ready to enter"
                + (f" · **{len(_db_watches)} watch signal{'s' if len(_db_watches)!=1 else ''}** waiting for pullback" if _db_watches else "")
            )

            def _render_db_card(sig, idx_key):
                _is_watch   = sig.signal_type == "WATCH"
                _card_cls   = "card-watch" if _is_watch else "card-buy"
                sc_         = _star_color(sig.stars)
                co          = cached_company_info(sig.ticker)
                _entry_price, _price_label = (
                    (float(sig.watch_buy_at), f"wait for ${sig.watch_buy_at:.2f}")
                    if _is_watch and sig.watch_buy_at
                    else (sig.price, f"near ${sig.price:.2f}")
                )

                with st.container():
                    st.markdown(f"""
                    <div class="card {_card_cls}">
                        <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px">
                            <div>
                                <span style="color:{sc_}">{"⭐"*sig.stars}</span>&nbsp;
                                <span class="ticker-big">{sig.ticker}</span>&nbsp;
                                <span class="price-tag">{_price_label}</span>
                                {_sig_type_badge(sig)}
                                {sector_badge(co.get("sector","") or co.get("industry",""))}
                            </div>
                            <div style="text-align:right;color:var(--faint);font-size:.83rem">
                                Risk {sig.stop_pct}% &nbsp;·&nbsp; Target {sig.gain_pct}% &nbsp;·&nbsp; R:R {sig.rr}:1
                            </div>
                        </div>
                        <div class="company-meta">
                            <span class="company-name">{co.get("name", sig.ticker)}</span>
                        </div>
                        <div class="thin-div"></div>
                        <p style="color:var(--muted);margin:0 0 10px;line-height:1.65">{sig.why_buy}</p>
                        <div>
                            <span class="pill">🛑 Stop <b>${sig.stop:.2f}</b></span>
                            <span class="pill">🎯 T1 <b>${sig.target1:.2f}</b></span>
                            <span class="pill">🎯 T2 <b>${sig.target2:.2f}</b></span>
                            {f'<span class="pill" style="color:#fbbf24">📍 Buy at <b>${sig.watch_buy_at:.2f}</b></span>' if _is_watch and sig.watch_buy_at else ''}
                        </div>
                        <p style="color:var(--dim);font-size:.8rem;margin:8px 0 0">⚡ {sig.what_to_watch}</p>
                    </div>
                    """, unsafe_allow_html=True)

                    _wb = _watch_banner_html(sig)
                    if _wb:
                        st.markdown(_wb, unsafe_allow_html=True)

                    for w in sig.warnings:
                        st.warning(w)

                    bc1, bc2, bc3, bc4 = st.columns([2, 2, 1, 1])
                    qty_db = bc1.number_input("Shares", min_value=0.0, step=1.0, value=1.0, format="%.4f", key=f"db_q_{idx_key}")
                    enp_db = bc2.number_input("At $", value=_entry_price, format="%.2f", key=f"db_p_{idx_key}")
                    bc3.markdown("<br>", unsafe_allow_html=True)
                    _btn_lbl = "👁 Watch" if _is_watch else "➕ Add"
                    if bc3.button(_btn_lbl, key=f"db_a_{idx_key}", type="primary", use_container_width=True):
                        _do_add(sig.ticker, enp_db, sig.stop, sig.target1, sig.target2, qty_db,
                                setup_type="swing", stars=sig.stars,
                                rs_rank=getattr(sig, "rs_rank", 0),
                                trend_template=getattr(sig, "trend_template", 0),
                                sector=getattr(sig, "sector", ""))
                    bc4.markdown("<br>", unsafe_allow_html=True)
                    if TOKEN and CHAT_ID:
                        if bc4.button("📲", key=f"db_t_{idx_key}", use_container_width=True):
                            _do_alert(sig.ticker, sig.price, sig.stop, sig.target1, sig.target2,
                                      sig.stop_pct, sig.gain_pct, sig.rr, sig.stars, sig.why_buy,
                                      signal_type=sig.signal_type,
                                      watch_buy_at=sig.watch_buy_at)

            # BUY signals
            for sig in _db_show:
                _render_db_card(sig, sig.ticker)

            # WATCH signals (collapsible section)
            if _db_watch_show:
                st.markdown(
                    '<div style="margin:20px 0 8px;color:#f59e0b;font-weight:700;font-size:.85rem;'
                    'text-transform:uppercase;letter-spacing:.5px">👁 Watch List — Pullback Targets</div>',
                    unsafe_allow_html=True,
                )
                for i, sig in enumerate(_db_watch_show):
                    _render_db_card(sig, f"w_{sig.ticker}")
                    with st.expander(f"🚀 Send {sig.ticker} to Moomoo", expanded=False):
                        _render_moomoo_panel(
                            sig.ticker, sig.price, sig.stop,
                            sig.target1, sig.target2,
                            _parse_atr(sig.indicators.get("Daily ATR", "$0")),
                            f"db_{sig.ticker}",
                        )
                    st.markdown("---")

            if len(signals) > 10:
                st.info(f"Showing top 10 of {len(signals)} signals. Go to **📡 Scanner** to see all results.")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE — SCANNER
# ══════════════════════════════════════════════════════════════════════════════
elif page == "📡  Scanner":
    from scanner import UNIVERSE, invalidate_cache, cache_info
    live_market_bar()
    _page_header("📡 Market Scanner", "Full sweep — 780+ tickers across every sector, scored for swing and day trades")

    # ── Session cache (120 s TTL) ─────────────────────────────────────────────
    if not _ssc_fresh("sc_regime", 120):
        _sc_regime = load_regime()
        _ssc_set("sc_regime", _sc_regime)
    else:
        _sc_regime = _ssc_get("sc_regime")
    regime     = _sc_regime
    rc, rl, rd = regime_plain(regime["regime"])

    st.markdown(f"""
    <div class="regime-banner" style="background:{rc}0e;
         border-color:{rc}30;padding:14px 22px;margin-bottom:20px">
        <span style="font-size:.95rem;font-weight:600;color:{rc}">{rl}</span>
        <span style="color:var(--dim);font-size:.82rem">{rd} &nbsp;·&nbsp; VIX {regime["vix"]:.1f} &nbsp;·&nbsp; SPY ${regime["spy"]:.2f}</span>
    </div>
    """, unsafe_allow_html=True)

    with st.expander("⚙️ Filters", expanded=False):
        fc1, fc2, fc3 = st.columns(3)
        price_min = fc1.number_input("Min price $", value=2.0,   min_value=0.5,  format="%.0f")
        price_max = fc2.number_input("Max price $", value=500.0, min_value=5.0,  format="%.0f")
        sectors   = fc3.multiselect("Sectors (blank = all)", options=list(UNIVERSE.keys()))

    # ── Scan cadence: hold the last result; re-scan only on the button, the first
    #    open of the session, or every 4 hours (the daemon keeps data fresh) ──
    _SCAN_MAX_AGE = 4 * 3600

    def _fetch_scan(regime_str: str):
        """Raw scan lists (daemon precompute, or a live fallback). Price-unfiltered."""
        from scanner import run_daily_scan as _s, load_scan_result
        precomp = load_scan_result(max_age_s=6 * 3600)
        if precomp is not None:
            _, _sw, _dt, _vcp, _mom = precomp
            return _sw, _dt, _vcp, _mom
        return _s({"regime": regime_str}, price_min=1.0, price_max=1e9)

    _now_ts  = _time.time()
    _scan_ts = st.session_state.get("_scan_ts", 0.0)
    _do_scan = ("_scan_raw" not in st.session_state) or (_now_ts - _scan_ts > _SCAN_MAX_AGE)

    # ── Controls ──────────────────────────────────────────────────────────────
    sr1, sr2, sr3 = st.columns([2, 2, 4])
    if sr1.button("🔄 Run Scan", type="primary", use_container_width=True):
        _do_scan = True
        st.session_state.pop("sc_sw_shown", None)
    if sr2.button("⬇️ Force Re-Download", use_container_width=True,
                  help="Wipe the ticker cache and re-download everything from scratch"):
        invalidate_cache()
        _do_scan = True

    if _do_scan:
        with st.spinner("Scanning…"):
            st.session_state["_scan_raw"] = _fetch_scan(regime["regime"])
        st.session_state["_scan_ts"] = _now_ts
        _scan_ts = _now_ts

    _raw_sw, _raw_dt, _raw_vcp, _raw_mom = st.session_state["_scan_raw"]
    _pf = lambda _L: [s for s in _L if price_min <= s.price <= price_max]
    swing_sigs, day_sigs, vcp_sigs, mom_sigs = _pf(_raw_sw), _pf(_raw_dt), _pf(_raw_vcp), _pf(_raw_mom)

    _age_min = int((_now_ts - _scan_ts) / 60) if _scan_ts else 0
    _ci = cache_info()
    sr3.markdown(
        f'<span style="color:var(--faint);font-size:.8rem">'
        f'Last scan: <b style="color:var(--muted)">{"just now" if _age_min==0 else str(_age_min)+" min ago"}</b> &nbsp;·&nbsp; '
        f'<b style="color:var(--accent)">{_ci.get("label","no cache")}</b> &nbsp;·&nbsp; '
        f'auto every 4h</span>', unsafe_allow_html=True)

    # ── Auto-refresh only when the 4h window elapses (fires even if left open) ──
    _remain_ms = max(60, int(_SCAN_MAX_AGE - (_now_ts - _scan_ts))) * 1000
    import streamlit.components.v1 as _sc1
    _sc1.html(f'<script>setTimeout(()=>window.parent.location.reload(),{_remain_ms});</script>', height=0)

    if sectors:
        keep = {t for s in sectors for t in UNIVERSE.get(s, [])}
        swing_sigs = [s for s in swing_sigs if s.ticker in keep]
        day_sigs   = [s for s in day_sigs   if s.ticker in keep]
        vcp_sigs   = [s for s in vcp_sigs   if s.ticker in keep]
        mom_sigs   = [s for s in mom_sigs   if s.ticker in keep]

    # ── Batch live quotes for all signal tickers ──────────────────────────────
    _all_sig_tickers = list({s.ticker for s in swing_sigs + day_sigs + vcp_sigs + mom_sigs})
    try:
        _live_quotes = live_quotes_batch(tuple(_all_sig_tickers)) if _all_sig_tickers else {}
    except Exception:
        _live_quotes = {}

    # ── Pre-compute live R:R and price for every signal (used for sorting) ────
    def _sig_live_rr(sig):
        lp   = float(_live_quotes.get(sig.ticker, {}).get("price") or sig.price)
        risk = max(lp - sig.stop, 0.001)
        rwd  = sig.target1 - lp
        return round(rwd / risk, 2) if lp < sig.target1 else 0.0

    def _sig_live_p(sig):
        return float(_live_quotes.get(sig.ticker, {}).get("price") or sig.price)

    def _conviction(sig):
        # Combined score: stars matter most; live R:R breaks ties within same star level
        return sig.stars * 10 + min(_sig_live_rr(sig), 5.0)

    # ── Sort controls ─────────────────────────────────────────────────────────
    _sort_options = {
        "🏆 Recommended":          "recommended",
        "🎯 Best Entry First":      "rr_desc",
        "⚠️ Worst Entry First":     "rr_asc",
        "💰 Cheapest First":        "price_asc",
        "💎 Most Expensive First":  "price_desc",
        "⭐ Featured (3-star only)": "featured",
    }
    _sb1, _sb2, _sb3 = st.columns([1, 3, 1])
    _sort_label = _sb2.selectbox(
        "Sort by", list(_sort_options.keys()), index=0,
        label_visibility="collapsed", key="sc_sort"
    )
    _sort_mode = _sort_options[_sort_label]

    # ── Search / filter ───────────────────────────────────────────────────────
    _sc_search = st.text_input(
        "filter",
        placeholder="🔍  Filter results — ticker symbol or company name (e.g. NVDA, Energy, Amazon)…",
        label_visibility="collapsed",
        key="sc_filter",
    ).strip().upper()

    if _sc_search:
        def _sc_match(sig):
            if _sc_search in sig.ticker:
                return True
            _scco = cached_company_info(sig.ticker)
            return _sc_search in _scco.get("name", "").upper()
        swing_sigs = [s for s in swing_sigs if _sc_match(s)]
        day_sigs   = [s for s in day_sigs   if _sc_match(s)]
        mom_sigs   = [s for s in mom_sigs   if _sc_match(s)]
        if not swing_sigs and not day_sigs and not mom_sigs:
            st.warning(f"No results for **{_sc_search}** — try a shorter keyword or clear the filter.")

    # Apply sort to swing signals
    if _sort_mode == "rr_desc":
        swing_sigs = sorted(swing_sigs, key=_sig_live_rr, reverse=True)
    elif _sort_mode == "rr_asc":
        swing_sigs = sorted(swing_sigs, key=_sig_live_rr)
    elif _sort_mode == "price_asc":
        swing_sigs = sorted(swing_sigs, key=_sig_live_p)
    elif _sort_mode == "price_desc":
        swing_sigs = sorted(swing_sigs, key=_sig_live_p, reverse=True)
    elif _sort_mode == "featured":
        swing_sigs = [s for s in swing_sigs if s.stars == 3]
        swing_sigs = sorted(swing_sigs, key=_sig_live_rr, reverse=True)
    else:  # recommended — stars first, then live R:R
        swing_sigs = sorted(swing_sigs, key=_conviction, reverse=True)

    # Apply same sort to day signals (use vol_ratio proxy for R:R on day trades)
    if _sort_mode == "price_asc":
        day_sigs = sorted(day_sigs, key=_sig_live_p)
    elif _sort_mode == "price_desc":
        day_sigs = sorted(day_sigs, key=_sig_live_p, reverse=True)
    elif _sort_mode == "featured":
        day_sigs = [s for s in day_sigs if s.stars == 3]
    else:
        day_sigs = sorted(day_sigs, key=lambda s: (s.stars, s.vol_ratio), reverse=True)

    _use_groups = _sort_mode in ("recommended", "featured")  # flat list for price/R:R sorts

    # Sort momentum signals (always by stars + freshness of MACD cross)
    mom_sigs = sorted(mom_sigs, key=lambda s: (s.stars, s.rr, -s.macd_cross_days_ago), reverse=True)
    if _sort_mode == "price_asc":
        mom_sigs = sorted(mom_sigs, key=lambda s: s.price)
    elif _sort_mode == "price_desc":
        mom_sigs = sorted(mom_sigs, key=lambda s: s.price, reverse=True)
    elif _sort_mode == "featured":
        mom_sigs = [s for s in mom_sigs if s.stars == 3]

    # ── Pullback (intraday) scan — watchlist + day-trade tickers ──────────────
    @st.cache_data(ttl=90, show_spinner=False)
    def _cached_pullbacks(tickers: tuple):
        from daytrader_pullback import scan_pullback
        out = []
        for _t in tickers:
            try:
                _s = scan_pullback(_t)
                if _s:
                    out.append(_s)
            except Exception:
                pass
        out.sort(key=lambda s: s.score, reverse=True)
        return out

    _pb_wl = [(w if isinstance(w, str) else w.get("ticker", "")) for w in get_watchlist()]
    _pb_universe = tuple(t for t in dict.fromkeys(_pb_wl + [s.ticker for s in day_sigs]) if t)[:40]
    try:
        pb_sigs = _cached_pullbacks(_pb_universe)
    except Exception:
        pb_sigs = []

    # ── Unified Swing Picks — multi-factor engine (reversals + swing + pullback) ──
    def _tech_from_stars(_s):
        return {1: 55, 2: 70, 3: 85}.get(int(getattr(_s, "stars", 2) or 2), 65)

    def _norm_trade(_s, _kind):
        return {
            "price":    float(getattr(_s, "price", 0) or 0),
            "entry":    float(getattr(_s, "entry", 0) or getattr(_s, "price", 0) or 0),
            "stop":     float(getattr(_s, "stop", 0) or 0),
            "target":   float(getattr(_s, "target", 0) or getattr(_s, "target1", 0) or 0),
            "stop_pct": getattr(_s, "stop_pct", None),
            "gain_pct": getattr(_s, "gain_pct", None),
            "why":      getattr(_s, "why", "") or "",
            "sector":   getattr(_s, "sector", "") or "",
            "kind":     _kind,
        }

    def _tech_pick(_s, _reason):
        _st = int(getattr(_s, "stars", 2) or 2)
        return {"ticker": _s.ticker, "stars": _st, "score": _tech_from_stars(_s),
                "factors": {"technical": _tech_from_stars(_s)},
                "reasons": [getattr(_s, "why", "") or _reason]}

    _sw_src, _sw_cand = {}, []
    for _s in mom_sigs:
        if _s.ticker not in _sw_src:
            _sw_src[_s.ticker] = _norm_trade(_s, "Reversal")
            _sw_cand.append((_s.ticker, _tech_from_stars(_s), getattr(_s, "why", "") or "reversal setup"))
    for _s in swing_sigs:
        if _s.ticker not in _sw_src:
            _sw_src[_s.ticker] = _norm_trade(_s, "Swing")
            _sw_cand.append((_s.ticker, _tech_from_stars(_s), getattr(_s, "why", "") or "swing base"))
    for _s in pb_sigs:
        if _s.ticker not in _sw_src:
            _sw_src[_s.ticker] = _norm_trade(_s, "Pullback")
            _t = float(getattr(_s, "score", 0) or 0)
            _sw_cand.append((_s.ticker, min(100.0, _t) if _t else _tech_from_stars(_s), "pullback to support"))

    # Enrich only the strongest candidates — bounds Finnhub calls on the first (uncached) load.
    _sw_cand.sort(key=lambda _c: _c[1], reverse=True)
    _sw_cand = _sw_cand[:25]

    @st.cache_data(ttl=3600, show_spinner="Scoring swing picks…")
    def _rank_swing_cached(_cand_t, _reg):
        from swing_engine import rank_swing
        return rank_swing([list(x) for x in _cand_t], _reg)
    try:
        _reg_str = (load_regime() or {}).get("regime", "")
    except Exception:
        _reg_str = ""
    try:
        _swing_ranked = _rank_swing_cached(tuple(tuple(x) for x in _sw_cand), _reg_str)
    except Exception:
        _swing_ranked = []

    tab_swing, tab_dt, tab_more = st.tabs([
        f"🌟  Swing Picks  ({len(_swing_ranked)})",
        f"⚡  Day Trades  ({len(day_sigs)})",
        "🔧  Raw Scans",
    ])

    # ── Shared card renderer — same look/features as the Day Trades tab ────────
    def _render_swing_card(_p, _tr, _keyns):
        # Matches the Day Trades card exactly (spacing / font sizes / button layout).
        _tk = _p["ticker"]
        _stars_txt = "⭐" * int(_p["stars"])
        _sc = "#fbbf24" if _p["stars"] == 3 else "var(--muted)" if _p["stars"] == 2 else "#b45309"
        _co = cached_company_info(_tk)
        _lq = _live_quotes.get(_tk, {}) if isinstance(_live_quotes, dict) else {}
        _lp = float(_lq.get("price") or _tr.get("price") or _tr.get("entry") or 0)
        _lpct = float(_lq.get("pct") or 0.0)
        _lcol = "var(--pos)" if _lpct >= 0 else "var(--neg)"
        _licon = "▲" if _lpct >= 0 else "▼"
        _rz = " · ".join(_p["reasons"][:4]) or _tr.get("why") or "meets the setup criteria"
        _sp = f'&nbsp;<span style="color:var(--faint)">({_tr["stop_pct"]}%)</span>' if _tr.get("stop_pct") not in (None, "") else ""
        _gp = f'&nbsp;<span style="color:var(--faint)">(+{_tr["gain_pct"]}%)</span>' if _tr.get("gain_pct") not in (None, "") else ""
        _levels = ""
        if _tr.get("entry") and _tr.get("stop") and _tr.get("target"):
            _levels = (
                f'<div style="display:flex;gap:20px;flex-wrap:wrap;align-items:center">'
                f'<span style="font-size:.85rem"><span style="color:var(--faint)">Entry</span>&nbsp;<b style="color:var(--fg)">${_tr["entry"]:.2f}</b></span>'
                f'<span style="font-size:.85rem"><span style="color:var(--faint)">Stop</span>&nbsp;<b style="color:var(--neg)">${_tr["stop"]:.2f}</b>{_sp}</span>'
                f'<span style="font-size:.85rem"><span style="color:var(--faint)">Target</span>&nbsp;<b style="color:var(--pos)">${_tr["target"]:.2f}</b>{_gp}</span>'
                f'</div>')
        _facts = "".join(f'<span class="pill">{_k} {_v}</span>' for _k, _v in _p["factors"].items())
        st.markdown(
            f'<div class="card" style="border-left-color:{_sc};background:{_sc}0a">'
            f'<div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:8px">'
            f'<span style="font-size:.95rem">{_stars_txt}</span>'
            f'<span class="ticker-big">{_tk}</span>'
            f'<span style="color:var(--faint);font-size:.88rem;font-weight:600">{_co.get("name", _tk)}</span>'
            f'<span style="color:var(--muted);font-size:.9rem">&nbsp;${_lp:.2f}</span>'
            f'<span style="color:{_lcol};font-size:.78rem;font-weight:600">&nbsp;{_licon} {abs(_lpct):.2f}%</span>'
            f'<span class="pill" style="border-color:{_sc};color:{_sc}">{_tr.get("kind","Swing")}</span>'
            f'<span style="color:var(--faint);font-size:.78rem;margin-left:auto">score {_p["score"]}/100</span>'
            f'</div>'
            f'<p style="color:var(--fg);font-size:.92rem;line-height:1.72;margin:0 0 14px">{_rz}</p>'
            f'{_levels}'
            f'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:12px">{_facts}</div>'
            f'</div>', unsafe_allow_html=True)
        _dc1, _dc2, _dc3 = st.columns([4, 1, 1])
        _dc1.caption(f"{_tr.get('kind','Swing')} setup — swing hold, manage to your plan.")
        _dc2.markdown("<br>", unsafe_allow_html=True)
        if _dc2.button("➕ Track", key=f"{_keyns}_trk_{_tk}", use_container_width=True):
            _do_add(_tk, _tr.get("entry") or _lp, _tr.get("stop", 0), _tr.get("target", 0),
                    _tr.get("target", 0), 1, setup_type="swing", stars=int(_p["stars"]),
                    sector=_tr.get("sector", ""))
        _dc3.markdown("<br>", unsafe_allow_html=True)
        if TOKEN and CHAT_ID:
            if _dc3.button("📲", key=f"{_keyns}_snd_{_tk}", use_container_width=True, help="Send to Telegram"):
                from alerts import _send
                _msg = (f"🌟 <b>SWING PICK — {_tk}</b>\n━━━━━━━━━━━━━━━\n"
                        f"{_stars_txt}  score {_p['score']}/100  |  ${_lp:.2f}\n"
                        f"📥 Entry  ${_tr.get('entry') or _lp:.2f}\n🛑 Stop   ${_tr.get('stop',0):.2f}\n"
                        f"🎯 Target ${_tr.get('target',0):.2f}\n━━━━━━━━━━━━━━━\n<i>{_rz}</i>")
                st.toast("📲 Sent!" if _send(_msg) else "❌ Failed")
        if st.button("🔎 Deep Dive — full Analyze", key=f"{_keyns}_dd_{_tk}", use_container_width=True):
            _goto_analyze(_tk)

    with tab_swing:
        st.caption("The best swing candidates from every setup — reversals, pullbacks, and swing bases — "
                   "scored on a **consistent** set of factors (technical · market · insider buying · "
                   "analyst ratings · financials) and ranked by ⭐. Cached, so the list stays steady day-to-day.")
        if not _swing_ranked:
            st.info("No swing candidates from the latest scan yet. Add tickers to your **Watchlist**, then run the Scanner.")
        else:
            st.success(f"✅ {len(_swing_ranked)} swing pick{'s' if len(_swing_ranked)!=1 else ''} — ranked best → worst by ⭐")
            for _p in _swing_ranked:
                _tr = _sw_src.get(_p["ticker"], {"kind": "Swing"})
                _render_swing_card(_p, _tr, "swp")

    with tab_more:
        st.caption("The raw component scans behind Swing Picks — each method on its own, same card style "
                   "and actions. Useful if you want the breakdown by setup type.")
        _raw_groups = [("🔄 Reversals", mom_sigs, "Reversal", "reversal setup"),
                       ("📅 Swing bases", swing_sigs, "Swing", "swing base"),
                       ("🎯 Pullbacks", pb_sigs, "Pullback", "pullback to support"),
                       ("🔭 VCP", vcp_sigs, "VCP", "volatility contraction")]
        _any_raw = False
        for _glabel, _gsigs, _gkind, _greason in _raw_groups:
            if not _gsigs:
                continue
            _any_raw = True
            st.markdown(f"### {_glabel}  <span style='color:var(--dim);font-size:.82rem'>({len(_gsigs)})</span>",
                        unsafe_allow_html=True)
            for _s in _gsigs[:30]:
                _render_swing_card(_tech_pick(_s, _greason), _norm_trade(_s, _gkind), f"raw{_gkind[:2]}")
        if not _any_raw:
            st.info("No raw setups from the latest scan.")

    # ── PULLBACKS (intraday, Moomoo real-time) ────────────────────────────────
    if False:  # legacy pullback scan — replaced by unified Swing Picks / Raw Scans
        _pb_src = "Moomoo live" if _pb_universe else ""
        if not _pb_universe:
            st.info("Add tickers to your **Watchlist** (or run the scanner so Day Trades populate) — "
                    "the pullback bot scans those on 5-minute bars.")
        elif not pb_sigs:
            st.warning(f"No pullback setups right now across {len(_pb_universe)} tickers. "
                       "It waits for price to pull back to a support level (where shorts cover) "
                       "in an uptrend, then targets the next resistance (where shorts enter).")
        else:
            st.info("🎯 **Pullback method** — enter at support (where shorts cover), exit at the next "
                    "resistance (where shorts enter). Bars from Moomoo OpenD when running, else yfinance. "
                    "⚠️ Intraday day trades — manage them actively.")
            for sig in pb_sigs:
                co    = cached_company_info(sig.ticker)
                _lq   = _live_quotes.get(sig.ticker, {}) if isinstance(_live_quotes, dict) else {}
                _lp   = float(_lq.get("price") or sig.price)
                _sup_pills = " ".join(f'<span class="pill">S ${l.price:.2f}·{l.touches}x</span>'
                                      for l in sig.levels if l.kind == "support")
                _res_pills = " ".join(f'<span class="pill">R ${l.price:.2f}·{l.touches}x</span>'
                                      for l in sig.levels if l.kind == "resistance")
                st.markdown(
                    f'<div class="card" style="border-left-color:var(--border-strong)">'
                    f'<div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:6px">'
                    f'<span class="ticker-big">{sig.ticker}</span>'
                    f'<span style="color:var(--faint);font-size:.88rem;font-weight:600">{co.get("name", sig.ticker)}</span>'
                    f'<span class="price-tag">${_lp:.2f}</span>'
                    f'<span style="color:#fbbf24;font-size:.9rem">{"⭐"*sig.stars}</span></div>'
                    f'<p style="color:var(--muted);font-size:.9rem;line-height:1.6;margin:0 0 10px">{sig.why}</p>'
                    f'<div style="display:flex;gap:18px;flex-wrap:wrap;margin-bottom:8px">'
                    f'<span class="stat"><span class="stat-label">Entry (support)</span><span class="stat-value">${sig.entry:.2f}</span></span>'
                    f'<span class="stat"><span class="stat-label">Stop</span><span class="stat-value" style="color:var(--neg)">${sig.stop:.2f} ({sig.stop_pct}%)</span></span>'
                    f'<span class="stat"><span class="stat-label">Target (resistance)</span><span class="stat-value" style="color:var(--pos)">${sig.target:.2f} (+{sig.gain_pct}%)</span></span>'
                    f'<span class="stat"><span class="stat-label">R:R</span><span class="stat-value">{sig.rr:.1f}:1</span></span></div>'
                    f'<div style="margin-bottom:4px">{_sup_pills} {_res_pills}</div>'
                    f'</div>', unsafe_allow_html=True)
                _pc1, _pc2, _pc3 = st.columns([4, 1, 1])
                if _pc2.button("➕ Track", key=f"pb_a_{sig.ticker}", use_container_width=True):
                    _do_add(sig.ticker, sig.entry, sig.stop, sig.target, sig.target,
                            1, setup_type="day", sector=co.get("sector", ""))
                if _pc3.button("📊", key=f"pb_d_{sig.ticker}", use_container_width=True, help="Deep Dive"):
                    _goto_analyze(sig.ticker)
                with st.expander(f"🚀 Send {sig.ticker} to Moomoo"):
                    _render_moomoo_panel(sig.ticker, sig.entry, sig.stop, sig.target, sig.target,
                                         round(sig.entry - sig.stop, 2), key_suffix=f"pb_{sig.ticker}")
                st.markdown("---")

    # ── SWING ─────────────────────────────────────────────────────────────────
    if False:  # legacy swing scan — replaced by unified Swing Picks / Raw Scans
        if not swing_sigs:
            st.warning("No swing setups found with current filters. Widen the price range or wait for market conditions to improve.")
        else:
            _sort_desc = {
                "recommended": "sorted by conviction score (stars + live R:R)",
                "rr_desc":     "sorted by live entry quality — best R:R first",
                "rr_asc":      "sorted by live entry quality — worst R:R first",
                "price_asc":   "sorted by price — cheapest first",
                "price_desc":  "sorted by price — most expensive first",
                "featured":    "3-star setups only — highest conviction picks",
            }[_sort_mode]
            st.success(f"✅ {len(swing_sigs)} swing setup{'s' if len(swing_sigs)>1 else ''} — {_sort_desc}")

            # Build iteration: either grouped by stars or flat
            if _use_groups:
                _swing_iter = []
                for star_level, label in [(3,"⭐⭐⭐ Strong Buy"), (2,"⭐⭐ Good Setup"), (1,"⭐ Developing")]:
                    grp = [s for s in swing_sigs if s.stars == star_level]
                    if grp:
                        _swing_iter.append((label, grp))
            else:
                _swing_iter = [("", swing_sigs)]  # single flat group, no header

            # ── Pagination: show 20 at a time ────────────────────────────────
            _PAGE_SIZE = 20
            _page_key  = "sc_sw_shown"
            if _page_key not in st.session_state:
                st.session_state[_page_key] = _PAGE_SIZE
            _shown = st.session_state[_page_key]

            # Flatten all groups for pagination, then re-group for display
            _all_sigs_flat = [s for _, grp in _swing_iter for s in grp]
            _visible_sigs  = set(s.ticker for s in _all_sigs_flat[:_shown])

            _swing_rank = 0  # global rank counter across all groups
            for _group_label, group in _swing_iter:
                # Only show tickers within the current page
                group = [s for s in group if s.ticker in _visible_sigs]
                if not group:
                    continue
                if _group_label:
                    st.markdown(
                        f"### {_group_label}  <span style='color:var(--dim);font-size:.82rem'>({len(group)})</span>",
                        unsafe_allow_html=True,
                    )

                for sig in group:
                    _swing_rank += 1
                    sc_  = "#fbbf24" if sig.stars==3 else "var(--muted)" if sig.stars==2 else "#b45309"
                    sec_ = next((s for s, tl in UNIVERSE.items() if sig.ticker in tl), "")
                    co   = cached_company_info(sig.ticker)
                    _lq  = _live_quotes.get(sig.ticker, {})
                    _live_p   = float(_lq.get("price") or sig.price)
                    _live_pct = float(_lq.get("pct")   or 0.0)
                    _live_col  = "var(--pos)" if _live_pct >= 0 else "var(--neg)"
                    _live_icon = "▲" if _live_pct >= 0 else "▼"
                    _price_changed = abs(_live_p - sig.price) > 0.005

                    # ── Feature 1: Live R:R Validator ─────────────────────────
                    _risk_live      = max(_live_p - sig.stop, 0.001)
                    _reward_live    = sig.target1 - _live_p
                    _drift          = _live_p - sig.price          # how far price moved since signal
                    _drift_pct      = _drift / sig.price * 100     # % move since signal

                    # Context suffix — always explain the price movement
                    if abs(_drift) < 0.03:                         # price essentially unchanged
                        _move_ctx = "price at signal level"
                    elif _drift > 0:
                        _move_ctx = f"price rose ${_drift:.2f} ({_drift_pct:+.1f}%) since signal — reward window narrowed"
                    else:
                        _move_ctx = f"price dipped ${abs(_drift):.2f} ({_drift_pct:+.1f}%) since signal — better entry than scanned"

                    _live_rr = 0.0   # default — overwritten below if price hasn't hit target
                    if _live_p >= sig.target1:
                        _rr_label = "❌ Setup Invalidated — price already hit target, don't chase"
                        _rr_col   = "var(--neg)"
                        _rr_bg    = "rgba(239,68,68,.08)"
                    else:
                        _live_rr = round(_reward_live / _risk_live, 1)
                        if _live_rr >= 2.5:
                            _rr_label = f"✅ Strong Entry · R:R {_live_rr}:1 — {_move_ctx}"
                            _rr_col   = "var(--pos)"
                            _rr_bg    = "rgba(34,197,94,.08)"
                        elif _live_rr >= 1.8:
                            _rr_label = f"✅ Good Entry · R:R {_live_rr}:1 — {_move_ctx}"
                            _rr_col   = "var(--pos)"
                            _rr_bg    = "rgba(34,197,94,.06)"
                        elif _live_rr >= 1.5:
                            _rr_label = f"⚡ Acceptable Entry · R:R {_live_rr}:1 — {_move_ctx}. Consider smaller size"
                            _rr_col   = "#f59e0b"
                            _rr_bg    = "rgba(245,158,11,.08)"
                        else:
                            _rr_label = (f"⚠️ Entry Slipped · R:R {_live_rr}:1 — {_move_ctx}. "
                                        f"Wait for pullback to ~${sig.price:.2f} to restore original setup")
                            _rr_col   = "var(--neg)"
                            _rr_bg    = "rgba(239,68,68,.08)"

                    # ── Feature 2: Intraday Volume Surge ──────────────────────
                    _today_vol = int(_lq.get("volume", 0))
                    _vol_pill  = ""
                    if _today_vol > 0:
                        try:
                            from market_data import get_avg_daily_volume as _gavd
                            _avg_vol = _gavd(sig.ticker)
                            if _avg_vol > 0:
                                _now2  = datetime.now()
                                _mins_open = max((_now2.hour - 9) * 60 + _now2.minute - 30, 5)
                                _dfrac = min(_mins_open / 390.0, 1.0)
                                _proj  = int(_today_vol / max(_dfrac, 0.05))
                                _vratio = round(_proj / _avg_vol, 1)
                                if _vratio >= 2.0:
                                    _vol_pill = (
                                        f'<span class="pill" style="background:rgba(34,197,94,.12);'
                                        f'border-color:rgba(34,197,94,.35);color:var(--pos);font-weight:700">'
                                        f'⚡ Volume Surge {_vratio}x</span>')
                                elif _vratio >= 1.4:
                                    _vol_pill = (
                                        f'<span class="pill" style="background:rgba(245,158,11,.1);'
                                        f'border-color:rgba(245,158,11,.3);color:#f59e0b">'
                                        f'📊 Volume Active {_vratio}x</span>')
                        except Exception:
                            pass

                    with st.container():
                        # ── Build card HTML with NO dynamic/indented blocks ────
                        # All variable content is pre-computed as single-line strings
                        _price_changed_html = (
                            f'<span style="color:var(--faint);font-size:.72rem;margin-left:6px">'
                            f'(signal @ ${sig.price:.2f})</span>' if _price_changed else ""
                        )
                        import html as _hesc
                        _co_desc_html = (
                            f' &mdash; {_hesc.escape(co.get("description",""))}' if co.get("description") else ""
                        )
                        _atr_label = sig.indicators.get("Daily ATR", "—")

                        # Prepend live-price context to why_buy when entry quality changed
                        if _live_rr < 1.5 and _drift > 0.03:
                            _why_display = (
                                f'<span style="color:var(--neg);font-size:.8rem;font-weight:600">'
                                f'Price rose ${_drift:.2f} since signal — same setup, worse entry. '
                                f'Reward shrinks while stop stays put.</span> '
                                f'{sig.why_buy}'
                            )
                        elif _drift < -0.03:
                            _why_display = (
                                f'<span style="color:var(--pos);font-size:.8rem;font-weight:600">'
                                f'Better entry than signal — price dipped ${abs(_drift):.2f} giving more room to target.</span> '
                                f'{sig.why_buy}'
                            )
                        else:
                            _why_display = sig.why_buy

                        _is_watch   = sig.signal_type == "WATCH"
                        _card_class = "card-watch" if _is_watch else "card-buy"
                        _rank_html  = (
                            f'<span style="color:var(--dim);font-size:.72rem;font-weight:700;'
                            f'background:var(--surface);border:1px solid var(--border);'
                            f'border-radius:5px;padding:1px 6px;margin-right:4px">#{_swing_rank}</span>'
                            if not _use_groups else ""
                        )
                        _watch_level_html = (
                            f'&nbsp;&nbsp;<span style="font-size:.85rem">📍&nbsp;'
                            f'<span style="color:var(--faint)">Wait for</span>&nbsp;'
                            f'<b style="color:#f59e0b">${sig.watch_buy_at:.2f}</b></span>'
                        ) if (_is_watch and sig.watch_buy_at) else ""

                        _card_html = (
                            f'<div class="card {_card_class}">'
                            f'<div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:8px">'
                            f'{_rank_html}'
                            f'<span class="ticker-big">{sig.ticker}</span>'
                            f'<span style="color:var(--faint);font-size:.88rem;font-weight:600">{co.get("name", sig.ticker)}</span>'
                            f'<span style="color:var(--muted);font-size:.9rem">&nbsp;${_live_p:.2f}</span>'
                            f'<span style="color:{_live_col};font-size:.78rem;font-weight:600">&nbsp;{_live_icon} {abs(_live_pct):.2f}%</span>'
                            f'{sector_badge(sec_)}{_price_changed_html}'
                            f'</div>'
                            f'<p style="color:var(--fg);font-size:.92rem;line-height:1.72;margin:0 0 14px">{_why_display}</p>'
                            + (f'<div style="margin:-6px 0 10px">{_vol_pill}</div>' if _vol_pill else '')
                            + f'<div style="display:flex;gap:20px;flex-wrap:wrap;align-items:center">'
                            f'<span style="font-size:.85rem"><span style="color:var(--faint)">Stop</span>&nbsp;'
                            f'<b style="color:var(--neg)">${sig.stop:.2f}</b>&nbsp;<span style="color:var(--faint)">({sig.stop_pct}%)</span></span>'
                            f'<span style="font-size:.85rem"><span style="color:var(--faint)">T1</span>&nbsp;'
                            f'<b style="color:var(--pos)">${sig.target1:.2f}</b>&nbsp;<span style="color:var(--faint)">(+{sig.gain_pct}%)</span></span>'
                            f'<span style="font-size:.85rem"><span style="color:var(--faint)">T2</span>&nbsp;'
                            f'<b style="color:var(--pos)">${sig.target2:.2f}</b></span>'
                            f'{_watch_level_html}'
                            f'</div>'
                            f'</div>'
                        )
                        st.markdown(_card_html, unsafe_allow_html=True)

                        # ── WATCH banner ──────────────────────────────────────
                        _wb = _watch_banner_html(sig)
                        if _wb:
                            st.markdown(_wb, unsafe_allow_html=True)

                        # ── Feature 1: Live R:R badge ─────────────────────────
                        st.markdown(
                            f'<div style="margin:-6px 0 4px;padding:6px 14px;border-radius:8px;'
                            f'background:{_rr_bg};border-left:3px solid {_rr_col};'
                            f'font-size:.82rem;font-weight:600;color:{_rr_col}">{_rr_label}</div>',
                            unsafe_allow_html=True,
                        )

                        # ── Warnings (first one only) ─────────────────────────
                        if sig.warnings:
                            _w = sig.warnings[0]
                            st.markdown(
                                f'<p style="color:#f59e0b;font-size:.8rem;margin:0 0 6px">'
                                f'⚠️ {_w}</p>',
                                unsafe_allow_html=True,
                            )

                        sc1_, sc2_, sc3_, sc4_, sc5_, sc6_ = st.columns([2, 2, 1, 1, 1, 1])
                        qty_sc  = sc1_.number_input("Shares", min_value=0.0, step=1.0, value=1.0, format="%.4f", key=f"sc_q_{sig.ticker}")
                        _default_entry = float(sig.watch_buy_at) if _is_watch and sig.watch_buy_at else float(_live_p)
                        enp_sc  = sc2_.number_input("At $", value=_default_entry, format="%.2f", key=f"sc_p_{sig.ticker}")
                        sc3_.markdown("<br>", unsafe_allow_html=True)
                        _add_label = "👁 Watch" if _is_watch else "➕ Add"
                        if sc3_.button(_add_label, key=f"sc_a_{sig.ticker}", type="primary", use_container_width=True):
                            _do_add(sig.ticker, enp_sc, sig.stop, sig.target1, sig.target2, qty_sc,
                                    setup_type="swing", stars=sig.stars,
                                    rs_rank=getattr(sig, "rs_rank", 0),
                                    trend_template=getattr(sig, "trend_template", 0),
                                    sector=getattr(sig, "sector", ""))
                        sc4_.markdown("<br>", unsafe_allow_html=True)
                        if TOKEN and CHAT_ID:
                            if sc4_.button("📲", key=f"sc_t_{sig.ticker}", use_container_width=True):
                                _do_alert(sig.ticker, sig.price, sig.stop, sig.target1, sig.target2,
                                          sig.stop_pct, sig.gain_pct, sig.rr, sig.stars, sig.why_buy)
                        # ── Feature 3: Level break alert button ───────────────
                        sc5_.markdown("<br>", unsafe_allow_html=True)
                        from level_alerts import watch_ticker as _wt, ticker_is_watched as _tiw
                        _already_watched = _tiw(sig.ticker)
                        _al_label = "🔔" if not _already_watched else "🔕"
                        _al_help  = "Watch for level breaks (52w high, prev-day H/L, target, stop)" if not _already_watched else "Already watching — click to remove"
                        if sc5_.button(_al_label, key=f"sc_al_{sig.ticker}",
                                       use_container_width=True, help=_al_help):
                            if _already_watched:
                                from level_alerts import remove_ticker as _rt
                                _rt(sig.ticker)
                                st.toast(f"🔕 {sig.ticker} removed from level alerts")
                            else:
                                try:
                                    from market_data import get_bars as _gb, compute_indicators as _ci
                                    _alert_df = _gb(sig.ticker, "1y", "1d")
                                    if _alert_df is not None and len(_alert_df) >= 2:
                                        _adf = _ci(_alert_df)
                                        _52h = float(_alert_df["high"].max())
                                        _pdh = float(_alert_df["high"].iloc[-2])
                                        _pdl = float(_alert_df["low"].iloc[-2])
                                        _e200 = float(_adf["ema200"].iloc[-1]) if "ema200" in _adf.columns else None
                                        _lvls = {
                                            "52w_high":       round(_52h, 2),
                                            "prev_day_high":  round(_pdh, 2),
                                            "prev_day_low":   round(_pdl, 2),
                                            "signal_target1": sig.target1,
                                            "signal_stop":    sig.stop,
                                        }
                                        if _e200:
                                            _lvls["ema200"] = round(_e200, 2)
                                        _wt(sig.ticker, _lvls)
                                        st.toast(f"🔔 {sig.ticker} level alerts set!")
                                    else:
                                        st.toast(f"⚠️ Couldn't load data for {sig.ticker}")
                                except Exception as _e:
                                    st.toast(f"❌ Error: {_e}")
                            st.rerun()

                        # ── Moomoo button ─────────────────────────────────────
                        sc6_.markdown("<br>", unsafe_allow_html=True)
                        sc6_.markdown(
                            '<div style="text-align:center;font-size:.68rem;color:var(--accent);'
                            'font-weight:700;margin-top:2px">🚀</div>',
                            unsafe_allow_html=True,
                        )
                        with st.expander(f"🚀 Send {sig.ticker} to Moomoo", expanded=False):
                            _render_moomoo_panel(
                                sig.ticker, float(_live_p), sig.stop,
                                sig.target1, sig.target2,
                                _parse_atr(sig.indicators.get("Daily ATR", "$0")),
                                f"sc_{sig.ticker}",
                            )

                        # ── Deep Dive toggle button ───────────────────────────
                        _dd_key  = f"dive_{sig.ticker}_sw"
                        _dd_open = st.session_state.get(_dd_key, False)
                        _dd_btn_label = "🔍 Hide Deep Dive" if _dd_open else f"📊 Deep Dive — News · Fundamentals · AI Entry Verdict"
                        if st.button(_dd_btn_label, key=f"sc_dd_{sig.ticker}",
                                     use_container_width=True):
                            st.session_state[_dd_key] = not _dd_open
                            # clear cached AI result when closing so next open re-runs fresh
                            if _dd_open:
                                st.session_state.pop(f"dive_ai_{sig.ticker}", None)
                            st.rerun()

                        # ── Deep Dive panel ───────────────────────────────────
                        if st.session_state.get(_dd_key, False):
                            try:
                                from fundamentals import get_fundamental_snapshot as _gfs
                                _snap2 = _gfs(sig.ticker)
                            except Exception:
                                _snap2 = {}
                            _sig_ctx2 = {
                                "price": sig.price, "live_price": _live_p, "stop": sig.stop,
                                "target1": sig.target1, "target2": sig.target2,
                                "rr": sig.rr, "live_rr": round(_live_rr, 1),
                                "stop_pct": sig.stop_pct, "gain_pct": sig.gain_pct,
                                "stars": sig.stars, "warnings": sig.warnings,
                            }
                            _deep_dive_panel(
                                sig.ticker, _snap2, co, _live_p,
                                _sig_ctx2, f"dive_ai_{sig.ticker}", ai_type="entry"
                            )

                        st.markdown("---")

            # ── Show More button ──────────────────────────────────────────────
            _total_sigs = len(_all_sigs_flat)
            if _shown < _total_sigs:
                _remaining = _total_sigs - _shown
                sm1, sm2, sm3 = st.columns([1, 2, 1])
                if sm2.button(
                    f"⬇️ Show {min(_PAGE_SIZE, _remaining)} more  ({_shown}/{_total_sigs} displayed)",
                    use_container_width=True
                ):
                    st.session_state[_page_key] = _shown + _PAGE_SIZE
                    st.rerun()
            else:
                st.caption(f"All {_total_sigs} setups displayed.")
                if _total_sigs > _PAGE_SIZE:
                    if st.button("⬆️ Collapse to top 20", use_container_width=True):
                        st.session_state[_page_key] = _PAGE_SIZE
                        st.rerun()

    # ── DAY TRADES ─────────────────────────────────────────────────────────────
    with tab_dt:
        if not day_sigs:
            st.warning("No day trade setups right now. Volume patterns change throughout the day — refresh during market hours.")
        else:
            st.info("⚡ Day trades are **intraday only** — enter and exit the same day.")
            _dt_sort_desc = {
                "recommended": "sorted by conviction",
                "rr_desc":     "sorted by entry quality",
                "rr_asc":      "entry slipped most — first",
                "price_asc":   "cheapest first",
                "price_desc":  "most expensive first",
                "featured":    "3-star only",
            }[_sort_mode]
            st.success(f"✅ {len(day_sigs)} day trade setup{'s' if len(day_sigs)>1 else ''} — {_dt_sort_desc}")

            for sig in day_sigs:
                tc_  = "var(--pos)" if sig.trend=="up" else "var(--neg)" if sig.trend=="down" else "var(--faint)"
                ti_  = "⬆️" if sig.trend=="up" else "⬇️" if sig.trend=="down" else "↔️"
                sc_  = "#fbbf24" if sig.stars==3 else "var(--muted)" if sig.stars==2 else "#b45309"
                gs_  = f"Gap {sig.gap_pct:+.1f}%" if abs(sig.gap_pct) >= 0.5 else ""
                co   = cached_company_info(sig.ticker)
                _dlq  = _live_quotes.get(sig.ticker, {})
                _dlive_p   = _dlq.get("price") or sig.price
                _dlive_pct = _dlq.get("pct") or 0.0
                _dlive_col = "var(--pos)" if _dlive_pct >= 0 else "var(--neg)"
                _dlive_icon= "▲" if _dlive_pct >= 0 else "▼"

                with st.container():
                    _gs_html = (
                        f'<span class="pill" style="background:rgba(245,158,11,.1);border-color:rgba(245,158,11,.3);'
                        f'color:#f59e0b;font-size:.75rem">{gs_}</span>'
                    ) if gs_ else ""
                    st.markdown(
                        f'<div class="card" style="border-left-color:{tc_};background:{tc_}0a">'
                        f'<div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:8px">'
                        f'<span class="ticker-big">{sig.ticker}</span>'
                        f'<span style="color:var(--faint);font-size:.88rem;font-weight:600">{co.get("name", sig.ticker)}</span>'
                        f'<span style="color:var(--muted);font-size:.9rem">&nbsp;${_dlive_p:.2f}</span>'
                        f'<span style="color:{_dlive_col};font-size:.78rem;font-weight:600">&nbsp;{_dlive_icon} {abs(_dlive_pct):.2f}%</span>'
                        f'<span style="color:{tc_};font-size:.78rem;font-weight:700">&nbsp;{ti_} {sig.trend.upper()}</span>'
                        f'{sector_badge(sig.sector)}{_gs_html}'
                        f'</div>'
                        f'<p style="color:var(--fg);font-size:.92rem;line-height:1.72;margin:0 0 14px">{sig.why}</p>'
                        f'<div style="display:flex;gap:20px;flex-wrap:wrap;align-items:center">'
                        f'<span style="font-size:.85rem"><span style="color:var(--faint)">Entry</span>&nbsp;<b style="color:var(--fg)">${sig.entry:.2f}</b></span>'
                        f'<span style="font-size:.85rem"><span style="color:var(--faint)">Stop</span>&nbsp;<b style="color:var(--neg)">${sig.stop:.2f}</b>&nbsp;<span style="color:var(--faint)">({sig.stop_pct}%)</span></span>'
                        f'<span style="font-size:.85rem"><span style="color:var(--faint)">Target</span>&nbsp;<b style="color:var(--pos)">${sig.target:.2f}</b>&nbsp;<span style="color:var(--faint)">(+{sig.gain_pct}%)</span></span>'
                        f'</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

                    dc1, dc2, dc3 = st.columns([4, 1, 1])
                    dc1.caption("Day trade only — enter near open, exit before close.")
                    dc2.markdown("<br>", unsafe_allow_html=True)
                    if dc2.button("➕ Track", key=f"dt_a_{sig.ticker}", use_container_width=True):
                        _do_add(sig.ticker, sig.entry, sig.stop, sig.target, sig.target, 1,
                                setup_type="day", stars=sig.stars, sector=getattr(sig, "sector", ""))
                    dc3.markdown("<br>", unsafe_allow_html=True)
                    if TOKEN and CHAT_ID:
                        if dc3.button("📲", key=f"dt_t_{sig.ticker}", use_container_width=True):
                            from alerts import _send
                            msg = (
                                f"⚡ <b>DAY TRADE — {sig.ticker}</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n"
                                f"{'⭐'*sig.stars}  {ti_} {sig.trend.upper()}  |  ${sig.price:.2f}\n"
                                f"Range: {sig.atr_pct}%  |  Volume: {sig.vol_ratio}x\n"
                                f"📥 Entry  ${sig.entry:.2f}\n"
                                f"🛑 Stop   ${sig.stop:.2f} ({sig.stop_pct}%)\n"
                                f"🎯 Target ${sig.target:.2f} (+{sig.gain_pct}%)\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n"
                                f"<i>{sig.why}</i>"
                            )
                            ok = _send(msg)
                            st.toast("📲 Sent!" if ok else "❌ Failed")

                    # ── Deep Dive toggle (day trade) ──────────────────────────
                    _ddt_key  = f"dive_dt_{sig.ticker}_sw"
                    _ddt_open = st.session_state.get(_ddt_key, False)
                    _ddt_btn  = "🔍 Hide Deep Dive" if _ddt_open else "📊 Deep Dive — News · Fundamentals · AI Entry Verdict"
                    if st.button(_ddt_btn, key=f"dt_dd_{sig.ticker}", use_container_width=True):
                        st.session_state[_ddt_key] = not _ddt_open
                        if _ddt_open:
                            st.session_state.pop(f"dive_ai_dt_{sig.ticker}", None)
                        st.rerun()

                    if st.session_state.get(_ddt_key, False):
                        with st.container():
                            st.markdown(
                                '<div style="background:rgba(15,23,42,.85);border:1px solid var(--border);'
                                'border-radius:12px;padding:20px 22px;margin:0 0 10px">',
                                unsafe_allow_html=True,
                            )
                            try:
                                from fundamentals import get_fundamental_snapshot as _gfs
                                from ai_analysis import analyze_entry as _ae, get_ticker_news as _gtn
                                _dsnap  = _gfs(sig.ticker)
                                _dan    = _dsnap.get("analyst",   {})
                                _dea    = _dsnap.get("earnings",  {})
                                _din    = _dsnap.get("insider",   {})
                                _dfi    = _dsnap.get("financials",{})
                                _dnw    = _dsnap.get("news",      {})
                                _dco    = co
                            except Exception:
                                _dsnap = _dan = _dea = _din = _dfi = _dnw = {}
                                _dco   = {}

                            _ddt_c1, _ddt_c2 = st.columns([1, 1], gap="large")
                            with _ddt_c1:
                                st.markdown('<p style="color:var(--muted);font-size:.72rem;font-weight:700;letter-spacing:.08em;margin:0 0 8px">COMPANY PROFILE</p>', unsafe_allow_html=True)
                                st.markdown(
                                    f'<p style="color:var(--fg);font-weight:700;font-size:1rem;margin:0 0 2px">{_dco.get("name",sig.ticker)}</p>'
                                    f'<p style="color:var(--faint);font-size:.78rem;margin:0 0 8px">{_dco.get("sector","")}</p>'
                                    + (f'<p style="color:var(--muted);font-size:.8rem;line-height:1.6;margin:0 0 14px">{_dco.get("description","")}</p>' if _dco.get("description") else ""),
                                    unsafe_allow_html=True,
                                )
                                st.markdown('<p style="color:var(--muted);font-size:.72rem;font-weight:700;letter-spacing:.08em;margin:8px 0 8px">FUNDAMENTALS</p>', unsafe_allow_html=True)
                                def _ddr(label, value, color="var(--muted)"):
                                    return (f'<div style="display:flex;justify-content:space-between;border-bottom:1px solid rgba(255,255,255,.04);padding:5px 0">'
                                            f'<span style="color:var(--faint);font-size:.8rem">{label}</span>'
                                            f'<span style="color:{color};font-size:.8rem;font-weight:600">{value}</span></div>')
                                _drec  = _dan.get("recommendation","—").replace("_"," ").title()
                                _drcc  = "var(--pos)" if _drec.lower() in ("buy","strong buy") else "var(--neg)" if _drec.lower() in ("sell","strong sell") else "var(--muted)"
                                _dtgt  = _dan.get("target_mean")
                                _ddte  = _dea.get("days_until", 999)
                                _dsurp = _dea.get("last_surprise")
                                _drg   = _dfi.get("revenue_growth_yoy")
                                _dpm   = _dfi.get("profit_margin")
                                st.markdown(
                                    _ddr("Wall St Rating", f"{_drec} ({_dan.get('num_analysts',0)} analysts)", _drcc)
                                    + _ddr("Analyst Target", f"${_dtgt:.2f}" if _dtgt else "—")
                                    + _ddr("Next Earnings", f"In {_ddte}d" if 0 < _ddte <= 60 else "None soon",
                                           "var(--neg)" if 0<_ddte<=7 else "#f59e0b" if 0<_ddte<=21 else "var(--faint)")
                                    + _ddr("Last EPS Surprise", f"{_dsurp:+.1f}%" if _dsurp is not None else "—",
                                           "var(--pos)" if _dsurp and _dsurp>0 else "var(--neg)" if _dsurp and _dsurp<0 else "var(--faint)")
                                    + _ddr("Revenue Growth", f"{_drg:.1f}%" if _drg is not None else "—",
                                           "var(--pos)" if _drg and _drg>10 else "var(--neg)" if _drg and _drg<0 else "var(--muted)")
                                    + _ddr("Profit Margin", f"{_dpm*100:.1f}%" if _dpm else "—")
                                    + _ddr("Insider Activity", _din.get("signal","—").title(),
                                           "var(--pos)" if _din.get("signal")=="buying" else "var(--neg)" if _din.get("signal")=="selling" else "var(--faint)"),
                                    unsafe_allow_html=True,
                                )

                            with _ddt_c2:
                                st.markdown('<p style="color:var(--muted);font-size:.72rem;font-weight:700;letter-spacing:.08em;margin:0 0 8px">RECENT NEWS</p>', unsafe_allow_html=True)
                                try:
                                    _dnt = _gtn(sig.ticker, max_items=8)
                                    _dnl = [l for l in _dnt.split("\n") if l.strip()]
                                    _drn = ""
                                    _di  = 0
                                    while _di < len(_dnl):
                                        _dl = _dnl[_di]
                                        if _dl.startswith("•"):
                                            _dp = _dl[1:].strip()
                                            _ddt2, _dtt = "", _dp
                                            if _dp.startswith("["):
                                                _eb = _dp.find("]")
                                                if _eb > 0:
                                                    _ddt2 = _dp[1:_eb]
                                                    _dtt  = _dp[_eb+2:].strip()
                                            _dsm = ""
                                            if _di+1 < len(_dnl) and not _dnl[_di+1].startswith("•"):
                                                _dsm = _dnl[_di+1].strip()
                                                _di += 1
                                            _drn += (f'<div style="margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid rgba(255,255,255,.04)">'
                                                     f'<span style="color:var(--dim);font-size:.7rem">{_ddt2}</span> '
                                                     f'<span style="color:var(--fg);font-size:.82rem;font-weight:500">{_dtt}</span>'
                                                     + (f'<br><span style="color:var(--faint);font-size:.75rem;line-height:1.5">{_dsm}</span>' if _dsm else "")
                                                     + '</div>')
                                        _di += 1
                                    st.markdown(_drn if _drn else '<p style="color:var(--faint);font-size:.82rem">No recent news found.</p>', unsafe_allow_html=True)
                                except Exception:
                                    _dhlns = _dnw.get("headlines", [])
                                    for _dh in (_dhlns or ["No news available."]):
                                        st.markdown(f'<p style="color:var(--muted);font-size:.8rem;border-bottom:1px solid rgba(255,255,255,.05);padding:4px 0;margin:0">• {_dh}</p>', unsafe_allow_html=True)

                            # AI verdict
                            st.markdown(
                                '<hr style="border:none;border-top:1px solid rgba(255,255,255,.07);margin:16px 0 14px">'
                                '<p style="color:var(--muted);font-size:.72rem;font-weight:700;letter-spacing:.08em;margin:0 0 12px">🤖 AI ENTRY ANALYSIS</p>',
                                unsafe_allow_html=True,
                            )
                            _dai_key = f"dive_ai_dt_{sig.ticker}"
                            if _dai_key not in st.session_state:
                                with st.spinner(f"Analyzing {sig.ticker}…"):
                                    _dlrr = round(max((sig.target - _dlive_p) / (_dlive_p - sig.stop), 0), 1) if _dlive_p > sig.stop else 0
                                    _dsig_ctx = {
                                        "price": sig.entry, "live_price": _dlive_p,
                                        "stop": sig.stop, "target1": sig.target, "target2": sig.target,
                                        "rr": round((sig.target - sig.entry) / (sig.entry - sig.stop), 1) if sig.entry > sig.stop else 0,
                                        "live_rr": _dlrr, "stop_pct": sig.stop_pct, "gain_pct": sig.gain_pct,
                                        "stars": sig.stars, "warnings": [],
                                    }
                                    _dai = _ae(sig.ticker, signal=_dsig_ctx, company_info=_dco, fundamentals=_dsnap)
                                    st.session_state[_dai_key] = _dai
                            else:
                                _dai = st.session_state[_dai_key]

                            if "error" in _dai:
                                st.warning(f"AI unavailable: {_dai.get('message','')}")
                            else:
                                _dverdict = _dai.get("verdict","—")
                                _dvc_map  = {
                                    "ENTER": ("var(--pos)","rgba(34,197,94,.12)","rgba(34,197,94,.3)"),
                                    "WAIT":  ("#f59e0b","rgba(245,158,11,.12)","rgba(245,158,11,.3)"),
                                    "PASS":  ("var(--neg)","rgba(239,68,68,.12)","rgba(239,68,68,.3)"),
                                }
                                _dvc, _dvbg, _dvbd = _dvc_map.get(_dverdict, ("var(--muted)","rgba(148,163,184,.1)","rgba(148,163,184,.2)"))
                                _dcconf = _dai.get("confidence","—")
                                _dcconf_c = "var(--pos)" if _dcconf=="High" else "#f59e0b" if _dcconf=="Medium" else "var(--neg)"
                                st.markdown(
                                    f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">'
                                    f'<span style="background:{_dvbg};border:1px solid {_dvbd};color:{_dvc};font-weight:800;font-size:1rem;padding:4px 14px;border-radius:8px;letter-spacing:.04em">{_dverdict}</span>'
                                    f'<span style="color:{_dcconf_c};font-size:.75rem;font-weight:600">{_dcconf} confidence</span></div>'
                                    f'<p style="color:var(--fg);font-weight:600;font-size:.95rem;margin:0 0 10px;line-height:1.5">{_dai.get("headline","")}</p>'
                                    f'<p style="color:var(--muted);font-size:.83rem;line-height:1.7;margin:0 0 14px">{_dai.get("reasoning","")}</p>',
                                    unsafe_allow_html=True,
                                )
                                _drc1, _drc2 = st.columns(2)
                                with _drc1:
                                    if _dai.get("risks"):
                                        st.markdown('<p style="color:var(--neg);font-size:.72rem;font-weight:700;letter-spacing:.08em;margin:0 0 6px">RISKS</p>', unsafe_allow_html=True)
                                        for _dr in _dai["risks"]:
                                            st.markdown(f'<p style="color:#fca5a5;font-size:.8rem;margin:0 0 4px">⚠️ {_dr}</p>', unsafe_allow_html=True)
                                with _drc2:
                                    if _dai.get("catalysts"):
                                        st.markdown('<p style="color:var(--pos);font-size:.72rem;font-weight:700;letter-spacing:.08em;margin:0 0 6px">CATALYSTS</p>', unsafe_allow_html=True)
                                        for _dc in _dai["catalysts"]:
                                            st.markdown(f'<p style="color:#86efac;font-size:.8rem;margin:0 0 4px">✅ {_dc}</p>', unsafe_allow_html=True)
                                if _dai.get("trade_tip"):
                                    st.markdown(
                                        f'<div style="margin-top:12px;background:rgba(59,130,246,.08);border-left:3px solid var(--accent);border-radius:6px;padding:8px 14px">'
                                        f'<span style="color:var(--accent);font-size:.72rem;font-weight:700;letter-spacing:.08em">💡 TRADE TIP</span><br>'
                                        f'<span style="color:var(--accent);font-size:.82rem">{_dai["trade_tip"]}</span></div>',
                                        unsafe_allow_html=True,
                                    )
                                if st.button("🔄 Re-run AI Analysis", key=f"dt_dd_refresh_{sig.ticker}", use_container_width=True):
                                    st.session_state.pop(_dai_key, None)
                                    st.rerun()

                            st.markdown('</div>', unsafe_allow_html=True)

                    st.markdown("---")

    # ── VCP PATTERNS ───────────────────────────────────────────────────────────
    if False:  # legacy VCP scan — replaced by Raw Scans
        if not vcp_sigs:
            st.warning("No VCP setups detected right now. VCPs need a prior uptrend + base + volume dry-up — run during market hours after a trending market.")
        else:
            st.info(
                "🔭 **Volatility Contraction Pattern (Minervini)** — progressively tighter pullbacks "
                "after a strong uptrend. Buy the breakout above the pivot high with tight risk."
            )
            st.success(f"✅ {len(vcp_sigs)} VCP setup{'s' if len(vcp_sigs)>1 else ''} found — sorted by quality then RS rank")

            for sig in vcp_sigs:
                _rs   = getattr(sig, "rs_rank", 0)
                _tt   = getattr(sig, "trend_template", 0)
                _rs_col = "var(--pos)" if _rs >= 80 else "#f59e0b" if _rs >= 60 else "var(--faint)"
                _tt_col = "var(--pos)" if _tt >= 6  else "#f59e0b" if _tt >= 4  else "var(--faint)"
                _rs_badge = (
                    f'<span title="Relative Strength rank vs SPY (0–99)" '
                    f'style="background:var(--surface);border:1px solid {_rs_col}40;'
                    f'color:{_rs_col};font-size:.72rem;font-weight:700;padding:2px 8px;'
                    f'border-radius:6px;margin-right:4px">RS {_rs:.0f}</span>'
                )
                _tt_badge = (
                    f'<span title="Minervini Trend Template (0–8 criteria)" '
                    f'style="background:var(--surface);border:1px solid {_tt_col}40;'
                    f'color:{_tt_col};font-size:.72rem;font-weight:700;padding:2px 8px;'
                    f'border-radius:6px;margin-right:4px">TT {_tt}/8</span>'
                )
                _sc_col  = "#fbbf24" if sig.stars==3 else "var(--muted)" if sig.stars==2 else "#b45309"
                _near_pct = round((sig.pivot_high - sig.price) / sig.pivot_high * 100, 1)
                _contractions_str = " → ".join(f"{d}%" for d in sig.contractions)
                co = cached_company_info(sig.ticker)
                _vlq = _live_quotes.get(sig.ticker, {})
                _vlive_p   = _vlq.get("price") or sig.price
                _vlive_pct = _vlq.get("pct") or 0.0
                _vlive_col = "var(--pos)" if _vlive_pct >= 0 else "var(--neg)"
                _vlive_icon= "▲" if _vlive_pct >= 0 else "▼"

                with st.container():
                    st.markdown(f"""
                    <div class="card" style="border-left-color:var(--border-strong);background:var(--surface)">
                        <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px">
                            <div>
                                <span style="color:{_sc_col}">{"⭐"*sig.stars}</span>&nbsp;
                                <span class="ticker-big">{sig.ticker}</span>&nbsp;
                                <span class="price-tag" title="Live price">${_vlive_p:.2f}</span>&nbsp;
                                <span style="color:{_vlive_col};font-weight:600;font-size:.78rem">{_vlive_icon} {abs(_vlive_pct):.2f}%</span>&nbsp;
                                <span style="color:var(--accent);font-weight:700;font-size:.85rem">🔭 VCP</span>
                                {sector_badge(sig.sector)}
                            </div>
                            <div style="text-align:right;color:var(--faint);font-size:.8rem">
                                {sig.num_pivots}-pivot base &nbsp;·&nbsp; {sig.base_days}d &nbsp;·&nbsp; tight {sig.tightness_pct}%
                            </div>
                        </div>
                        <div style="margin:6px 0 4px">{_rs_badge}{_tt_badge}</div>
                        <div class="company-meta">
                            <span class="company-name">{co.get("name", sig.ticker)}</span>
                            {f" — {co.get('description','')}" if co.get('description') else ""}
                        </div>
                        <div class="thin-div"></div>
                        <div style="margin-bottom:8px">
                            <span style="color:var(--muted);font-size:.78rem;font-weight:600">CONTRACTIONS: </span>
                            <span style="color:var(--accent);font-size:.82rem">{_contractions_str}</span>
                            &nbsp;·&nbsp;
                            <span style="color:#{'22c55e' if sig.vol_dry_up else '64748b'};font-size:.78rem">
                                {'✅ Volume dried up' if sig.vol_dry_up else '⚠️ Volume not yet dry'}
                            </span>
                            &nbsp;·&nbsp;
                            <span style="color:var(--muted);font-size:.78rem">Prior trend +{sig.prior_trend_pct:.0f}%</span>
                        </div>
                        <p style="color:var(--muted);margin:0 0 10px;line-height:1.65">{sig.why}</p>
                        <div>
                            <span class="pill">🚀 Pivot <b>${sig.pivot_high:.2f}</b> ({_near_pct}% away)</span>
                            <span class="pill">🛑 Stop <b>${sig.stop:.2f}</b></span>
                            <span class="pill">🎯 Target <b>${sig.target:.2f}</b></span>
                        </div>
                    </div>
                    """, unsafe_allow_html=True)

                    vc1, vc2, vc3 = st.columns([4, 1, 1])
                    vc1.caption("Buy on breakout above pivot high — not before. Wait for volume confirmation.")
                    vc2.markdown("<br>", unsafe_allow_html=True)
                    if vc2.button("➕ Track", key=f"vcp_a_{sig.ticker}", use_container_width=True):
                        _do_add(sig.ticker, sig.pivot_high, sig.stop, sig.target, sig.target, 1,
                                setup_type="vcp", stars=sig.stars,
                                rs_rank=getattr(sig, "rs_rank", 0),
                                trend_template=getattr(sig, "trend_template", 0),
                                sector=getattr(sig, "sector", ""))
                    vc3.markdown("<br>", unsafe_allow_html=True)
                    if TOKEN and CHAT_ID:
                        if vc3.button("📲", key=f"vcp_t_{sig.ticker}", use_container_width=True):
                            from alerts import _send
                            _ct_str = " → ".join(f"{d}%" for d in sig.contractions)
                            msg = (
                                f"🔭 <b>VCP SETUP — {sig.ticker}</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n"
                                f"{'⭐'*sig.stars}  {sig.num_pivots}-pivot base  |  ${sig.price:.2f}\n"
                                f"Contractions: {_ct_str}\n"
                                f"Pivot breakout: ${sig.pivot_high:.2f} ({_near_pct}% away)\n"
                                f"Tight: {sig.tightness_pct}%  |  {'Vol dried up ✅' if sig.vol_dry_up else 'Vol OK'}\n"
                                f"RS {_rs:.0f}  |  TT {_tt}/8\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n"
                                f"📥 Entry above  ${sig.pivot_high:.2f}\n"
                                f"🛑 Stop          ${sig.stop:.2f}\n"
                                f"🎯 Target        ${sig.target:.2f}\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n"
                                f"<i>{sig.why}</i>"
                            )
                            ok = _send(msg)
                            st.toast("📲 Sent!" if ok else "❌ Failed")

                    st.markdown("---")

    # ── MOMENTUM REVERSALS ─────────────────────────────────────────────────────
    if False:  # legacy reversal scan — replaced by unified Swing Picks / Raw Scans
        if not mom_sigs:
            st.warning(
                "No turnarounds detected right now. This scan hunts for stocks at the **end of a "
                "bearish run that are starting to reverse** (MACD crossing up, bouncing off an "
                "oversold low, holding a higher low) or with a **fresh catalyst** (gap-up on heavy "
                "volume) — including small-cap 'next NVDA' names. Nothing's set up cleanly yet."
            )
        else:
            st.info(
                "🔄 **Turnarounds & Catalysts** — beaten-down stocks starting to reverse, deep-bottom "
                "bounces off oversold lows, and catalyst gap-ups on heavy volume. Includes small-cap "
                "growth names. ⚠️ **Higher risk** than momentum swings — you're catching the turn early, "
                "so size smaller and honor the stop."
            )
            st.success(
                f"✅ {len(mom_sigs)} turnaround{'s' if len(mom_sigs)>1 else ''} — "
                f"reclaim · deep-bottom · catalyst, best setups first"
            )

            for sig in mom_sigs:
                co        = cached_company_info(sig.ticker)
                _mlq      = _live_quotes.get(sig.ticker, {})
                _mlive_p  = float(_mlq.get("price") or sig.price)
                _mlive_pct= float(_mlq.get("pct")   or 0.0)
                _mlive_col= "var(--pos)" if _mlive_pct >= 0 else "var(--neg)"
                _mlive_icon="▲" if _mlive_pct >= 0 else "▼"

                _setup        = getattr(sig, "setup_type", "reclaim")
                _cross_label  = (
                    "today" if sig.macd_cross_days_ago == 0 else
                    f"{sig.macd_cross_days_ago}d ago"
                )
                # Setup badge — catalyst gap vs MACD cross
                if _setup == "catalyst":
                    _setup_badge = (
                        '<span style="font-size:.75rem;font-weight:700;color:#fbbf24;'
                        'background:rgba(245,158,11,.12);border:1px solid rgba(245,158,11,.35);'
                        'border-radius:4px;padding:1px 6px;margin-left:4px">📣 Catalyst gap</span>'
                    )
                else:
                    _setup_badge = (
                        '<span style="font-size:.75rem;font-weight:700;color:var(--muted);'
                        'background:var(--surface);border:1px solid var(--border-strong);'
                        f'border-radius:4px;padding:1px 6px;margin-left:4px">🔄 MACD cross {_cross_label}</span>'
                    )
                # 200-MA state — Above / Near / Deep bottom
                if sig.above_200ma:
                    _200ma_badge = (
                        '<span style="font-size:.75rem;font-weight:700;color:var(--pos);'
                        'background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.3);'
                        'border-radius:4px;padding:1px 6px;margin-left:4px">Above 200MA</span>'
                    )
                elif _setup == "deep_bottom":
                    _200ma_badge = (
                        '<span style="font-size:.75rem;font-weight:700;color:#f87171;'
                        'background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);'
                        'border-radius:4px;padding:1px 6px;margin-left:4px">Deep bottom</span>'
                    )
                else:
                    _200ma_badge = (
                        '<span style="font-size:.75rem;font-weight:700;color:#f59e0b;'
                        'background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.3);'
                        'border-radius:4px;padding:1px 6px;margin-left:4px">Near 200MA</span>'
                    )

                with st.container():
                    st.markdown(
                        f'<div class="card" style="border-left-color:var(--border-strong);background:var(--surface)">'
                        f'<div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:8px">'
                        f'<span class="ticker-big">{sig.ticker}</span>'
                        f'<span style="color:var(--faint);font-size:.88rem;font-weight:600">{co.get("name", sig.ticker)}</span>'
                        f'<span style="color:var(--muted);font-size:.9rem">&nbsp;${_mlive_p:.2f}</span>'
                        f'<span style="color:{_mlive_col};font-size:.78rem;font-weight:600">&nbsp;{_mlive_icon} {abs(_mlive_pct):.2f}%</span>'
                        f'{_setup_badge}'
                        f'{_200ma_badge}'
                        f'{sector_badge(sig.sector)}'
                        f'</div>'
                        f'<p style="color:var(--fg);font-size:.92rem;line-height:1.72;margin:0 0 14px">{sig.why}</p>'
                        f'<div style="display:flex;gap:20px;flex-wrap:wrap;align-items:center">'
                        f'<span style="font-size:.85rem"><span style="color:var(--faint)">Stop</span>&nbsp;'
                        f'<b style="color:var(--neg)">${sig.stop:.2f}</b>&nbsp;<span style="color:var(--faint)">({sig.stop_pct}%)</span></span>'
                        f'<span style="font-size:.85rem"><span style="color:var(--faint)">T1</span>&nbsp;'
                        f'<b style="color:var(--pos)">${sig.target1:.2f}</b>&nbsp;<span style="color:var(--faint)">(+{sig.gain_pct}%)</span></span>'
                        f'<span style="font-size:.85rem"><span style="color:var(--faint)">T2</span>&nbsp;'
                        f'<b style="color:var(--pos)">${sig.target2:.2f}</b></span>'
                        f'<span style="font-size:.85rem;color:var(--faint)">R:R <b style="color:var(--accent)">{sig.rr:.1f}:1</b></span>'
                        f'</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

                    _mc1, _mc2, _mc3, _mc4 = st.columns([4, 1, 1, 1])
                    _mc1.caption(
                        f"RSI {sig.rsi:.0f}  ·  Vol {sig.vol_ratio:.1f}x  ·  ADX {sig.adx:.0f}"
                        + ("  ·  ✅ Above 200MA" if sig.above_200ma else "  ·  ⚠️ Below 200MA")
                    )
                    _mc2.markdown("<br>", unsafe_allow_html=True)
                    if _mc2.button("➕ Track", key=f"mr_a_{sig.ticker}", use_container_width=True):
                        _do_add(sig.ticker, sig.price, sig.stop, sig.target1, sig.target2,
                                1, setup_type="swing", stars=sig.stars, sector=sig.sector)
                    _mc3.markdown("<br>", unsafe_allow_html=True)
                    if TOKEN and CHAT_ID:
                        if _mc3.button("📲", key=f"mr_t_{sig.ticker}", use_container_width=True,
                                       help="Send to Telegram"):
                            from alerts import _send
                            _msg = (
                                f"🔄 <b>REVERSAL — {sig.ticker}</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n"
                                f"{'⭐'*sig.stars}  MACD cross {_cross_label}  |  ${sig.price:.2f}\n"
                                f"RSI {sig.rsi:.0f}  |  Vol {sig.vol_ratio:.1f}x  |  "
                                f"{'Above' if sig.above_200ma else 'Near'} 200MA\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n"
                                f"🛑 Stop     ${sig.stop:.2f}  ({sig.stop_pct}%)\n"
                                f"🎯 T1       ${sig.target1:.2f}  (+{sig.gain_pct}%)\n"
                                f"🎯 T2       ${sig.target2:.2f}\n"
                                f"R:R         {sig.rr:.1f}:1\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n"
                                f"<i>{sig.why}</i>"
                            )
                            ok = _send(_msg)
                            st.toast("📲 Sent!" if ok else "❌ Failed")
                    _mc4.markdown("<br>", unsafe_allow_html=True)
                    if _mc4.button("📊", key=f"mr_d_{sig.ticker}", use_container_width=True,
                                   help="Deep Dive analysis"):
                        st.session_state["dd_ticker"] = sig.ticker
                        st.session_state["page"]      = "🔎  Analyze"
                        st.rerun()
                    st.markdown("---")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE — PORTFOLIO  (Stocks + Options in one place)
# ══════════════════════════════════════════════════════════════════════════════
elif page == "💼  Portfolio":
    live_market_bar()
    _page_header("💼 Portfolio", "Open positions & options · live prices via Finnhub WebSocket")

    # ── Live price ticker ─────────────────────────────────────────────────────
    live_ticker_strip()

    tab_stk, tab_opt, tab_hist, tab_perf = st.tabs(["📈  Stocks", "🎯  Options", "📋  History", "📊  Performance"])

    # ── STOCKS ────────────────────────────────────────────────────────────────
    with tab_stk:
        positions = get_positions()

        # ── Add new position ──────────────────────────────────────────────────
        # No st.form here on purpose: plain widgets rerun as the user types, so we
        # can recommend a stop/targets from their entry price AND register on the
        # very first click of Add (the old form needed multiple tries).
        with st.expander("➕ Add New Position", expanded=(not positions)):
            # Clear the inputs after a successful add — done at the top of the next
            # run, before the widgets are recreated (Streamlit-safe).
            if st.session_state.pop("pf_add_clear", False):
                for _k in ("pf_add_tkr", "pf_add_entry", "pf_add_qty", "pf_add_stop",
                           "pf_add_t1", "pf_add_t2", "pf_add_notes", "pf_add_reckey"):
                    st.session_state.pop(_k, None)
            st.session_state.setdefault("pf_add_entry", 0.0)
            st.session_state.setdefault("pf_add_qty", 10.0)

            _pfa1, _pfa2, _pfa3 = st.columns([3, 1, 1])
            _pft = _pfa1.text_input("Ticker", placeholder="AAPL", key="pf_add_tkr").upper().strip()
            _pfe = _pfa2.number_input("Entry $", min_value=0.0, step=0.01, format="%.2f", key="pf_add_entry")
            _pfq = _pfa3.number_input("Shares", min_value=0.0, step=1.0, format="%.4f", key="pf_add_qty",
                                      help="Fractional shares are allowed — e.g. 1.5 or 0.25")

            # As soon as ticker + entry are set, recommend stop/targets from THAT
            # entry price and pre-fill the fields below (still editable).
            _rec = {}
            if _pft and _pfe > 0:
                _rec = _entry_reco(_pft, float(_pfe)) or {}
                if _rec and st.session_state.get("pf_add_reckey") != f"{_pft}:{_pfe:.2f}":
                    st.session_state["pf_add_reckey"] = f"{_pft}:{_pfe:.2f}"
                    st.session_state["pf_add_stop"] = _rec["stop"]
                    st.session_state["pf_add_t1"]   = _rec["target1"]
                    st.session_state["pf_add_t2"]   = _rec["target2"]

            if _rec:
                _vcol = {"Fair fill": "var(--pos)", "Patient": "var(--accent)",
                         "Chasing": "var(--neg)"}.get(_rec["verdict"], "var(--fg)")
                _notes_html = "".join(f"<li style='margin:2px 0'>{_n}</li>" for _n in _rec["notes"])
                st.markdown(
                    f'<div style="border:1px solid var(--border);border-radius:10px;'
                    f'padding:12px 14px;margin:8px 0 12px;background:var(--surface)">'
                    f'<div style="font-size:.72rem;letter-spacing:.5px;color:var(--dim);'
                    f'text-transform:uppercase;margin-bottom:7px">Recommendation from your entry '
                    f'<span style="color:{_vcol};font-weight:800">· {_rec["verdict"]}</span></div>'
                    f'<div style="display:flex;gap:18px;flex-wrap:wrap;font-size:.9rem;margin-bottom:9px">'
                    f'<span style="color:var(--muted)">Live <b style="color:var(--fg)">${_rec["current"]:.2f}</b></span>'
                    f'<span style="color:var(--muted)">Stop <b style="color:var(--neg)">${_rec["stop"]:.2f}</b></span>'
                    f'<span style="color:var(--muted)">Target 1 <b style="color:var(--pos)">${_rec["target1"]:.2f}</b></span>'
                    f'<span style="color:var(--muted)">Target 2 <b style="color:var(--pos)">${_rec["target2"]:.2f}</b></span>'
                    f'<span style="color:var(--muted)">R:R <b style="color:var(--fg)">{_rec["rr"]}:1</b></span>'
                    f'<span style="color:var(--muted)">ATR <b style="color:var(--fg)">${_rec["atr"]:.2f}</b></span></div>'
                    f'<ul style="margin:0;padding-left:18px;font-size:.83rem;color:var(--muted);line-height:1.4">{_notes_html}</ul>'
                    f'</div>', unsafe_allow_html=True)
                st.caption("Suggested stop & targets are pre-filled below — adjust if you like, then click Add.")
            elif _pft and _pfe > 0:
                st.caption("Couldn't pull data for a recommendation — enter your own stop and targets below.")

            st.session_state.setdefault("pf_add_stop", 0.0)
            st.session_state.setdefault("pf_add_t1", 0.0)
            st.session_state.setdefault("pf_add_t2", 0.0)
            _sc1, _sc2, _sc3 = st.columns(3)
            _pfs  = _sc1.number_input("Stop $",     min_value=0.0, step=0.01, format="%.2f", key="pf_add_stop")
            _pft1 = _sc2.number_input("Target 1 $", min_value=0.0, step=0.01, format="%.2f", key="pf_add_t1")
            _pft2 = _sc3.number_input("Target 2 $", min_value=0.0, step=0.01, format="%.2f", key="pf_add_t2")
            _pfn  = st.text_input("Notes (optional)", key="pf_add_notes")

            if st.button("➕ Add Position", type="primary", key="pf_add_btn", use_container_width=True):
                if _pft and _pfe > 0 and _pfq > 0:
                    # Fill any blanks with sensible long-only defaults so ONE click
                    # always registers — no re-entering.
                    _stop = _pfs  if _pfs  > 0 else round(_pfe * 0.95, 2)
                    _t1   = _pft1 if _pft1 > 0 else round(_pfe * 1.10, 2)
                    _t2   = _pft2 if _pft2 > 0 else round(_t1  * 1.08, 2)
                    add_position(_pft, float(_pfe), _stop, _t1, _t2, round(float(_pfq), 6), notes=_pfn)
                    st.session_state["pf_add_clear"] = True
                    st.toast(f"✅ {_pft} added to portfolio!")
                    st.rerun()
                else:
                    st.error("Enter a Ticker, an Entry $, and Shares greater than 0.")

        import broker_store as _bstore0
        from auth import current_user as _cu0
        if not positions and not _bstore0.load(_cu0()):
            st.info("No open stock positions yet. Add one above, or grab a pick "
                    "from the Scanner.")
        else:
            # ── One-time per session: run chart analysis for each position ────
            # (slow AI call — done outside the live fragment so it only runs once)
            for _pt, _pp in positions.items():
                _ca_key = f"ca_{_pt}"
                if _ca_key not in st.session_state:
                    try:
                        _dh = 0
                        try:
                            _dh = (date.today() - date.fromisoformat(_pp.get("date_in", str(date.today())))).days
                        except Exception:
                            pass
                        st.session_state[_ca_key] = chart_analyze(_pt, position={
                            "entry":   _pp["entry"], "stop": _pp["stop"],
                            "target1": _pp.get("target1", 0), "target2": _pp.get("target2", 0),
                            "qty":     _pp.get("qty", 1), "days_held": _dh,
                        })
                    except Exception:
                        st.session_state[_ca_key] = {}

            # ── Auto-populate holdings from THIS user's connected brokers (60s cache) ─
            # Per-user encrypted store — each user only ever sees their own broker data.
            import broker_store as _bstore1
            from auth import current_user as _cu1
            from brokers import build_adapter as _ba
            _my_creds = _bstore1.load(_cu1())
            _conn_brokers = list(_my_creds.keys())
            if _conn_brokers:
                if (_time.time() - st.session_state.get("broker_holdings_ts", 0)) > 60:
                    with st.spinner("Loading your broker holdings…"):
                        _bh = []
                        for _bn in _conn_brokers:
                            _adp = _ba(_bn, _my_creds.get(_bn, {}))
                            if _adp:
                                for _pp in (_adp.get_positions() or []):
                                    _bh.append({"account": _bn, **_pp})
                        st.session_state["broker_holdings"] = _bh
                        st.session_state["broker_holdings_ts"] = _time.time()
                _c1b, _c2b = st.columns([4, 1])
                _c1b.caption(f"Auto-synced from {', '.join(_conn_brokers)}  ·  "
                             f"{len(st.session_state.get('broker_holdings', []))} broker holdings")
                if _c2b.button("🔄 Refresh now", use_container_width=True):
                    st.session_state["broker_holdings_ts"] = 0
                    st.rerun()

            # ── Live fragment — reruns every 5 seconds ────────────────────────
            @st.fragment(run_every=5)
            def _live_portfolio(pos_snapshot: dict):
                """All live numbers live here — re-executes every 5 s automatically."""
                # Fetch live prices for tracked + broker tickers in one batch call
                _broker_holds = st.session_state.get("broker_holdings", [])
                _held = tuple({*pos_snapshot.keys(), *(b["ticker"] for b in _broker_holds)})
                try:
                    _lq = live_quotes_batch(_held) if _held else {}
                except Exception:
                    _lq = {}

                # ── Aggregate summary ─────────────────────────────────────────
                _total_pnl   = 0.0
                _total_cost  = 0.0
                _total_value = 0.0
                _day_pnl     = 0.0    # today's $ move across all positions
                _pos_data    = {}

                for _tk, _pos in pos_snapshot.items():
                    _q        = _lq.get(_tk, {})
                    _lp       = _q.get("price") or _pos["entry"]
                    _prev_c   = _q.get("prev_close") or _lp   # prev close for day P&L
                    _qty      = _pos.get("qty", 1)
                    _pnl_dol  = (_lp - _pos["entry"]) * _qty
                    _day_move = (_lp - _prev_c) * _qty
                    _cost     = _pos["entry"] * _qty
                    _total_pnl   += _pnl_dol
                    _total_cost  += _cost
                    _total_value += _lp * _qty
                    _day_pnl     += _day_move
                    _pos_data[_tk] = {
                        "pos":      _pos,
                        "live_p":   _lp,
                        "prev_c":   _prev_c,
                        "pct_chg":  _q.get("pct", 0.0),     # today % vs prev close
                        "pnl_dol":  _pnl_dol,
                        "day_move": _day_move,
                    }

                # ── Fold connected-broker holdings into the live totals ────────
                for _bh in _broker_holds:
                    _q  = _lq.get(_bh["ticker"], {})
                    _qy = _bh.get("qty", 0); _ac = _bh.get("avg_cost", 0)
                    _lp = _q.get("price") or (_bh.get("mkt_value", 0) / _qy if _qy else 0)
                    _total_value += _lp * _qy
                    _total_cost  += _ac * _qy
                    _total_pnl   += _bh.get("pnl", (_lp - _ac) * _qy)
                    _day_pnl     += (_lp - (_q.get("prev_close") or _lp)) * _qy

                # ── Summary bar ───────────────────────────────────────────────
                _ts = datetime.now().strftime("%H:%M:%S")
                _pnl_col  = "var(--pos)" if _total_pnl  >= 0 else "var(--neg)"
                _day_col  = "var(--pos)" if _day_pnl    >= 0 else "var(--neg)"

                # Live heartbeat dot + timestamp
                st.markdown(
                    f'<div style="display:flex;justify-content:flex-end;align-items:center;'
                    f'gap:6px;margin-bottom:6px">'
                    f'<span style="width:7px;height:7px;border-radius:50%;background:var(--pos);'
                    f'display:inline-block;animation:pulse 2s infinite"></span>'
                    f'<span style="color:var(--dim);font-size:.73rem">Live · {_ts}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                sp1, sp2, sp3, sp4, sp5 = st.columns(5)
                sp1.metric("Positions",        len(pos_snapshot) + len(_broker_holds))
                sp2.metric("Unrealized P&L",   f"${_total_pnl:+,.2f}",
                           delta=f"{_total_pnl/_total_cost*100:+.2f}%" if _total_cost else None)
                sp3.metric("Today's Move",     f"${_day_pnl:+,.2f}",
                           delta=f"{_day_pnl/_total_cost*100:+.2f}%" if _total_cost else None)
                sp4.metric("Market Value",     f"${_total_value:,.2f}")
                sp5.metric("Cost Basis",       f"${_total_cost:,.2f}")
                st.markdown("---")

                # ── Fidelity-style holdings table (sortable · tracked + brokers) ──
                st.markdown('<p class="section-label">Holdings — click a column to sort</p>', unsafe_allow_html=True)
                _th = st.session_state.get("theme", "dark")
                _g  = "#00c805" if _th == "dark" else "#00a406"
                _r  = "#ff5000" if _th == "dark" else "#e5352b"
                _rows = []
                for _tk, _d in _pos_data.items():
                    _p = _d["pos"]; _lp = _d["live_p"]; _qy = _p.get("qty", 1)
                    _rows.append({"Account": "Tracked", "Symbol": _tk, "Shares": _qy, "Last": _lp,
                                  "Today %": _d["pct_chg"], "Avg Cost": _p["entry"],
                                  "Mkt Value": _lp * _qy, "Gain/Loss": _d["pnl_dol"],
                                  "G/L %": (_lp - _p["entry"]) / _p["entry"] * 100 if _p["entry"] else 0})
                for _bh in st.session_state.get("broker_holdings", []):
                    _q  = _lq.get(_bh["ticker"], {})
                    _qy = _bh.get("qty", 0) or 1
                    _last = _q.get("price") or (_bh.get("mkt_value", 0) / _qy if _qy else 0)
                    _ac = _bh.get("avg_cost", 0)
                    _rows.append({"Account": _bh.get("account", "Broker"), "Symbol": _bh["ticker"],
                                  "Shares": _bh.get("qty", 0), "Last": _last, "Today %": _q.get("pct", 0.0),
                                  "Avg Cost": _ac, "Mkt Value": _bh.get("mkt_value") or _last * _qy,
                                  "Gain/Loss": _bh.get("pnl", 0),
                                  "G/L %": (_bh.get("pnl", 0) / (_ac * _qy) * 100) if (_ac and _qy) else 0})
                if _rows:
                    # Simple per-account subtotals (market value + P&L)
                    _by_acct = {}
                    for _rr in _rows:
                        _av = _by_acct.setdefault(_rr["Account"], {"mv": 0.0, "gl": 0.0})
                        _av["mv"] += _rr["Mkt Value"]; _av["gl"] += _rr["Gain/Loss"]
                    if len(_by_acct) > 1:
                        _chips = " ".join(
                            f'<span class="pill"><b>{_a}</b> ${_v["mv"]:,.0f} '
                            f'<span style="color:{"var(--pos)" if _v["gl"] >= 0 else "var(--neg)"}">'
                            f'{_v["gl"]:+,.0f}</span></span>'
                            for _a, _v in _by_acct.items())
                        st.markdown(f'<div style="margin:0 0 10px">{_chips}</div>', unsafe_allow_html=True)
                    _hdf = pd.DataFrame(_rows)
                    _sty = (_hdf.style
                            .format({"Last": "${:,.2f}", "Avg Cost": "${:,.2f}", "Mkt Value": "${:,.2f}",
                                     "Gain/Loss": "${:+,.2f}", "Today %": "{:+.2f}%", "G/L %": "{:+.1f}%"})
                            .map(lambda v: f"color:{_g if (isinstance(v,(int,float)) and v >= 0) else _r}",
                                 subset=["Today %", "Gain/Loss", "G/L %"]))
                    st.dataframe(_sty, use_container_width=True, hide_index=True)
                st.markdown("---")

                # ── Per-position cards (manage: stop/target/deep dive) ────────
                st.markdown('<p class="section-label">Manage positions</p>', unsafe_allow_html=True)
                for _tk, _d in _pos_data.items():
                    _pos     = _d["pos"]
                    _lp      = _d["live_p"]
                    _pnl_dol = _d["pnl_dol"]
                    _pct_chg = _d["pct_chg"]       # today's % vs prev close
                    _pnl_pct = round((_lp - _pos["entry"]) / _pos["entry"] * 100, 2)
                    _lv_col  = "var(--pos)" if _pct_chg >= 0 else "var(--neg)"
                    _lv_icon = "▲" if _pct_chg >= 0 else "▼"
                    _pnl_col2= "var(--pos)" if _pnl_pct >= 0 else "var(--neg)"
                    _stop_d  = round((_lp - _pos["stop"])    / _lp * 100, 1) if _lp > 0 else 0
                    _t1_d    = round((_pos["target1"] - _lp) / _lp * 100, 1) if _lp > 0 else 0
                    _t2_d    = round((_pos["target2"] - _lp) / _lp * 100, 1) if _lp > 0 else 0
                    _co      = cached_company_info(_tk)

                    # Cached slow signals (from outside the fragment)
                    _s = cached_position_check(_tk, _pos["entry"], _pos["stop"],
                                               _pos["target1"], _pos["date_in"], _pos["qty"])
                    _chart = st.session_state.get(f"ca_{_tk}", {})
                    _action, _a_color, _reason = _merged_action(
                        _s if _s else PositionStatus(
                            _tk, _lp, _pos["entry"], _pnl_pct, _pnl_dol,
                            "HOLD", "var(--pos)", "Monitoring..."
                        ),
                        _chart,
                    )

                    # Target auto-raise check
                    _target_updated = False
                    _atr_v = 0.0
                    if _s and _s.indicators:
                        try:
                            _atr_v = float(str(_s.indicators.get("Daily ATR","$0")).replace("$","").strip())
                        except Exception:
                            pass
                    if _atr_v > 0 and _s and _s.pnl_pct > 5:
                        _new_t1 = round(_s.price + _atr_v * 3.0, 2)
                        _target_updated = _new_t1 > _pos["target1"] * 1.02

                    with st.expander(
                        f"{_tk}  ·  ${_lp:.2f} {_lv_icon}{abs(_pct_chg):.2f}%  ·  "
                        f"P&L {_pnl_pct:+.2f}% (${_pnl_dol:+,.0f})  ·  {_action}",
                        expanded=(_action in ("EXIT NOW", "TAKE PROFIT", "TIGHTEN STOP"))
                    ):
                        # Company header
                        _co_desc2 = _co.get("description", "")
                        st.markdown(
                            f'<div class="company-meta" style="margin-bottom:10px">'
                            f'<b style="color:var(--muted);font-size:.95rem">{_co.get("name",_tk)}</b>'
                            f'{sector_badge(_co.get("sector","") or _co.get("industry",""))}'
                            f'{f"<br>{_co_desc2}" if _co_desc2 else ""}'
                            f'</div>',
                            unsafe_allow_html=True,
                        )

                        # ── Live metrics row ──────────────────────────────────
                        pm1,pm2,pm3,pm4,pm5,pm6 = st.columns(6)
                        pm1.metric("Entry",    f"${_pos['entry']:.2f}")
                        pm2.metric("Live Price",
                                   f"${_lp:.2f}",
                                   f"{_pct_chg:+.2f}% today")
                        pm3.metric("Unrealized P&L",
                                   f"${_pnl_dol:+,.2f}",
                                   f"{_pnl_pct:+.2f}%")
                        pm4.metric("Stop",
                                   f"${_pos['stop']:.2f}",
                                   f"{_stop_d:+.1f}% away" if _lp > _pos["stop"] else "⚠️ HIT")
                        pm5.metric("Target 1",
                                   f"${_pos['target1']:.2f}",
                                   f"{_t1_d:+.1f}% to go" if _lp < _pos["target1"] else "✅ HIT")
                        pm6.metric("Shares", _pos["qty"])

                        # Optional Target 2 distance row
                        if _pos.get("target2") and _pos["target2"] != _pos["target1"]:
                            st.markdown(
                                f'<p style="color:var(--faint);font-size:.75rem;margin:2px 0 8px">'
                                f'Target 2: <b style="color:var(--muted)">${_pos["target2"]:.2f}</b> '
                                f'({_t2_d:+.1f}% to go)</p>',
                                unsafe_allow_html=True,
                            )

                        # Status card
                        st.markdown(
                            f'<div class="card" style="border-left-color:{_a_color};'
                            f'background:{_a_color}0a;margin-top:8px">'
                            f'{action_badge(_action, _a_color)}&nbsp;&nbsp;'
                            f'<span style="color:var(--muted)">{_reason}</span>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )

                        # Indicator pills
                        if _s and _s.indicators:
                            pills2_ = "".join(
                                f'<span class="pill">{k}: <b>{v}</b></span>'
                                for k, v in _s.indicators.items()
                            )
                            st.markdown(pills2_, unsafe_allow_html=True)

                        # Auto-update notices
                        if _s and _s.suggested_stop and _s.suggested_stop > _pos["stop"] * 1.01:
                            st.info(f"🔄 Stop pending auto-tighten: **${_pos['stop']:.2f} → ${_s.suggested_stop:.2f}**")
                        if _target_updated:
                            st.success(f"🎯 Targets auto-raised to T1 **${_pos['target1']:.2f}** · T2 **${_pos['target2']:.2f}**")

                        # Close controls
                        st.markdown("<br>", unsafe_allow_html=True)
                        _cl1, _cl2 = st.columns([2, 1])
                        _exit_p = _cl1.number_input("Close at $", value=float(_lp),
                                                     format="%.2f", key=f"cls_{_tk}")
                        if _cl2.button(f"Close {_tk}", key=f"clb_{_tk}",
                                       type="primary" if _action == "EXIT NOW" else "secondary"):
                            close_position(_tk, _exit_p, _reason[:80])
                            _refresh()
                        if _pos.get("notes"):
                            st.caption(f"📝 {_pos['notes']}")

                        # Chart analysis section
                        st.markdown(
                            "<hr style='border:none;border-top:1px solid rgba(255,255,255,.05);margin:14px 0'>",
                            unsafe_allow_html=True,
                        )
                        _ra1, _ra2 = st.columns([5, 1])
                        _ra1.markdown(
                            '<div style="color:var(--accent);font-size:.78rem;font-weight:700;'
                            'text-transform:uppercase;letter-spacing:.5px;padding-top:6px">'
                            '📊 Live Chart Analysis</div>',
                            unsafe_allow_html=True,
                        )
                        if _ra2.button("🔄 Refresh", key=f"ca_ref_{_tk}", use_container_width=True):
                            try:
                                _rdh = (date.today() - date.fromisoformat(_pos.get("date_in", str(date.today())))).days
                            except Exception:
                                _rdh = 0
                            st.session_state[f"ca_{_tk}"] = chart_analyze(_tk, position={
                                "entry":   _pos["entry"], "stop":    _pos["stop"],
                                "target1": _pos.get("target1", 0),
                                "target2": _pos.get("target2", 0),
                                "qty":     _pos.get("qty", 1), "days_held": _rdh,
                            })
                            st.rerun()
                        _show_ai_result(st.session_state.get(f"ca_{_tk}", {}))

                        # ── Deep Dive ─────────────────────────────────────────
                        st.markdown(
                            "<hr style='border:none;border-top:1px solid rgba(255,255,255,.05);margin:14px 0'>",
                            unsafe_allow_html=True,
                        )
                        _pd_key  = f"port_dive_{_tk}"
                        _pd_open = st.session_state.get(_pd_key, False)
                        _pd_lbl  = "🔍 Hide Deep Dive" if _pd_open else "📊 Deep Dive — AI Position Advice · News · Fundamentals"
                        if st.button(_pd_lbl, key=f"port_dd_{_tk}", use_container_width=True):
                            st.session_state[_pd_key] = not _pd_open
                            if _pd_open:
                                st.session_state.pop(f"port_ai_{_tk}", None)
                                st.session_state.pop(f"port_fund_{_tk}", None)
                            st.rerun()

                        if st.session_state.get(_pd_key, False):
                            # Load fundamentals once per session
                            _pf_key = f"port_fund_{_tk}"
                            if _pf_key not in st.session_state:
                                try:
                                    from fundamentals import get_fundamental_snapshot as _gfs3
                                    st.session_state[_pf_key] = _gfs3(_tk)
                                except Exception:
                                    st.session_state[_pf_key] = {}
                            _pd_fund = st.session_state[_pf_key]
                            try:
                                _pd_dh = (date.today() - date.fromisoformat(_pos.get("date_in", str(date.today())))).days
                            except Exception:
                                _pd_dh = 0
                            _pd_pos_ctx = {
                                "entry": _pos["entry"], "stop": _pos["stop"],
                                "target1": _pos.get("target1", 0), "target2": _pos.get("target2", 0),
                                "qty": _pos.get("qty", 1), "days_held": _pd_dh,
                            }
                            _deep_dive_panel(
                                _tk, _pd_fund, _co, _lp,
                                _pd_pos_ctx, f"port_ai_{_tk}", ai_type="position"
                            )

            # ── Call the live fragment ────────────────────────────────────────
            _live_portfolio(dict(positions))

    # ── OPTIONS ───────────────────────────────────────────────────────────────
    with tab_opt:
        options       = get_options()
        closed_opts   = get_closed_options()

        with st.expander("➕ Add New Option Position", expanded=(len(options) == 0)):
            with st.form("add_opt", clear_on_submit=True):
                oc1, oc2, oc3 = st.columns(3)
                und_    = oc1.text_input("Underlying (AAPL)").upper().strip()
                otype_  = oc2.selectbox("Type", ["call","put"])
                strike_ = oc3.number_input("Strike $", min_value=0.01, format="%.2f")
                expiry_ = st.text_input("Expiry (YYYY-MM-DD)", placeholder="2026-06-20")
                op1,op2,op3,op4 = st.columns(4)
                ent_p_  = op1.number_input("Entry premium $", min_value=0.01, format="%.2f")
                stp_p_  = op2.number_input("Stop $",          min_value=0.01, format="%.2f")
                tgt_p_  = op3.number_input("Target $",        min_value=0.01, format="%.2f")
                n_c_    = op4.number_input("Contracts",        min_value=1, step=1)
                notes_  = st.text_input("Notes (optional)")
                if st.form_submit_button("Save Option", type="primary"):
                    if und_ and strike_ and expiry_ and ent_p_ and stp_p_ and tgt_p_:
                        add_option(und_, otype_, strike_, expiry_, int(n_c_), ent_p_, stp_p_, tgt_p_, notes_)
                        st.success(f"✅ {und_} ${strike_:.0f} {otype_.upper()} saved!")
                        _refresh()
                    else:
                        st.error("Fill in all required fields")

        if not options:
            st.info("No open option positions.")
        else:
            st.markdown(f"**{len(options)} open contract{'s' if len(options)>1 else ''}**")
            for oid, opt in options.items():
                price_ = get_option_price(opt["underlying"], opt["opt_type"],
                                          opt["strike"], opt["expiry"])
                dte_   = days_to_expiry(opt["expiry"])

                if price_ is None:
                    ostatus, oc_ = "PRICE UNAVAILABLE", "var(--faint)"
                    omsg = "Cannot fetch current price. Market may be closed."
                elif price_ <= opt["stop_price"]:
                    ostatus, oc_ = "EXIT NOW — STOP HIT", "var(--neg)"
                    omsg = f"Premium hit ${price_:.2f} — below stop of ${opt['stop_price']:.2f}. Exit to limit loss."
                elif price_ >= opt["target_price"]:
                    ostatus, oc_ = "TAKE PROFIT — TARGET HIT", "#f59e0b"
                    omsg = f"Premium hit ${price_:.2f} — your target of ${opt['target_price']:.2f} was reached. Consider selling."
                elif dte_ <= 5:
                    ostatus, oc_ = f"EXPIRING IN {dte_}d", "#f59e0b"
                    omsg = f"Only {dte_} day{'s' if dte_!=1 else ''} left. Close, roll, or let expire."
                else:
                    ostatus, oc_ = "OPEN", "var(--pos)"
                    omsg = "Monitoring. You'll get a Telegram alert when stop or target is hit."

                opnl_str, opnl_clr = "—", "var(--faint)"
                if price_:
                    opct_ = round((price_ - opt["entry_price"]) / opt["entry_price"] * 100, 1)
                    odol_ = round((price_ - opt["entry_price"]) * 100 * opt["contracts"], 2)
                    opnl_str = f"{opct_:+.1f}% (${odol_:+,.2f})"
                    opnl_clr = "var(--pos)" if opct_ >= 0 else "var(--neg)"

                with st.expander(
                    f"{opt['underlying']} ${opt['strike']:.0f} {opt['opt_type'].upper()} · {opt['expiry']} · {ostatus}",
                    expanded=(ostatus not in ("OPEN","PRICE UNAVAILABLE"))
                ):
                    om1,om2,om3,om4,om5 = st.columns(5)
                    om1.metric("Current", f"${price_:.2f}" if price_ else "—")
                    om2.metric("Entry",   f"${opt['entry_price']:.2f}")
                    om3.metric("Stop",    f"${opt['stop_price']:.2f}")
                    om4.metric("Target",  f"${opt['target_price']:.2f}")
                    om5.metric("DTE",     str(dte_))
                    st.markdown(
                        f"**P&L:** <span style='color:{opnl_clr};font-size:1.1rem;font-weight:700'>{opnl_str}</span>",
                        unsafe_allow_html=True
                    )
                    st.markdown(f"""
                    <div class="card" style="border-left-color:{oc_};background:{oc_}0a;margin:10px 0">
                        {action_badge(ostatus, oc_)}&nbsp;&nbsp;
                        <span style="color:var(--muted)">{omsg}</span>
                    </div>
                    """, unsafe_allow_html=True)

                    flags_ = []
                    if opt.get("alerted_stop"):   flags_.append("🔔 Stop alert sent")
                    if opt.get("alerted_target"): flags_.append("🔔 Target alert sent")
                    if opt.get("alerted_expiry"): flags_.append("🔔 Expiry alert sent")
                    if flags_:
                        st.caption("  ·  ".join(flags_))
                    if opt.get("notes"):
                        st.caption(f"📝 {opt['notes']}")

                    oc1_, oc2_ = st.columns([2, 1])
                    close_p_ = oc1_.number_input("Close at $",
                                                  value=float(price_ or opt["entry_price"]),
                                                  format="%.2f", key=f"oc_{oid}")
                    if oc2_.button("Close Position", key=f"ocb_{oid}",
                                   type="primary" if "EXIT" in ostatus or "PROFIT" in ostatus else "secondary"):
                        close_option(oid, close_p_, omsg[:80])
                        st.success("✅ Closed.")
                        _refresh()

        if closed_opts:
            st.markdown("---")
            st.markdown("**Closed Options**")
            for t in reversed(closed_opts):
                opct = t.get("pnl_pct", 0)
                odol = t.get("pnl_total", 0)
                oclr = "var(--pos)" if opct >= 0 else "var(--neg)"
                st.markdown(f"""
                <div class="card" style="border-left-color:{oclr};background:{oclr}0a">
                    <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px">
                        <div>{"✅" if opct>=0 else "❌"} &nbsp;<b>{t["underlying"]} ${t["strike"]:.0f} {t["opt_type"].upper()}</b>
                        &nbsp;·&nbsp; exp {t["expiry"]} &nbsp;·&nbsp; {t["contracts"]} contract{"s" if t["contracts"]>1 else ""}
                        &nbsp;&nbsp;<span style="color:{oclr};font-weight:700">{opct:+.1f}% (${odol:+,.2f})</span></div>
                        <div style="color:var(--dim);font-size:.78rem">
                            ${t["entry_price"]:.2f} → ${t.get("exit_price",0):.2f}
                            &nbsp;·&nbsp; {t.get("date_in","?")} → {t.get("date_out","?")}
                        </div>
                    </div>
                </div>
                """, unsafe_allow_html=True)


    # ── HISTORY ───────────────────────────────────────────────────────────────
    with tab_hist:
        _hist_closed = get_closed()
        if not _hist_closed:
            st.info("No closed trades yet. Your history will appear here after you close your first position.")
        else:
            wins__   = [t for t in _hist_closed if t.get("pnl_pct", 0) > 0]
            losses__ = [t for t in _hist_closed if t.get("pnl_pct", 0) <= 0]
            total__  = sum(t.get("pnl_dollars", 0) for t in _hist_closed)
            avg_w__  = np.mean([t["pnl_pct"] for t in wins__])   if wins__   else 0
            avg_l__  = np.mean([t["pnl_pct"] for t in losses__]) if losses__ else 0
            wr__     = len(wins__) / len(_hist_closed) * 100 if _hist_closed else 0

            hm1, hm2, hm3, hm4, hm5 = st.columns(5)
            hm1.metric("Total P&L",  f"${total__:+,.2f}")
            hm2.metric("Win Rate",   f"{wr__:.0f}%")
            hm3.metric("Win / Loss", f"{len(wins__)} / {len(losses__)}")
            hm4.metric("Avg Win",    f"+{avg_w__:.2f}%")
            hm5.metric("Avg Loss",   f"{avg_l__:.2f}%")

            if len(_hist_closed) > 1:
                cum__ = pd.Series([t.get("pnl_dollars", 0) for t in _hist_closed]).cumsum()
                _th = st.session_state.get("theme", "dark")
                _up = float(cum__.iloc[-1]) >= 0
                _pc = ("#00a406" if _up else "#e5352b") if _th == "light" else ("#00c805" if _up else "#ff5000")
                _fl = (("rgba(0,164,6,.08)" if _up else "rgba(229,53,43,.08)") if _th == "light"
                       else ("rgba(0,200,5,.10)" if _up else "rgba(255,80,0,.10)"))
                fig_h = go.Figure(go.Scatter(y=cum__, mode="lines", line=dict(color=_pc, width=2.5),
                    fill="tozeroy", fillcolor=_fl, hovertemplate="$%{y:,.0f}<extra></extra>"))
                fig_h.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color="#79828c" if _th == "light" else "#9ca3a8", size=11), height=220,
                    margin=dict(l=0, r=10, t=10, b=0), showlegend=False, hovermode="x",
                    xaxis=dict(showgrid=False, zeroline=False, title="Trade #"),
                    yaxis=dict(showgrid=False, zeroline=False, side="right", tickprefix="$"))
                st.plotly_chart(fig_h, use_container_width=True, config={"displayModeBar": False})

            st.markdown("---")
            for t in reversed(_hist_closed):
                pct__ = t.get("pnl_pct", 0)
                dol__ = t.get("pnl_dollars", 0)
                clr__ = "var(--pos)" if pct__ >= 0 else "var(--neg)"
                ico__ = "✅" if pct__ >= 0 else "❌"
                st.markdown(f"""
                <div class="card" style="border-left-color:{clr__};background:{clr__}0a">
                    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
                        <div>
                            <span style="font-size:1.1rem">{ico__}</span>&nbsp;
                            <span class="ticker-big">{t["ticker"]}</span>&nbsp;
                            <span style="color:{clr__};font-size:1.05rem;font-weight:700">
                                {pct__:+.2f}% &nbsp;·&nbsp; ${dol__:+.2f}
                            </span>
                        </div>
                        <div style="color:var(--dim);font-size:.78rem;text-align:right">
                            ${t["entry"]:.2f} → ${t.get("exit_price",0):.2f}
                            &nbsp;·&nbsp; {t.get("date_in","?")} → {t.get("date_out","?")}
                            &nbsp;·&nbsp; {t.get("qty",0)} shares
                        </div>
                    </div>
                    <div style="color:var(--dim);font-size:.78rem;margin-top:5px">{t.get("exit_reason","—")}</div>
                </div>
                """, unsafe_allow_html=True)

    # ── PERFORMANCE ───────────────────────────────────────────────────────────
    with tab_perf:
        from performance import compute_stats, get_lessons
        _perf_closed = get_closed()

        if not _perf_closed:
            st.info("No closed trades yet. Close your first position to start seeing performance data.")
        else:
            stats = compute_stats(_perf_closed)

            _wr_col  = "var(--pos)" if stats["win_rate"] >= 55 else "#f59e0b" if stats["win_rate"] >= 45 else "var(--neg)"
            _pf_col  = "var(--pos)" if stats["profit_factor"] >= 1.5 else "#f59e0b" if stats["profit_factor"] >= 1.0 else "var(--neg)"
            _pnl_col = "var(--pos)" if stats["total_dollars"] >= 0 else "var(--neg)"
            _stk_col = "var(--pos)" if stats["streak_type"] == "win" else "var(--neg)"
            _exp_col = "var(--pos)" if stats["expectancy"] >= 0 else "var(--neg)"

            k1, k2, k3, k4, k5 = st.columns(5)
            k1.markdown(_kpi_card("WIN RATE",      f"{stats['win_rate']}%",            _wr_col,  f"{stats['win_count']}W / {stats['loss_count']}L"), unsafe_allow_html=True)
            k2.markdown(_kpi_card("PROFIT FACTOR", f"{stats['profit_factor']}×",       _pf_col,  "wins ÷ losses $"), unsafe_allow_html=True)
            k3.markdown(_kpi_card("TOTAL P&L",     f"${stats['total_dollars']:+,.0f}", _pnl_col, f"{stats['total']} trades"), unsafe_allow_html=True)
            k4.markdown(_kpi_card("EXPECTANCY",    f"${stats['expectancy']:+.0f}",     _exp_col, "avg $ per trade"), unsafe_allow_html=True)
            k5.markdown(_kpi_card("STREAK",        f"{stats['streak']} {stats['streak_type'].upper()}S", _stk_col, f"avg hold {stats['avg_hold_days']}d"), unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)

            if stats["equity_curve"]:
                eq_df = pd.DataFrame(stats["equity_curve"])
                st.markdown('<p style="color:var(--muted);font-size:.78rem;font-weight:700;letter-spacing:.08em;margin:0 0 6px">EQUITY CURVE — cumulative P&L ($)</p>', unsafe_allow_html=True)
                _th = st.session_state.get("theme", "dark")
                _eq = eq_df.set_index("date")["equity"]
                _up = float(_eq.iloc[-1]) >= float(_eq.iloc[0])
                _pc = ("#00a406" if _up else "#e5352b") if _th == "light" else ("#00c805" if _up else "#ff5000")
                _fl = (("rgba(0,164,6,.08)" if _up else "rgba(229,53,43,.08)") if _th == "light"
                       else ("rgba(0,200,5,.10)" if _up else "rgba(255,80,0,.10)"))
                _eqfig = go.Figure(go.Scatter(x=_eq.index, y=_eq.values, mode="lines",
                    line=dict(color=_pc, width=2), fill="tozeroy", fillcolor=_fl,
                    hovertemplate="$%{y:,.0f}<extra></extra>"))
                _eqfig.update_layout(height=200, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color="#79828c" if _th == "light" else "#9ca3a8", size=11),
                    margin=dict(l=0, r=10, t=6, b=0), showlegend=False, hovermode="x",
                    xaxis=dict(showgrid=False, zeroline=False),
                    yaxis=dict(showgrid=False, zeroline=False, side="right", tickprefix="$"))
                st.plotly_chart(_eqfig, use_container_width=True, config={"displayModeBar": False})

            lessons = get_lessons(_perf_closed)
            if lessons:
                st.markdown("<br>", unsafe_allow_html=True)
                st.markdown('<p style="color:#f59e0b;font-size:.78rem;font-weight:700;letter-spacing:.08em;margin:0 0 10px">⚠️ PATTERNS TO WATCH</p>', unsafe_allow_html=True)
                for lesson in lessons:
                    st.markdown(
                        f'<div style="background:rgba(245,158,11,.07);border-left:3px solid #f59e0b;'
                        f'border-radius:6px;padding:10px 16px;margin-bottom:8px;color:#fcd34d;font-size:.85rem">'
                        f'{lesson}</div>',
                        unsafe_allow_html=True,
                    )

            st.markdown("<br>", unsafe_allow_html=True)
            bt1, bt2, bt3, bt4 = st.tabs(["🔧 By Setup", "🌐 By Sector", "⭐ By Quality", "📋 Trade Log"])

            with bt1:
                sd = stats.get("setup_breakdown", {})
                if not sd:
                    st.info("No data yet.")
                else:
                    st.markdown("<br>", unsafe_allow_html=True)
                    for stype, v in sorted(sd.items(), key=lambda x: x[1]["win_rate"], reverse=True):
                        _bar_col = "var(--pos)" if v["win_rate"] >= 55 else "#f59e0b" if v["win_rate"] >= 45 else "var(--neg)"
                        _icon = {"swing": "📅", "day": "⚡", "vcp": "🔭", "manual": "✋"}.get(stype, "📌")
                        st.markdown(
                            f'<div style="background:var(--surface);border:1px solid var(--border);'
                            f'border-radius:10px;padding:14px 18px;margin-bottom:10px">'
                            f'<div style="display:flex;justify-content:space-between;align-items:center">'
                            f'<span style="color:var(--fg);font-weight:700">{_icon} {stype.upper()}</span>'
                            f'<span style="color:{_bar_col};font-size:1.1rem;font-weight:800">{v["win_rate"]}% WR</span>'
                            f'</div>'
                            f'<div style="color:var(--faint);font-size:.78rem;margin-top:6px">'
                            f'{v["total"]} trades · {v["wins"]}W / {v["total"]-v["wins"]}L · avg {v["avg_pnl"]:+.1f}% · total ${v["total_$"]:+,.0f}'
                            f'</div>'
                            f'<div style="margin-top:8px;background:var(--surface);border-radius:4px;height:6px">'
                            f'<div style="background:{_bar_col};width:{v["win_rate"]}%;height:6px;border-radius:4px"></div>'
                            f'</div></div>',
                            unsafe_allow_html=True,
                        )

            with bt2:
                sec = stats.get("sector_breakdown", {})
                if not sec:
                    st.info("No data yet.")
                else:
                    st.markdown("<br>", unsafe_allow_html=True)
                    for sector_name, v in sorted(sec.items(), key=lambda x: x[1]["total_$"], reverse=True):
                        if v["total"] < 1:
                            continue
                        _bar_col = "var(--pos)" if v["total_$"] >= 0 else "var(--neg)"
                        _wr_col2 = "var(--pos)" if v["win_rate"] >= 55 else "#f59e0b" if v["win_rate"] >= 45 else "var(--neg)"
                        st.markdown(
                            f'<div style="background:var(--surface);border:1px solid var(--border);'
                            f'border-radius:10px;padding:12px 18px;margin-bottom:8px">'
                            f'<div style="display:flex;justify-content:space-between;align-items:center">'
                            f'<span style="color:var(--fg);font-weight:600">{sector_name}</span>'
                            f'<span style="color:{_bar_col};font-weight:700">${v["total_$"]:+,.0f}</span>'
                            f'</div>'
                            f'<div style="color:var(--faint);font-size:.78rem;margin-top:4px">'
                            f'{v["total"]} trades · <span style="color:{_wr_col2}">{v["win_rate"]}% WR</span>'
                            f'</div></div>',
                            unsafe_allow_html=True,
                        )

            with bt3:
                qb = stats.get("quality_breakdown", {})
                rb = stats.get("rs_breakdown", {})
                q1, q2 = st.columns(2)
                with q1:
                    st.markdown('<p style="color:var(--muted);font-size:.72rem;font-weight:700;letter-spacing:.08em;margin:10px 0 8px">BY STARS</p>', unsafe_allow_html=True)
                    if not qb:
                        st.info("No data.")
                    else:
                        for stars_val in sorted(qb.keys(), reverse=True):
                            v = qb[stars_val]
                            _s_col = "#fbbf24" if stars_val == 3 else "var(--muted)" if stars_val == 2 else "#b45309"
                            _w_col = "var(--pos)" if v["win_rate"] >= 55 else "#f59e0b" if v["win_rate"] >= 45 else "var(--neg)"
                            st.markdown(
                                f'<div style="background:var(--surface);border-radius:8px;padding:10px 14px;margin-bottom:6px">'
                                f'<span style="color:{_s_col}">{"⭐"*stars_val if stars_val else "—"}</span> &nbsp;'
                                f'<span style="color:{_w_col};font-weight:700">{v["win_rate"]}% WR</span> &nbsp;'
                                f'<span style="color:var(--faint);font-size:.78rem">{v["total"]} trades · ${v["total_$"]:+,.0f}</span>'
                                f'</div>',
                                unsafe_allow_html=True,
                            )
                with q2:
                    st.markdown('<p style="color:var(--muted);font-size:.72rem;font-weight:700;letter-spacing:.08em;margin:10px 0 8px">BY RS RANK</p>', unsafe_allow_html=True)
                    if not rb:
                        st.info("No data.")
                    else:
                        for tier in ["Elite (80+)", "Strong (60-79)", "Weak (<60)"]:
                            if tier not in rb:
                                continue
                            v = rb[tier]
                            _t_col = "var(--pos)" if tier == "Elite (80+)" else "#f59e0b" if tier == "Strong (60-79)" else "var(--faint)"
                            _w_col = "var(--pos)" if v["win_rate"] >= 55 else "#f59e0b" if v["win_rate"] >= 45 else "var(--neg)"
                            st.markdown(
                                f'<div style="background:var(--surface);border-radius:8px;padding:10px 14px;margin-bottom:6px">'
                                f'<span style="color:{_t_col};font-weight:700">{tier}</span> &nbsp;'
                                f'<span style="color:{_w_col};font-weight:700">{v["win_rate"]}% WR</span> &nbsp;'
                                f'<span style="color:var(--faint);font-size:.78rem">{v["total"]} trades · avg {v["avg_pnl"]:+.1f}%</span>'
                                f'</div>',
                                unsafe_allow_html=True,
                            )

            with bt4:
                st.markdown("<br>", unsafe_allow_html=True)
                all_trades = stats.get("trades", [])
                if not all_trades:
                    st.info("No trades.")
                else:
                    _b5 = stats.get("best5", [])
                    _w5 = stats.get("worst5", [])
                    _ba1, _ba2 = st.columns(2)
                    with _ba1:
                        st.markdown('<p style="color:var(--pos);font-size:.72rem;font-weight:700;letter-spacing:.08em;margin:0 0 6px">🏆 BEST TRADES</p>', unsafe_allow_html=True)
                        for tr in _b5:
                            st.markdown(
                                f'<div style="font-size:.82rem;padding:4px 0;border-bottom:1px solid rgba(255,255,255,.05)">'
                                f'<b style="color:var(--fg)">{tr["ticker"]}</b>'
                                f' <span style="color:var(--pos)">{tr["pnl_pct"]:+.1f}%</span>'
                                f' <span style="color:var(--faint)">· ${tr["pnl_$"]:+.0f} · {tr["setup_type"]}</span>'
                                f'</div>', unsafe_allow_html=True,
                            )
                    with _ba2:
                        st.markdown('<p style="color:var(--neg);font-size:.72rem;font-weight:700;letter-spacing:.08em;margin:0 0 6px">💀 WORST TRADES</p>', unsafe_allow_html=True)
                        for tr in _w5:
                            st.markdown(
                                f'<div style="font-size:.82rem;padding:4px 0;border-bottom:1px solid rgba(255,255,255,.05)">'
                                f'<b style="color:var(--fg)">{tr["ticker"]}</b>'
                                f' <span style="color:var(--neg)">{tr["pnl_pct"]:+.1f}%</span>'
                                f' <span style="color:var(--faint)">· ${tr["pnl_$"]:+.0f} · {tr["setup_type"]}</span>'
                                f'</div>', unsafe_allow_html=True,
                            )
                    st.markdown("<br>", unsafe_allow_html=True)
                    _log_rows = []
                    for t in reversed(all_trades):
                        pct = float(t.get("pnl_pct", 0))
                        _log_rows.append({
                            "Date Out":    t.get("date_out", ""),
                            "Ticker":      t.get("ticker", ""),
                            "Setup":       (t.get("setup_type") or "manual").title(),
                            "Stars":       "⭐" * int(t.get("stars") or 0) if t.get("stars") else "—",
                            "Entry $":     f'${float(t.get("entry",0)):.2f}',
                            "Exit $":      f'${float(t.get("exit_price",0)):.2f}',
                            "P&L %":       f'{pct:+.2f}%',
                            "P&L $":       f'${float(t.get("pnl_dollars",0)):+.0f}',
                            "Sector":      t.get("sector") or "—",
                            "Exit Reason": t.get("exit_reason") or "—",
                        })
                    st.dataframe(pd.DataFrame(_log_rows), use_container_width=True, hide_index=True, height=400)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE — ANALYZE
# ══════════════════════════════════════════════════════════════════════════════
elif page == "🔎  Analyze":
    from market_data import search_symbols as _search_symbols
    live_market_bar()

    # ── Back to Watchlist button (shown when arriving from Watchlist) ──────────
    if st.session_state.get("_an_from_wl"):
        if st.button("← Back to Watchlist", key="an_back_wl"):
            st.session_state.pop("_an_from_wl", None)
            st.session_state["page"] = "👁️  Watchlist"
            st.rerun()

    _page_header("🔎 Analyze Any Stock",
                 "Any stock, anywhere — instant signal, chart, AI analysis and fundamentals")

    # ── Search bar ─────────────────────────────────────────────────────────────
    # Pull ticker set from IPO / scanner "Analyze" jump via session state
    _jump = st.session_state.pop("analyze_ticker", None) or st.session_state.pop("dd_ticker", None)
    if _jump is not None:
        st.session_state["analyze_search"] = _jump

    search_q = st.text_input(
        "", placeholder="🔍  Ticker or company name — NVDA · Apple · lithium ETF · Berkshire…",
        label_visibility="collapsed", key="analyze_search",
    ).strip()

    # ── Symbol search (fires on company name / multi-word input) ──────────────
    _raw = search_q
    _is_name = _raw and (" " in _raw or (len(_raw) > 5 and not _raw.isupper()))

    if _is_name:
        @st.cache_data(ttl=30, show_spinner=False)
        def _sym_search(q): return _search_symbols(q, limit=10)
        with st.spinner("Searching Finnhub…"):
            _sr = _sym_search(_raw)
        if _sr:
            _opts = {f"{r['symbol']}  —  {r['name']}": r["symbol"] for r in _sr}
            _pick = st.selectbox("Results:", ["— select one —"] + list(_opts.keys()),
                                 label_visibility="collapsed", key="an_sym_pick")
            if _pick != "— select one —":
                st.session_state["analyze_ticker"] = _opts[_pick]
                st.rerun()
        elif len(_raw) >= 3:
            st.caption("No matches — try a shorter keyword or type the ticker directly.")

    search_q = search_q.upper().strip()

    # Track recent searches (up to 8)
    if search_q:
        _rec: list = st.session_state.get("_an_recent", [])
        if search_q not in _rec:
            _rec.insert(0, search_q)
        st.session_state["_an_recent"] = _rec[:8]

    # ── EMPTY STATE ────────────────────────────────────────────────────────────
    if not search_q:
        _recent = st.session_state.get("_an_recent", [])

        # Recent searches
        if _recent:
            st.markdown(
                '<div class="section-label" style="margin-top:20px">Recent searches</div>',
                unsafe_allow_html=True,
            )
            _rc = st.columns(min(len(_recent), 8))
            for i, t in enumerate(_recent):
                if _rc[i].button(t, key=f"an_rec_{t}", use_container_width=True):
                    st.session_state["analyze_ticker"] = t
                    st.rerun()

        # Popular stocks by category
        _popular = {
            "🔥 Trending":  ["NVDA","PLTR","COIN","MSTR","TSLA"],
            "🏆 Mega Cap":  ["AAPL","MSFT","GOOGL","AMZN","META"],
            "📊 ETFs":      ["SPY","QQQ","IWM","GLD","TLT"],
            "✈️ High Beta": ["RKLB","ACHR","HOOD","SOFI","RIVN"],
        }
        st.markdown('<div class="section-label" style="margin-top:24px">Browse popular stocks</div>',
                    unsafe_allow_html=True)
        for cat, tickers in _popular.items():
            st.markdown(
                f'<div style="color:var(--faint);font-size:.75rem;font-weight:600;'
                f'margin:12px 0 6px">{cat}</div>',
                unsafe_allow_html=True,
            )
            _pc = st.columns(len(tickers))
            for i, t in enumerate(tickers):
                if _pc[i].button(t, key=f"an_pop_{cat}_{t}", use_container_width=True):
                    st.session_state["analyze_ticker"] = t
                    st.rerun()

        st.markdown(
            '<div style="text-align:center;padding:40px 20px 0;color:var(--dim);font-size:.8rem">'
            'Or type any ticker / company name in the search bar above<br>'
            'Finnhub covers 30,000+ US and global stocks'
            '</div>',
            unsafe_allow_html=True,
        )
        st.stop()

    # ── Load data ──────────────────────────────────────────────────────────────
    _an_key = f"an_{search_q}"
    if not _ssc_fresh(_an_key, 300):
        with st.spinner(f"Loading {search_q}…"):
            co_        = cached_company_info(search_q)
            df_an      = load_chart_df(search_q, "6mo")
            price_an, chg_pct_an, chg_dol_an = cached_price_change(search_q)
            regime_an  = load_regime()
            sig_an     = cached_score(search_q, regime_an["regime"])
        _ssc_set(_an_key, dict(co_=co_, df_an=df_an, price_an=price_an,
                               chg_pct_an=chg_pct_an, chg_dol_an=chg_dol_an,
                               regime_an=regime_an, sig_an=sig_an))
    else:
        _d         = _ssc_get(_an_key)
        co_        = _d["co_"]
        df_an      = _d["df_an"]
        price_an   = _d["price_an"]
        chg_pct_an = _d["chg_pct_an"]
        chg_dol_an = _d["chg_dol_an"]
        regime_an  = _d["regime_an"]
        sig_an     = _d["sig_an"]

    if price_an is None or df_an is None or df_an.empty:
        st.error(f"**{search_q}** — no data found. Check the ticker symbol and try again.")
        if st.button("← Back to search"):
            st.session_state["analyze_ticker"] = ""
            st.rerun()
    else:
        import html as _html_mod
        co_name_ = _html_mod.escape(co_.get("name", search_q))
        co_sec_  = co_.get("sector","") or co_.get("industry","")
        co_desc_ = _html_mod.escape((co_.get("description","") or "")[:200])
        chg_clr_ = "var(--pos)" if chg_pct_an >= 0 else "var(--neg)"
        chg_icon_= "▲" if chg_pct_an >= 0 else "▼"

        # ── Company header ─────────────────────────────────────────────────────
        _hc1, _hc2 = st.columns([5, 2])
        with _hc1:
            st.markdown(
                f'<div style="background:var(--surface);border:1px solid rgba(255,255,255,.06);'
                f'border-radius:12px;padding:18px 22px;margin-bottom:4px">'
                f'<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">'
                f'<span style="font-size:1.9rem;font-weight:900;color:var(--fg);letter-spacing:-1px">{search_q}</span>'
                f'{sector_badge(co_sec_)}'
                f'</div>'
                f'<div style="color:var(--faint);font-size:.9rem;font-weight:600;margin:4px 0 2px">{co_name_}</div>'
                + (f'<div style="color:var(--dim);font-size:.78rem;line-height:1.55">{co_desc_}…</div>' if co_desc_ else "")
                + f'</div>',
                unsafe_allow_html=True,
            )
        with _hc2:
            live_single_ticker_bar(search_q, price_an, chg_pct_an)

        # ── Tabs ───────────────────────────────────────────────────────────────
        st.markdown('<div style="margin-top:20px"></div>', unsafe_allow_html=True)
        _tab_ov, _tab_ch, _tab_ai, _tab_fund = st.tabs([
            "📈  Overview",
            "📊  Chart",
            "🤖  AI Analysis",
            "📋  Fundamentals",
        ])

        # ── Shared indicator values ────────────────────────────────────────────
        d_an   = df_an.iloc[-1]
        rsi_an = float(d_an.get("rsi",       0))
        adx_an = float(d_an.get("adx",       0))
        atr_an = float(d_an.get("atr",       0))
        vr_an  = float(d_an.get("vol_ratio", 1.0))
        sl_an  = ema200_slope(df_an)
        rsi_clr = "var(--neg)" if rsi_an > 70 else "#f59e0b" if rsi_an < 35 else "var(--pos)"
        adx_clr = "var(--pos)" if adx_an > 30 else "#f59e0b" if adx_an > 20 else "var(--faint)"
        sl_clr  = "var(--pos)" if sl_an == "positive" else "var(--neg)" if sl_an == "negative" else "#f59e0b"
        sl_lbl  = "↑ Rising" if sl_an == "positive" else "↓ Falling" if sl_an == "negative" else "→ Flat"

        # ══ TAB 1: OVERVIEW ═══════════════════════════════════════════════════
        with _tab_ov:
            # Indicator tiles
            st.markdown(
                f'<div style="display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:16px">'
                f'<div class="tile"><div class="tile-label">RSI (14)</div>'
                f'<div class="tile-value" style="color:{rsi_clr}">{rsi_an:.0f}</div>'
                f'<div class="tile-sub">{"Overbought" if rsi_an>70 else "Oversold" if rsi_an<35 else "Healthy"}</div></div>'
                f'<div class="tile"><div class="tile-label">ADX</div>'
                f'<div class="tile-value" style="color:{adx_clr}">{adx_an:.0f}</div>'
                f'<div class="tile-sub">{"Strong" if adx_an>30 else "Moderate" if adx_an>20 else "Weak"} trend</div></div>'
                f'<div class="tile"><div class="tile-label">Daily ATR</div>'
                f'<div class="tile-value">${atr_an:.2f}</div>'
                f'<div class="tile-sub">{round(atr_an/price_an*100,1) if price_an else 0}% avg move</div></div>'
                f'<div class="tile"><div class="tile-label">Volume</div>'
                f'<div class="tile-value">{vr_an:.1f}x</div>'
                f'<div class="tile-sub">vs 20-day avg</div></div>'
                f'<div class="tile"><div class="tile-label">200d Trend</div>'
                f'<div class="tile-value" style="font-size:1.1rem;color:{sl_clr}">{sl_lbl}</div>'
                f'<div class="tile-sub">Long-term</div></div>'
                f'</div>',
                unsafe_allow_html=True,
            )

            # Signal card
            if sig_an:
                _an_is_watch = sig_an.signal_type == "WATCH"
                _an_card_cls = "card-watch" if _an_is_watch else "card-buy"
                _an_entry    = float(sig_an.watch_buy_at) if _an_is_watch and sig_an.watch_buy_at else sig_an.price
                sc_an        = _star_color(sig_an.stars)

                st.markdown(f"""
                <div class="card {_an_card_cls}">
                    <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px">
                        <div>
                            <span style="color:{sc_an}">{"⭐"*sig_an.stars}</span>&nbsp;
                            <span style="font-weight:700;color:var(--fg);font-size:1.05rem">{sig_an.headline}</span>
                            {_sig_type_badge(sig_an)}
                        </div>
                        <div style="color:var(--faint);font-size:.83rem">
                            Risk {sig_an.stop_pct}% &nbsp;·&nbsp; Reward {sig_an.gain_pct}% &nbsp;·&nbsp; R:R {sig_an.rr}:1
                        </div>
                    </div>
                    <div class="thin-div"></div>
                    <p style="color:var(--muted);margin:0 0 12px;line-height:1.65">{sig_an.why_buy}</p>
                    <div>
                        <span class="pill">📥 Enter ~<b>${_an_entry:.2f}</b></span>
                        <span class="pill">🛑 Stop <b>${sig_an.stop:.2f}</b></span>
                        <span class="pill">🎯 T1 <b>${sig_an.target1:.2f}</b></span>
                        <span class="pill">🎯 T2 <b>${sig_an.target2:.2f}</b></span>
                        {f'<span class="pill" style="color:#fbbf24">📍 Wait for <b>${sig_an.watch_buy_at:.2f}</b></span>' if _an_is_watch and sig_an.watch_buy_at else ''}
                    </div>
                    <p style="color:var(--dim);font-size:.78rem;margin:8px 0 0">⚡ {sig_an.what_to_watch}</p>
                </div>
                """, unsafe_allow_html=True)

                _wb_an = _watch_banner_html(sig_an)
                if _wb_an:
                    st.markdown(_wb_an, unsafe_allow_html=True)
                for w in sig_an.warnings:
                    st.warning(w)

                # Actions
                _aa1, _aa2, _aa3, _aa4 = st.columns([2, 2, 1, 1])
                qty_an_ = _aa1.number_input("Shares", min_value=0.0, step=1.0, value=1.0, format="%.4f", key="an_qty")
                enp_an_ = _aa2.number_input("At $", value=_an_entry, format="%.2f", key="an_ep")
                _aa3.markdown("<br>", unsafe_allow_html=True)
                if _aa3.button("👁 Watch" if _an_is_watch else "➕ Add",
                               type="primary", use_container_width=True, key="an_add"):
                    _do_add(search_q, enp_an_, sig_an.stop, sig_an.target1, sig_an.target2, qty_an_)
                _aa4.markdown("<br>", unsafe_allow_html=True)
                _ab1, _ab2 = _aa4.columns(2)
                if _ab1.button("👁️", use_container_width=True, key="an_wl", help="Add to watchlist"):
                    add_to_watchlist(search_q)
                    st.toast(f"✅ {search_q} added to watchlist!")
                if TOKEN and CHAT_ID:
                    if _ab2.button("📲", use_container_width=True, key="an_tg", help="Send alert"):
                        _do_alert(sig_an.ticker, sig_an.price, sig_an.stop, sig_an.target1,
                                  sig_an.target2, sig_an.stop_pct, sig_an.gain_pct, sig_an.rr,
                                  sig_an.stars, sig_an.why_buy,
                                  signal_type=sig_an.signal_type,
                                  watch_buy_at=sig_an.watch_buy_at)

                with st.expander(f"🚀 Send {search_q} to Moomoo"):
                    _render_moomoo_panel(search_q, sig_an.price, sig_an.stop,
                                         sig_an.target1, sig_an.target2, atr_an, f"an_{search_q}")
            else:
                st.info(
                    f"No buy signal on **{search_q}** right now — setup criteria not met. "
                    f"Check back after a pullback or when momentum builds."
                )
                _nx1, _nx2 = st.columns([1, 3])
                if _nx1.button("👁️ Add to Watchlist", use_container_width=True):
                    add_to_watchlist(search_q)
                    st.toast(f"✅ {search_q} added!")
                with _nx2.expander("➕ Log manually anyway"):
                    with st.form("an_manual_add", clear_on_submit=True):
                        _m1, _m2 = st.columns(2)
                        _me  = _m1.number_input("Entry $",  value=float(price_an), format="%.2f")
                        _mq  = _m2.number_input("Shares",   min_value=0.0, step=1.0, value=10.0, format="%.4f")
                        _m3, _m4 = st.columns(2)
                        _ms  = _m3.number_input("Stop $",   value=round(float(price_an)*.95, 2), format="%.2f")
                        _mt1 = _m4.number_input("Target $", value=round(float(price_an)*1.1, 2), format="%.2f")
                        if st.form_submit_button("➕ Add", type="primary"):
                            _do_add(search_q, _me, _ms, _mt1, _mt1, _mq)

        # ══ TAB 2: CHART ══════════════════════════════════════════════════════
        with _tab_ch:
            _theme = st.session_state.get("theme", "dark")
            _cp1, _cp2 = st.columns([4, 1])
            _cp1.markdown(
                '<div style="color:var(--muted);font-size:.83rem;padding-top:8px">'
                'Price line coloured by net change · your position levels shown if held</div>',
                unsafe_allow_html=True)
            chart_period = _cp2.selectbox("Period", ["1mo","3mo","6mo","1y","2y"],
                                          index=2, label_visibility="collapsed", key="an_period")
            if chart_period != "6mo":
                df_an = load_chart_df(search_q, chart_period)
            if df_an is None or df_an.empty:
                st.warning("Could not load chart data for this period."); st.stop()
            pos_an = get_positions().get(search_q, {})

            _rh = _robinhood_chart(df_an, search_q, pos_an, _theme)
            if _rh is not None:
                st.plotly_chart(_rh, use_container_width=True, config={"displayModeBar": False})

            with st.expander("🔬 Advanced — candlestick · EMAs · RSI · MACD"):
                _txt  = "#79828c" if _theme == "light" else "#9ca3a8"
                _grid = "rgba(0,0,0,.06)" if _theme == "light" else "rgba(255,255,255,.05)"
                fig_an = make_subplots(rows=3, cols=1, shared_xaxes=True,
                    row_heights=[0.6,0.2,0.2], vertical_spacing=0.03,
                    subplot_titles=[f"{search_q} — Price & EMAs","RSI (14)","MACD"])
                fig_an.add_trace(go.Candlestick(x=df_an.index, open=df_an["open"], high=df_an["high"],
                    low=df_an["low"], close=df_an["close"], name="Price",
                    increasing_line_color="#00c805", decreasing_line_color="#ff5000",
                    increasing_fillcolor="rgba(0,200,5,.12)", decreasing_fillcolor="rgba(255,80,0,.12)"), row=1, col=1)
                for _ec,_ecl,_el in [("ema20","#22a3e6","20d"),("ema50","#fb923c","50d"),("ema200","#a78bfa","200d")]:
                    if _ec in df_an.columns:
                        fig_an.add_trace(go.Scatter(x=df_an.index, y=df_an[_ec], name=_el,
                                                    line=dict(color=_ecl, width=1.4)), row=1, col=1)
                _lx, _fx = df_an.index[-1], df_an.index[max(-90, -len(df_an))]
                for _ly,_lc,_ll in [(pos_an.get("entry"),_txt,"Entry"),(pos_an.get("stop"),"#ff5000","Stop"),
                                    (pos_an.get("target1"),"#00c805","T1"),(pos_an.get("target2"),"#22a3e6","T2")]:
                    if _ly:
                        fig_an.add_shape(type="line", x0=_fx, x1=_lx, y0=_ly, y1=_ly,
                                         line=dict(color=_lc, width=1.2, dash="dash"), row=1, col=1)
                        fig_an.add_annotation(x=_lx, y=_ly, text=f"  {_ll} ${_ly:.2f}",
                                              font=dict(color=_lc, size=10), showarrow=False, xanchor="left", row=1, col=1)
                if "rsi" in df_an.columns:
                    fig_an.add_trace(go.Scatter(x=df_an.index, y=df_an["rsi"], name="RSI",
                                                line=dict(color="#fb923c", width=1.4)), row=2, col=1)
                    for _rl,_rc in [(70,"#ff5000"),(50,_txt),(30,"#00c805")]:
                        fig_an.add_hline(y=_rl, line_dash="dot", line_color=_rc, row=2, col=1)
                if "macd" in df_an.columns:
                    fig_an.add_trace(go.Scatter(x=df_an.index, y=df_an["macd"], name="MACD",
                                                line=dict(color="#22a3e6", width=1.2)), row=3, col=1)
                    fig_an.add_trace(go.Scatter(x=df_an.index, y=df_an["macd_sig"], name="Signal",
                                                line=dict(color="#fb923c", width=1.2)), row=3, col=1)
                    _hc = ["#00c805" if v >= 0 else "#ff5000" for v in df_an["macd_hist"].fillna(0)]
                    fig_an.add_trace(go.Bar(x=df_an.index, y=df_an["macd_hist"], name="Hist",
                                            marker_color=_hc, opacity=0.7), row=3, col=1)
                fig_an.update_layout(height=640, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(color=_txt, size=11), xaxis_rangeslider_visible=False,
                    legend=dict(orientation="h", y=1.02, bgcolor="rgba(0,0,0,0)", font=dict(size=11)),
                    margin=dict(l=0, r=85, t=40, b=0))
                fig_an.update_xaxes(gridcolor=_grid, zeroline=False)
                fig_an.update_yaxes(gridcolor=_grid, zeroline=False)
                st.plotly_chart(fig_an, use_container_width=True, config={"displayModeBar": False})

        # ══ TAB 3: AI ANALYSIS ════════════════════════════════════════════════
        with _tab_ai:
            st.markdown(
                '<div style="color:var(--accent);font-weight:700;font-size:.95rem;margin-bottom:4px">'
                '🤖 Live Chart Analysis</div>'
                '<div style="color:var(--faint);font-size:.82rem;margin-bottom:16px">'
                'Claude reads every indicator — RSI · MACD · ADX · ATR · Volume · EMAs — '
                'and returns a BUY / HOLD / SELL verdict with reasoning.</div>',
                unsafe_allow_html=True,
            )
            _ai_key = f"ai_{search_q}"
            if st.button("Run AI Analysis", key="an_ai_btn", type="primary"):
                with st.spinner(f"Analyzing {search_q}…"):
                    _pos_ctx = None
                    _hp = get_positions().get(search_q)
                    if _hp:
                        try: _hdays = (date.today() - date.fromisoformat(_hp.get("date_in", str(date.today())))).days
                        except Exception: _hdays = 0
                        _pos_ctx = {"entry": _hp["entry"], "stop": _hp["stop"],
                                    "target1": _hp.get("target1",0), "target2": _hp.get("target2",0),
                                    "qty": _hp.get("qty",1), "days_held": _hdays}
                    st.session_state[_ai_key] = chart_analyze(search_q, position=_pos_ctx)
            if _ai_key in st.session_state:
                _show_ai_result(st.session_state[_ai_key])
            else:
                st.markdown(
                    '<div style="text-align:center;padding:40px;color:var(--dim)">'
                    '<div style="font-size:2rem;margin-bottom:10px">🤖</div>'
                    'Click "Run AI Analysis" to get a full verdict on this stock</div>',
                    unsafe_allow_html=True,
                )

        # ══ TAB 4: FUNDAMENTALS ═══════════════════════════════════════════════
        with _tab_fund:
            try:
                from fundamentals import get_fundamental_snapshot as _gfs4
                _an_snap = _gfs4(search_q)
            except Exception:
                _an_snap = {}
            _sig_ctx = None
            if sig_an:
                _sig_ctx = {
                    "price": sig_an.price, "live_price": price_an,
                    "stop": sig_an.stop, "target1": sig_an.target1,
                    "target2": sig_an.target2, "rr": sig_an.rr, "live_rr": sig_an.rr,
                    "stop_pct": sig_an.stop_pct, "gain_pct": sig_an.gain_pct,
                    "stars": sig_an.stars, "warnings": sig_an.warnings,
                }
            _deep_dive_panel(search_q, _an_snap, co_, price_an,
                             _sig_ctx, f"an_fund_{search_q}", ai_type="entry")

        # Quick-access watchlist buttons
        wl_qs = get_watchlist()
        if wl_qs:
            st.markdown('<div style="color:var(--dim);font-size:.8rem;text-align:center;margin-bottom:8px">Quick access from your watchlist:</div>', unsafe_allow_html=True)
            qs_cols = st.columns(min(len(wl_qs[:12]), 6))
            for i, qs_t in enumerate(wl_qs[:12]):
                if qs_cols[i % 6].button(qs_t, key=f"qs_{qs_t}", use_container_width=True):
                    st.session_state["analyze_ticker"] = qs_t
                    st.rerun()


# PAGE — BROKER  (connect any supported broker; view + manage portfolio)
# ══════════════════════════════════════════════════════════════════════════════
elif page == "🏦  Broker":
    live_market_bar()
    _page_header("🏦 Broker", "Connect your own broker to view and manage your portfolio in-app")

    from brokers import BROKERS, build_adapter
    import broker_store as _bstore
    from auth import current_user, get_broker_consent, set_broker_consent

    _user = current_user()

    # ── One-time risk consent before any live brokerage connection ────────────
    if not get_broker_consent(_user):
        st.warning("⚠️  **Read this before connecting a live brokerage account.**")
        st.markdown("""
Connecting a broker lets this app **see your balances and positions**, and — for brokers
that support it — **place trades using your API keys**.

- **You** are solely responsible for every order placed through your account.
- This app provides information and tools **only**. It is **not** financial advice and
  **cannot guarantee any outcome — you can lose money.**
- Your API keys are **encrypted on this computer** and are never shown to other users.
  Even so, connect only keys you're comfortable using, and prefer a **paper / simulated**
  account or **read-only** keys while you get familiar with the app.
- You can **disconnect and delete your keys** from this page at any time.
        """)
        _agree = st.checkbox(
            "I understand the risks and accept the Terms. I am responsible for my own trades."
        )
        if st.button("Continue", type="primary", disabled=not _agree):
            set_broker_consent(_user, True)
            st.rerun()
        st.stop()

    _creds_all = _bstore.load(_user)
    _bsel = st.selectbox("Broker", BROKERS, key="broker_sel")
    _saved = _creds_all.get(_bsel, {})
    _status = "🟢 Connected" if _saved else "⚪ Not connected"
    st.markdown(f'<div style="color:var(--muted);font-size:.85rem;margin:-6px 0 14px">{_status}</div>', unsafe_allow_html=True)

    # ── Guided walkthrough: how to create + paste API keys, per broker ────────
    _GUIDE = {
        "Alpaca": (
            "**Alpaca** — free API-first US broker; supports paper **and** live trading.\n\n"
            "1. Go to **alpaca.markets** and sign in (or create a free account).\n"
            "2. Open your dashboard. Pick **Paper Trading** first (top-left toggle) — this uses "
            "fake money so you can test with zero risk.\n"
            "3. On the right, find **API Keys** → **Generate New Key**.\n"
            "4. Copy the **Key ID** and the **Secret Key** (the secret is shown only once).\n"
            "5. Paste them below, keep **Paper account** checked, and click **Connect**.\n\n"
            "When you're ready for real money, generate a *live* key and uncheck **Paper account**."
        ),
        "Tradier": (
            "**Tradier** — US broker with a simple REST API and a free sandbox.\n\n"
            "1. Go to **tradier.com** and sign in (or open a Brokerage account).\n"
            "2. Visit **dash.tradier.com** → **Settings** → **API Access**.\n"
            "3. For testing, use the **Sandbox** section to get a sandbox **Access Token**.\n"
            "4. Copy your **Access Token** and your **Account ID** (looks like `VA########`).\n"
            "5. Paste them below, keep **Sandbox** checked to test, then click **Connect**."
        ),
        "Moomoo": (
            "**Moomoo / Futu** — connects through the **OpenD** gateway that runs on your own PC "
            "(no API keys to paste — you log in inside OpenD).\n\n"
            "1. Download **OpenD** from moomoo's OpenAPI page and install it.\n"
            "2. Open OpenD, log in with your moomoo account, and leave it running "
            "(it listens on port **11111**).\n"
            "3. Choose **SIMULATE** below to use the paper account, or **REAL** for live.\n"
            "4. Leave **Account ID** as `0` for the default account, then click **Connect**."
        ),
        "Webull": (
            "**Webull** — connects with **official OpenAPI keys** (an App Key + App Secret).\n\n"
            "1. Go to **developer.webull.com** and sign in with your Webull account.\n"
            "2. Apply for / open **OpenAPI access** and create an app. Webull issues you an "
            "**App Key** and an **App Secret** (the secret is shown once — copy it).\n"
            "3. Paste both below and pick your **Region** (US / HK / JP). Leave **Account ID** blank.\n"
            "4. Click **Connect**, then **choose which account** (Cash, Margin, Roth IRA…) in the "
            "selector that appears — Webull exposes all of them, so pick the right one.\n\n"
            "*Note:* Webull OpenAPI must be enabled on your account — not every retail account "
            "has it yet. If your keys don't connect, check that OpenAPI is approved for your account."
        ),
        "IBKR": (
            "**Interactive Brokers** connects through IBKR's **Client Portal Gateway**, which "
            "runs on your own PC. This adapter is still being finished — check back soon."
        ),
    }
    with st.expander(f"📖  How to connect {_bsel} — step by step", expanded=not _saved):
        st.markdown(_GUIDE.get(_bsel, ""))

    # ── Connect form (per-broker credentials) ─────────────────────────────────
    with st.form(f"broker_connect_{_bsel}"):
        _creds = {}
        if _bsel == "Alpaca":
            _creds["key"]    = st.text_input("API Key ID", value=_saved.get("key", ""))
            _creds["secret"] = st.text_input("API Secret", value=_saved.get("secret", ""), type="password")
            _creds["paper"]  = st.checkbox("Paper account", value=_saved.get("paper", True))
        elif _bsel == "Tradier":
            _creds["token"]      = st.text_input("Access Token", value=_saved.get("token", ""), type="password")
            _creds["account_id"] = st.text_input("Account ID", value=_saved.get("account_id", ""))
            _creds["paper"]      = st.checkbox("Sandbox", value=_saved.get("paper", True))
        elif _bsel == "Moomoo":
            _creds["env"]    = st.selectbox("Environment", ["SIMULATE", "REAL"],
                                            index=0 if _saved.get("env", "SIMULATE") == "SIMULATE" else 1)
            _creds["acc_id"] = st.text_input("Account ID (0 = default)", value=str(_saved.get("acc_id", 0)))
            st.caption("Requires Moomoo OpenD running locally on port 11111.")
        elif _bsel == "Webull":
            _creds["app_key"]    = st.text_input("App Key", value=_saved.get("app_key", ""))
            _creds["app_secret"] = st.text_input("App Secret", value=_saved.get("app_secret", ""), type="password")
            _regions = ["us", "hk", "jp"]
            _creds["region"]     = st.selectbox(
                "Region", _regions,
                index=_regions.index(_saved.get("region", "us")) if _saved.get("region", "us") in _regions else 0)
            _creds["account_id"] = st.text_input("Account ID (blank = auto-detect)", value=_saved.get("account_id", ""))
            st.caption("Uses Webull's official OpenAPI — requires OpenAPI access enabled on your account.")
        else:  # IBKR
            st.info("IBKR needs the Client Portal Gateway running locally — adapter coming soon.")
        _bc1, _bc2 = st.columns(2)
        _connect = _bc1.form_submit_button("🔌 Connect", type="primary", use_container_width=True)
        _disc    = _bc2.form_submit_button("Disconnect", use_container_width=True)

    if _disc:
        _bstore.clear(_user, _bsel)
        for k in (f"broker_acct_{_bsel}", f"broker_pos_{_bsel}"):
            st.session_state.pop(k, None)
        st.toast(f"{_bsel} disconnected"); st.rerun()

    if _connect:
        if _bsel == "Moomoo":
            try: _creds["acc_id"] = int(_creds.get("acc_id", 0) or 0)
            except Exception: _creds["acc_id"] = 0
        _ad = build_adapter(_bsel, _creds)
        ok, msg = (_ad.connect() if _ad else (False, "Unavailable"))
        if ok:
            _bstore.save(_user, _bsel, _creds); _saved = _creds
            st.session_state.pop(f"acct_opts_{_bsel}", None)   # refresh account list for new keys
            st.success(f"✅ {_bsel}: {msg}  ·  keys encrypted and saved to your account")
        else:
            st.error(f"❌ {_bsel}: {msg}")

    # ── Choose WHICH account, for any broker that exposes more than one ────────
    # Webull/Tradier can hold Cash + Margin + Roth etc.; Moomoo lists its OpenD
    # accounts. Auto-picking the first is what once grabbed a user's Roth, so the
    # user chooses explicitly. (Field the chosen id is saved under, per broker.)
    _ACCT_FIELD = {"Webull": "account_id", "Tradier": "account_id", "Moomoo": "acc_id"}
    _afield = _ACCT_FIELD.get(_bsel)
    if _afield and _saved:
        st.markdown('<div class="thin-div"></div>', unsafe_allow_html=True)
        _rc1, _rc2 = st.columns([4, 1])
        _rc1.markdown(f'<p class="section-label" style="margin-top:6px">Which {_bsel} account should this app use?</p>',
                      unsafe_allow_html=True)
        _optkey = f"acct_opts_{_bsel}"
        if _rc2.button("🔄 List accounts", use_container_width=True, key=f"listacct_{_bsel}") \
                or _optkey not in st.session_state:
            with st.spinner(f"Fetching your {_bsel} accounts…"):
                _adp = build_adapter(_bsel, _saved)
                st.session_state[_optkey] = _adp.list_account_options() if _adp else []
        _opts = st.session_state.get(_optkey, [])
        if not _opts:
            st.caption("No accounts found yet — click **List accounts** (needs a valid connection"
                       + (", with OpenD running" if _bsel == "Moomoo" else "") + ").")
        else:
            _ids = [str(o["id"]) for o in _opts]
            _cur = str(_saved.get(_afield, "") or "")
            _has_cur = _cur in _ids
            _idx = _ids.index(_cur) if _has_cur else 0
            _pick = st.selectbox(
                "Account", list(range(len(_opts))),
                format_func=lambda i: _opts[i]["label"], index=_idx,
                label_visibility="collapsed", key=f"acct_pick_{_bsel}")
            st.caption("Currently using: **"
                       + (_opts[_ids.index(_cur)]["label"] if _has_cur
                          else f"{_opts[0]['label']}  — auto (first account; pick one to be sure)")
                       + "**")
            if st.button("✅ Use this account", type="primary", key=f"useacct_{_bsel}"):
                _chosen = _ids[_pick]
                if _bsel == "Moomoo":
                    try: _chosen = int(_chosen)
                    except Exception: pass
                _new = dict(_saved); _new[_afield] = _chosen
                _bstore.save(_user, _bsel, _new)
                for _k in (f"broker_acct_{_bsel}", f"broker_pos_{_bsel}", "broker_holdings_ts"):
                    st.session_state.pop(_k, None)
                st.success(f"Now using {_opts[_pick]['label']}")
                st.rerun()
    elif _bsel == "Alpaca" and _saved:
        # One account per Alpaca key pair — nothing to pick, but show which one.
        _optkey = "acct_opts_Alpaca"
        if _optkey not in st.session_state:
            _adp = build_adapter("Alpaca", _saved)
            st.session_state[_optkey] = _adp.list_account_options() if _adp else []
        _ao = st.session_state.get(_optkey, [])
        if _ao:
            st.caption(f"Connected account: **{_ao[0]['label']}**  ·  each Alpaca key maps to a single account.")

    # ── Portfolio (loaded on demand so we don't hit broker APIs every rerun) ──
    if _saved:
        st.markdown('<div class="thin-div"></div>', unsafe_allow_html=True)
        if st.button("🔄 Load / refresh portfolio", use_container_width=True):
            _ad = build_adapter(_bsel, _saved)
            st.session_state[f"broker_acct_{_bsel}"] = _ad.get_account() if _ad else None
            st.session_state[f"broker_pos_{_bsel}"]  = _ad.get_positions() if _ad else []
        _acct = st.session_state.get(f"broker_acct_{_bsel}")
        _pos  = st.session_state.get(f"broker_pos_{_bsel}", [])
        if _acct:
            _a1, _a2, _a3 = st.columns(3)
            _a1.markdown(_metric_tile("Cash",         f"${_acct['cash']:,.0f}"),         unsafe_allow_html=True)
            _a2.markdown(_metric_tile("Equity",       f"${_acct['equity']:,.0f}"),       unsafe_allow_html=True)
            _a3.markdown(_metric_tile("Buying Power",  f"${_acct['buying_power']:,.0f}"), unsafe_allow_html=True)
            st.markdown('<p class="section-label" style="margin-top:16px">Positions</p>', unsafe_allow_html=True)
            if _pos:
                st.dataframe(pd.DataFrame(_pos), use_container_width=True, hide_index=True)
            else:
                st.caption("No open positions (or position list not available for this broker).")
        else:
            st.caption("Click **Load / refresh portfolio** to pull your account.")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE — WATCHLIST
# ══════════════════════════════════════════════════════════════════════════════
elif page == "👁️  Watchlist":
    live_market_bar()
    _page_header("👁️ Watchlist", "Track and manage every ticker you're watching — live prices, one-click remove")

    wl_all = get_watchlist()

    # ── Top action bar ────────────────────────────────────────────────────────
    _wa1, _wa2, _wa3 = st.columns([4, 1, 1])
    _wl_input = _wa1.text_input(
        "", placeholder="➕  Add tickers — e.g.  NVDA  CRWD  MSTR  (comma or space separated)",
        label_visibility="collapsed", key="wl_page_add"
    )
    if _wa2.button("Add", type="primary", use_container_width=True, key="wl_add_btn"):
        _tickers_to_add = _parse_tickers(_wl_input)
        if _tickers_to_add:
            for x in _tickers_to_add:
                add_to_watchlist(x)
            st.toast(f"✅ Added {', '.join(_tickers_to_add)}")
            _refresh()
    if _wa3.button("🔄 Refresh", use_container_width=True, key="wl_refresh_btn"):
        _refresh()

    if not wl_all:
        st.markdown("""
        <div style="text-align:center;padding:60px 20px">
            <div style="font-size:3rem;margin-bottom:14px">👁️</div>
            <div style="font-size:1.05rem;color:var(--faint);margin-bottom:6px">Your watchlist is empty</div>
            <div style="font-size:.83rem;color:var(--dim)">Add tickers above to start tracking them</div>
        </div>
        """, unsafe_allow_html=True)
    else:
        # ── Controls row ──────────────────────────────────────────────────────
        _wc1, _wc2, _wc3 = st.columns([3, 2, 2])
        _wl_search = _wc1.text_input(
            "", placeholder="🔍  Filter by ticker or name…",
            label_visibility="collapsed", key="wl_filter"
        ).upper().strip()
        _wl_sort = _wc2.selectbox(
            "", ["A → Z", "Z → A", "Biggest Gain", "Biggest Loss", "Recently Added"],
            label_visibility="collapsed", key="wl_sort"
        )
        _wc3.markdown(
            f'<div style="padding-top:8px;color:var(--faint);font-size:.83rem;text-align:right">'
            f'<b style="color:var(--fg)">{len(wl_all)}</b> tickers tracked</div>',
            unsafe_allow_html=True
        )

        st.markdown('<div style="height:6px"></div>', unsafe_allow_html=True)

        # ── Fetch live quotes for all tickers in one batch ────────────────────
        with st.spinner("Loading live prices…"):
            try:
                _wl_quotes = live_quotes_batch(tuple(wl_all))
            except Exception:
                _wl_quotes = {}

        # ── Build display rows ────────────────────────────────────────────────
        _wl_rows = []
        for _t in wl_all:
            _co = cached_company_info(_t)
            _q  = _wl_quotes.get(_t, {})
            _wl_rows.append({
                "ticker":  _t,
                "name":    _co.get("name", _t),
                "sector":  _co.get("sector", "") or _co.get("industry", ""),
                "price":   float(_q.get("price") or 0),
                "pct":     float(_q.get("pct")   or 0),
            })

        # ── Apply search filter ───────────────────────────────────────────────
        if _wl_search:
            _wl_rows = [r for r in _wl_rows
                        if _wl_search in r["ticker"]
                        or _wl_search in r["name"].upper()]

        # ── Apply sort ────────────────────────────────────────────────────────
        if _wl_sort == "A → Z":
            _wl_rows.sort(key=lambda r: r["ticker"])
        elif _wl_sort == "Z → A":
            _wl_rows.sort(key=lambda r: r["ticker"], reverse=True)
        elif _wl_sort == "Biggest Gain":
            _wl_rows.sort(key=lambda r: r["pct"], reverse=True)
        elif _wl_sort == "Biggest Loss":
            _wl_rows.sort(key=lambda r: r["pct"])
        # "Recently Added" keeps the original insertion order

        if not _wl_rows:
            st.info(f"No tickers match **{_wl_search}**.")
        else:
            # ── Column headers ────────────────────────────────────────────────
            _h1, _h2, _h3, _h4, _h5 = st.columns([2, 4, 2, 2, 2])
            for _col, _lbl in zip(
                [_h1, _h2, _h3, _h4, _h5],
                ["Ticker", "Company", "Price", "Day Change", "Actions"]
            ):
                _col.markdown(
                    f'<div style="font-size:.67rem;font-weight:700;color:var(--dim);'
                    f'text-transform:uppercase;letter-spacing:.7px;padding-bottom:6px;'
                    f'border-bottom:1px solid rgba(255,255,255,.06)">{_lbl}</div>',
                    unsafe_allow_html=True
                )

            st.markdown('<div style="height:2px"></div>', unsafe_allow_html=True)

            # ── Render rows — fetch portfolio set once to avoid repeated I/O ─────
            _portfolio_tickers = set(get_positions().keys())
            _cell = 'style="padding:12px 0;font-size:.85rem"'  # shared cell padding

            for _row in _wl_rows:
                _t   = _row["ticker"]
                _pc  = _row["pct"]
                _pr  = _row["price"]
                _chg_col = "var(--pos)" if _pc >= 0 else "var(--neg)"
                _chg_ico = "▲" if _pc >= 0 else "▼"
                _in_port  = _t in _portfolio_tickers

                _rc1, _rc2, _rc3, _rc4, _rc5 = st.columns([2, 4, 2, 2, 2])

                _rc1.markdown(
                    f'<div {_cell}>'
                    f'<span style="font-size:1rem;font-weight:800;color:var(--fg)">{_t}</span>'
                    + (f'<br><span style="font-size:.67rem;color:var(--faint)">{_row["sector"][:20]}</span>' if _row["sector"] else "")
                    + (' <span style="font-size:.6rem;background:rgba(34,197,94,.15);color:#86efac;border-radius:3px;padding:1px 5px;font-weight:700">HELD</span>' if _in_port else "")
                    + '</div>',
                    unsafe_allow_html=True
                )
                _rc2.markdown(
                    f'<div {_cell} style="color:var(--faint);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'
                    f'{_row["name"]}</div>',
                    unsafe_allow_html=True
                )
                _rc3.markdown(
                    f'<div {_cell} style="font-weight:700;color:var(--fg);font-variant-numeric:tabular-nums">'
                    f'{"$" + f"{_pr:.2f}" if _pr else "—"}</div>',
                    unsafe_allow_html=True
                )
                _rc4.markdown(
                    f'<div {_cell} style="font-weight:600;color:{_chg_col};font-variant-numeric:tabular-nums">'
                    f'{"—" if not _pr else f"{_chg_ico} {abs(_pc):.2f}%"}</div>',
                    unsafe_allow_html=True
                )

                _ab1, _ab2 = _rc5.columns(2)
                if _ab1.button("🔎", key=f"wl_an_{_t}", help=f"Analyze {_t}", use_container_width=True):
                    st.session_state["analyze_search"] = _t
                    st.session_state["page"] = "🔎  Analyze"
                    st.session_state["_an_from_wl"] = True
                    st.rerun()
                if _ab2.button("✕", key=f"wl_rm_{_t}", help=f"Remove {_t}", use_container_width=True):
                    remove_from_watchlist(_t)
                    st.toast(f"Removed {_t}")
                    _refresh()

                st.markdown('<hr style="border:none;border-top:1px solid rgba(255,255,255,.04);margin:0">', unsafe_allow_html=True)

        # ── Bulk actions ──────────────────────────────────────────────────────
        st.markdown('<div style="height:20px"></div>', unsafe_allow_html=True)
        with st.expander("⚙️  Bulk Actions"):
            _ba1, _ba2, _ba3 = st.columns(3)
            if _ba1.button("📋 Load S&P 500", use_container_width=True):
                with st.spinner("Fetching S&P 500 list…"):
                    _sp = load_sp500()
                if _sp:
                    _existing = set(get_positions().keys())
                    set_watchlist(list(_existing) + [t for t in _sp if t not in _existing])
                    st.success(f"✅ {len(_sp)} tickers loaded!")
                    _refresh()
                else:
                    st.error("Fetch failed — check internet connection")
            if _ba2.button("🔄 Reset to Default", use_container_width=True):
                from state import DEFAULT_STATE as _DS
                set_watchlist(_DS["watchlist"])
                st.toast("Watchlist reset to defaults")
                _refresh()
            if _ba3.button("🗑️ Clear All", use_container_width=True):
                set_watchlist([])
                st.toast("Watchlist cleared")
                _refresh()


# ══════════════════════════════════════════════════════════════════════════════
# PAGE — IPOS
# ══════════════════════════════════════════════════════════════════════════════
elif page == "📅  IPOs":
    from ipo_data import get_ipo_year, get_ipo_details, MONTHS

    live_market_bar()
    _page_header("📅 IPO Calendar", "Full-year timeline — click any company for valuation, news and details")

    # ── Controls row ──────────────────────────────────────────────────────────
    _cur_year = date.today().year
    _ic1, _ic2, _ic3, _ic4 = st.columns([1, 2, 2, 1])
    _ipo_year = _ic1.selectbox(
        "Year", [_cur_year - 1, _cur_year, _cur_year + 1],
        index=1, label_visibility="collapsed", key="ipo_year_sel"
    )
    _ipo_filter = _ic2.selectbox(
        "Filter", ["All", "Upcoming", "Past — Trading", "Hot (≥$500M)"],
        label_visibility="collapsed", key="ipo_filter_sel"
    )
    _ipo_search = _ic3.text_input(
        "search", placeholder="🔍  Search company or ticker…",
        label_visibility="collapsed", key="ipo_search"
    ).strip().upper()
    if _ic4.button("🔄 Refresh", use_container_width=True, key="ipo_refresh"):
        st.cache_data.clear()
        st.rerun()

    # ── Load calendar (one fast call, no price fetches) ───────────────────────
    @st.cache_data(ttl=21600, show_spinner=False)
    def _load_ipo_year(yr):
        return get_ipo_year(yr)

    with st.spinner("Loading IPO calendar…"):
        _cal = _load_ipo_year(_ipo_year)

    # ── Detail loader (lazy — only fires when user expands) ───────────────────
    @st.cache_data(ttl=300, show_spinner=False)
    def _load_detail(ticker, price_mid):
        return get_ipo_details(ticker, price_mid)

    _today = date.today()

    # ── Apply filter & search ─────────────────────────────────────────────────
    _all_ipos = []
    for _m in range(1, 13):
        for _ipo in _cal.get(_m, []):
            _ipo["_month"] = _m
            _all_ipos.append(_ipo)

    if _ipo_filter == "Upcoming":
        _all_ipos = [x for x in _all_ipos if not x["is_past"]]
    elif _ipo_filter == "Past — Trading":
        _all_ipos = [x for x in _all_ipos if x["is_past"]]
    elif _ipo_filter == "Hot (≥$500M)":
        _all_ipos = [x for x in _all_ipos if (x["mkt_cap_raw"] or 0) >= 500_000_000]

    if _ipo_search:
        _all_ipos = [
            x for x in _all_ipos
            if _ipo_search in x["ticker"].upper()
            or _ipo_search in x["name"].upper()
        ]

    # Re-group by month after filtering
    _by_month_filtered = {}
    for _ipo in _all_ipos:
        _by_month_filtered.setdefault(_ipo["_month"], []).append(_ipo)

    _total = len(_all_ipos)
    st.markdown(
        f'<p style="color:var(--faint);font-size:.8rem;margin:0 0 16px">'
        f'{_total} IPO{"s" if _total != 1 else ""} shown for {_ipo_year}</p>',
        unsafe_allow_html=True,
    )

    if not _all_ipos:
        st.info("No IPOs match the current filter.")
    else:
        # ── Render month by month ──────────────────────────────────────────────
        for _m in range(1, 13):
            _month_ipos = _by_month_filtered.get(_m, [])
            if not _month_ipos:
                continue

            _is_current = (_m == _today.month and _ipo_year == _cur_year)
            _m_col = "var(--accent)" if _is_current else "var(--dim)"
            st.markdown(
                f'<div style="display:flex;align-items:center;gap:12px;margin:24px 0 8px">'
                f'<span style="color:{_m_col};font-weight:800;font-size:1rem;'
                f'letter-spacing:.5px;text-transform:uppercase">'
                f'{"▶ " if _is_current else ""}{MONTHS[_m-1].upper()}</span>'
                f'<span style="color:var(--dim);font-size:.75rem">{len(_month_ipos)} IPO{"s" if len(_month_ipos)!=1 else ""}</span>'
                f'<div style="flex:1;height:1px;background:var(--surface)"></div>'
                f'</div>',
                unsafe_allow_html=True,
            )

            for _ipo in _month_ipos:
                _day_str   = _ipo["date"][8:10]   # DD
                _ticker    = _ipo["ticker"] or "—"
                _name      = _ipo["name"]
                _exch      = _ipo["exchange"]
                _range     = _ipo["price_range"]
                _cap       = _ipo["mkt_cap"]
                _status    = _ipo["status"]
                _is_past   = _ipo["is_past"]
                _da        = _ipo["days_away"]
                _cd, _cd_col = _ipo_countdown(_da)

                # Row line
                st.markdown(
                    f'<div style="display:flex;align-items:center;gap:0;'
                    f'padding:9px 14px;border-radius:10px;margin:2px 0;'
                    f'background:var(--surface);border:1px solid rgba(255,255,255,.05);'
                    f'{"opacity:.65;" if _is_past and _status not in ("priced","") else ""}">'
                    # Date
                    f'<span style="color:var(--faint);font-size:.8rem;font-weight:600;'
                    f'min-width:36px">{_day_str}</span>'
                    # Ticker
                    f'<span style="color:var(--fg);font-weight:800;font-size:.95rem;'
                    f'min-width:80px">{_ticker}</span>'
                    # Status badge
                    f'{_ipo_status_badge(_status)}'
                    # Name + exchange
                    f'<span style="color:var(--faint);font-size:.82rem;margin-left:12px;flex:1;'
                    f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'
                    f'<b style="color:var(--muted)">{_name}</b>'
                    f'{"  ·  " + _exch if _exch else ""}</span>'
                    # Price range
                    f'<span style="color:var(--muted);font-size:.82rem;min-width:90px;text-align:right">'
                    f'{_range}</span>'
                    # Market cap
                    f'<span style="color:var(--faint);font-size:.78rem;min-width:70px;text-align:right">'
                    f'{_cap}</span>'
                    # Countdown / days ago
                    f'<span style="color:{_cd_col};font-size:.78rem;font-weight:600;'
                    f'min-width:70px;text-align:right">{_cd}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                # ── Expandable detail panel ────────────────────────────────────
                with st.expander(f"  Details — {_name}", expanded=False):
                    with st.spinner("Loading company details…"):
                        _det = _load_detail(_ticker if _ticker != "—" else "", _ipo["price_mid"])

                    if not _det:
                        st.warning("No data available — ticker may not be trading yet.")
                    else:
                        _prof  = _det.get("profile") or {}
                        _fin   = _det.get("fin_metrics") or {}
                        _news  = _det.get("news") or []
                        _cur_p = _det.get("cur_price", 0.0)
                        _dpct  = _det.get("day_pct", 0.0)
                        _ret   = _det.get("ipo_return")

                        # ── Header: logo + name + price ───────────────────────
                        _logo = _prof.get("logo", "")
                        _url  = _prof.get("weburl", "")
                        _desc = _prof.get("description") or _prof.get("finnhubIndustry", "")
                        _ceo  = _prof.get("ceo", "")
                        _emp  = _prof.get("employeeTotal")
                        _ipo_date_prof = _prof.get("ipo", "")
                        _country = _prof.get("country", "")

                        dc1, dc2 = st.columns([3, 2])

                        with dc1:
                            # Overview
                            st.markdown(
                                f'<div style="margin-bottom:12px">'
                                + (f'<img src="{_logo}" style="height:36px;margin-bottom:8px;border-radius:6px">' if _logo else "")
                                + f'<div style="font-size:1.05rem;font-weight:800;color:var(--fg)">{_name}</div>'
                                f'<div style="color:var(--faint);font-size:.8rem;margin:2px 0 8px">'
                                + (f'{_prof.get("exchange","")} · ' if _prof.get("exchange") else "")
                                + (f'{_prof.get("finnhubIndustry","")} · ' if _prof.get("finnhubIndustry") else "")
                                + (f'{_country}' if _country else "")
                                + f'</div>'
                                + (f'<div style="color:var(--muted);font-size:.82rem;line-height:1.6;margin-bottom:8px">{_desc[:300]}{"…" if len(_desc)>300 else ""}</div>' if _desc else "")
                                + f'</div>',
                                unsafe_allow_html=True,
                            )
                            # Key facts
                            _facts = []
                            if _ceo:                  _facts.append(("CEO",        _ceo))
                            if _emp:                  _facts.append(("Employees",  f"{int(_emp):,}"))
                            if _ipo_date_prof:        _facts.append(("IPO Date",   _ipo_date_prof))
                            if _url:                  _facts.append(("Website",    f'<a href="{_url}" target="_blank" style="color:var(--accent)">{_url}</a>'))
                            if _ipo["price_range"] != "TBD": _facts.append(("Price Range", _ipo["price_range"]))
                            if _ipo["shares"] != "—": _facts.append(("Shares Offered", _ipo["shares"]))
                            if _ipo["mkt_cap"] != "—": _facts.append(("Est. Market Cap", _ipo["mkt_cap"]))

                            for _lbl, _val in _facts:
                                st.markdown(
                                    f'<div style="display:flex;gap:8px;padding:4px 0;'
                                    f'border-bottom:1px solid rgba(255,255,255,.04)">'
                                    f'<span style="color:var(--faint);font-size:.78rem;min-width:120px">{_lbl}</span>'
                                    f'<span style="color:var(--fg);font-size:.82rem">{_val}</span>'
                                    f'</div>',
                                    unsafe_allow_html=True,
                                )

                        with dc2:
                            # Live price / IPO return (if trading)
                            if _cur_p:
                                _pc = "var(--pos)" if _dpct >= 0 else "var(--neg)"
                                _pi = "▲" if _dpct >= 0 else "▼"
                                _rc = "var(--pos)" if (_ret or 0) >= 0 else "var(--neg)"
                                _ri = "▲" if (_ret or 0) >= 0 else "▼"
                                st.markdown(
                                    f'<div style="background:var(--surface);border:1px solid var(--border);'
                                    f'border-radius:10px;padding:16px;margin-bottom:12px">'
                                    f'<div style="color:var(--dim);font-size:.68rem;text-transform:uppercase;letter-spacing:.5px">Current Price</div>'
                                    f'<div style="color:var(--fg);font-size:1.6rem;font-weight:800">${_cur_p:.2f}</div>'
                                    f'<div style="color:{_pc};font-size:.9rem;font-weight:600">{_pi} {abs(_dpct):.2f}% today</div>'
                                    + (f'<div style="color:{_rc};font-size:.85rem;margin-top:6px;font-weight:600">'
                                       f'{_ri} {abs(_ret):.1f}% from IPO price</div>' if _ret is not None else "")
                                    + f'</div>',
                                    unsafe_allow_html=True,
                                )

                            # Key financials
                            _fin_rows = [(k, v) for k, v in _fin.items() if v != "—"]
                            if _fin_rows:
                                st.markdown(
                                    '<p style="color:var(--dim);font-size:.68rem;font-weight:700;'
                                    'text-transform:uppercase;letter-spacing:.5px;margin:0 0 6px">Key Metrics</p>',
                                    unsafe_allow_html=True,
                                )
                                for _k, _v in _fin_rows:
                                    st.markdown(
                                        f'<div style="display:flex;justify-content:space-between;'
                                        f'padding:3px 0;border-bottom:1px solid rgba(255,255,255,.04)">'
                                        f'<span style="color:var(--faint);font-size:.78rem">{_k}</span>'
                                        f'<span style="color:var(--fg);font-size:.8rem;font-weight:600">{_v}</span>'
                                        f'</div>',
                                        unsafe_allow_html=True,
                                    )

                        # ── News ──────────────────────────────────────────────
                        if _news:
                            st.markdown(
                                '<p style="color:var(--dim);font-size:.68rem;font-weight:700;'
                                'text-transform:uppercase;letter-spacing:.5px;margin:16px 0 8px">Recent News</p>',
                                unsafe_allow_html=True,
                            )
                            for _n in _news[:5]:
                                _ts = ""
                                try:
                                    _ts = datetime.fromtimestamp(_n["datetime"]).strftime("%b %d")
                                except Exception:
                                    pass
                                st.markdown(
                                    f'<div style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,.05)">'
                                    f'<a href="{_n["url"]}" target="_blank" style="color:var(--accent);font-size:.85rem;'
                                    f'font-weight:600;text-decoration:none">{_n["headline"]}</a>'
                                    f'<div style="color:var(--faint);font-size:.74rem;margin-top:2px">'
                                    f'{_n["source"]}{"  ·  " + _ts if _ts else ""}</div>'
                                    f'</div>',
                                    unsafe_allow_html=True,
                                )
                        else:
                            st.caption("No recent news found.")

                        # ── Action buttons ────────────────────────────────────
                        st.markdown("<br>", unsafe_allow_html=True)
                        _ab1, _ab2, _ab3 = st.columns([1, 1, 3])
                        if _ticker != "—":
                            if _ab1.button("🔎 Analyze", key=f"det_an_{_ticker}_{_m}",
                                           use_container_width=True):
                                st.session_state["analyze_ticker"] = _ticker
                                st.session_state["page"] = "🔎  Analyze"
                                st.rerun()
                            if _ab2.button("👁 Watchlist", key=f"det_wl_{_ticker}_{_m}",
                                           use_container_width=True):
                                add_to_watchlist(_ticker)
                                st.toast(f"✅ {_ticker} added to watchlist")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE — STRATEGY
# ══════════════════════════════════════════════════════════════════════════════
elif page == "⚙️  Strategy":
    from strategy_review import (
        load_config, save_config as _raw_save_config, apply_suggestion,
        compute_performance_stats, run_ai_review,
        save_review, get_review_history,
    )
    from auth import is_admin

    # The market scan is precomputed once for ALL subscribers, so the strategy
    # config is shared — only the admin may change it. Guard every write centrally
    # (all save_config call-sites below route through this) so a subscriber can
    # view and experiment with the AI review without altering everyone's strategy.
    _can_edit = is_admin()

    def save_config(cfg):
        if not _can_edit:
            st.error("🔒 The strategy is shared across all subscribers — only the admin "
                     "can change it, so your edit wasn't saved.")
            return
        _raw_save_config(cfg)

    live_market_bar()
    _page_header("⚙️ Strategy", "Config-driven entry/exit rules · AI review engine · Evidence-based updates")

    if not _can_edit:
        st.info("🔒 **Read-only** — you can explore the config and run AI reviews, but "
                "strategy changes are managed by the admin and apply to all subscribers.")

    _strat_cfg   = load_config()
    _closed_all  = get_closed()
    _perf_stats  = compute_performance_stats(_closed_all)

    tab_cfg, tab_review, tab_history = st.tabs([
        "📋  Current Config",
        "🤖  AI Review",
        "🕐  Review History",
    ])

    # ── TAB 1: CURRENT CONFIG ─────────────────────────────────────────────────
    with tab_cfg:
        import copy as _copy

        st.markdown(
            '<div style="color:var(--muted);font-size:.78rem;margin-bottom:16px">'
            'Live thresholds driving every signal — changes take effect on the next scan.'
            '</div>',
            unsafe_allow_html=True
        )

        _sw = _strat_cfg.get("swing",          {})
        _dt = _strat_cfg.get("day_trade",      {})
        _ps = _strat_cfg.get("position_sizing",{})

        _tab_sw_en, _tab_sw_ex, _tab_dt_en, _tab_dt_ex = st.tabs([
            "📅 Swing — Entry", "📅 Swing — Exit",
            "⚡ Day Trade — Entry", "⚡ Day Trade — Exit",
        ])

        # ── Swing Entry ────────────────────────────────────────────────────────
        with _tab_sw_en:
            _hg = _sw.get("entry", {}).get("hard_gates", {})
            st.markdown('<p class="section-label">Hard Gates — all must pass before scoring</p>', unsafe_allow_html=True)
            _c1, _c2, _c3, _c4, _c5, _c6 = st.columns(6)
            _sw_rs  = _c1.number_input("RS Rank Min",        value=float(_hg.get("rs_rank_min",       70)), min_value=0.0,  max_value=99.0,  step=5.0,  help="Top 30% RS stocks only (Minervini)", key="sw_en_rs")
            _sw_tt  = _c2.number_input("Trend Template Min", value=float(_hg.get("trend_template_min",  5)), min_value=0.0,  max_value=8.0,   step=1.0,  help="5+ of 8 criteria (Minervini)",       key="sw_en_tt")
            _sw_sc  = _c3.number_input("Min Score",          value=float(_hg.get("min_score",          50)), min_value=0.0,  max_value=100.0, step=5.0,                                              key="sw_en_sc")
            _sw_rr  = _c4.number_input("Min R:R",            value=float(_hg.get("min_rr",            1.5)), min_value=1.0,  max_value=5.0,   step=0.5,  help="Min reward:risk ratio",               key="sw_en_rr")
            _sw_vol = _c5.number_input("Volume Gate (×avg)", value=float(_hg.get("volume_ratio_min",  1.3)), min_value=1.0,  max_value=3.0,   step=0.1,  help="Min volume vs 20-day avg",            key="sw_en_vol")
            _sw_mb  = _c6.number_input("Min Bars",           value=float(_hg.get("min_bars",           60)), min_value=20.0, max_value=200.0, step=10.0, help="Min history bars required",           key="sw_en_mb")
            st.markdown('<div style="height:8px"></div>', unsafe_allow_html=True)
            if st.button("💾 Save Swing Entry", type="primary", key="sw_en_save"):
                _new = _copy.deepcopy(_strat_cfg)
                _new["swing"]["entry"]["hard_gates"].update({
                    "rs_rank_min":        _sw_rs,
                    "trend_template_min": int(_sw_tt),
                    "min_score":          _sw_sc,
                    "min_rr":             _sw_rr,
                    "volume_ratio_min":   _sw_vol,
                    "min_bars":           int(_sw_mb),
                })
                save_config(_new)
                st.success("✅ Saved — next scan uses these thresholds.")
                _refresh()

        # ── Swing Exit ─────────────────────────────────────────────────────────
        with _tab_sw_ex:
            _ex = _sw.get("exit", {})
            st.markdown('<p class="section-label">Take Profit & Trailing Stops</p>', unsafe_allow_html=True)
            _p1, _p2, _p3, _p4 = st.columns(4)
            _sw_t1pct  = _p1.number_input("T1 Partial Exit (%)",  value=float(_ex.get("take_profit",    {}).get("partial_sell_pct",    50)), min_value=25.0, max_value=100.0, step=5.0,  help="% to sell when T1 is hit",            key="sw_ex_t1pct")
            _sw_be_r   = _p2.number_input("Break-even after (R)", value=float(_ex.get("trailing_stops", {}).get("breakeven_after_r",  1.0)), min_value=0.5,  max_value=3.0,   step=0.5,  help="Move stop to break-even after N×R",   key="sw_ex_be_r")
            _sw_trail  = _p3.number_input("Trail EMA21 after (%)",value=float(_ex.get("trailing_stops", {}).get("trail_ema21_after_pct",4.0)), min_value=1.0, max_value=15.0, step=0.5,  help="Start trailing stop after N% gain",   key="sw_ex_trail")
            _sw_ob_rsi = _p4.number_input("Overbought RSI Exit",  value=float(_ex.get("overbought",     {}).get("rsi_threshold",       72)), min_value=60.0, max_value=85.0,  step=1.0,  help="Exit when RSI exceeds this + momentum fades", key="sw_ex_ob_rsi")
            st.markdown('<hr style="border:none;border-top:1px solid rgba(255,255,255,.05);margin:12px 0">', unsafe_allow_html=True)
            st.markdown('<p class="section-label">Watch Closely & Stale Trade</p>', unsafe_allow_html=True)
            _w1, _w2, _w3, _w4 = st.columns(4)
            _sw_stale  = _w1.number_input("Stale Exit (days)",   value=float(_ex.get("stale_trade",  {}).get("days",          14)), min_value=5.0,  max_value=30.0, step=1.0,  help="Exit if no move after N days (O'Neil)", key="sw_ex_stale")
            _sw_stm    = _w2.number_input("Stale Min Move (%)",  value=float(_ex.get("stale_trade",  {}).get("min_move_pct",   1.0)), min_value=0.5, max_value=5.0,  step=0.5,  help="Minimum % move to not be considered stale", key="sw_ex_stm")
            _sw_stp_d  = _w3.number_input("Stop Watch Dist (%)", value=float(_ex.get("watch_closely",{}).get("stop_dist_pct", 2.0)), min_value=0.5, max_value=5.0,  step=0.5,  help="Alert when within this % of stop",      key="sw_ex_stpd")
            _sw_ts_rsi = _w4.number_input("Tighten Stop RSI",    value=float(_ex.get("tighten_stop", {}).get("rsi_threshold",  65)), min_value=55.0, max_value=80.0,step=1.0,  help="Tighten stop when RSI reaches this",    key="sw_ex_ts_rsi")
            st.markdown('<div style="height:8px"></div>', unsafe_allow_html=True)
            if st.button("💾 Save Swing Exit", type="primary", key="sw_ex_save"):
                _new = _copy.deepcopy(_strat_cfg)
                _new["swing"]["exit"]["take_profit"]["partial_sell_pct"]               = _sw_t1pct
                _new["swing"]["exit"]["trailing_stops"]["breakeven_after_r"]           = _sw_be_r
                _new["swing"]["exit"]["trailing_stops"]["trail_ema21_after_pct"]       = _sw_trail
                _new["swing"]["exit"]["overbought"]["rsi_threshold"]                   = _sw_ob_rsi
                _new["swing"]["exit"]["stale_trade"]["days"]                           = int(_sw_stale)
                _new["swing"]["exit"]["stale_trade"]["min_move_pct"]                   = _sw_stm
                _new["swing"]["exit"]["watch_closely"]["stop_dist_pct"]                = _sw_stp_d
                _new["swing"]["exit"]["tighten_stop"]["rsi_threshold"]                 = _sw_ts_rsi
                save_config(_new)
                st.success("✅ Saved — next scan uses these thresholds.")
                _refresh()

        # ── Day Trade Entry ────────────────────────────────────────────────────
        with _tab_dt_en:
            _hg = _dt.get("entry", {}).get("hard_gates", {})
            st.markdown('<p class="section-label">Hard Gates — all must pass before scoring</p>', unsafe_allow_html=True)
            _d1, _d2, _d3 = st.columns(3)
            _dt_atr = _d1.number_input("Min ATR % (daily range)", value=float(_hg.get("min_atr_pct",      2.0)), min_value=0.5, max_value=10.0, step=0.5, help="Min daily range as % of price",  key="dt_en_atr")
            _dt_vol = _d2.number_input("Min Volume Ratio (×avg)", value=float(_hg.get("min_volume_ratio",  1.5)), min_value=1.0, max_value=5.0,  step=0.1, help="Min volume vs 20-day average",   key="dt_en_vol")
            _dt_sc  = _d3.number_input("Min Score",               value=float(_hg.get("min_score",        50)), min_value=0.0,  max_value=100.0, step=5.0,                                        key="dt_en_sc")
            st.markdown('<div style="height:8px"></div>', unsafe_allow_html=True)
            if st.button("💾 Save Day Trade Entry", type="primary", key="dt_en_save"):
                _new = _copy.deepcopy(_strat_cfg)
                _new["day_trade"]["entry"]["hard_gates"].update({
                    "min_atr_pct":      _dt_atr,
                    "min_volume_ratio": _dt_vol,
                    "min_score":        _dt_sc,
                })
                save_config(_new)
                st.success("✅ Saved — next scan uses these thresholds.")
                _refresh()

        # ── Day Trade Exit ─────────────────────────────────────────────────────
        with _tab_dt_ex:
            _ex = _dt.get("exit", {})
            st.markdown('<p class="section-label">Intraday Exit Rules — no overnight holds</p>', unsafe_allow_html=True)
            _e1, _e2, _e3, _e4, _e5 = st.columns(5)
            _dt_eod_h   = _e1.number_input("EOD Close Hour",       value=float(_ex.get("eod_close_hour",   15)), min_value=13.0, max_value=15.0, step=1.0,  help="Force-close hour (ET, 24h)",     key="dt_ex_eod_h")
            _dt_eod_m   = _e2.number_input("EOD Close Minute",     value=float(_ex.get("eod_close_minute", 45)), min_value=0.0,  max_value=59.0, step=5.0,  help="Force-close minute",             key="dt_ex_eod_m")
            _dt_stale_m = _e3.number_input("Stale Exit (minutes)", value=float(_ex.get("stale_trade",      {}).get("minutes_no_move", 30)), min_value=5.0, max_value=60.0, step=5.0, help="Exit if flat for N minutes", key="dt_ex_stale_m")
            _dt_stop    = _e4.number_input("Stop ATR Mult",        value=float(_ex.get("stop_atr_mult",   0.75)), min_value=0.25, max_value=2.0, step=0.25, help="Stop = ATR × this",              key="dt_ex_stop")
            _dt_tgt_rr  = _e5.number_input("Target R:R",          value=float(_ex.get("target_rr",        2.0)), min_value=1.0,  max_value=5.0, step=0.5,  help="Target = risk × this",           key="dt_ex_tgt_rr")
            st.markdown('<div style="height:8px"></div>', unsafe_allow_html=True)
            if st.button("💾 Save Day Trade Exit", type="primary", key="dt_ex_save"):
                _new = _copy.deepcopy(_strat_cfg)
                _new["day_trade"]["exit"].update({
                    "eod_close_hour":   int(_dt_eod_h),
                    "eod_close_minute": int(_dt_eod_m),
                    "stop_atr_mult":    _dt_stop,
                    "target_rr":        _dt_tgt_rr,
                })
                _new["day_trade"]["exit"]["stale_trade"]["minutes_no_move"] = int(_dt_stale_m)
                save_config(_new)
                st.success("✅ Saved — next scan uses these thresholds.")
                _refresh()

        # ── Position Sizing (shared) ───────────────────────────────────────────
        st.markdown('<hr style="border:none;border-top:1px solid rgba(255,255,255,.06);margin:20px 0 12px">', unsafe_allow_html=True)
        st.markdown('<p class="section-label">Position Sizing — applies to all trade types</p>', unsafe_allow_html=True)
        _ps1, _ps2 = st.columns(2)
        _ps_risk = _ps1.number_input("Max risk per trade (% of account)", value=float(_ps.get("max_risk_per_trade_pct", 1.0)), min_value=0.25, max_value=5.0,  step=0.25, key="ps_risk")
        _ps_heat = _ps2.number_input("Max portfolio heat (%)",            value=float(_ps.get("max_portfolio_heat_pct", 6.0)), min_value=1.0,  max_value=20.0, step=0.5,  help="Total % at risk across all open positions", key="ps_heat")
        if st.button("💾 Save Sizing", type="primary", key="ps_save"):
            _new = _copy.deepcopy(_strat_cfg)
            _new["position_sizing"]["max_risk_per_trade_pct"] = _ps_risk
            _new["position_sizing"]["max_portfolio_heat_pct"] = _ps_heat
            save_config(_new)
            st.success("✅ Saved")
            _refresh()

        with st.expander("🗂️ View raw strategy_config.json"):
            st.json(_strat_cfg)

    # ── TAB 2: AI REVIEW ─────────────────────────────────────────────────────
    with tab_review:
        if not _closed_all:
            st.info(
                "**No closed trades yet.** The AI review needs your trade history to make "
                "data-backed suggestions. Close your first position to unlock this feature."
            )
        else:
            # Performance snapshot
            st.markdown('<p class="section-label">Your performance snapshot</p>', unsafe_allow_html=True)
            _rv1, _rv2, _rv3, _rv4 = st.columns(4)
            _rv1.metric("Trades",        _perf_stats.get("total_trades",0))
            _rv2.metric("Win Rate",      f"{_perf_stats.get('win_rate',0)}%")
            _rv3.metric("Profit Factor", f"{_perf_stats.get('profit_factor',0)}×")
            _rv4.metric("Total P&L",     f"${_perf_stats.get('total_pnl',0):+,.0f}")

            st.markdown('<div style="height:8px"></div>', unsafe_allow_html=True)
            st.markdown(
                '<div style="background:rgba(59,130,246,.07);border-left:3px solid var(--accent);'
                'border-radius:6px;padding:10px 16px;color:var(--accent);font-size:.83rem;margin-bottom:16px">'
                '🤖 The AI review sends your <b>anonymized</b> performance stats and current config to Claude. '
                'It returns specific, numbered suggestions with exact values to change. '
                'You review each one and decide what to apply — nothing changes automatically.'
                '</div>',
                unsafe_allow_html=True
            )

            _rv_key = "strategy_review_result"
            if st.button("🚀 Run AI Strategy Review", type="primary", use_container_width=False):
                with st.spinner("Analyzing your trade data with Claude…"):
                    _result = run_ai_review(_closed_all, _strat_cfg)
                    if "error" not in _result:
                        save_review(_result, _perf_stats)
                    st.session_state[_rv_key] = _result
                st.rerun()

            _rv_result = st.session_state.get(_rv_key)
            if _rv_result:
                if "error" in _rv_result:
                    err = _rv_result["error"]
                    if err == "no_key":
                        st.warning("🔑 Add `ANTHROPIC_API_KEY=sk-ant-...` to your `.env` file to enable AI review.")
                    elif err == "no_package":
                        st.warning("📦 Run `pip install anthropic` then restart.")
                    else:
                        st.error(f"Review error: {_rv_result.get('message', err)}")
                else:
                    # Grade + summary
                    _grade   = _rv_result.get("grade", "—")
                    _grade_c = {"A": "var(--pos)", "B": "var(--accent)", "C": "#f59e0b", "D": "var(--neg)"}.get(_grade, "var(--faint)")
                    st.markdown(
                        f'<div style="display:flex;align-items:center;gap:14px;margin-bottom:14px">'
                        f'<div style="background:{_grade_c}22;border:2px solid {_grade_c};border-radius:10px;'
                        f'padding:6px 20px;font-size:2rem;font-weight:900;color:{_grade_c}">{_grade}</div>'
                        f'<div>'
                        f'<div style="color:var(--fg);font-weight:700">{_rv_result.get("grade_reason","")}</div>'
                        f'<div style="color:var(--faint);font-size:.83rem;margin-top:4px">{_rv_result.get("summary","")}</div>'
                        f'</div></div>',
                        unsafe_allow_html=True
                    )

                    # What's working
                    _working = _rv_result.get("what_is_working", [])
                    if _working:
                        st.markdown(
                            '<p style="color:var(--pos);font-size:.72rem;font-weight:700;letter-spacing:.08em;margin:0 0 6px">✅ WHAT IS WORKING</p>'
                            + "".join(f'<div style="color:#86efac;font-size:.83rem;margin:3px 0">• {w}</div>' for w in _working),
                            unsafe_allow_html=True
                        )

                    # Biggest risk
                    _risk = _rv_result.get("biggest_risk","")
                    if _risk:
                        st.markdown(
                            f'<div style="background:rgba(239,68,68,.08);border-left:3px solid var(--neg);'
                            f'border-radius:6px;padding:8px 14px;color:#fca5a5;font-size:.83rem;margin:10px 0">'
                            f'⚠️ <b>Biggest risk:</b> {_risk}</div>',
                            unsafe_allow_html=True
                        )

                    # Suggestions
                    _suggs = _rv_result.get("suggestions", [])
                    if _suggs:
                        st.markdown(
                            f'<p style="color:var(--muted);font-size:.72rem;font-weight:700;letter-spacing:.08em;margin:16px 0 10px">'
                            f'SUGGESTIONS ({len(_suggs)}) — review each and apply the ones you agree with</p>',
                            unsafe_allow_html=True
                        )
                        for _idx, _s in enumerate(_suggs):
                            _pri_c = {"high": "var(--neg)", "medium": "#f59e0b", "low": "var(--faint)"}.get(_s.get("priority","low"), "var(--faint)")
                            _ds_c  = {"strong": "var(--pos)", "moderate": "#f59e0b", "insufficient": "var(--faint)"}.get(_s.get("data_support",""), "var(--faint)")
                            with st.expander(
                                f"{'🔴' if _s.get('priority')=='high' else '🟡' if _s.get('priority')=='medium' else '⚪'} "
                                f"{_s.get('title','Suggestion')}  ·  "
                                f"{_s.get('param_path','')}  →  {_s.get('current_value','')} → {_s.get('suggested_value','')}",
                                expanded=(_s.get("priority") == "high")
                            ):
                                st.markdown(
                                    f'<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">'
                                    f'<span style="background:{_pri_c}22;color:{_pri_c};border-radius:4px;padding:2px 10px;font-size:.72rem;font-weight:700">{_s.get("priority","").upper()} PRIORITY</span>'
                                    f'<span style="background:{_ds_c}22;color:{_ds_c};border-radius:4px;padding:2px 10px;font-size:.72rem;font-weight:700">DATA: {_s.get("data_support","").upper()}</span>'
                                    f'<span style="background:rgba(99,102,241,.2);color:var(--accent);border-radius:4px;padding:2px 10px;font-size:.72rem;font-weight:700">{_s.get("trader_basis","")}</span>'
                                    f'</div>'
                                    f'<div style="color:var(--muted);font-size:.88rem;margin-bottom:8px">{_s.get("reason","")}</div>'
                                    f'<div style="color:var(--faint);font-size:.8rem;margin-bottom:12px">📈 Expected impact: <b style="color:var(--muted)">{_s.get("expected_impact","")}</b></div>',
                                    unsafe_allow_html=True
                                )
                                _ac1, _ac2 = st.columns([1, 4])
                                if _ac1.button("✅ Apply", key=f"apply_sugg_{_idx}", type="primary", use_container_width=True):
                                    import copy as _copy
                                    _updated = apply_suggestion(
                                        _strat_cfg,
                                        _s.get("param_path",""),
                                        _s.get("suggested_value")
                                    )
                                    save_config(_updated)
                                    st.success(f"Applied: {_s.get('param_path','')} → {_s.get('suggested_value','')}")
                                    st.session_state.pop(_rv_key, None)
                                    _refresh()
                                _ac2.markdown(
                                    f'<div style="padding:6px 0;color:var(--faint);font-size:.8rem">'
                                    f'Change: <b style="color:var(--fg)">{_s.get("param_path","")}</b> '
                                    f'from <span style="color:var(--neg)">{_s.get("current_value","")}</span> '
                                    f'to <span style="color:var(--pos)">{_s.get("suggested_value","")}</span></div>',
                                    unsafe_allow_html=True
                                )

                    _regime_notes = _rv_result.get("regime_notes","")
                    if _regime_notes:
                        st.markdown(
                            f'<div style="background:rgba(99,102,241,.07);border-left:3px solid var(--accent);'
                            f'border-radius:6px;padding:10px 16px;color:var(--accent);font-size:.82rem;margin-top:12px">'
                            f'🌐 <b>Regime notes:</b> {_regime_notes}</div>',
                            unsafe_allow_html=True
                        )
                    _next = _rv_result.get("next_review_trigger","")
                    if _next:
                        st.caption(f"💡 Next review: {_next}")

    # ── TAB 3: HISTORY ────────────────────────────────────────────────────────
    with tab_history:
        _hist = get_review_history()
        if not _hist:
            st.info("No reviews yet. Run your first AI review to start tracking strategy evolution.")
        else:
            for _rev in reversed(_hist):
                _ts  = _rev.get("timestamp","")[:10]
                _rv  = _rev.get("review", {})
                _sn  = _rev.get("stats_snapshot", {})
                _gr  = _rv.get("grade","—")
                _gc  = {"A":"var(--pos)","B":"var(--accent)","C":"#f59e0b","D":"var(--neg)"}.get(_gr,"var(--faint)")
                with st.expander(
                    f"📅 {_ts}  ·  Grade {_gr}  ·  "
                    f"{_sn.get('total_trades',0)} trades  ·  "
                    f"{_sn.get('win_rate',0)}% WR  ·  "
                    f"${_sn.get('total_pnl',0):+,.0f} P&L"
                ):
                    st.markdown(
                        f'<span style="color:{_gc};font-size:1.5rem;font-weight:900">{_gr}</span>'
                        f'<span style="color:var(--faint);font-size:.83rem;margin-left:10px">{_rv.get("summary","")}</span>',
                        unsafe_allow_html=True
                    )
                    _suggs = _rv.get("suggestions", [])
                    if _suggs:
                        st.markdown(f"**{len(_suggs)} suggestions** were made:")
                        for _s in _suggs:
                            st.markdown(
                                f'<div style="font-size:.82rem;padding:4px 0;border-bottom:1px solid rgba(255,255,255,.04)">'
                                f'<span style="color:var(--muted)">{_s.get("param_path","")}</span>: '
                                f'{_s.get("current_value","")} → '
                                f'<b style="color:var(--pos)">{_s.get("suggested_value","")}</b> '
                                f'<span style="color:var(--faint)">({_s.get("priority","").upper()})</span>'
                                f'</div>',
                                unsafe_allow_html=True
                            )


# ══════════════════════════════════════════════════════════════════════════════
# PAGE — GUIDE (tutorial: what each tool does)
# ══════════════════════════════════════════════════════════════════════════════
elif page == "❓  Guide":
    _page_header("❓ Guide", "What each tool does — and how to get the most out of it")

    st.markdown(
        "Welcome! This app helps you **find and rank swing-trade candidates** and track your trades. "
        "Everything here is for **education only — not financial advice** "
        "(see the disclaimer at the bottom of every page).")

    st.markdown("### ⭐ How the ranking works")
    st.markdown(
        "Every stock the app surfaces is scored on a **consistent set of factors** — the technical setup "
        "(pullback / reversal), the overall market, insider buying/selling, analyst ratings, and the "
        "company's financials & growth trajectory — then given a star rating:")
    st.markdown(
        "- **⭐⭐⭐** — strongest setups (the best of the best)\n"
        "- **⭐⭐** — solid, worth a closer look\n"
        "- **⭐** — weaker / watch only\n\n"
        "More stars = more factors lining up. Stars rank quality, they are **not** a promise of profit — "
        "always do your own research first.")

    st.markdown("### 🧭 The tabs")
    _guide = [
        ("🏠 Dashboard", "Your home base — the market 'weather' (bull/bear regime), your open positions and P&L, and today's top-rated swing picks at a glance."),
        ("📡 Scanner", "The engine room. Scans your watchlist and ranks the best swing-trade candidates by star rating, showing <i>why</i> each scored (pullback, insiders, analyst ratings, fundamentals…)."),
        ("💼 Portfolio", "Track your positions: entry, stop, targets, live price, and profit/loss. Log new trades and close them when you exit."),
        ("👁️ Watchlist", "The list of stocks the app watches and scans. Add or remove tickers to control what shows up in the Scanner."),
        ("🔎 Analyze", "Deep-dive any ticker: live chart, technical indicators, fundamentals, recent news, and an AI read on the setup."),
        ("📅 IPOs", "Upcoming IPO calendar — companies about to go public."),
        ("🏦 Broker", "Connect a brokerage to pull live account holdings. Managed by the app owner."),
        ("⚙️ Strategy", "The rules and thresholds behind every signal, plus an AI review of closed trades. Managed by the app owner."),
    ]
    for _gt, _gd in _guide:
        st.markdown(
            f'<div style="padding:10px 2px;border-bottom:1px solid var(--border)">'
            f'<span style="font-weight:700;color:var(--fg);font-size:1rem">{_gt}</span><br>'
            f'<span style="color:var(--muted);font-size:.9rem">{_gd}</span></div>',
            unsafe_allow_html=True)

    st.markdown("")
    st.info("💡 **New here?** Open **📡 Scanner** to see today's top-rated swing setups, then use "
            "**🔎 Analyze** on any ticker to dig deeper before making your own decision.")


# ── Legal disclaimer — shown at the bottom of every page ──────────────────────
try:
    from legal import render_footer as _render_legal_footer
    _render_legal_footer()
except Exception:
    pass
