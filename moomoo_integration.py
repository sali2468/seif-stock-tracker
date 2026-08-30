"""
Moomoo / Futu OpenAPI integration.

Requires:
  • FutuOpenD running locally  (download from https://www.futunn.com/download/openAPI)
  • pip install futu-api

All order calls are wrapped so that if futu-api is not installed (or FutuOpenD
is not running) they fail gracefully with a clear error string — the UI can
display the error without crashing.

Supported order flow:
  1. place_entry_order()   → market/limit buy
  2. place_stop_loss()     → STOP order below entry
  3. place_take_profit()   → LIMIT sell at target
  4. check_fills()         → poll order statuses, update plan
  5. check_pullback_add()  → decide whether to add on a pullback
  6. place_pullback_add()  → limit buy in pullback zone
  7. check_staged_exits()  → partial sells at 2R / 3R / trail
  8. cancel_all_orders()   → cancel every open order for a plan
"""

import logging
from datetime import datetime
from typing import Optional, Tuple
import pandas as pd

logger = logging.getLogger(__name__)

# ─── futu-api import (soft) ───────────────────────────────────────────────────

try:
    from futu import (
        OpenSecTradeContext, OpenQuoteContext,
        TrdSide, OrderType, TrdMarket, TrdEnv,
        SubType, KLType, AuType, Currency,
        RET_OK,
    )
    FUTU_AVAILABLE = True
except ImportError:
    FUTU_AVAILABLE = False
    logger.warning("futu-api not installed — Moomoo integration disabled. "
                   "Run: pip install futu-api")

# ─── defaults ─────────────────────────────────────────────────────────────────

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 11111            # FutuOpenD default port


# ─── connection context-manager ───────────────────────────────────────────────

