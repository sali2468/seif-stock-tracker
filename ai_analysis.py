"""
AI-powered stock analysis — StockPal
Combines live chart indicators + recent news headlines into a plain-English
recommendation using the Claude API.

Uses Claude (ANTHROPIC_API_KEY) when available, otherwise falls back to
Groq / Llama 3.3 70B (GROQ_API_KEY). Set either one in .env.
"""

import os
import json
import re
import numpy as np
from typing import Optional

from ai_style import PLAIN_LANGUAGE_RULE

# Read the module-level snapshot for reference, but the functions below read the
# env at CALL time so key availability doesn't depend on import order vs load_dotenv.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


def _anthropic_key() -> str:
    return os.getenv("ANTHROPIC_API_KEY", "").strip()


def _groq_key() -> str:
    return os.getenv("GROQ_API_KEY", "").strip()


def _ai_available() -> bool:
    """True if at least one LLM provider key is configured."""
    return bool(_anthropic_key() or _groq_key())


def _llm_complete(prompt: str, max_tokens: int = 900) -> str:
    """
    Send a prompt to whichever LLM provider is configured and return the raw text.
    Prefers Claude (best quality); falls back to Groq / Llama 3.3 70B when only
    GROQ_API_KEY is set. Raises RuntimeError on any failure so the caller's
    try/except turns it into a friendly api_error.
    """
    anthropic_key = _anthropic_key()
    groq_key      = _groq_key()

    if anthropic_key:
        try:
            import anthropic
        except ImportError:
            raise RuntimeError("Run: pip install anthropic")
        client = anthropic.Anthropic(api_key=anthropic_key)
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return (msg.content[0].text or "").strip()

    if groq_key:
        try:
            from groq import Groq
        except ImportError:
            raise RuntimeError("Run: pip install groq")
        client = Groq(api_key=groq_key)
        kw = dict(
            model="llama-3.3-70b-versatile",
            max_tokens=max_tokens,
            temperature=0.3,   # lower = more reliable, better-formed JSON
            messages=[
                {"role": "system", "content":
                 "You are a professional swing trader. Respond ONLY with one valid "
                 "JSON object matching the requested schema - no markdown, no extra text."},
                {"role": "user", "content": prompt},
            ],
        )
        try:
            resp = client.chat.completions.create(response_format={"type": "json_object"}, **kw)
        except Exception:
            # JSON-mode 400s if the model emits slightly malformed JSON; retry
            # without it and let the caller's lenient regex/json parser recover.
            resp = client.chat.completions.create(**kw)
        return (resp.choices[0].message.content or "").strip()

    raise RuntimeError("No AI provider configured - add ANTHROPIC_API_KEY or GROQ_API_KEY to .env.")


# ── News fetcher ──────────────────────────────────────────────────────────────

def get_ticker_news(ticker: str, max_items: int = 10) -> str:
    """
    Pull recent news headlines + summaries via yfinance.
    Returns formatted plain text ready for the AI prompt.
    """
    try:
        import yfinance as yf
        raw = yf.Ticker(ticker).news or []
        lines = []
        for item in raw[:max_items]:
            content = item.get("content", {})
            title   = (content.get("title") or item.get("title", "")).strip()
            summary = (content.get("summary") or item.get("summary", "")).strip()
            pub     = (content.get("pubDate") or "").split("T")[0]

            if not title:
                continue
            line = f"• [{pub}] {title}"
            if summary:
                # Trim to first 2 sentences max
                sents = [s.strip() for s in summary.replace("\n", " ").split(".") if s.strip()]
                short = ". ".join(sents[:2]) + "."
                line += f"\n  {short[:280]}"
            lines.append(line)
        return "\n".join(lines) if lines else "No recent news found."
    except Exception:
        return "Could not fetch news."


# ── Indicator builder ─────────────────────────────────────────────────────────

