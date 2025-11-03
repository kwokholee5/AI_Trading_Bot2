"""
Binance API客户端封装
"""
import os
import time
import hmac
import hashlib
import requests
from typing import Optional, Dict, Any
from binance.client import Client
from binance.exceptions import BinanceAPIException
from typing import Optional, Dict, Any
from functools import lru_cache

class BinanceClient:
    """Binance API客户端封装"""
    
    def __init__(self, api_key: Optional[str] = None, 
                 api_secret: Optional[str] = None, timeout: int = 30):
        """
        初始化Binance客户端（正式网）
        
        Args:
            api_key: API密钥（默认从环境变量读取 BINANCE_API_KEY）
            api_secret: API密钥Secret（默认从环境变量读取 BINANCE_SECRET）
            timeout: 请求超时时间（秒）
        """
        self.api_key = api_key or os.getenv('BINANCE_API_KEY')
        self.api_secret = api_secret or os.getenv('BINANCE_SECRET')
        self.timeout = timeout
        
        # 使用正式网 U本位合约
        self.base_url = 'https://fapi.binance.com'
        self.coin_margin_base_url = self.base_url
        
        # 创建客户端
        try:
            self.client = Client(
                api_key=self.api_key,
                api_secret=self.api_secret,
                requests_params={'timeout': timeout}
            )
            print(f"🔗 连接到币安正式网 (U本位合约)")
            print(f"✅ 已连接到币安正式网")
        except Exception as e:
            print(f"❌ 初始化Binance客户端失败: {e}")
            raise
    
    def _coin_margin_request(self, method: str, endpoint: str, params: dict = None, signed: bool = True) -> dict:
        """
        发送币本位合约API请求
        
        Args:
            method: HTTP方法
            endpoint: API端点
            params: 请求参数
            signed: 是否需要签名
        """
        url = f"{self.coin_margin_base_url}{endpoint}"
        
        if params is None:
            params = {}
        
        if signed:
            params['timestamp'] = int(time.time() * 1000)
            query_string = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
            
            # 生成签名
            signature = hmac.new(
                self.api_secret.encode('utf-8'),
                query_string.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            params['signature'] = signature
            
            headers = {'X-MBX-APIKEY': self.api_key}
        else:
            headers = {}
        
        try:
            if method == 'GET':
                response = requests.get(url, params=params, headers=headers, timeout=self.timeout)
            else:
                response = requests.post(url, data=params, headers=headers, timeout=self.timeout)
            
            response.raise_for_status()
            return response.json()
            
        except Exception as e:
            print(f"⚠️ 币本位合约API请求失败: {e}")
            raise
    
    # ==================== 市场数据 ====================
    
    def get_klines(self, symbol: str, interval: str, limit: int = 500) -> list:
        """
        获取K线数据
        
        Args:
            symbol: 交易对，如 'BTCUSDT'
            interval: 时间间隔，如 '1m', '5m', '15m', '1h', '4h', '1d'
            limit: 获取数量
            
        Returns:
            K线数据列表
        """
        try:
            # 统一使用 U本位合约 API
            klines = self.client.futures_klines(symbol=symbol, interval=interval, limit=limit)
            return klines
        except BinanceAPIException as e:
            print(f"⚠️ 获取K线失败 {symbol} {interval}: {e}")
            return []
    
    def get_ticker(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取24小时行情数据
        
        Returns:
            {
                'lastPrice': '115000.00',
                'priceChangePercent': '1.23',
                'volume': '10000.00',
                'quoteVolume': '1150000.00',
                ...
            }
        """
        try:
            # 统一使用 U本位合约 API
            ticker = self.client.futures_ticker(symbol=symbol)
            return ticker
        except BinanceAPIException as e:
            print(f"⚠️ 获取行情失败 {symbol}: {e}")
            return None
    
    def get_funding_rate(self, symbol: str) -> Optional[float]:
        """获取资金费率"""
        try:
            data = self.client.futures_funding_rate(symbol=symbol, limit=1)
            if data and len(data) > 0:
                # 尝试多个可能的字段名
                for field in ['lastFundingRate', 'fundingRate', 'rate']:
                    if field in data[0]:
                        return float(data[0][field])
                # 如果都没有，返回第一列的数值字段
                return float(data[0].get('rate', 0)) if 'rate' in data[0] else None
            return None
        except (BinanceAPIException, KeyError, TypeError, ValueError) as e:
            print(f"⚠️ 获取资金费率失败 {symbol}: {e}")
            return None
    
    def get_open_interest(self, symbol: str) -> Optional[float]:
        """获取持仓量"""
        try:
            data = self.client.futures_open_interest(symbol=symbol)
            return float(data['openInterest']) if data else None
        except BinanceAPIException as e:
            print(f"⚠️ 获取持仓量失败 {symbol}: {e}")
            return None
    
    # ==================== 账户和持仓数据 ====================
    
    def get_account(self) -> Optional[Dict[str, Any]]:
        """
        获取期货账户信息
        
        Returns:
            {
                'totalWalletBalance': '10000.00',
                'availableBalance': '8000.00',
                'totalUnrealizedProfit': '100.00',
                ...
            }
        """
        try:
            # 统一使用 U本位合约 API
            account = self.client.futures_account()
            return account
        except BinanceAPIException as e:
            print(f"⚠️ 获取账户信息失败: {e}")
            return None
    
    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取持仓信息
        
        Returns:
            {
                'positionAmt': '0.001',  # 持仓数量（正数=多仓，负数=空仓）
                'entryPrice': '115000.00',
                'markPrice': '115050.00',
                'unRealizedProfit': '5.00',
                'leverage': '10',
                'isolatedMargin': '115.00',
                'liquidationPrice': '105000.00',
                ...
            }
        """
        try:
            # 统一使用 U本位合约 API
            positions = self.client.futures_position_information(symbol=symbol)
            
            # 查找有持仓的（positionAmt != '0'）
            for pos in positions:
                if float(pos['positionAmt']) != 0:
                    return pos
            return None
        except BinanceAPIException as e:
            print(f"⚠️ 获取持仓失败 {symbol}: {e}")
            return None
    
    def get_all_positions(self) -> list:
        """获取所有持仓"""
        try:
            # 统一使用 U本位合约 API
            positions = self.client.futures_position_information()
            
            # 只返回有持仓的（过滤掉positionAmt为0的）
            active_positions = [pos for pos in positions if float(pos['positionAmt']) != 0]
            return active_positions
        except BinanceAPIException as e:
            print(f"⚠️ 获取所有持仓失败: {e}")
            return []
    
    # ==================== 交易操作 ====================
    
    def create_market_order(self, symbol: str, side: str, quantity: float, **kwargs) -> Dict[str, Any]:
        """
        创建市价单（开仓或平仓）
        
        Args:
            symbol: 交易对
            side: 买卖方向 'BUY' 或 'SELL'
            quantity: 数量
            **kwargs: 其他参数
            
        Returns:
            订单信息
        """
        try:
            # 统一使用 U本位合约 API
            order = self.client.futures_create_order(
                symbol=symbol,
                side=side,
                type='MARKET',
                quantity=quantity,
                **kwargs
            )
            return order
        except BinanceAPIException as e:
            print(f"⚠️ 创建订单失败 {symbol} {side} {quantity}: {e}")
            raise
    
    def create_limit_order(self, symbol: str, side: str, quantity: float, 
                          price: float, **kwargs) -> Dict[str, Any]:
        """
        创建限价单
        
        Args:
            symbol: 交易对
            side: 买卖方向
            quantity: 数量
            price: 价格
            **kwargs: 其他参数
            
        Returns:
            订单信息
        """
        try:
            order = self.client.futures_create_order(
                symbol=symbol,
                side=side,
                type='LIMIT',
                timeInForce='GTC',
                quantity=quantity,
                price=price,
                **kwargs
            )
            return order
        except BinanceAPIException as e:
            print(f"⚠️ 创建限价单失败 {symbol} {side} {quantity} @ {price}: {e}")
            raise
    
    def cancel_order(self, symbol: str, order_id: int) -> Dict[str, Any]:
        """撤销订单"""
        try:
            result = self.client.futures_cancel_order(symbol=symbol, orderId=order_id)
            return result
        except BinanceAPIException as e:
            print(f"⚠️ 撤销订单失败 {symbol} {order_id}: {e}")
            raise
    
    def cancel_all_orders(self, symbol: str) -> Dict[str, Any]:
        """撤销所有挂单"""
        try:
            result = self.client.futures_cancel_all_open_orders(symbol=symbol)
            return result
        except BinanceAPIException as e:
            print(f"⚠️ 撤销所有订单失败 {symbol}: {e}")
            raise
    
    # ==================== 仓位管理 ====================
    
    def change_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """
        修改杠杆倍数
        
        Args:
            symbol: 交易对
            leverage: 杠杆倍数（1-100）
            
        Returns:
            修改结果
        """
        try:
            result = self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
            return result
        except BinanceAPIException as e:
            print(f"⚠️ 修改杠杆失败 {symbol} {leverage}x: {e}")
            raise
    
    def change_margin_type(self, symbol: str, margin_type: str = 'ISOLATED') -> Dict[str, Any]:
        """
        修改保证金类型
        
        Args:
            symbol: 交易对
            margin_type: 'ISOLATED'(逐仓) 或 'CROSSED'(全仓)
        """
        try:
            result = self.client.futures_change_margin_type(symbol=symbol, marginType=margin_type)
            return result
        except BinanceAPIException as e:
            print(f"⚠️ 修改保证金类型失败 {symbol} {margin_type}: {e}")
            raise
    
    def set_hedge_mode(self, enabled: bool = True):
        """
        设置持仓模式（双向持仓）
        
        Args:
            enabled: True=启用双向持仓, False=单向持仓
        """
        try:
            if enabled:
                result = self.client.futures_change_position_mode(dualSidePosition='true')
            else:
                result = self.client.futures_change_position_mode(dualSidePosition='false')
            return result
        except BinanceAPIException as e:
            print(f"⚠️ 设置持仓模式失败: {e}")
            raise
    
    # ==================== 止盈止损 ====================
    
    def set_take_profit_stop_loss(self, symbol: str, side: str, quantity: float, 
                                   take_profit_price: float = None, 
                                   stop_loss_price: float = None) -> list:
        """
        设置止盈止损
        
        注意：币安期货的止盈止损是通过特殊订单类型实现的
        
        Args:
            symbol: 交易对
            side: 方向 'BUY' 或 'SELL'
            quantity: 数量
            take_profit_price: 止盈价
            stop_loss_price: 止损价
            
        Returns:
            创建的订单列表
        """
        orders = []
        try:
            # 设置止盈
            if take_profit_price:
                tp_order = self.client.futures_create_order(
                    symbol=symbol,
                    side='SELL' if side == 'BUY' else 'BUY',
                    type='TAKE_PROFIT_MARKET',  # 止盈市价单
                    stopPrice=take_profit_price,
                    closePosition=True
                )
                orders.append(tp_order)
            
            # 设置止损
            if stop_loss_price:
                sl_order = self.client.futures_create_order(
                    symbol=symbol,
                    side='SELL' if side == 'BUY' else 'BUY',
                    type='STOP_MARKET',  # 止损市价单
                    stopPrice=stop_loss_price,
                    closePosition=True
                )
                orders.append(sl_order)
            
            return orders
            
        except BinanceAPIException as e:
            print(f"⚠️ 设置止盈止损失败 {symbol}: {e}")
            raise
    
    # ==================== 查询订单 ====================
    
    def get_order(self, symbol: str, order_id: int) -> Optional[Dict[str, Any]]:
        """查询订单"""
        try:
            order = self.client.futures_get_order(symbol=symbol, orderId=order_id)
            return order
        except BinanceAPIException as e:
            print(f"⚠️ 查询订单失败 {symbol} {order_id}: {e}")
            return None
    
    def get_open_orders(self, symbol: str = None) -> list:
        """获取所有挂单"""
        try:
            if symbol:
                orders = self.client.futures_get_open_orders(symbol=symbol)
            else:
                orders = self.client.futures_get_all_orders()
            return orders
        except BinanceAPIException as e:
            print(f"⚠️ 获取挂单失败: {e}")
            return []
    
    # ==================== 工具方法 ====================
    
    def get_server_time(self) -> Dict[str, Any]:
        """获取服务器时间"""
        try:
            time = self.client.futures_time()
            return time
        except BinanceAPIException as e:
            print(f"⚠️ 获取服务器时间失败: {e}")
            return None
    
    def test_connection(self) -> bool:
        """测试连接"""
        try:
            self.get_server_time()
            return True
        except Exception as e:
            print(f"⚠️ 连接测试失败: {e}")
            return False

    @lru_cache(maxsize=256)
    def get_symbol_filters(self, symbol: str):
        """
        Return SymbolFilters for a USDT-M futures symbol.
        Uses futures_exchange_info() and extracts the symbol's filters.
        Cached to avoid repeated calls.
        """
        try:
            info = self.client.futures_exchange_info()  # dict with "symbols": [...]
            symbols = info.get("symbols", [])
            target = next((s for s in symbols if s.get("symbol") == symbol), None)
            if not target:
                # Fallback: direct REST call to /fapi/v1/exchangeInfo?symbol=...
                import requests, time
                url = f"{self.base_url}/fapi/v1/exchangeInfo"
                resp = requests.get(url, params={"symbol": symbol}, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                syms = data.get("symbols", [])
                target = syms[0] if syms else None

            if not target:
                raise ValueError(f"Symbol {symbol} not found in exchange info")

            from src.utils.symbol_filters import SymbolFilters
            return SymbolFilters(target)

        except Exception as e:
            # Surface a clear error; your retry decorator can handle transient issues
            raise RuntimeError(f"Failed to load filters for {symbol}: {e}")