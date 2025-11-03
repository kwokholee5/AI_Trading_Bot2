# src/utils/symbol_filters.py
from __future__ import annotations
from typing import Dict, Any
from decimal import Decimal, ROUND_DOWN

class SymbolFilters:
    def __init__(self, info: Dict[str, Any]):
        self.info = info or {}
        self.filters = {f["filterType"]: f for f in self.info.get("filters", [])}

        lot = self.filters.get("LOT_SIZE") or self.filters.get("MARKET_LOT_SIZE") or {}
        pricef = self.filters.get("PRICE_FILTER") or {}
        notional = self.filters.get("MIN_NOTIONAL") or {}

        # Keep as strings → convert to Decimal later (no float artifacts)
        self.stepSize = lot.get("stepSize", "0.001")
        self.minQty   = lot.get("minQty", "0.001")
        self.maxQty   = lot.get("maxQty", "100000000")

        self.tickSize = pricef.get("tickSize", "0.01")
        self.minPrice = pricef.get("minPrice", "0.01")
        self.maxPrice = pricef.get("maxPrice", "100000000")

        # On USDT Futures it’s often "notional"; on some it’s "minNotional"
        self.minNotional = notional.get("notional") or notional.get("minNotional") or "0"

        # Prebuild Decimals
        self._step  = Decimal(self.stepSize)
        self._minQ  = Decimal(self.minQty)
        self._maxQ  = Decimal(self.maxQty)
        self._tick  = Decimal(self.tickSize)
        self._minP  = Decimal(self.minPrice)
        self._maxP  = Decimal(self.maxPrice)
        self._minN  = Decimal(self.minNotional)

    def _floor_to_step(self, value: Decimal, step: Decimal) -> Decimal:
        # emulate floor(value/step)*step in Decimal space
        return (value // step) * step

    def quantize_qty(self, qty: float | str) -> str:
        q = Decimal(str(qty))
        q = max(self._minQ, min(q, self._maxQ))
        q = self._floor_to_step(q, self._step)
        if q < self._minQ:
            q = self._minQ
        # Normalize to plain string (no scientific notation)
        return format(q.normalize(), 'f')

    def quantize_price(self, price: float | str) -> str:
        p = Decimal(str(price))
        p = max(self._minP, min(p, self._maxP))
        p = self._floor_to_step(p, self._tick)
        if p < self._minP:
            p = self._minP
        return format(p.normalize(), 'f')

    def meets_notional(self, qty: float | str, price: float | str) -> bool:
        if self._minN <= 0:
            return True
        q = Decimal(str(qty))
        p = Decimal(str(price))
        return (q * p) >= self._minN