def build_context(ticker: str, position: Optional[dict] = None) -> dict:
    """
    Compute all key technical indicators for the ticker.
    Returns a flat dict ready to be embedded in the AI prompt.
    """
    try:
        from market_data import get_bars, compute_indicators, ema200_slope
        df = get_bars(ticker, "6mo", "1d")
        if df is None or len(df) < 20:
            return {}

        df   = compute_indicators(df)
        d    = df.iloc[-1]
        prev = df.iloc[-2]

        price  = float(d["close"])
        atr    = float(d.get("atr",       np.nan))
        rsi    = float(d.get("rsi",       np.nan))
        adx    = float(d.get("adx",       np.nan))
        macd_h = float(d.get("macd_hist", np.nan))
        pmh    = float(prev.get("macd_hist", np.nan))
        ema20  = float(d.get("ema20",     np.nan))
        ema50  = float(d.get("ema50",     np.nan))
        ema200 = float(d.get("ema200",    np.nan))
        vr     = float(d.get("vol_ratio", np.nan))
        slope  = ema200_slope(df)

        macd_trend_5 = [round(float(x), 3) for x in df["macd_hist"].tail(5).tolist()]
        day_chg      = round((price - float(prev["close"])) / float(prev["close"]) * 100, 2)
        today_open   = float(d.get("open", price))
        gap_pct      = round((today_open - float(prev["close"])) / float(prev["close"]) * 100, 2)

        ctx = {
            "price":        round(price, 2),
            "day_change":   day_chg,
            "gap_pct":      gap_pct,
            "rsi":          round(rsi, 1),
            "adx":          round(adx, 1),
            "atr":          round(atr, 2),
            "atr_pct":      round(atr / price * 100, 1) if price else 0,
            "macd_hist":    round(macd_h, 4),
            "macd_hist_prev": round(pmh, 4),
            "macd_direction": "fading" if macd_h < pmh else "building",
            "macd_trend_5d": macd_trend_5,
            "vol_ratio":    round(vr, 2),
            "ema20":        round(ema20, 2),
            "ema50":        round(ema50, 2),
            "ema200":       round(ema200, 2) if not np.isnan(ema200) else None,
            "vs_ema20":     "above" if price > ema20 else "below",
            "vs_ema50":     "above" if price > ema50 else "below",
            "slope_200d":   slope,
            "low_5d":       round(float(df["low"].tail(5).min()), 2),
            "low_10d":      round(float(df["low"].tail(10).min()), 2),
            "high_20d":     round(float(df["high"].tail(20).max()), 2),
            "today_high":   round(float(d.get("high", price)), 2),
            "today_low":    round(float(d.get("low",  price)), 2),
        }

        # Position context (if holding)
        if position:
            pnl_pct = round((price - position["entry"]) / position["entry"] * 100, 2)
            pnl_dol = round((price - position["entry"]) * position.get("qty", 1), 2)
            cushion = round((price - position["stop"]) / price * 100, 2)
            ctx["position"] = {
                "entry":          position["entry"],
                "stop":           position["stop"],
                "target1":        position.get("target1", 0),
                "target2":        position.get("target2", 0),
                "qty":            position.get("qty", 1),
                "days_held":      position.get("days_held", 0),
                "pnl_pct":        pnl_pct,
                "pnl_dollars":    pnl_dol,
                "cushion_pct":    cushion,
                "stop_atr_ratio": round((price - position["stop"]) / atr, 2) if atr else 0,
            }

        return ctx

    except Exception as e:
        return {}


# ── Main AI analysis ──────────────────────────────────────────────────────────