class MoomooTrader:
    """
    Thin wrapper around Futu OpenSecTradeContext.

    Usage (fire-and-forget, each call opens / closes a connection):
        mt = MoomooTrader(env="SIMULATE", acc_id=283445331594399121)
        ok, msg, order_id = mt.place_entry_order(plan)

    acc_id:  Pass the numeric Moomoo account ID to target a specific account.
             Use 0 (default) to let FutuOpenD pick the first available account.
             For REAL trading always pass the cash account ID from .env.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        env: str = "SIMULATE",      # "REAL" or "SIMULATE"
        acc_id: int = 0,            # 0 = FutuOpenD default; set to cash acc_id for REAL
    ):
        self.host    = host
        self.port    = port
        self.acc_id  = int(acc_id)
        self.trd_env = TrdEnv.REAL if env == "REAL" else TrdEnv.SIMULATE

    # ── internal: get a live context ──────────────────────────────────────────

    def _ctx(self):
        """Return an OpenSecTradeContext (caller must close it)."""
        if not FUTU_AVAILABLE:
            raise RuntimeError(
                "futu-api not installed. Run:  pip install futu-api"
            )
        return OpenSecTradeContext(
            host=self.host,
            port=self.port,
            filter_trdmarket=TrdMarket.US,
        )

    # ── account info ──────────────────────────────────────────────────────────

    def get_account_info(self) -> Tuple[bool, str, dict]:
        """Return (ok, msg, info_dict)."""
        try:
            ctx = self._ctx()
            kwargs = {"trd_env": self.trd_env, "currency": Currency.USD}
            if self.acc_id:
                kwargs["acc_id"] = self.acc_id
            ret, data = ctx.accinfo_query(**kwargs)
            ctx.close()
            if ret != RET_OK:
                return False, str(data), {}
            row = data.iloc[0].to_dict()
            return True, "ok", {
                "cash":         float(row.get("cash", 0)),
                "total_assets": float(row.get("total_assets", 0)),
                "market_value": float(row.get("market_val", 0)),
            }
        except Exception as e:
            return False, str(e), {}

    def get_buying_power(self) -> float:
        """Quick helper — returns cash available or 0 on error."""
        ok, _, info = self.get_account_info()
        return info.get("cash", 0.0) if ok else 0.0

    def list_accounts(self) -> list:
        """Return [{acc_id, trd_env, acc_type, security_firm, …}] for every account
        visible to OpenD (used to let the user pick which account to trade)."""
        try:
            ctx = self._ctx()
            ret, data = ctx.get_acc_list()
            ctx.close()
            if ret != RET_OK:
                return []
            return data.to_dict("records")
        except Exception:
            return []

    # ── entry order ───────────────────────────────────────────────────────────

    def place_entry_order(
        self,
        ticker: str,
        qty: int,
        price: float,
        order_type: str = "LIMIT",    # "LIMIT" or "MARKET"
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Buy `qty` shares of `ticker`.
        Returns (success, message, order_id).
        """
        try:
            ctx = self._ctx()
            code = f"US.{ticker.upper()}"

            if order_type == "MARKET":
                otype = OrderType.MARKET
                p = 0.0
            else:
                otype = OrderType.NORMAL   # limit
                p = round(price, 2)

            order_kwargs = dict(
                price=p, qty=qty, code=code,
                trd_side=TrdSide.BUY, order_type=otype,
                trd_env=self.trd_env, remark="entry",
            )
            if self.acc_id:
                order_kwargs["acc_id"] = self.acc_id
            ret, data = ctx.place_order(**order_kwargs)
            ctx.close()

            if ret != RET_OK:
                return False, str(data), None

            order_id = str(data["order_id"].iloc[0])
            return True, "Entry order placed", order_id

        except Exception as e:
            return False, str(e), None

    # ── stop-loss order ───────────────────────────────────────────────────────

    def place_stop_loss(
        self,
        ticker: str,
        qty: int,
        stop_price: float,
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Place a STOP SELL order at `stop_price`.
        Futu uses OrderType.STOP (stop-market on US equities).
        Returns (success, message, order_id).
        """
        try:
            ctx = self._ctx()
            code = f"US.{ticker.upper()}"

            order_kwargs = dict(
                price=round(stop_price, 2), qty=qty, code=code,
                trd_side=TrdSide.SELL, order_type=OrderType.STOP,
                trd_env=self.trd_env, remark="stop_loss",
            )
            if self.acc_id:
                order_kwargs["acc_id"] = self.acc_id
            ret, data = ctx.place_order(**order_kwargs)
            ctx.close()

            if ret != RET_OK:
                return False, str(data), None

            order_id = str(data["order_id"].iloc[0])
            return True, "Stop-loss order placed", order_id

        except Exception as e:
            return False, str(e), None

    # ── take-profit order ─────────────────────────────────────────────────────

    def place_take_profit(
        self,
        ticker: str,
        qty: int,
        target_price: float,
        label: str = "tp",
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Place a LIMIT SELL order at `target_price`.
        Returns (success, message, order_id).
        """
        try:
            ctx = self._ctx()
            code = f"US.{ticker.upper()}"

            order_kwargs = dict(
                price=round(target_price, 2), qty=qty, code=code,
                trd_side=TrdSide.SELL, order_type=OrderType.NORMAL,
                trd_env=self.trd_env, remark=label,
            )
            if self.acc_id:
                order_kwargs["acc_id"] = self.acc_id
            ret, data = ctx.place_order(**order_kwargs)
            ctx.close()

            if ret != RET_OK:
                return False, str(data), None

            order_id = str(data["order_id"].iloc[0])
            return True, f"Take-profit ({label}) order placed", order_id

        except Exception as e:
            return False, str(e), None

    # ── cancel a single order ─────────────────────────────────────────────────

    def cancel_order(
        self, order_id: str
    ) -> Tuple[bool, str]:
        """Cancel one order by ID. Returns (success, message)."""
        try:
            ctx = self._ctx()
            kwargs = {"orderid": order_id, "trd_env": self.trd_env}
            if self.acc_id:
                kwargs["acc_id"] = self.acc_id
            ret, data = ctx.cancel_order(**kwargs)
            ctx.close()
            if ret != RET_OK:
                return False, str(data)
            return True, "Cancelled"
        except Exception as e:
            return False, str(e)

    # ── cancel all orders for a plan ─────────────────────────────────────────

    def cancel_plan_orders(self, plan: dict) -> list:
        """
        Cancel every open order attached to `plan`.
        Returns list of (order_id, success, msg) tuples.
        """
        orders = plan.get("orders", {})
        ids = [
            orders.get("stop_order_id"),
            orders.get("target1_order_id"),
            orders.get("target2_order_id"),
        ] + orders.get("add_order_ids", [])

        results = []
        for oid in ids:
            if oid:
                ok, msg = self.cancel_order(oid)
                results.append((oid, ok, msg))
        return results

    # ── modify an existing order price ────────────────────────────────────────

    def modify_order_price(
        self, order_id: str, new_price: float, qty: int
    ) -> Tuple[bool, str]:
        """Modify price (e.g. trail stop). Returns (success, message)."""
        try:
            ctx = self._ctx()
            from futu import ModifyOrderOp
            kwargs = dict(
                modify_order_op=ModifyOrderOp.NORMAL,
                orderid=order_id, qty=qty,
                price=round(new_price, 2),
                trd_env=self.trd_env,
            )
            if self.acc_id:
                kwargs["acc_id"] = self.acc_id
            ret, data = ctx.modify_order(**kwargs)
            ctx.close()
            if ret != RET_OK:
                return False, str(data)
            return True, "Modified"
        except Exception as e:
            return False, str(e)

    # ── order status ──────────────────────────────────────────────────────────

    def get_order_status(
        self, order_id: str
    ) -> Tuple[bool, str, Optional[dict]]:
        """
        Returns (ok, msg, status_dict).
        status_dict keys: status, filled_qty, avg_fill_price
        """
        try:
            ctx = self._ctx()
            kwargs = {"order_id": order_id, "trd_env": self.trd_env}
            if self.acc_id:
                kwargs["acc_id"] = self.acc_id
            ret, data = ctx.order_list_query(**kwargs)
            ctx.close()
            if ret != RET_OK:
                return False, str(data), None
            if data.empty:
                return False, "Order not found", None

            row = data.iloc[0]
            return True, "ok", {
                "status":          str(row.get("order_status", "")),
                "filled_qty":      int(row.get("dealt_qty", 0)),
                "avg_fill_price":  float(row.get("dealt_avg_price", 0)),
            }
        except Exception as e:
            return False, str(e), None

    # ── position query ────────────────────────────────────────────────────────

    def list_positions(self) -> list:
        """All open positions for the account: [{ticker, qty, avg_cost, mkt_value, pnl}]."""
        try:
            ctx = self._ctx()
            kwargs = {"trd_env": self.trd_env}
            if self.acc_id:
                kwargs["acc_id"] = self.acc_id
            ret, data = ctx.position_list_query(**kwargs)
            ctx.close()
            if ret != RET_OK or data.empty:
                return []
            out = []
            for _, row in data.iterrows():
                out.append({
                    "ticker":    str(row.get("code", "")).replace("US.", ""),
                    "qty":       int(row.get("qty", 0)),
                    "avg_cost":  float(row.get("cost_price", 0)),
                    "mkt_value": float(row.get("market_val", 0)),
                    "pnl":       float(row.get("unrealized_pl", 0)),
                })
            return out
        except Exception:
            return []

    def get_position(self, ticker: str) -> Optional[dict]:
        """Return current Moomoo position for ticker, or None."""
        try:
            ctx = self._ctx()
            code = f"US.{ticker.upper()}"
            kwargs = {"code": code, "trd_env": self.trd_env}
            if self.acc_id:
                kwargs["acc_id"] = self.acc_id
            ret, data = ctx.position_list_query(**kwargs)
            ctx.close()
            if ret != RET_OK or data.empty:
                return None
            row = data.iloc[0]
            return {
                "qty":       int(row.get("qty", 0)),
                "avg_cost":  float(row.get("cost_price", 0)),
                "mkt_value": float(row.get("market_val", 0)),
                "pnl":       float(row.get("unrealized_pl", 0)),
            }
        except Exception:
            return None


# ─── Moomoo market data (real-time bars + quotes via OpenD) ──────────────────

class MoomooData:
    """Intraday K-line + live quotes from Moomoo's OpenD (OpenQuoteContext).

    Returns data in the same lowercase OHLCV shape as market_data.get_bars, so
    callers can swap it in transparently. All methods return None on any failure
    (OpenD down, no quota, not installed) so callers can fall back to yfinance.
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host, self.port = host, port

    def _opend_up(self, timeout: float = 1.0) -> bool:
        """Fast TCP check — avoids OpenQuoteContext hanging when OpenD is down."""
        import socket
        try:
            with socket.create_connection((self.host, self.port), timeout=timeout):
                return True
        except Exception:
            return False

    def _kltypes(self, interval: str):
        if not FUTU_AVAILABLE:
            return None, None
        m = {
            "1m":  (SubType.K_1M,  KLType.K_1M),
            "5m":  (SubType.K_5M,  KLType.K_5M),
            "15m": (SubType.K_15M, KLType.K_15M),
            "30m": (SubType.K_30M, KLType.K_30M),
            "60m": (SubType.K_60M, KLType.K_60M),
        }
        return m.get(interval, (SubType.K_5M, KLType.K_5M))

    def get_intraday_bars(self, ticker: str, interval: str = "5m", num: int = 300) -> Optional[pd.DataFrame]:
        if not FUTU_AVAILABLE or not self._opend_up():
            return None
        sub, kl = self._kltypes(interval)
        code = f"US.{ticker.upper()}"
        try:
            ctx = OpenQuoteContext(host=self.host, port=self.port)
            r1, _ = ctx.subscribe([code], [sub], subscribe_push=False)
            if r1 != RET_OK:
                ctx.close()
                return None
            ret, data = ctx.get_cur_kline(code, num, kl, AuType.QFQ)
            ctx.close()
            if ret != RET_OK or data is None or len(data) == 0:
                return None
            df = data.rename(columns={"time_key": "dt"})[["dt", "open", "high", "low", "close", "volume"]].copy()
            df["dt"] = pd.to_datetime(df["dt"])
            df = df.set_index("dt")
            for c in ("open", "high", "low", "close", "volume"):
                df[c] = df[c].astype(float)
            return df
        except Exception as e:
            logger.warning(f"MoomooData.get_intraday_bars({ticker}) failed: {e}")
            return None

    def get_quote(self, ticker: str) -> Optional[dict]:
        if not FUTU_AVAILABLE or not self._opend_up():
            return None
        code = f"US.{ticker.upper()}"
        try:
            ctx = OpenQuoteContext(host=self.host, port=self.port)
            ret, data = ctx.get_market_snapshot([code])
            ctx.close()
            if ret != RET_OK or data is None or len(data) == 0:
                return None
            row = data.iloc[0]
            return {
                "price":      float(row.get("last_price", 0)),
                "open":       float(row.get("open_price", 0)),
                "high":       float(row.get("high_price", 0)),
                "low":        float(row.get("low_price", 0)),
                "prev_close": float(row.get("prev_close_price", 0)),
                "volume":     int(row.get("volume", 0)),
            }
        except Exception as e:
            logger.warning(f"MoomooData.get_quote({ticker}) failed: {e}")
            return None


# ─── high-level trade-plan executor ──────────────────────────────────────────

def execute_trade_plan(
    plan: dict,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    acc_id: int = 0,
) -> Tuple[bool, str, dict]:
    """
    One-shot: place the entry, stop-loss, and take-profit orders for a plan.
    Updates the plan's `orders` dict in-place and returns (ok, msg, updated_plan).

    Staged exits:
      - target1_order: 1/3 of qty at 2R  (33%)
      - target2_order: 1/3 of qty at 3R  (33%)
      - remaining 1/3 will be trailed manually by check_and_manage()

    Stop-loss covers the *full* qty so the position is always protected.
    """
    env  = plan.get("trd_env", "SIMULATE")
    mt   = MoomooTrader(host=host, port=port, env=env, acc_id=acc_id)
    tick = plan["ticker"]
    qty  = plan["qty"]

    # ── 1. entry order ────────────────────────────────────────────────────────
    ok, msg, entry_oid = mt.place_entry_order(
        ticker=tick,
        qty=qty,
        price=plan["entry_price"],
        order_type="LIMIT",
    )
    if not ok:
        return False, f"Entry order failed: {msg}", plan

    plan["orders"]["entry_order_id"] = entry_oid
    plan["status"] = "active"
    logger.info(f"[{tick}] Entry order {entry_oid} placed @ {plan['entry_price']}")

    # ── 2. stop-loss (full qty) ───────────────────────────────────────────────
    ok, msg, stop_oid = mt.place_stop_loss(
        ticker=tick,
        qty=qty,
        stop_price=plan["stop_price"],
    )
    if not ok:
        logger.warning(f"[{tick}] Stop-loss failed: {msg}")
    else:
        plan["orders"]["stop_order_id"] = stop_oid
        logger.info(f"[{tick}] Stop-loss order {stop_oid} @ {plan['stop_price']}")

    # ── 3. take-profit 1 (33% @ 2R) ──────────────────────────────────────────
    tp1_qty = max(1, qty // 3)
    ok, msg, tp1_oid = mt.place_take_profit(
        ticker=tick,
        qty=tp1_qty,
        target_price=plan["target1_price"],
        label="tp1_2R",
    )
    if not ok:
        logger.warning(f"[{tick}] TP1 failed: {msg}")
    else:
        plan["orders"]["target1_order_id"] = tp1_oid
        logger.info(f"[{tick}] TP1 order {tp1_oid} @ {plan['target1_price']} x{tp1_qty}")

    # ── 4. take-profit 2 (33% @ 3R) ──────────────────────────────────────────
    tp2_qty = max(1, qty // 3)
    ok, msg, tp2_oid = mt.place_take_profit(
        ticker=tick,
        qty=tp2_qty,
        target_price=plan["target2_price"],
        label="tp2_3R",
    )
    if not ok:
        logger.warning(f"[{tick}] TP2 failed: {msg}")
    else:
        plan["orders"]["target2_order_id"] = tp2_oid
        logger.info(f"[{tick}] TP2 order {tp2_oid} @ {plan['target2_price']} x{tp2_qty}")

    return True, "Trade plan executed", plan


# ─── background monitor (called from Streamlit fragment) ─────────────────────

def check_and_manage(
    plan: dict,
    current_price: float,
    current_volume: float,
    avg_volume: float,
    current_rsi: float,
    ema21: float,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    acc_id: int = 0,
) -> Tuple[dict, list]:
    """
    Given the latest market data, decide if any automated action is needed.
    Returns (updated_plan, [list of action strings taken]).

    Actions possible:
      A) Pullback add   — price dips into zone, EMA near, RSI < 60, vol surge
      B) Trail stop     — after TP1 fills, move stop to breakeven then trail
      C) Time stop      — after 21 days with no meaningful move, exit
      D) Danger zone    — price hits danger zone, cancel pending adds, let stop handle it

    NOTE: this function mutates `plan` in-place and persists changes via
    moomoo_state.update_plan().  Callers should then refresh their local copy.
    """
    from moomoo_state import update_plan  # imported here to avoid circular at module level
    mt      = MoomooTrader(host=host, port=port, env=plan.get("trd_env", "SIMULATE"), acc_id=acc_id)
    actions = []
    tick    = plan["ticker"]
    pb      = plan["pullback"]

    # ── A. Pullback add ───────────────────────────────────────────────────────
    if (
        plan["status"] in ("active", "partial")
        and pb["enabled"]
        and pb["adds_done"] < pb["max_adds"]
        and current_price >= pb["zone_low"]
        and current_price <= pb["zone_high"]
        and current_rsi < pb["require_rsi_lt"]
        and ema21 > 0
        and abs(current_price - ema21) / ema21 < 0.02   # within 2% of 21-EMA
    ):
        vol_ok = (not pb["require_vol_surge"]) or (current_volume >= avg_volume * 1.3)
        if vol_ok:
            add_qty = pb["add_qty"]
            ok, msg, add_oid = mt.place_entry_order(
                ticker=tick, qty=add_qty, price=current_price, order_type="LIMIT"
            )
            if ok:
                pb["adds_done"] += 1
                plan["qty_remaining"] += add_qty
                plan["orders"]["add_order_ids"].append(add_oid)
                # Expand stop to cover extra shares (keep same stop price)
                # Re-cancel old stop, place new one covering full qty
                old_stop_oid = plan["orders"].get("stop_order_id")
                if old_stop_oid:
                    mt.cancel_order(old_stop_oid)
                new_qty = plan["qty_remaining"]
                ok2, msg2, new_stop_oid = mt.place_stop_loss(
                    ticker=tick, qty=new_qty, stop_price=plan["stop_price"]
                )
                if ok2:
                    plan["orders"]["stop_order_id"] = new_stop_oid
                msg_out = (f"Pullback add #{pb['adds_done']}: "
                           f"+{add_qty} shares @ ${current_price:.2f}")
                actions.append(msg_out)
                logger.info(f"[{tick}] {msg_out}")

    # ── B. Danger zone → disable pullback adds ────────────────────────────────
    if (
        current_price < pb["danger_zone"]
        and pb["enabled"]
    ):
        pb["enabled"] = False
        actions.append(f"Danger zone hit (${current_price:.2f} < ${pb['danger_zone']:.2f}) — pullback adds disabled")
        logger.warning(f"[{tick}] Danger zone hit @ {current_price}")

    # ── C. Trail stop after TP1 fills ────────────────────────────────────────
    exits = plan["exits"]
    if not exits["exit1_done"]:
        tp1_oid = plan["orders"].get("target1_order_id")
        if tp1_oid:
            ok, _, st = mt.get_order_status(tp1_oid)
            if ok and st and "FILLED" in st["status"].upper():
                exits["exit1_done"] = True
                # Move stop to break-even
                new_stop = plan["entry_price"]
                old_stop_oid = plan["orders"].get("stop_order_id")
                if old_stop_oid:
                    mt.cancel_order(old_stop_oid)
                remaining = plan["qty_remaining"] - max(1, plan["qty"] // 3)
                plan["qty_remaining"] = remaining
                ok2, _, new_stop_oid = mt.place_stop_loss(
                    ticker=tick, qty=remaining, stop_price=new_stop
                )
                if ok2:
                    plan["orders"]["stop_order_id"] = new_stop_oid
                    plan["stop_price"] = new_stop   # update displayed stop
                exits["trail_stop"] = new_stop
                exits["trailing"]   = True
                plan["status"] = "partial"
                actions.append(f"TP1 filled — stop moved to breakeven ${new_stop:.2f}")

    if not exits["exit2_done"] and exits["exit1_done"]:
        tp2_oid = plan["orders"].get("target2_order_id")
        if tp2_oid:
            ok, _, st = mt.get_order_status(tp2_oid)
            if ok and st and "FILLED" in st["status"].upper():
                exits["exit2_done"] = True
                remaining = plan["qty_remaining"] - max(1, plan["qty"] // 3)
                plan["qty_remaining"] = remaining
                # Start trailing with ATR-distance stop
                trail_dist   = plan["atr"] * 1.5
                new_trail    = round(current_price - trail_dist, 2)
                old_stop_oid = plan["orders"].get("stop_order_id")
                if old_stop_oid:
                    mt.cancel_order(old_stop_oid)
                ok2, _, new_stop_oid = mt.place_stop_loss(
                    ticker=tick, qty=remaining, stop_price=new_trail
                )
                if ok2:
                    plan["orders"]["stop_order_id"] = new_stop_oid
                exits["trail_stop"] = new_trail
                actions.append(f"TP2 filled — trailing remainder with stop @ ${new_trail:.2f}")

    # ── D. Tighten trailing stop ──────────────────────────────────────────────
    if exits["trailing"] and exits["trail_stop"] is not None:
        trail_dist    = plan["atr"] * 1.5
        proposed_stop = round(current_price - trail_dist, 2)
        if proposed_stop > exits["trail_stop"]:
            old_stop_oid = plan["orders"].get("stop_order_id")
            if old_stop_oid:
                ok, _ = mt.modify_order_price(
                    old_stop_oid, proposed_stop, plan["qty_remaining"]
                )
                if ok:
                    exits["trail_stop"] = proposed_stop
                    plan["stop_price"]  = proposed_stop
                    actions.append(f"Trail stop raised to ${proposed_stop:.2f}")

    # ── E. Time stop ──────────────────────────────────────────────────────────
    try:
        created = datetime.fromisoformat(plan["created_at"])
        days_held = (datetime.utcnow() - created).days
        if days_held >= plan.get("time_stop_days", 21) and not exits["exit2_done"]:
            # Close remaining via market order — let stop handle it (already placed)
            # We just flag the plan for manual review
            actions.append(
                f"⏰ Time stop: {days_held} days held with no full exit — review position"
            )
    except Exception:
        pass

    # Persist mutations
    update_plan(plan["plan_id"], plan)
    return plan, actions


# ─── pre-trade risk validator ─────────────────────────────────────────────────

def validate_trade(
    entry_price: float,
    stop_price: float,
    target1_price: float,
    target2_price: float,
    account_size: float,
    risk_pct: float = 1.0,          # max % of account to risk per trade
    cash_balance: float = 0.0,      # live cash in account; 0 = skip cash check
) -> dict:
    """
    Check the trade for:
      • Minimum 3:1 R/R to target2
      • Position size by risk %
      • Ensure stop is below entry
      • Order cost does not exceed available cash (when cash_balance > 0)

    Returns dict with keys: ok, warnings, errors, suggested_qty, risk_dollars,
                            max_affordable_qty, cash_balance
    """
    errors   = []
    warnings = []

    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        errors.append("Stop price must be BELOW entry price.")

    if risk_per_share > 0:
        rr1 = (target1_price - entry_price) / risk_per_share
        rr2 = (target2_price - entry_price) / risk_per_share

        if rr2 < 3.0:
            errors.append(
                f"Target2 R/R is {rr2:.1f}:1 — minimum is 3:1. "
                f"Move target to at least ${entry_price + 3 * risk_per_share:.2f}."
            )
        elif rr2 < 4.0:
            warnings.append(f"Target2 R/R is {rr2:.1f}:1 — good, but 4:1+ is ideal.")

        if rr1 < 1.5:
            warnings.append(f"Target1 (2R exit) R/R is only {rr1:.1f}:1 — consider widening.")
    else:
        rr1, rr2 = 0, 0

    # Position sizing by risk %
    risk_dollars   = account_size * (risk_pct / 100.0)
    suggested_qty  = int(risk_dollars / risk_per_share) if risk_per_share > 0 else 0
    position_value = suggested_qty * entry_price

    # ── Cash sufficiency check ────────────────────────────────────────────────
    # max_affordable_qty: how many shares the available cash can actually buy
    max_affordable_qty = int(cash_balance / entry_price) if (cash_balance > 0 and entry_price > 0) else 0

    if cash_balance > 0:
        if position_value > cash_balance:
            # Cap suggested_qty to what cash allows
            suggested_qty  = max_affordable_qty
            position_value = suggested_qty * entry_price
            errors.append(
                f"Not enough cash — order would cost ${suggested_qty * entry_price:,.2f} "
                f"but only ${cash_balance:,.2f} is available. "
                f"Max affordable qty at ${entry_price:.2f}: {max_affordable_qty} shares."
            )
        elif position_value > cash_balance * 0.80:
            warnings.append(
                f"Order cost ${position_value:,.2f} uses "
                f"{position_value / cash_balance * 100:.0f}% of your ${cash_balance:,.2f} cash — "
                f"leaves little room for other trades."
            )

    if cash_balance <= 0 and position_value > account_size * 0.20:
        warnings.append(
            f"Position size (${position_value:,.0f}) exceeds 20% of account — consider reducing."
        )

    return {
        "ok":                 len(errors) == 0,
        "errors":             errors,
        "warnings":           warnings,
        "rr1":                round(rr1, 2),
        "rr2":                round(rr2, 2),
        "risk_dollars":       round(risk_dollars, 2),
        "risk_per_share":     round(risk_per_share, 4),
        "suggested_qty":      suggested_qty,
        "position_value":     round(position_value, 2),
        "max_affordable_qty": max_affordable_qty,
        "cash_balance":       round(cash_balance, 2),
    }
