"""
brokers.py — pluggable multi-broker layer.

One BrokerAdapter interface + concrete adapters for the brokers with open APIs
(Moomoo, Alpaca, Tradier, IBKR). Users connect their preferred broker to view
and manage their portfolio in-app. A broker aggregator (SnapTrade / Plaid) can
be added later as just another adapter implementing the same interface.

Credentials are stored locally in broker_config.json (gitignored) — never in
source. Single-user app for now.
"""

import json
import os
import logging
from abc import ABC, abstractmethod
from typing import Optional, Tuple, List

log = logging.getLogger("brokers")
_CFG = os.path.join(os.path.dirname(__file__), "broker_config.json")

BROKERS = ["Moomoo", "Alpaca", "Tradier", "Webull", "IBKR"]


# ── Local credential store (gitignored) ───────────────────────────────────────

def load_creds() -> dict:
    try:
        with open(_CFG) as f:
            return json.load(f)
    except Exception:
        return {}


def save_creds(broker: str, creds: dict) -> None:
    data = load_creds()
    data[broker] = creds
    try:
        with open(_CFG, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning("save_creds failed: %s", e)


def clear_creds(broker: str) -> None:
    data = load_creds()
    data.pop(broker, None)
    try:
        with open(_CFG, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ── Interface ─────────────────────────────────────────────────────────────────

class BrokerAdapter(ABC):
    name = "broker"

    @abstractmethod
    def connect(self) -> Tuple[bool, str]:
        """Return (ok, message)."""

    @abstractmethod
    def get_account(self) -> Optional[dict]:
        """Return {cash, equity, buying_power} or None."""

    @abstractmethod
    def get_positions(self) -> List[dict]:
        """Return [{ticker, qty, avg_cost, mkt_value, pnl}]."""


# ── Alpaca (REST) ─────────────────────────────────────────────────────────────

class AlpacaAdapter(BrokerAdapter):
    name = "Alpaca"

    def __init__(self, key: str, secret: str, paper: bool = True):
        self.key, self.secret = key, secret
        self.base = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"

    def _h(self):
        return {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}

    def connect(self):
        import requests
        try:
            r = requests.get(f"{self.base}/v2/account", headers=self._h(), timeout=8)
            return (r.status_code == 200,
                    "Connected" if r.status_code == 200 else f"{r.status_code}: {r.text[:80]}")
        except Exception as e:
            return False, str(e)

    def list_account_options(self):
        # Alpaca keys map to exactly one account — return it for display only.
        import requests
        try:
            a = requests.get(f"{self.base}/v2/account", headers=self._h(), timeout=8).json()
            num = a.get("account_number") or a.get("id") or ""
            if not num:
                return []
            env = "paper" if "paper-api" in self.base else "live"
            return [{"id": str(num), "label": f"Alpaca {env}   (…{str(num)[-6:]})"}]
        except Exception:
            return []

    def get_account(self):
        import requests
        try:
            a = requests.get(f"{self.base}/v2/account", headers=self._h(), timeout=8).json()
            return {"cash": float(a.get("cash", 0)), "equity": float(a.get("equity", 0)),
                    "buying_power": float(a.get("buying_power", 0))}
        except Exception:
            return None

    def get_positions(self):
        import requests
        try:
            ps = requests.get(f"{self.base}/v2/positions", headers=self._h(), timeout=8).json()
            return [{"ticker": p["symbol"], "qty": float(p["qty"]),
                     "avg_cost": float(p["avg_entry_price"]),
                     "mkt_value": float(p["market_value"]),
                     "pnl": float(p["unrealized_pl"])} for p in ps]
        except Exception:
            return []


# ── Tradier (REST) ────────────────────────────────────────────────────────────

class TradierAdapter(BrokerAdapter):
    name = "Tradier"

    def __init__(self, token: str, account_id: str, paper: bool = True):
        self.token, self.acct = token, account_id
        self.base = "https://sandbox.tradier.com" if paper else "https://api.tradier.com"

    def _h(self):
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def connect(self):
        import requests
        try:
            r = requests.get(f"{self.base}/v1/user/profile", headers=self._h(), timeout=8)
            return (r.status_code == 200,
                    "Connected" if r.status_code == 200 else f"{r.status_code}")
        except Exception as e:
            return False, str(e)

    def list_account_options(self):
        """A Tradier token can cover several accounts — list them so the user picks
        the right one (individual, Roth IRA, joint, …)."""
        import requests
        try:
            p = requests.get(f"{self.base}/v1/user/profile", headers=self._h(), timeout=8).json()
            accts = (p.get("profile") or {}).get("account", [])
            if isinstance(accts, dict):
                accts = [accts]
            opts = []
            for a in (accts or []):
                if not isinstance(a, dict):
                    continue
                num = a.get("account_number") or a.get("account_id") or ""
                if not num:
                    continue
                parts = [str(a.get(k)) for k in ("type", "classification", "status") if a.get(k)]
                label = " · ".join(parts) or "Account"
                opts.append({"id": str(num), "label": f"{label}   (…{str(num)[-6:]})"})
            return opts
        except Exception:
            return []

    def get_account(self):
        import requests
        try:
            b = requests.get(f"{self.base}/v1/accounts/{self.acct}/balances",
                             headers=self._h(), timeout=8).json().get("balances", {})
            cash = b.get("cash", {})
            return {"cash": float(b.get("total_cash", 0)),
                    "equity": float(b.get("total_equity", 0)),
                    "buying_power": float(cash.get("cash_available", 0) if isinstance(cash, dict) else 0)}
        except Exception:
            return None

    def get_positions(self):
        import requests
        try:
            p = requests.get(f"{self.base}/v1/accounts/{self.acct}/positions",
                             headers=self._h(), timeout=8).json()
            items = (p.get("positions") or {}).get("position", [])
            if isinstance(items, dict):
                items = [items]
            out = []
            for x in items:
                qty = float(x.get("quantity", 0)) or 1.0
                out.append({"ticker": x.get("symbol", ""), "qty": float(x.get("quantity", 0)),
                            "avg_cost": float(x.get("cost_basis", 0)) / qty,
                            "mkt_value": 0.0, "pnl": 0.0})
            return out
        except Exception:
            return []


# ── Moomoo (reuses moomoo_integration — no duplication) ───────────────────────

class MoomooAdapter(BrokerAdapter):
    name = "Moomoo"

    def __init__(self, env: str = "SIMULATE", acc_id: int = 0):
        self.env, self.acc_id = env, int(acc_id or 0)

    def _up(self):
        from moomoo_integration import MoomooData
        return MoomooData()._opend_up()

    def connect(self):
        up = self._up()
        return (up, "OpenD reachable" if up else "Start OpenD (port 11111)")

    def list_account_options(self):
        """List the accounts OpenD exposes for this environment so the user can pick
        which one to trade (SIMULATE vs REAL, cash vs margin, etc.)."""
        if not self._up():
            return []
        try:
            from moomoo_integration import MoomooTrader
            rows = MoomooTrader(env=self.env, acc_id=0).list_accounts()
            want = "REAL" if str(self.env).upper() == "REAL" else "SIMULATE"
            opts = []
            for r in (rows or []):
                aid = r.get("acc_id")
                if aid in (None, "", "nan"):
                    continue
                env = str(r.get("trd_env", "") or "")
                # keep only this environment's accounts when the field is usable
                if env and want not in env.upper():
                    continue
                bits = [x for x in (env, str(r.get("acc_type", "") or ""),
                                    str(r.get("security_firm", "") or "")) if x and x != "nan"]
                label = " · ".join(bits) or "Account"
                opts.append({"id": str(aid), "label": f"{label}   (…{str(aid)[-6:]})"})
            return opts
        except Exception:
            return []

    def get_account(self):
        if not self._up():
            return None
        from moomoo_integration import MoomooTrader
        ok, _, info = MoomooTrader(env=self.env, acc_id=self.acc_id).get_account_info()
        if not ok:
            return None
        return {"cash": info.get("cash", 0), "equity": info.get("total_assets", 0),
                "buying_power": info.get("cash", 0)}

    def get_positions(self):
        if not self._up():
            return []
        from moomoo_integration import MoomooTrader
        return MoomooTrader(env=self.env, acc_id=self.acc_id).list_positions()


# ── Webull (official OpenAPI — HMAC-signed REST via webull_openapi) ────────────

class WebullAdapter(BrokerAdapter):
    name = "Webull"

    def __init__(self, app_key: str = "", app_secret: str = "",
                 region: str = "us", account_id: str = ""):
        self.app_key = app_key
        self.app_secret = app_secret
        self.region = region or "us"
        self.account_id = str(account_id or "")

    def _client(self):
        from webull_openapi import WebullOpenAPI
        return WebullOpenAPI(self.app_key, self.app_secret, self.region)

    @staticmethod
    def _account_id_of(it: dict) -> str:
        return str(it.get("account_id") or it.get("accountId")
                   or it.get("secAccountId") or it.get("id") or "")

    def list_account_options(self) -> list:
        """Return [{"id", "label"}] for EVERY account under these keys, so the user
        can pick the right one (Webull returns Cash, Margin, Roth IRA, etc. with no
        guaranteed order — auto-picking the first is what grabbed the wrong one)."""
        try:
            cli = self._client()
            r = cli.list_accounts()
            if r.status_code != 200:
                return []
            data = r.json()
            items = data.get("data") if isinstance(data, dict) else data
            opts = []
            for it in (items or []):
                if not isinstance(it, dict):
                    continue
                aid = self._account_id_of(it)
                if not aid:
                    continue
                # Build a human label from whatever descriptive fields exist.
                parts = []
                for k in ("account_type", "account_tag", "account_display_name",
                          "account_sub_type", "account_category", "brokerage_name",
                          "brokerage", "account_number", "account_no", "currency"):
                    v = it.get(k)
                    if v:
                        parts.append(str(v))
                label = " · ".join(dict.fromkeys(parts)) or "Account"
                opts.append({"id": aid, "label": f"{label}   (…{aid[-6:]})"})
            return opts
        except Exception:
            return []

    def _resolve_account_id(self, cli) -> str:
        """Use the saved account_id if given, else fall back to the first account
        from the subscriptions list (the UI account picker is the real fix for
        multi-account users — see list_account_options)."""
        if self.account_id:
            return self.account_id
        try:
            r = cli.list_accounts()
            if r.status_code == 200:
                data = r.json()
                items = data.get("data") if isinstance(data, dict) else data
                for it in (items or []):
                    if isinstance(it, dict):
                        aid = self._account_id_of(it)
                        if aid:
                            self.account_id = aid
                            return self.account_id
        except Exception:
            pass
        return ""

    def connect(self):
        if not (self.app_key and self.app_secret):
            return False, "Enter your Webull OpenAPI App Key and App Secret."
        try:
            cli = self._client()
        except Exception as e:
            return False, f"Client unavailable: {e}"
        ok, msg = cli.ping()
        if ok:
            self._resolve_account_id(cli)   # best-effort; connection is already valid
        return ok, msg

    def get_account(self):
        try:
            cli = self._client()
            aid = self._resolve_account_id(cli)
            if not aid:
                return None
            r = cli.get_balance(aid, "USD")
            if r.status_code != 200:
                return None
            b = r.json()
            b = b.get("data", b) if isinstance(b, dict) else {}

            def _num(*keys):
                for k in keys:
                    v = b.get(k)
                    if v not in (None, ""):
                        try:
                            return float(v)
                        except Exception:
                            pass
                return 0.0

            return {
                "cash":         _num("cash_balance", "total_cash", "settled_funds", "available_cash"),
                "equity":       _num("net_liquidation", "total_asset", "total_market_value", "account_value"),
                "buying_power": _num("buying_power", "day_buying_power", "available_buying_power"),
            }
        except Exception:
            return None

    def get_positions(self):
        try:
            cli = self._client()
            aid = self._resolve_account_id(cli)
            if not aid:
                return []
            r = cli.get_positions(aid)
            if r.status_code != 200:
                return []
            data = r.json()
            items = data
            if isinstance(data, dict):
                items = (data.get("holdings") or data.get("items")
                         or data.get("positions") or data.get("data") or [])
            out = []
            for p in (items or []):
                if not isinstance(p, dict):
                    continue

                def g(*keys, default=0.0):
                    for k in keys:
                        v = p.get(k)
                        if v not in (None, ""):
                            try:
                                return float(v)
                            except Exception:
                                pass
                    return default

                out.append({
                    "ticker":    p.get("ticker") or p.get("symbol") or p.get("instrument_id") or "",
                    "qty":       g("quantity", "position", "qty"),
                    "avg_cost":  g("cost_price", "avg_cost", "average_cost", "cost_basis"),
                    "mkt_value": g("market_value", "mkt_value", "position_value"),
                    "pnl":       g("unrealized_profit_loss", "unrealized_pnl", "unrealized_pl"),
                })
            return out
        except Exception:
            return []


# ── IBKR (requires Client Portal Gateway — stub for now) ──────────────────────

class IBKRAdapter(BrokerAdapter):
    name = "IBKR"

    def connect(self):
        return False, "IBKR needs the Client Portal Gateway running locally (adapter coming soon)."

    def get_account(self):
        return None

    def get_positions(self):
        return []


# ── Factory ───────────────────────────────────────────────────────────────────

def build_adapter(broker: str, creds: dict) -> Optional[BrokerAdapter]:
    if broker == "Alpaca":
        return AlpacaAdapter(creds.get("key", ""), creds.get("secret", ""), creds.get("paper", True))
    if broker == "Tradier":
        return TradierAdapter(creds.get("token", ""), creds.get("account_id", ""), creds.get("paper", True))
    if broker == "Moomoo":
        return MoomooAdapter(creds.get("env", "SIMULATE"), creds.get("acc_id", 0))
    if broker == "Webull":
        return WebullAdapter(creds.get("app_key", ""), creds.get("app_secret", ""),
                             creds.get("region", "us"), creds.get("account_id", ""))
    if broker == "IBKR":
        return IBKRAdapter()
    return None