def analyze(ticker: str,
            position: Optional[dict] = None,
            company_info: Optional[dict] = None) -> dict:
    """
    Full AI stock analysis.

    Parameters
    ----------
    ticker       : stock symbol (e.g. "RKLB")
    position     : optional dict with keys entry, stop, target1, target2, qty, days_held
    company_info : optional dict with keys name, sector, description

    Returns
    -------
    dict with keys:
        action, confidence, headline, reasoning,
        risks, catalysts, suggested_stop, outlook
    OR  {"error": ..., "message": ...} on failure.
    """
    if not _ai_available():
        return {
            "error": "no_key",
            "message": "Add ANTHROPIC_API_KEY (or GROQ_API_KEY) to your .env file.",
        }

    ctx  = build_context(ticker, position)
    if not ctx:
        return {"error": "no_data", "message": f"Could not load chart data for {ticker}."}

    news = get_ticker_news(ticker)
    co_name   = company_info.get("name",    ticker) if company_info else ticker
    co_sector = company_info.get("sector",  "")     if company_info else ""
    co_desc   = company_info.get("description", "") if company_info else ""

    # ── Position block ────────────────────────────────────────────────────────
    pos_block = ""
    if "position" in ctx:
        p = ctx["position"]
        pos_block = f"""
=== OPEN POSITION ===
Entry ${p['entry']:.2f}  ·  Now ${ctx['price']:.2f}  ·  P&L {p['pnl_pct']:+.2f}% (${p['pnl_dollars']:+.2f})
Stop ${p['stop']:.2f}  ·  T1 ${p['target1']:.2f}  ·  T2 ${p['target2']:.2f}
Cushion to stop: {p['cushion_pct']:.2f}%  ·  Stop is {p['stop_atr_ratio']:.1f} ATR units below price
Days held: {p['days_held']}
"""

    # ── Prompt ────────────────────────────────────────────────────────────────
    prompt = f"""You are a professional swing trader and market analyst with 20 years of experience.
Analyze {ticker} and give a clear, direct, honest recommendation. Do not hedge. Be specific.

{PLAIN_LANGUAGE_RULE}

This is a LONG-ONLY account: we only ever buy stock we expect to rise. Never recommend
short-selling, put options, inverse/bearish products, or any position that profits from a
decline. "SELL" means exit an existing holding (or, if the person is flat, stay out / avoid) —
it never means open a short. If a stock looks weak and no position is held, say WATCH or advise
avoiding it — do not suggest shorting.

=== COMPANY ===
{co_name} | {co_sector}
{co_desc[:200] if co_desc else ""}

=== TECHNICAL SNAPSHOT ===
Price: ${ctx['price']:.2f}  |  Day: {ctx['day_change']:+.2f}%  |  Gap at open: {ctx['gap_pct']:+.2f}%
Today: High ${ctx['today_high']:.2f} / Low ${ctx['today_low']:.2f}
RSI: {ctx['rsi']}  |  ADX: {ctx['adx']}  |  ATR: ${ctx['atr']:.2f} ({ctx['atr_pct']}% of price)
MACD histogram (5 days): {ctx['macd_trend_5d']} → momentum is {ctx['macd_direction'].upper()}
Volume: {ctx['vol_ratio']}x vs 20-day average
EMA20: ${ctx['ema20']:.2f} (price is {ctx['vs_ema20']})  |  EMA50: ${ctx['ema50']:.2f} (price is {ctx['vs_ema50']})
200-day trend: {ctx['slope_200d']}
5-day low: ${ctx['low_5d']:.2f}  |  10-day low: ${ctx['low_10d']:.2f}  |  20-day high: ${ctx['high_20d']:.2f}
{pos_block}
=== RECENT NEWS & CATALYSTS ===
{news}

=== YOUR TASK ===
Give a recommendation. Consider ALL of the above — technicals, position risk, AND news/events.
If there is an upcoming event in the news (IPO, earnings, contract, product launch, macro event)
that directly affects this stock, call it out explicitly and factor it into your recommendation.

If the person is holding a position, tell them clearly: HOLD or SELL and exactly why.
Consider stop proximity relative to ATR — if the stop is less than 1 ATR away on a volatile stock, that is a meaningful risk.

Respond ONLY with valid JSON, no extra text:
{{
  "action": "BUY" | "HOLD" | "SELL" | "WATCH",
  "confidence": "High" | "Medium" | "Low",
  "headline": "One direct punchy sentence — your main call",
  "reasoning": "3-5 sentences. Reference specific numbers and news. Be direct.",
  "risks": ["specific risk 1", "specific risk 2", "specific risk 3"],
  "catalysts": ["specific catalyst 1", "specific catalyst 2"],
  "suggested_stop": <number or null>,
  "outlook": "e.g. Short-term bearish / Long-term bullish"
}}"""

    try:
        raw = _llm_complete(prompt, max_tokens=1000)

        # Extract JSON robustly (model might add markdown fences)
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            return json.loads(match.group())

        # If JSON parse fails, return the raw text in reasoning
        return {
            "action":     "UNKNOWN",
            "confidence": "Low",
            "headline":   "Analysis returned unexpected format",
            "reasoning":  raw,
            "risks":      [],
            "catalysts":  [],
            "suggested_stop": None,
            "outlook":    "",
        }

    except Exception as e:
        return {"error": "api_error", "message": str(e)}


# ── Entry-specific analysis (scanner deep-dive) ───────────────────────────────

