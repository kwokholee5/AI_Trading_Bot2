"""
交易执行器
负责执行开仓、平仓等交易操作
"""
import time
from typing import Dict, Any, Optional

from src.api.hedge_client import HedgeClient
from src.utils.decorators import retry_on_failure, log_execution
from src.utils.symbol_filters import SymbolFilters
from src.utils.logger import get_logger


class Hedger:
    """交易执行器"""

    def __init__(self, client: HedgeClient, config: Dict[str, Any]):
        """
        初始化交易执行器

        Args:
            client: Binance API客户端
            config: 交易配置
        """
        self.client = client
        self.config = config
        self.position_manager = None  # 将在外部设置
        self.log = get_logger("hedge")  # 專用交易 logger
        self.log.info("hedge init")
    # --------------------- 内部工具 ---------------------

    def _get_filters(self, symbol: str) -> SymbolFilters:
        return self.client.get_symbol_filters(symbol)

    def _ensure_qty_price(self, symbol: str, quantity: float, price: Optional[float] = None):
        """
        根据交易对过滤规则修正数量/价格，并确保满足最小名义金额。
        返回 (adj_qty_str, adj_price_str, used_price_float)
        """
        filters = self._get_filters(symbol)

        # price used for notional checks
        if price is not None:
            used_price = float(price)
        else:
            t = self.client.get_ticker(symbol)
            p = t.get("lastPrice") or t.get("price") or t.get("markPrice")
            used_price = float(p)

        # Quantize to **strings** for API
        adj_qty_str = filters.quantize_qty(quantity)               # string
        adj_price_str = None if price is None else filters.quantize_price(price)  # string or None

        # For local checks, convert to float
        adj_qty_num = float(adj_qty_str)

        # Ensure min notional if present
        # (filters.minNotional may be str if you followed the Decimal approach)
        min_notional = float(getattr(filters, "minNotional", 0) or 0)
        if min_notional > 0 and (adj_qty_num * used_price) < min_notional:
            min_needed_qty = min_notional / max(used_price, 1e-12)
            adj_qty_str = filters.quantize_qty(min_needed_qty)     # string
            adj_qty_num = float(adj_qty_str)

        return adj_qty_str, adj_price_str, used_price  # used_price stays float

    def _quantize_stop_prices(self, symbol: str, take_profit: Optional[float], stop_loss: Optional[float]):
        filters = self._get_filters(symbol)
        tp = None if take_profit is None else filters.quantize_price(take_profit)  # string
        sl = None if stop_loss is None else filters.quantize_price(stop_loss)      # string
        return tp, sl

    # ==================== 开仓 ====================

    @log_execution
    @retry_on_failure(max_retries=3, delay=1)
    def open_long(self, symbol: str, quantity: float, leverage: int = None,
                  take_profit: float = None, stop_loss: float = None) -> Dict[str, Any]:
        """
        开多仓
        """
        # 调整杠杆
        self.log.info("OPEN_LONG %s qty=%s lev=%s", symbol, quantity, leverage)
        if leverage and leverage > 1:
            try:
                self.client.change_leverage(symbol, leverage)
                time.sleep(0.5)  # 等待杠杆调整生效
            except Exception as e:
                self.log.info(f"⚠️ 调整杠杆失败（继续开仓）: {e}")

        # 量化数量 & 名义金额检查
        adj_qty, _, used_price = self._ensure_qty_price(symbol, quantity)
        if float(adj_qty) <= 0:
            raise ValueError(f"{symbol} 数量无效（量化后<=0）")
        
        # 开仓
        try:
            order = self.client.create_market_order(
                symbol=symbol,
                side='BUY',
                quantity=adj_qty
            )
            self.log.info(f"✅ 开多仓成功: {symbol} {adj_qty}")

            # 设置止盈止损（量化 stopPrice）
            if take_profit or stop_loss:
                time.sleep(1)  # 等待订单成交
                # 用「覆蓋式」TP/SL，會先清掉舊的
                self.update_take_profit_stop_loss(
                    symbol=symbol,
                    side='BUY',   # open_long 用 BUY；open_short 用 SELL
                    quantity=adj_qty,
                    take_profit=take_profit,
                    stop_loss=stop_loss
                )

            return order
        except Exception as e:
            self.log.info(f"❌ 开多仓失败: {e}")
            raise

    @log_execution
    @retry_on_failure(max_retries=3, delay=1)
    def open_short(self, symbol: str, quantity: float, leverage: int = None,
                   take_profit: float = None, stop_loss: float = None) -> Dict[str, Any]:
        """
        开空仓
        """
        self.log.info("OPEN_SHORT %s qty=%s lev=%s", symbol, quantity, leverage)
        # 调整杠杆
        if leverage and leverage > 1:
            try:
                self.client.change_leverage(symbol, leverage)
                time.sleep(0.5)
            except Exception as e:
                self.log.info(f"⚠️ 调整杠杆失败（继续开仓）: {e}")

        # 量化数量 & 名义金额检查
        adj_qty, _, used_price = self._ensure_qty_price(symbol, quantity)
        if float(adj_qty) <= 0:
            raise ValueError(f"{symbol} 数量无效（量化后<=0）")

        # 开仓
        try:
            order = self.client.create_market_order(
                symbol=symbol,
                side='SELL',
                quantity=adj_qty
            )
            self.log.info(f"✅ 开空仓成功: {symbol} {adj_qty}")

            # 设置止盈止损（量化 stopPrice）
            if take_profit or stop_loss:
                time.sleep(2)
                # 用「覆蓋式」TP/SL，會先清掉舊的
                self.update_take_profit_stop_loss(
                    symbol=symbol,
                    side='SELL',   # open_long 用 BUY；open_short 用 SELL
                    quantity=adj_qty,
                    take_profit=take_profit,
                    stop_loss=stop_loss
                )

            return order
        except Exception as e:
            self.log.info(f"❌ 开空仓失败: {e}")
            raise

    # ==================== 平仓 ====================

    @log_execution
    @retry_on_failure(max_retries=3, delay=1)
    def close_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        平仓（平掉整个持仓）
        会自动判断当前持仓方向并执行反向操作
        """
        self.log.info("CLOSE_POSITION %s", symbol)
        try:
            position = self.client.get_position(symbol)
            if not position or float(position['positionAmt']) == 0:
                self.log.info(f"⚠️ {symbol} 无持仓")
                return None

            # 持仓方向：正数=多仓 → 用 SELL 平；负数=空仓 → 用 BUY 平
            amt = float(position['positionAmt'])
            side = 'SELL' if amt > 0 else 'BUY'
            amount = abs(amt)

            # 撤销所有挂单
            try:
                self.client.cancel_all_orders(symbol)
            except Exception:
                pass

            # 量化平仓数量（有的symbol需要按stepSize）
            adj_qty, _, _ = self._ensure_qty_price(symbol, amount)
            if float(adj_qty) <= 0:
                self.log.info(f"⚠️ {symbol} 平仓数量量化后为0，跳过")
                return None

            order = self.client.create_market_order(
                symbol=symbol,
                side=side,
                quantity=adj_qty
            )
            self.log.info(f"✅ 平仓成功: {symbol} {side} {adj_qty}")
            return order

        except Exception as e:
            self.log.info(f"❌ 平仓失败 {symbol}: {e}")
            raise

    def close_position_partial(self, symbol: str, percentage: float) -> Optional[Dict[str, Any]]:
        """
        部分平仓
        """
        self.log.info("CLOSE_PARTIAL %s pct=%.2f", symbol, percentage)
        if not 0 < percentage <= 1:
            raise ValueError("平仓比例必须在0-1之间")

        try:
            position = self.client.get_position(symbol)
            if not position or float(position['positionAmt']) == 0:
                self.log.info(f"⚠️ {symbol} 无持仓")
                return None

            total_amount = abs(float(position['positionAmt']))
            close_amount = total_amount * percentage

            side = 'SELL' if float(position['positionAmt']) > 0 else 'BUY'

            # 量化数量 & 名义金额检查
            adj_qty, _, _ = self._ensure_qty_price(symbol, close_amount)
            if float(adj_qty) <= 0:
                self.log.info(f"⚠️ {symbol} 部分平仓数量量化后为0，跳过")
                return None

            order = self.client.create_market_order(
                symbol=symbol,
                side=side,
                quantity=adj_qty,
                reduceOnly=True   # ← 關鍵：確保只會減少現有倉位
            )

            self.log.info(f"✅ 部分平仓成功: {symbol} {adj_qty} ({percentage*100}%)")
            return order

        except Exception as e:
            self.log.info(f"❌ 部分平仓失败 {symbol}: {e}")
            raise

    def force_close_position(self, symbol: str, reason: str) -> Optional[Dict[str, Any]]:
        """强制平仓（风控触发）"""
        self.log.info(f"🚨 强制平仓: {symbol}, 原因: {reason}")
        return self.close_position(symbol)

    # ==================== 止盈止损 ====================

    def _fmt_price(self, p) -> str:
        try:
            return f"{float(p):.2f}"
        except Exception:
            return str(p)
        
    def _set_take_profit_stop_loss(self, symbol: str, side: str, quantity: float,
                                   take_profit: float = None, stop_loss: float = None):
        """设置止盈止损（量化 stopPrice 到 tickSize）"""
        try:
            tp, sl = self._quantize_stop_prices(symbol, take_profit, stop_loss)
            orders = self.client.set_take_profit_stop_loss(
                symbol=symbol,
                side=side,
                quantity=quantity,          # 数量已在开仓时量化
                take_profit_price=tp,
                stop_loss_price=sl
            )

            if tp:
                self.log.info(f"   📈 止盈价: ${self._fmt_price(tp)}")
            if sl:
                self.log.info(f"   🛑 止损价: ${self._fmt_price(sl)}")

        except Exception as e:
            self.log.info(f"⚠️ 设置止盈止损失败: {e}")
    
    def update_take_profit_stop_loss(self, symbol: str, side: str,
                                 quantity: float,
                                 take_profit: float = None,
                                 stop_loss: float = None):
        """
        先清舊的 TP/SL/STOP 類單，再依目前設定建立新的。
        """
        self.log.info("UPDATE_TP_SL %s tp=%s sl=%s (will cancel old)", symbol, take_profit, stop_loss)
        try:
            # 先砍掉會關倉的舊條件單，避免累積
            try:
                self.client.cancel_close_orders(symbol)
            except Exception as e:
                self.log.info(f"⚠️ 清除舊 TP/SL 失敗（繼續覆蓋）: {e}")

            # 再設新的
            tp, sl = self._quantize_stop_prices(symbol, take_profit, stop_loss)
            self.client.set_take_profit_stop_loss(
                symbol=symbol,
                side=side,
                quantity=quantity,
                take_profit_price=tp,
                stop_loss_price=sl
            )

            if tp:
                self.log.info(f"   📈 新止盈價: ${self._fmt_price(tp)}")
            if sl:
                self.log.info(f"   🛑 新止損價: ${self._fmt_price(sl)}")

        except Exception as e:
            self.log.info(f"⚠️ 更新 TP/SL 失敗: {e}")