"""
提示詞/JSON 構建器
把市場數據轉為 JSON 載荷，並可生成給模型的中文提示詞（內嵌 JSON）
"""
from typing import Dict, Any, List, Optional
from datetime import datetime
import math
import json
from decimal import Decimal, InvalidOperation

class PromptBuilder:
    """提示詞構建器（支援 JSON 輸出）"""

    @staticmethod
    def _decimals_from_step(step, default_dp: int = 2) -> int:
        """
        Derive number of decimal places from a tick/step value.
        Works with strings like '0.01000000' or scientific '1e-6', and floats.
        Returns default_dp if step is missing/invalid/non-positive.
        """
        if step is None:
            return default_dp
        try:
            # Always go through str() to preserve precision (e.g. '1e-6')
            d = Decimal(str(step))
            if d <= 0:
                return default_dp
            # Decimal exponent is negative for decimals; -exponent == number of dp
            # Example: 0.01 -> exponent -2 -> dp 2; 1E-6 -> exponent -6 -> dp 6
            dp = max(0, -d.as_tuple().exponent)
            # Optional safety clamp
            if dp > 18:
                dp = 18
            return int(dp)
        except (InvalidOperation, ValueError, TypeError):
            return default_dp
        
    def __init__(self, config: Dict[str, Any] , precision_map:Dict[str, Dict[str, int]]):
        """
        初始化提示詞構建器
        Args:
            config: 交易配置
        """
        self.config = config
        self.ai_config = config.get("ai", {})
        # 預設的時間框架輸出順序（只輸出存在於資料中的）
        self.default_intervals = ["5m", "15m", "1h", "4h", "1D"]

        # === 新增：每個 symbol 的精度表 ===
        # 結構：{"BTCUSDT": {"price_dp": 2, "qty_dp": 6}, ...}
        self.symbol_precisions = precision_map

    # ---------------------------
    # 小工具：數值安全處理 / 取值 / 四捨五入
    # ---------------------------
    @staticmethod
    def _is_num(x) -> bool:
        try:
            return (x is not None) and (not isinstance(x, bool)) and math.isfinite(float(x))
        except Exception:
            return False

    @staticmethod
    def _to_float(x, default: float = 0.0) -> float:
        try:
            v = float(x)
            if math.isfinite(v):
                return v
            return default
        except Exception:
            return default

    @staticmethod
    def _round(x, n=4):
        try:
            return round(float(x), n)
        except Exception:
            return 0.0

    @staticmethod
    def _get(d: dict, key: str, default=0.0, n: Optional[int] = None):
        v = d.get(key, default)
        if n is None:
            return PromptBuilder._to_float(v, default if isinstance(default, (int, float)) else 0.0)
        return PromptBuilder._round(v, n)

    @staticmethod
    def _norm_confidence(c) -> float:
        """
        把字串 HIGH/MEDIUM/LOW 或數字轉成 0~1 浮點數
        """
        if isinstance(c, (int, float)):
            try:
                v = float(c)
                if 0.0 <= v <= 1.0:
                    return v
            except Exception:
                pass
            return 0.5
        if isinstance(c, str):
            cs = c.strip().upper()
            if cs == "HIGH":
                return 0.8
            if cs == "MEDIUM":
                return 0.6
            if cs == "LOW":
                return 0.4
            try:
                v = float(c)
                if 0.0 <= v <= 1.0:
                    return v
            except Exception:
                pass
        return 0.5

    def _price_dp(self, symbol: str, fallback: int = 2) -> int:
        return int(self.symbol_precisions.get(symbol, {}).get("price_dp", fallback))

    def _qty_dp(self, symbol: str, fallback: int = 4) -> int:
        return int(self.symbol_precisions.get(symbol, {}).get("qty_dp", fallback))

    def _round_price(self, symbol: str, x: Any) -> float:
        try:
            return round(float(x), self._price_dp(symbol))
        except Exception:
            return 0.0

    def _round_qty(self, symbol: str, x: Any) -> float:
        try:
            return round(float(x), self._qty_dp(symbol))
        except Exception:
            return 0.0

    # ---------------------------
    # 歷史決策分組：按幣種歸檔（舊→新）
    # ---------------------------
    def _group_history_by_symbol(
        self,
        decision_history: Optional[List[Dict[str, Any]]],
        max_per_symbol: int = 10,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        將全域 decision_history 依 symbol 分組，輸出為「舊→新」。
        若超過 max_per_symbol，保留最後 N 筆（最近 N 筆），
        但輸出順序仍維持舊→新以與 RSI/MACD/OHLC 一致。
        """
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        if not decision_history:
            return grouped

        # 先將全部紀錄按時間「舊→新」排序
        def _ts_key(rec: Dict[str, Any]) -> float:
            ts = rec.get("timestamp")
            try:
                return datetime.fromisoformat(ts).timestamp()
            except Exception:
                return 0.0

        sorted_all = sorted(decision_history, key=_ts_key, reverse=False)  # 舊→新

        # 依幣種分桶
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for rec in sorted_all:
            sym = rec.get("symbol")
            if not sym:
                continue
            arr = buckets.setdefault(sym, [])
            arr.append(rec)

        # 對每個幣種：只保留最後 N 筆（最近 N 筆），但輸出順序仍舊→新
        for sym, arr in buckets.items():
            trimmed = arr[-max_per_symbol:]

            cleaned_list: List[Dict[str, Any]] = []
            for rec in trimmed:
                cleaned = {
                    "timestamp": rec.get("timestamp"),
                    "action": rec.get("action"),
                    "confidence": self._norm_confidence(rec.get("confidence")),
                    "leverage": self._to_float(rec.get("leverage"), 0.0),
                    "position_percent": self._to_float(rec.get("position_percent"), 0.0),
                    "reason": rec.get("reason"),
                    "price": self._to_float(rec.get("price"), 0.0),
                }
                cleaned_list.append(cleaned)

            grouped[sym] = cleaned_list  # 舊→新

        return grouped

    # ---------------------------
    # 單一時間框架 → JSON 區塊（價格類欄位依 symbol 精度）
    # ---------------------------
    def _build_interval_block(self, interval: str, data: Dict[str, Any], symbol: str) -> Optional[Dict[str, Any]]:
        """
        將單一 timeframe 的資料整理成 JSON block：
        {
          "time_frame": "5m",
          "boll_upper": ...,
          "boll_middle": ...,
          "boll_lower": ...,
          "funding": 0.0001,
          "rsi": [ ... 10 values, old->new ],
          "macd": [ ... 10 values, old->new ],
          "histogram": [ ... 10 values, old->new ],
          "ema20":  ...,
          "ema50":  ...,
          "sma20":  ...,
          "sma50":  ...,
          "atr14":  ...,
          "ohlc": [ {O,H,L,C,V}, ... 10 rows old->new ]
        }
        """
        if not data:
            return None

        ind = data.get("indicators", {}) or {}
        df = data.get("dataframe")

        # 價格類指標：動態價格精度
        block: Dict[str, Any] = {
            "time_frame": interval,
            "boll_upper": self._round_price(symbol, ind.get("bollinger_upper", 0.0)),
            "boll_middle": self._round_price(symbol, ind.get("bollinger_middle", 0.0)),
            "boll_lower": self._round_price(symbol, ind.get("bollinger_lower", 0.0)),
            "ema20": self._round_price(symbol, ind.get("ema_20", 0.0)),
            "ema50": self._round_price(symbol, ind.get("ema_50", 0.0)),
            "sma20": self._round_price(symbol, ind.get("sma_20", 0.0)),
            "sma50": self._round_price(symbol, ind.get("sma_50", 0.0)),
            "atr14": self._round_price(symbol, ind.get("atr_14", 0.0)),  # ATR 為價格距離，也用價格精度
        }

        # ===== RSI / MACD arrays（舊→新）=====
        rsi_arr, macd_arr, hist_arr = [], [], []
        if df is not None and len(df) >= 30 and "close" in df:
            closes = df["close"]

            # RSI（1 位小數）
            try:
                delta = closes.diff()
                gain = delta.where(delta > 0, 0).rolling(window=14).mean()
                loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
                rs = gain / loss
                rsi_full = 100 - (100 / (1 + rs))
                rsi_arr = [self._round(x, 1) for x in rsi_full.tail(10).tolist()]
            except Exception:
                pass

            # MACD 與 Hist（4 位小數）
            try:
                ema_fast = closes.ewm(span=12, adjust=False).mean()
                ema_slow = closes.ewm(span=26, adjust=False).mean()
                macd_line = ema_fast - ema_slow
                signal_line = macd_line.ewm(span=9, adjust=False).mean()
                hist = macd_line - signal_line
                macd_arr = [self._round(x, 4) for x in macd_line.tail(10).tolist()]
                hist_arr = [self._round(x, 4) for x in hist.tail(10).tolist()]
            except Exception:
                pass

        block["rsi"] = rsi_arr
        block["macd"] = macd_arr
        block["histogram"] = hist_arr

        # ===== OHLC（最近10根，舊→新；價格用動態價格精度）=====
        ohlc_list: List[Dict[str, float]] = []
        if df is not None and len(df) > 0:
            tail = df.tail(10)
            for _, row in tail.iterrows():
                o = self._round_price(symbol, row.get("open", 0))
                h = self._round_price(symbol, row.get("high", 0))
                l = self._round_price(symbol, row.get("low", 0))
                c = self._round_price(symbol, row.get("close", 0))
                v = self._round(row.get("volume", 0), 0)  # 量仍用 0 位
                ohlc_list.append({"O": o, "H": h, "L": l, "C": c, "V": v})
        block["ohlc"] = ohlc_list

        return block

    # ---------------------------
    # 整體：多幣種 → JSON 載荷（dict）
    # ---------------------------
    def build_multi_symbol_analysis_payload(
        self,
        all_symbols_data: Dict[str, Any],
        all_positions: Dict[str, Any],
        account_summary: Optional[Dict[str, Any]] = None,
        decision_history: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        產出**JSON 載荷**（Python dict 可直接 json.dumps）
        結構：
        {
          "meta": {...},
          "account": {...},
          "symbols": [
            {
              "market": "ETH/USDT",
              "funding": ...,
              "open_interest": ...,
              "current_price": ...,
              "position": {...} | null,
              "market_data": [ {...}, ... ],
              "decision_history": [ {... 舊→新 ...} ]
            },
            ...
          ]
        }
        """
        payload: Dict[str, Any] = {
            "meta": {
                "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "exchange": "Binance Perp (USDT-M)",
            },
            "account": {},
            "symbols": [],
        }

        # 帳戶摘要
        if account_summary:
            payload["account"] = {
                "equity": self._get(account_summary, "equity", 0.0, 2),
                "available_balance": self._get(account_summary, "available_balance", 0.0, 2),
                "total_unrealized_pnl": self._get(account_summary, "total_unrealized_pnl", 0.0, 2),
                "risk": {
                    "default_stop_loss_percent": self._get(self.config.get("risk", {}), "stop_loss_default_percent", 2.0, 4),
                    "default_take_profit_percent": self._get(self.config.get("risk", {}), "take_profit_default_percent", 5.0, 4),
                },
                "position_rules": {
                    "min_position_percent": self._get(self.config.get("trading", {}), "min_position_percent", 10.0, 4),
                    "max_position_percent": self._get(self.config.get("trading", {}), "max_position_percent", 30.0, 4),
                },
            }

        # 將歷史決策按幣種分組（舊→新）
        grouped_hist = self._group_history_by_symbol(decision_history, max_per_symbol=10)

        # 遍歷幣種
        for symbol, symbol_data in all_symbols_data.items():
            market_data = symbol_data.get("market_data", {}) or {}
            position = symbol_data.get("position")
            coin_name = symbol.replace("USDT", "")
            realtime = (market_data.get("realtime") or {})

            # 頂層行情（價格用動態價格精度）
            current_price = self._round_price(symbol, realtime.get("price", 0.0))
            funding_rate = self._get(realtime, "funding_rate", 0.0, 6)
            open_interest = self._get(realtime, "open_interest", 0.0, 0)

            symbol_obj: Dict[str, Any] = {
                "market": f"{coin_name}/USDT",
                "funding": funding_rate,
                "open_interest": open_interest,
                "current_price": current_price,
                "position": None,
                "market_data": [],
                # 這裡掛上該幣種的歷史決策（舊→新）
                "decision_history": grouped_hist.get(symbol, []),
            }

            # 持倉（若有）— 數量用 qty 精度，價格用 price 精度
            if position:
                symbol_obj["position"] = {
                    "side": position.get("side") or ("LONG" if self._to_float(position.get("positionAmt"), 0.0) > 0 else "SHORT"),
                    "positionAmt": self._round_qty(symbol, position.get("positionAmt", 0.0)),
                    "entry_price": self._round_price(symbol, position.get("entry_price", 0.0)),
                    "leverage": self._to_float(position.get("leverage"), 0.0),
                    "unrealized_pnl": self._get(position, "unrealized_pnl", 0.0, 4),
                    "pnl_percent": self._get(position, "pnl_percent", 0.0, 4),
                    "isolatedMargin": self._get(position, "isolatedMargin", 0.0, 4),
                    "updateTime": position.get("updateTime") or 0,
                }

            # 各時間框架
            multi = market_data.get("multi_timeframe", {}) or {}
            for interval in self.default_intervals:
                if interval not in multi:
                    continue
                block = self._build_interval_block(interval, multi.get(interval) or {}, symbol)
                if block:
                    # 若希望每個 timeframe 也帶 funding，可複製 symbol 層的 funding（可選）
                    block["funding"] = funding_rate
                    symbol_obj["market_data"].append(block)

            payload["symbols"].append(symbol_obj)

        return payload

    # ---------------------------
    # 文字提示：內嵌 JSON（給 DeepSeek）
    # ---------------------------
    def build_multi_symbol_analysis_prompt_json(
        self,
        all_symbols_data: Dict[str, Any],
        all_positions: Dict[str, Any],
        account_summary: Optional[Dict[str, Any]] = None,
        decision_history: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        產生**中文提示詞** + 內嵌 **JSON 載荷**。
        模型請以該 JSON 為依據，回傳每個幣種的決策 JSON。
        """
        payload = self.build_multi_symbol_analysis_payload(
            all_symbols_data, all_positions, account_summary, decision_history
        )
        payload_json = json.dumps(payload, ensure_ascii=False, indent=2)

        prompt = f"""
你是一位專業的日內交易員。以下提供多幣種的結構化市場資料（JSON），
請逐一分析每個幣種並輸出**決策 JSON**，格式如下（幣種鍵以實際輸入為準）：

{{
  "BTCUSDT": {{
    "action": "BUY_OPEN" | "SELL_OPEN" | "CLOSE" | "HOLD",
    "reason": "1-2句話說明決策理由（含關鍵指標與數值）",
    "confidence": 0.0 - 1.0,
    "leverage": 1-100,
    "position_percent": 0-30,
    "take_profit_percent": 5.0,
    "stop_loss_percent": -2.0
  }},
  "...": {{ ... }}
}}

說明：
- 若判斷風險較高或趨勢不明確，可使用 HOLD。
- BUY_OPEN/SELL_OPEN 時務必提供合理止盈止損百分比。
- 可參考 market_data 內不同 time_frame 的 RSI/MACD/HIST 與 OHLC（皆為「舊→新」序列）。
- 每個幣種下方含有該幣的 decision_history（舊→新），可用以對齊你的建議與既有持倉/歷史。

# 當前時間
{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

# 帳戶配置（僅供參考）
- 最小倉位: {self.config.get('trading', {}).get('min_position_percent', 10)}%
- 最大倉位: {self.config.get('trading', {}).get('max_position_percent', 30)}%
- 預設止損: -{self.config.get('risk', {}).get('stop_loss_default_percent', 2)}%
- 預設止盈: +{self.config.get('risk', {}).get('take_profit_default_percent', 5)}%

# 市場資料 JSON（請據此做判斷）
{payload_json}
""".strip()

        return prompt

    # ---------------------------
    # 保留原本的單幣 Prompt（若你還要用）
    # ---------------------------
    def build_analysis_prompt(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        position: Optional[Dict[str, Any]] = None,
        history: List[Dict[str, Any]] = None,
    ) -> str:
        """
        舊版：單幣種的文字型提示（保留以防有用）
        """
        prompt = f"""
# 加密貨幣期貨交易分析
當前時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## 帳戶資訊
- 幣種: {symbol}
- 槓桿範圍: 1-100倍（建議3-10倍）

## 風險/倉位
- 最小倉位: {self.config['trading'].get('min_position_percent', 10)}%
- 最大倉位: {self.config['trading'].get('max_position_percent', 30)}%
- 預設止損: -{self.config['risk'].get('stop_loss_default_percent', 2)}%
- 預設止盈: +{self.config['risk'].get('take_profit_default_percent', 5)}%

請分析下面市場資料並輸出決策 JSON（同多幣格式的一個子集）。
"""
        return prompt.strip()

    # ---------------------------
    # 舊的純文字渲染（若你需要仍可使用）
    # ---------------------------
    def _format_market_data(self, symbol: str, market_data: Dict[str, Any]) -> str:
        """舊版：把單幣市場數據渲染成文字（保留）"""
        realtime = market_data.get("realtime", {})
        multi_data = market_data.get("multi_timeframe", {})
        result = f"### {symbol} 即時行情\n"

        price = realtime.get("price") or 0
        change_24h = realtime.get("change_24h") or 0
        change_15m = realtime.get("change_15m") or 0
        funding_rate = realtime.get("funding_rate") or 0
        open_interest = realtime.get("open_interest") or 0

        result += f"- 當前價格: ${price:,.2f}\n"
        result += f"- 24h漲跌: {change_24h:.2f}%\n"
        result += f"- 15m漲跌: {change_15m:.2f}%\n"
        result += f"- 資金費率: {funding_rate:.6f}\n"
        result += f"- 持倉量: {open_interest:,.0f}\n"

        for interval, data in multi_data.items():
            if "indicators" not in data:
                continue
            ind = data["indicators"]
            df = data.get("dataframe")
            result += f"\n### {interval} 週期\n"
            if df is not None and len(df) >= 3:
                for _, row in df.tail(3).iterrows():
                    close = row["close"]
                    change = ((row["close"] - row["open"]) / row["open"]) * 100
                    result += f"- K線: C${close:.2f} ({change:+.2f}%)\n"
            rsi = ind.get("rsi") or 0
            macd = ind.get("macd") or 0
            macd_signal = ind.get("macd_signal") or 0
            macd_hist = ind.get("macd_histogram") or 0
            ema20 = ind.get("ema_20") or 0
            ema50 = ind.get("ema_50") or 0
            atr = ind.get("atr_14") or 0
            result += f"- RSI(14): {rsi:.1f}\n"
            result += f"- MACD: {macd:.2f}, Signal: {macd_signal:.2f}, Hist: {macd_hist:.2f}\n"
            result += f"- EMA20: {ema20:.2f}, EMA50: {ema50:.2f}\n"
            result += f"- ATR(14): {atr:.2f}\n"
        return result

    def _format_account_summary(self, account_summary: Dict[str, Any]) -> str:
        if not account_summary:
            return ""
        equity = account_summary.get("equity", 0)
        available = account_summary.get("available_balance", 0)
        unrealized_pnl = account_summary.get("total_unrealized_pnl", 0)
        return f"""
帳戶餘額: {equity:.2f} USDT
可用餘額: {available:.2f} USDT
未實現損益: {unrealized_pnl:+.2f} USDT
""".strip()