def analyze_entry(ticker: str,
                  signal: Optional[dict] = None,
                  company_info: Optional[dict] = None,
                  fundamentals: Optional[dict] = None) -> dict:
    """
    Pre-trade entry analysis for a specific scanner signal.

    Parameters
    ----------
    ticker       : stock symbol
    signal       : dict with price, stop, target1, target2, rr, stars, warnings, live_price, live_rr
    company_info : dict with name, sector, description
    fundamentals : dict from get_fundamental_snapshot() — analyst, earnings, news, insider, financials

    Returns
    -------
    dict with keys:
        verdict    ('ENTER' | 'PASS' | 'WAIT')
        confidence ('High' | 'Medium' | 'Low')
        headline   (one punchy sentence)
        reasoning  (3-5 sentences, specific numbers)
        risks      (list of strings)
        catalysts  (list of strings)
        trade_tip  (specific tactical advice e.g. limit order, timing)
    OR  {"error": ..., "message": ...} on failure.
    """
    if not _ai_available():
        return {"error": "no_key",
                "message": "Add ANTHROPIC_API_KEY (or GROQ_API_KEY) to your .env file."}

    ctx = build_context(ticker)
    if not ctx:
        return {"error": "no_data", "message": f"Could not load chart data for {ticker}."}

    news = get_ticker_news(ticker, max_items=8)
    co_name   = company_info.get("name",        ticker) if company_info else ticker
    co_sector = company_info.get("sector",       "")    if company_info else ""
    co_desc   = company_info.get("description",  "")    if company_info else ""

    # Signal block
    sig_block = ""
    if signal:
        live_p  = signal.get("live_price", signal.get("price", ctx["price"]))
        live_rr = signal.get("live_rr",    signal.get("rr", "—"))
        sig_block = f"""
=== PROPOSED TRADE SETUP ===
Signal price: ${signal.get('price', '—'):.2f}   Live price NOW: ${live_p:.2f}
Stop loss:    ${signal.get('stop',  '—'):.2f}   Risk: {signal.get('stop_pct','—')}% below entry
Target 1:     ${signal.get('target1','—'):.2f}  Gain to T1: {signal.get('gain_pct','—')}%
Target 2:     ${signal.get('target2','—'):.2f}
Original R:R: {signal.get('rr','—')}:1   Live R:R at current price: {live_rr}:1
Conviction:   {'⭐' * signal.get('stars', 1)} ({signal.get('stars', 1)}/3 stars)
Warnings:     {'; '.join(signal.get('warnings', [])) or 'None'}
"""

    # Fundamentals block
    fund_block = ""
    if fundamentals:
        a = fundamentals.get("analyst", {})
        e = fundamentals.get("earnings", {})
        ins = fundamentals.get("insider", {})
        fins = fundamentals.get("financials", {})
        dte = e.get("days_until", 999)
        surprise = e.get("last_surprise")
        fund_block = f"""
=== FUNDAMENTALS ===
Analyst consensus: {a.get('recommendation','—').replace('_',' ').title()} ({a.get('num_analysts',0)} analysts)
Analyst avg target: ${a.get('target_mean') or '—'}  High: ${a.get('target_high') or '—'}  Low: ${a.get('target_low') or '—'}
Earnings: {'in ' + str(dte) + ' days (' + str(e.get('next_date','')) + ')' if 0 < dte <= 90 else 'None in next 90 days'}
Last earnings surprise: {(str(surprise) + '%') if surprise is not None else '—'}
Insider activity: {ins.get('signal','—')} ({ins.get('summary','—')})
Revenue growth YoY: {str(fins.get('revenue_growth_yoy')) + '%' if fins.get('revenue_growth_yoy') is not None else '—'}
Profit margin: {str(round(fins.get('profit_margin',0),1)) + '%' if fins.get('profit_margin') else '—'}
P/E ratio: {fins.get('pe_ratio','—')}
"""

    prompt = f"""You are a professional swing trader and market analyst with 20 years of experience.
A scanner flagged {ticker} as a potential buy. Your job: decide if a trader should ENTER this trade right now, WAIT for a better entry, or PASS entirely.
This is a LONG-ONLY account — the only trade is buying to open. Never suggest short-selling, puts, or profiting from a decline; if the setup is weak, the answer is WAIT or PASS.

{PLAIN_LANGUAGE_RULE}

=== COMPANY ===
{co_name} | {co_sector}
{co_desc[:250] if co_desc else "(no description)"}
{sig_block}
=== TECHNICAL SNAPSHOT ===
Price: ${ctx['price']:.2f}  |  Day: {ctx['day_change']:+.2f}%  |  Gap at open: {ctx['gap_pct']:+.2f}%
Today: High ${ctx['today_high']:.2f} / Low ${ctx['today_low']:.2f}
RSI: {ctx['rsi']}  |  ADX: {ctx['adx']}  |  ATR: ${ctx['atr']:.2f} ({ctx['atr_pct']}% of price)
MACD histogram (last 5 days): {ctx['macd_trend_5d']} → momentum is {ctx['macd_direction'].upper()}
Volume: {ctx['vol_ratio']}x vs 20-day average
EMA20: ${ctx['ema20']:.2f} ({ctx['vs_ema20']})  |  EMA50: ${ctx['ema50']:.2f} ({ctx['vs_ema50']})
200-day trend: {ctx['slope_200d']}
5-day low: ${ctx['low_5d']:.2f}  |  10-day low: ${ctx['low_10d']:.2f}  |  20-day high: ${ctx['high_20d']:.2f}
{fund_block}
=== RECENT NEWS ===
{news}

=== YOUR TASK ===
Give a direct, honest entry verdict for a swing trade. Be specific — reference actual numbers from the data above.
Consider: Is the R:R still valid at the live price? Is momentum aligned? Any catalysts or risks from news?
Does the technical setup AND fundamental picture agree? Is there a better entry point to wait for?

Respond ONLY with valid JSON, no extra text:
{{
  "verdict": "ENTER" | "WAIT" | "PASS",
  "confidence": "High" | "Medium" | "Low",
  "headline": "One punchy direct sentence — your main call with the key reason",
  "reasoning": "3-5 sentences. Use specific numbers (RSI, ADX, vol_ratio, news). Don't be vague.",
  "risks": ["specific risk 1 with number/data", "specific risk 2", "specific risk 3"],
  "catalysts": ["specific catalyst 1", "specific catalyst 2"],
  "trade_tip": "Specific tactical advice — limit order price, timing, position sizing nuance, or what to watch for"
}}"""

    try:
        raw = _llm_complete(prompt, max_tokens=900)
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            return json.loads(match.group())
        return {
            "verdict":    "UNKNOWN",
            "confidence": "Low",
            "headline":   "Analysis returned unexpected format",
            "reasoning":  raw,
            "risks":      [],
            "catalysts":  [],
            "trade_tip":  "",
        }
    except Exception as e:
        return {"error": "api_error", "message": str(e)}


