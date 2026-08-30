# Veteran's Edge — Task List

---

## ✅ Done

- [x] Moomoo real cash account connected (acc_id `283445331594399121`)
- [x] Entry orders placed through app (market + limit)
- [x] Auto stop raised when position gains (daemon checks every 5 min)
- [x] Auto targets raised when position up >5%
- [x] Cash balance fetched live — blocks orders that exceed available cash
- [x] UI shows cash bar + caps suggested qty to what you can afford
- [x] Position sizing by risk % (default 1%) via `validate_trade()`
- [x] Hard error if R:R < 2:1, warning if < 3:1
- [x] Background daemon — alerts fire even when app is closed
- [x] Alert throttle (600s cooldown per ticker per alert type)
- [x] All alert messages rewritten in casual, direct style
- [x] Fixed 3x duplicate notifications
- [x] Briefing button sends to Telegram correctly
- [x] Batch yfinance download — watchlist scan ~10x faster
- [x] Daemon status badge in sidebar
- [x] Two-way Telegram chatbot powered by Groq (Llama 3.3 70B)
- [x] Intent classifier (Llama 3 8B) — no keyword matching, understands any phrasing
- [x] Conversation memory — supports follow-up questions (last 10 messages)
- [x] Universe scanner (300+ tickers) accessible via Telegram
- [x] Full analyzer (technical + fundamental) accessible via Telegram
- [x] Scan + analysis cross-validated — analysis overrides scan if contradicted
- [x] HTML formatting fixed — no raw `**asterisks**`
- [x] Add/remove watchlist tickers via Telegram text
- [x] RS rank (0–99 percentile vs SPY) on every scanner result
- [x] Minervini Trend Template (0–8 criteria) on every scanner result
- [x] Scanner sorts by TT tier → RS rank → stars (best quality first)
- [x] RS and TT badges shown on scanner cards in app (color-coded)
- [x] Telegram bot includes RS + TT in scan results
- [x] **VCP detection** — detect progressively tighter price contractions, volume dry-up, score quality (1–3 stars), shown as separate tab in scanner and included in Telegram bot scan results
- [x] **Performance dashboard** — equity curve, win rate by setup type, sector breakdown, RS/quality breakdown, lessons learned, trade log; Groq bot references your history when making calls
- [x] **Scheduled morning briefing** — auto-fires at 8:30 AM ET via daemon; covers regime, positions, top watchlist setups, VCPs, performance edge, earnings this week; ☀️ button to send on demand

---

## 🔲 To Do
- [x] **Trade execution via Telegram** — stop/target alerts include YES/NO prompt; YES executes market order via Moomoo; NO lets you adjust qty then re-confirm; manual "buy 10 NVDA" / "sell PLUG" commands also supported
- [x] **Smart pullback adds** — alerts when winning managed position pulls back to EMA20 with volume dry-up; suggests qty (1% risk budget vs available cash); 4-hour cooldown; YES executes via Moomoo, NO adjusts qty
- [x] **Staged exits + time stop** — T1 hit: sell half via YES/NO, stop moves to breakeven; T2 hit: sell half of remainder, trailing 5% stop activates on rest; 10-day flat alert prompts exit; all wired to Moomoo execution
- [ ] **Trade execution via Telegram** — bot sends "stop hit on PLUG, exit? reply YES" and fires market sell through Moomoo on YES reply
- [ ] **Scheduled morning briefing** — auto-fires at 8:30 AM ET, no need to text it
- [ ] **Performance dashboard** — win rate by setup type, avg hold time, equity curve, best/worst trades
- [ ] **Paper trading mode** — test signals without real money

---

## 💡 Backlog (lower priority)

- [ ] Options unusual activity / flow tracking on watchlist tickers
- [ ] Backtesting — run signal logic against historical data
