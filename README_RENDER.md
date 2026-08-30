# Deploying StockPal on Render

This runs the app from GitHub at a public URL (and your own domain), with accounts
that persist — replacing the ngrok tunnel and your laptop having to stay on.

## Before you start
- A GitHub account, with this folder pushed to a repo.
- A Render account (render.com) — sign in with GitHub.
- ~**$7/mo** (Render **Starter** plan — required for the persistent disk that keeps
  accounts; the free plan has no disk and would wipe data on every restart).
- Your secret values from the current `.env` (Finnhub, Anthropic, Telegram, ADMIN_USER).

## Step 1 — Generate the broker encryption key (once)
Run locally and copy the output:
```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
You'll paste it as `BROKER_ENC_KEY` on Render. **Save it somewhere safe forever** —
losing it makes saved broker connections undecryptable.

## Step 2 — Push this folder to GitHub
```
cd "C:\Day Trading\render-deploy"
git init
git add -A
git commit -m "StockPal - Render deploy"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```
Confirm `.env`, `user_data/`, and `app_data/` are NOT pushed (they're gitignored).

## Step 3 — Create the service on Render
1. render.com → **New → Blueprint**.
2. Connect GitHub, pick this repo. Render reads `render.yaml` and proposes a **web
   service + a 1 GB disk**. Click **Apply**.

## Step 4 — Set your secrets
Open the service → **Environment** tab → fill the values shown blank (`sync: false`):
- `ADMIN_USER`, `FINNHUB_API_KEY`, `ANTHROPIC_API_KEY`
- `BROKER_ENC_KEY` (from Step 1)
- optional: `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `SMTP_*`

`DATA_DIR`, `PYTHON_VERSION`, and `SESSION_SECRET` are already set for you. Save →
Render redeploys automatically.

## Step 5 — First run + prove data persists
- Open the Render URL (e.g. `https://stockpal.onrender.com`).
- Register a test account. Then in Render click **Manual Deploy → Deploy latest**
  (or **Restart**). Log back in — the account is still there. That confirms the disk
  is persisting data (the whole point of the paid plan).

## Step 6 — Your own domain (optional)
- Buy a domain (Namecheap / Cloudflare).
- Render → your service → **Settings → Custom Domains** → add `yourdomain.com` →
  create the DNS record it shows, at your registrar. Render issues HTTPS automatically.

## Step 7 — Retire ngrok
- On your PC, stop `ngrok_watchdog` and remove the Startup VBS so it doesn't relaunch.

## What does NOT run on Render (by design)
- **Moomoo** broker — needs OpenD running on a local PC. **Alpaca / Tradier / Webull**
  (all API-key REST) work fine in the cloud.
- **Background alert daemon + Telegram poller** — the web service only runs the app.
  To keep alerts running, either add a Render **Background Worker** for
  `monitor_daemon.py` (advanced), or keep running the daemon on your PC.

## Troubleshooting
- **Caches reset on redeploy** (company_cache.json, .swing_factors_cache.json, etc.) —
  that's fine, they regenerate. Only accounts / positions / broker keys need to persist,
  and those live on the disk at `DATA_DIR=/var/data`.
- **Page loads but never connects** (websocket blocked) — set
  `enableXsrfProtection = false` in `.streamlit/config.toml`, commit, and redeploy.
- **Build fails on a package** — check it's pinned in `requirements.txt`.