# ── Position management analysis (portfolio deep-dive) ────────────────────────

def analyze_position(ticker: str,
                     position: dict,
                     company_info: Optional[dict] = None,
                     fundamentals: Optional[dict] = None) -> dict:
    """
    Position management analysis for an existing holding.

    Parameters
    ----------
    ticker       : stock symbol
    position     : dict with entry, stop, target1, target2, qty, days_held
    company_info : dict with name, sector, description
    fundamentals : dict from get_fundamental_snapshot()

    Returns
    -------
    dict with keys:
        action          ('HOLD' | 'TIGHTEN_STOP' | 'TAKE_PARTIAL' | 'EXIT_NOW')
        confidence      ('High' | 'Medium' | 'Low')
        headline        (one direct sentence)
        reasoning       (3-5 sentences with specific numbers)
        risks           (list)
        catalysts       (list)
        suggested_stop  (new stop price or null)
        next_target     (next price target or null)
        trade_tip       (specific tactical advice)
    OR  {"error": ..., "message": ...} on failure.
    """
    if not _ai_available():
        return {"error": "no_key",
                "message": "Add ANTHROPIC_API_KEY (or GROQ_API_KEY) to your .env file."}

    ctx = build_context(ticker, position)
    if not ctx:
        return {"error": "no_data", "message": f"No chart data for {ticker}."}

    news      = get_ticker_news(ticker, max_items=6)
    co_name   = company_info.get("name",        ticker) if company_info else ticker
    co_sector = company_info.get("sector",       "")    if company_info else ""
    co_desc   = company_info.get("description",  "")    if company_info else ""

    pos      = ctx.get("position", {})
    pnl_pct  = pos.get("pnl_pct",      0)
    cushion  = pos.get("cushion_pct",  0)
    days_h   = pos.get("days_held",    0)

    fund_block = ""
    if fundamentals:
        a    = fundamentals.get("analyst",    {})
        e    = fundamentals.get("earnings",   {})
        ins  = fundamentals.get("insider",    {})
        fi   = fundamentals.get("financials", {})
        dte  = e.get("days_until", 999)
        surp = e.get("last_surprise")
        fund_block = f"""
=== FUNDAMENTALS ===
Analyst: {a.get('recommendation','—').replace('_',' ').title()} ({a.get('num_analysts',0)} analysts) · Avg target ${a.get('target_mean') or '—'}
Next earnings: {'⚠️ in ' + str(dte) + ' days — earnings risk!' if 0 < dte <= 7 else 'in ' + str(dte) + ' days' if 0 < dte <= 60 else 'none soon'}
Last EPS surprise: {(str(surp)+'%') if surp is not None else '—'}
Insider activity: {ins.get('signal','—')} ({ins.get('summary','—')})
Revenue growth YoY: {str(fi.get('revenue_growth_yoy'))+'%' if fi.get('revenue_growth_yoy') is not None else '—'}
Profit margin: {str(round(fi.get('profit_margin',0),1))+'%' if fi.get('profit_margin') else '—'}
"""

    prompt = f"""You are a professional swing trader and position manager with 20 years of experience.
The trader currently holds {ticker} and needs your honest recommendation on what to do with this position RIGHT NOW.
This is a LONG-ONLY account — the trader owns shares. Recommendations only cover managing or exiting this long (hold, trim, tighten stop, exit). Never suggest shorting, puts, or adding a bearish/inverse position.

{PLAIN_LANGUAGE_RULE}

=== COMPANY ===
{co_name} | {co_sector}
{co_desc[:200] if co_desc else ""}

=== OPEN POSITION ===
Entry: ${position['entry']:.2f}  |  Current price: ${ctx['price']:.2f}
Unrealized P&L: {pnl_pct:+.2f}%
Stop: ${position['stop']:.2f} ({cushion:.1f}% cushion from price)
Target 1: ${position.get('target1',0):.2f}  |  Target 2: ${position.get('target2',0):.2f}
Shares held: {position.get('qty',1)}  |  Days in trade: {days_h}

=== TECHNICAL SNAPSHOT ===
RSI: {ctx['rsi']}  |  ADX: {ctx['adx']}  |  ATR: ${ctx['atr']:.2f} ({ctx['atr_pct']}% of price)
MACD momentum: {ctx['macd_direction'].upper()} (last 5 bars: {ctx['macd_trend_5d']})
Volume: {ctx['vol_ratio']}x vs 20-day avg
Price vs EMA20: {ctx['vs_ema20']}  |  vs EMA50: {ctx['vs_ema50']}
200-day trend: {ctx['slope_200d']}
Stop is {pos.get('stop_atr_ratio',0):.1f} ATR units below current price
{fund_block}
=== RECENT NEWS ===
{news}

=== YOUR TASK ===
Give a direct, honest position management recommendation with specific reasoning.
Choose ONE action:
• HOLD — setup still valid, momentum intact, let it run
• TIGHTEN_STOP — raise the stop to protect gains or reduce risk
• TAKE_PARTIAL — sell partial shares to lock in some profit
• EXIT_NOW — close the position immediately (stop too close, thesis broken, or big risk event)

Key rules:
- If earnings are in ≤7 days, strongly consider EXIT_NOW or TAKE_PARTIAL to avoid earnings risk
- If P&L >15% and MACD is fading, recommend TAKE_PARTIAL
- If stop is <1 ATR below current price, recommend TIGHTEN_STOP with a specific new price
- If the original trade thesis is broken (price below key EMA, MACD bearish cross), EXIT_NOW
- Reference specific numbers (RSI, P&L%, ATR, news events) in your reasoning

Respond ONLY with valid JSON, no extra text:
{{
  "action": "HOLD" | "TIGHTEN_STOP" | "TAKE_PARTIAL" | "EXIT_NOW",
  "confidence": "High" | "Medium" | "Low",
  "headline": "One direct sentence — your main call with the single most important reason",
  "reasoning": "3-5 sentences. Reference P&L%, RSI, MACD direction, news. Be specific and direct.",
  "risks": ["specific risk 1 with data", "specific risk 2", "specific risk 3"],
  "catalysts": ["specific catalyst 1", "specific catalyst 2"],
  "suggested_stop": <new stop price number if TIGHTEN_STOP, otherwise null>,
  "next_target": <next price target to watch, or null>,
  "trade_tip": "Specific tactic — exact order type, timing, sizing, or what level to watch"
}}"""

    try:
        raw = _llm_complete(prompt, max_tokens=900)
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            return json.loads(match.group())
        return {
            "action":        "HOLD",
            "confidence":    "Low",
            "headline":      "Analysis returned unexpected format",
            "reasoning":     raw,
            "risks":         [],
            "catalysts":     [],
            "suggested_stop": None,
            "next_target":   None,
            "trade_tip":     "",
        }
    except Exception as e:
        return {"error": "api_error", "message": str(e)}
