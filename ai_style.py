"""ai_style.py — one shared instruction that forces every AI response in the app
into plain, beginner-friendly English.

Import PLAIN_LANGUAGE_RULE into any LLM prompt so that a user who has never heard
of RSI (or any other trading term) still fully understands what the AI is saying.
Keep this as the single source of truth — edit the wording here and every prompt
that imports it updates at once.
"""

PLAIN_LANGUAGE_RULE = (
    "HOW TO WRITE — READ THIS CAREFULLY:\n"
    "- Write for a total beginner who has NEVER traded a stock and does not know any "
    "finance, chart, or trading terms.\n"
    "- Use short, plain, everyday sentences. No Wall Street slang and no abbreviations "
    "the reader might not know.\n"
    "- The FIRST time you use any technical term — for example RSI, ATR, MACD, EMA, ADX, "
    "moving average, support, resistance, volume, gap, momentum, overbought, oversold, "
    "breakout, pullback, R:R (risk/reward), stop, or P&L — explain it in simple words right "
    "after it, in parentheses. "
    "Example: \"RSI is 74 (RSI is a 0-100 speed gauge for the price; above 70 usually means "
    "it has jumped up fast and may need to cool off).\"\n"
    "- When you can, just say the plain meaning instead of the jargon — e.g. write "
    "\"the average price over the last 50 days\" instead of \"the 50-day EMA.\"\n"
    "- Always tell the reader what it MEANS for their decision, not just the raw number.\n"
    "- Keep your confident, direct, specific tone. Simple language does NOT mean vague — "
    "still use the real numbers, just explain them."
)
