from __future__ import annotations
from typing import Any, Dict, List, Optional
import os

from binance.client import Client

def _client_from_env(timeout=(10, 20)) -> Client:
    return Client(
        os.getenv("BINANCE_API_KEY", ""),
        os.getenv("BINANCE_SECRET", ""),
        {"timeout": timeout, "verify": True},
    )

class AccountDataManager:
    """
    Futures-first account adapter that prefers the repo's BinanceClient wrapper,
    but still works with a raw python-binance Client.
    """

    def __init__(self, client=None, futures: bool = True):
        # keep both references:
        self.wrapper = client                            # project wrapper (BinanceClient) if given
        self.client  = getattr(client, "client", client) # raw python-binance Client if present
        if self.client is None:
            self.client = _client_from_env()
        self.futures = futures

    # -------------------- Public API used by main.py --------------------

    def get_account_summary(self) -> dict:
        """
        Returns a compact snapshot:
        {
          'equity': float,                  # total wallet balance (USDT-M)
          'total_unrealized_pnl': float,    # USDT-M unrealized PnL
          'margin_ratio': float             # % = totalMaintMargin / totalWalletBalance * 100
        }
        """
        # 1) Prefer the project's wrapper: wrapper.get_account() → futures_account()
        try:
            if self.wrapper and hasattr(self.wrapper, "get_account"):
                acct = self.wrapper.get_account() or {}
                equity = float(acct.get("totalWalletBalance") or 0.0)
                unreal = float(acct.get("totalUnrealizedProfit") or 0.0)
                maint  = float(acct.get("totalMaintMargin") or 0.0)
                availableBalance = float(acct.get("availableBalance") or 0.0)
                margin_ratio = (maint / equity * 100.0) if equity > 0 else 0.0
                return {
                    "equity": equity,
                    "total_unrealized_pnl": unreal,
                    "margin_ratio": margin_ratio,
                    "available_balance" : availableBalance
                }
        except Exception:
            pass

        # 2) Fall back to raw python-binance futures endpoints
        try:
            acct = self.client.futures_account()
            equity = float(acct.get("totalWalletBalance") or acct.get("totalMarginBalance") or 0.0)
            unreal = float(acct.get("totalUnrealizedProfit") or 0.0)
            maint  = float(acct.get("totalMaintMargin") or 0.0)
            availableBalance = float(acct.get("availableBalance") or 0.0)
            margin_ratio = (maint / equity * 100.0) if equity > 0 else 0.0
            return {
                "equity": equity,
                "total_unrealized_pnl": unreal,
                "margin_ratio": margin_ratio,
                "available_balance" : availableBalance
            }
        except Exception:
            pass

        # 3) Spot fallback (very rough; used only if futures endpoints unavailable)
        try:
            acct = self.client.get_account()
            balances = acct.get("balances", [])
            usdt = next((b for b in balances if b.get("asset") == "USDT"), None)
            free = float(usdt["free"]) if usdt else 0.0
            locked = float(usdt["locked"]) if usdt else 0.0
            return {
                "equity": free + locked,
                "total_unrealized_pnl": 0.0,
                "margin_ratio": 0.0,
            }
        except Exception:
            return {}

    # Optional helpers (may be handy later)

    def list_balances(self, min_free: float = 0.0) -> List[Dict[str, Any]]:
        """
        Futures-first per-asset balances if available; otherwise spot balances.
        """
        # try futures per-asset
        try:
            rows = None
            if hasattr(self.client, "futures_account_balance"):
                rows = self.client.futures_account_balance()
            if rows:
                out = []
                for b in rows:
                    asset = b.get("asset")
                    free  = float(b.get("availableBalance") or b.get("crossWalletBalance") or b.get("balance") or 0.0)
                    if free >= min_free:
                        out.append({"asset": asset, "free": free})
                return out
        except Exception:
            pass

        # spot fallback
        try:
            acct = self.client.get_account()
            out = []
            for b in acct.get("balances", []):
                free = float(b["free"])
                locked = float(b["locked"])
                if free >= min_free or locked > 0:
                    out.append(
                        {"asset": b["asset"], "free": free, "locked": locked, "total": free + locked}
                    )
            return out
        except Exception:
            return []