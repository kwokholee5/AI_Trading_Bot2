# src/data/position_data.py
from __future__ import annotations

import json
import os
import pathlib
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional


@dataclass
class Position:
    symbol: str               # e.g., "BTCUSDT"
    side: str                 # "LONG" | "SHORT"
    positionAmt: float                # absolute position size (>0)
    isolatedMargin: float
    entry_price: float
    mark_price: float
    leverage: float
    unrealized_pnl: float
    pnl_percent: float
    liq_price: Optional[float]
    isolated: bool
    opened_at: float          # epoch seconds
    meta: Dict[str, Any]


class PositionDataManager:
    """
    Futures-first position manager.

    - Reads live position info from the project's BinanceClient wrapper
      (wrapper.get_position / wrapper.get_all_positions) when available.
    - Falls back to raw python-binance futures endpoints.
    - Also keeps a small JSON file for local persistence / debugging.
    """

    def __init__(self, client=None, path: str = "data/positions.json"):
        # keep both: the wrapper (repo class) and the raw python-binance client
        self.wrapper = client
        self.client = getattr(client, "client", client)
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write([])

    # ----------------- storage helpers -----------------

    def _read(self) -> List[Dict[str, Any]]:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []

    def _write(self, rows: List[Dict[str, Any]]) -> None:
        self.path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    # ----------------- exchange fetchers -----------------

    def _fetch_exchange_position(self, symbol: str) -> Optional[Position]:
        """
        Query the exchange for a single symbol's active position (size != 0).
        Prefer the repo wrapper; fallback to python-binance.
        """
        symbol = symbol.upper()

        # 1) Try wrapper first
        try:
            if self.wrapper and hasattr(self.wrapper, "get_position"):
                pos = self.wrapper.get_position(symbol)  # None or dict
                if pos:
                    return self._normalize_pos_dict(symbol, pos)
        except Exception as e:
            print(e)
            return None

    def _fetch_all_exchange_positions(self) -> List[Position]:
        # 1) wrapper first
        try:
            if self.wrapper and hasattr(self.wrapper, "get_all_positions"):
                rows = self.wrapper.get_all_positions() or []
                return [self._normalize_pos_dict(r.get("symbol", ""), r)
                        for r in rows
                        if float(r.get("positionAmt", 0)) != 0.0]
        except Exception:
            return []

    # ----------------- normalization -----------------

    def _normalize_pos_dict(self, symbol: str, d: Dict[str, Any]) -> Position:
        """
        Convert various futures position dicts into our Position dataclass.
        Works with both wrapper.get_position() and python-binance rows.
        """
        symbol = (symbol or d.get("symbol", "")).upper()
        amt = float(d.get("positionAmt", 0.0))
        side = "LONG" if amt > 0 else "SHORT"
        positionAmt = abs(amt)
        isolatedMargin = float(d.get("isolatedMargin", 0.0) or 0.0)
        entry = float(d.get("entryPrice", 0.0) or 0.0)
        mark = float(d.get("markPrice", d.get("markPriceAvg", 0.0)) or 0.0)
        lev = float(d.get("leverage", 0.0) or 0.0)
        upnl = float(d.get("unRealizedProfit", d.get("unrealizedProfit", 0.0)) or 0.0)
        liq = d.get("liquidationPrice", None)
        liq_price = float(liq) if liq not in (None, "", "0", 0) else None

        isolated_flag = str(d.get("isolated", d.get("marginType", ""))).upper()
        isolated = (isolated_flag == "TRUE") or (isolated_flag == "ISOLATED")

        pnl_percent = (upnl / isolatedMargin) * 100
        return Position(
            symbol=symbol,
            side=side,
            positionAmt=positionAmt,
            isolatedMargin=isolatedMargin,
            entry_price=entry,
            mark_price=mark,
            leverage=lev,
            unrealized_pnl=upnl,
            pnl_percent=pnl_percent,
            liq_price=liq_price,
            isolated=isolated,
            opened_at=time.time(),  # we don’t have exchange open time here
            meta=d,
        )

    # ----------------- public API expected by main.py -----------------

    def get_current_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Return a JSON-serializable dict for the symbol's active position, or None.
        Exchange → fallback to local JSON.
        """
        pos = self._fetch_exchange_position(symbol)
        if pos is None:
            # fallback to local cache
            cached = self.get(symbol)
            if not cached:
                return None
            pos = cached

        return {
            "symbol": pos.symbol,
            "side": pos.side,
            "positionAmt": pos.positionAmt,
            "entry_price": pos.entry_price,
            "mark_price": pos.mark_price,
            "leverage": pos.leverage,
            "unrealized_pnl": pos.unrealized_pnl,
            "pnl_percent" : pos.pnl_percent,
            "isolatedMargin" : pos.isolatedMargin,
            "liq_price": pos.liq_price,
            "isolated": pos.isolated,
            "opened_at": pos.opened_at,
            "meta": pos.meta,
        }

    def get_all_open_positions(self) -> List[Dict[str, Any]]:
        """
        List all open positions as dicts. Exchange-first, fallback to local cache.
        """
        rows = self._fetch_all_exchange_positions()
        if not rows:
            rows = self.list_open()

        out: List[Dict[str, Any]] = []
        for p in rows:
            if isinstance(p, Position):
                out.append({
                    "symbol": p.symbol,
                    "side": p.side,
                    "positionAmt": p.positionAmt,
                    "entry_price": p.entry_price,
                    "mark_price": p.mark_price,
                    "leverage": p.leverage,
                    "unrealized_pnl": p.unrealized_pnl,
                    "pnl_percent" : p.pnl_percent,
                    "isolatedMargin" : p.isolatedMargin,
                    "liq_price": p.liq_price,
                    "isolated": p.isolated,
                    "opened_at": p.opened_at,
                    "meta": p.meta,
                })
            else:
                # already a dict (from local cache)
                out.append(p)
        return out

    # ----------------- local cache convenience -----------------

    def list_open(self) -> List[Position]:
        return [Position(**row) for row in self._read()]

    def get(self, symbol: str) -> Optional[Position]:
        symbol = symbol.upper()
        for p in self.list_open():
            if p.symbol.upper() == symbol:
                return p
        return None

    def upsert(self, symbol: str, side: str, positionAmt: float, entry_price: float, meta: Optional[Dict[str, Any]] = None) -> Position:
        symbol = symbol.upper()
        existing = self.list_open()
        now = time.time()
        pos = Position(
            symbol=symbol,
            side=side.upper(),
            positionAmt=float(positionAmt),
            entry_price=float(entry_price),
            mark_price=float(meta.get("mark_price", entry_price) if meta else entry_price),
            leverage=float(meta.get("leverage", 0) if meta else 0),
            unrealized_pnl=float(meta.get("unrealized_pnl", 0) if meta else 0),
            liq_price=float(meta.get("liq_price", 0)) if meta and meta.get("liq_price") else None,
            isolated=bool(meta.get("isolated", True) if meta else True),
            opened_at=now,
            meta=meta or {},
        )
        kept: List[Dict[str, Any]] = [asdict(p) for p in existing if p.symbol.upper() != symbol]
        kept.append(asdict(pos))
        self._write(kept)
        return pos

    def close(self, symbol: str) -> bool:
        symbol = symbol.upper()
        before = self.list_open()
        after = [asdict(p) for p in before if p.symbol.upper() != symbol]
        self._write(after)
        return len(after) != len(before)