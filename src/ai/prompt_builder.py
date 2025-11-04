"""
提示词/JSON 构建器
把市场数据转为 JSON 载荷，并可生成给模型的中文提示词（内嵌 JSON）
"""
from typing import Dict, Any, List, Optional
from datetime import datetime
import math
import json
from decimal import Decimal, InvalidOperation


class PromptBuilder:
    """提示词构建器（支援 JSON 输出）"""

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
            d = Decimal(str(step))  # preserve precision
            if d <= 0:
                return default_dp
            dp = max(0, -d.as_tuple().exponent)  # 0.01 -> 2; 1E-6 -> 6
            return int(min(dp, 18))
        except (InvalidOperation, ValueError, TypeError):
            return default_dp

    def __init__(self, config: Dict[str, Any], precision_map: Dict[str, Dict[str, int]]):
        """
        初始化提示词构建器
        Args:
            config: 交易配置
            precision_map: 每个 symbol 的精度表 {"BTCUSDT": {"price_dp": 2, "qty_dp": 6}, ...}
        """
        self.config = config
        self.ai_config = config.get("ai", {})
        # 预设的时间框架输出顺序（只输出存在于资料中的）
        self.default_intervals = ["3m" , "5m", "15m", "1h", "4h", "1d"]
        self.symbol_precisions = precision_map

    # ---------------------------
    # 小工具：数值安全处理 / 取值 / 四捨五入
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
        把字串 HIGH/MEDIUM/LOW 或数字转成 0~1 浮点数
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

    # ---------------------------
    # K线形态检测
    # ---------------------------
    @staticmethod
    def _detect_candlestick_patterns(ohlc_tail: List[Dict[str, float]]) -> List[str]:
        patterns: List[str] = []
        if len(ohlc_tail) == 0:
            return patterns

        def body(o, c): return abs(c - o)
        def upper(o, h, c): return h - max(o, c)
        def lower(o, l, c): return min(o, c) - l

        last = ohlc_tail[-1]
        o, h, l, c = last["O"], last["H"], last["L"], last["C"]
        rng = max(1e-9, h - l)
        b = body(o, c)
        up = upper(o, h, c)
        lo = lower(o, l, c)

        if b <= rng * 0.1:
            patterns.append("Doji")
        if (lo >= rng * 0.5) and (up <= rng * 0.2) and (c > o):
            patterns.append("Hammer")
        if (up >= rng * 0.5) and (lo <= rng * 0.2) and (c < o):
            patterns.append("ShootingStar")

        if len(ohlc_tail) >= 2:
            prev = ohlc_tail[-2]
            o2, c2 = prev["O"], prev["C"]
            if (c2 < o2) and (c > o) and (c >= max(o2, c2)) and (o <= min(o2, c2)):
                patterns.append("BullishEngulfing")
            if (c2 > o2) and (c < o) and (o >= max(o2, c2)) and (c <= min(o2, c2)):
                patterns.append("BearishEngulfing")
        return patterns
    
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
    # 历史决策分组：按币种归档（旧→新）
    # ---------------------------
    def _group_history_by_symbol(
        self,
        decision_history: Optional[List[Dict[str, Any]]],
        max_per_symbol: int = 10,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        将全域 decision_history 依 symbol 分组，输出为「旧→新」。
        若超过 max_per_symbol，保留最后 N 笔（最近 N 笔），
        但输出顺序仍维持旧→新以与 RSI/MACD/OHLC 一致。
        """
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        if not decision_history:
            return grouped

        # 先将全部纪录按时间「旧→新」排序
        def _ts_key(rec: Dict[str, Any]) -> float:
            ts = rec.get("timestamp")
            try:
                return datetime.fromisoformat(ts).timestamp()
            except Exception:
                return 0.0

        sorted_all = sorted(decision_history, key=_ts_key, reverse=False)  # 旧→新

        # 依币种分桶
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for rec in sorted_all:
            sym = rec.get("symbol")
            if not sym:
                continue
            arr = buckets.setdefault(sym, [])
            arr.append(rec)

        # 对每个币种：只保留最后 N 笔（最近 N 笔），但输出顺序仍旧→新
        for sym, arr in buckets.items():
            trimmed = arr[-max_per_symbol:]

            cleaned_list: List[Dict[str, Any]] = []
            for rec in trimmed:
                cleaned = {
                    "timestamp": rec.get("timestamp"),
                    "action": rec.get("action"),
                    "open_percent" : rec.get("open_percent") or 0,
                    "reduce_percent" : rec.get("reduce_percent") or 0,
                    "confidence": self._norm_confidence(rec.get("confidence")),
                    "leverage": self._to_float(rec.get("leverage"), 0.0),
                    "reason": rec.get("reason"),
                    "price": self._to_float(rec.get("price"), 0.0),
                    "positionAfterExecution" : rec.get("positionAfterExecution"),
                }
                cleaned_list.append(cleaned)

            grouped[sym] = cleaned_list  # 旧→新

        return grouped

    # ---------------------------
    # 计算多组 KDJ（旧→新，近 10 组）
    # ---------------------------
    @staticmethod
    def _compute_kdj_series(df, n: int = 9) -> List[Dict[str, float]]:
        """
        返回最近 10 组 KDJ（旧→新），格式：
        [{"k": 45.3, "d": 42.8, "j": 50.4}, ...]
        """
        try:
            if df is None or len(df) < n or not all(col in df for col in ("high", "low", "close")):
                return []

            low_n = df["low"].rolling(window=n, min_periods=n).min()
            high_n = df["high"].rolling(window=n, min_periods=n).max()
            rsv = (df["close"] - low_n) / (high_n - low_n) * 100.0
            rsv = rsv.fillna(50.0).clip(lower=0.0, upper=100.0)

            k_list, d_list, j_list = [], [], []
            k_prev, d_prev = 50.0, 50.0
            for val in rsv:
                k_val = (2.0 / 3.0) * k_prev + (1.0 / 3.0) * float(val)
                d_val = (2.0 / 3.0) * d_prev + (1.0 / 3.0) * k_val
                j_val = 3.0 * k_val - 2.0 * d_val
                k_list.append(k_val)
                d_list.append(d_val)
                j_list.append(j_val)
                k_prev, d_prev = k_val, d_val

            # 取最后 10 组（旧→新），四舍五入 1 位小数
            tail_k = k_list[-10:]
            tail_d = d_list[-10:]
            tail_j = j_list[-10:]

            result = [
                {"k": round(k, 1), "d": round(d, 1), "j": round(j, 1)}
                for k, d, j in zip(tail_k, tail_d, tail_j)
            ]
            return result
        except Exception:
            return []

    # ---------------------------
    # 计算多组 BOLL（旧→新，近 10 组）
    # ---------------------------
    def _compute_boll_series(self, df, symbol: str, window: int = 20) -> List[Dict[str, float]]:
        """
        返回最近 10 组布林带（旧→新），格式：
        [{"upper": x, "middle": y, "lower": z}, ...]
        所有价格栏位均用该 symbol 的动态价格精度进行四舍五入。
        """
        try:
            if df is None or len(df) < window or "close" not in df:
                return []

            closes = df["close"]
            sma = closes.rolling(window=window, min_periods=window).mean()
            std = closes.rolling(window=window, min_periods=window).std()

            upper = sma + (std * 2)
            lower = sma - (std * 2)

            tail_u = upper.tail(10).tolist()
            tail_m = sma.tail(10).tolist()
            tail_l = lower.tail(10).tolist()

            out = []
            for u, m, l in zip(tail_u, tail_m, tail_l):
                out.append({
                    "upper": self._round_price(symbol, u),
                    "middle": self._round_price(symbol, m),
                    "lower": self._round_price(symbol, l),
                })
            return out
        except Exception:
            return []

    # ---------------------------
    # 单一时间框架 → JSON 区块（价格类栏位依 symbol 精度）
    # ---------------------------
    def _build_interval_block(self, interval: str, data: Dict[str, Any], symbol: str) -> Optional[Dict[str, Any]]:
        """
        生成单一 timeframe 的 JSON 区块：
        {
          "time_frame": "5m",
          "funding": 0.0001,
          "rsi": [ ... 10 values, old->new ],
          "macd": [ ... 10 values, old->new ],
          "histogram": [ ... 10 values, old->new ],
          "ema20":  ...,
          "atr14":  ...,
          "kdj": [ {"k":..,"d":..,"j":..}, ... 10 ],
          "boll": [ {"upper":..,"middle":..,"lower":..}, ... 10 ],
          "ohlc": [ {O,H,L,C,V}, ... 10 rows old->new ]
        }
        说明：
        - KDJ 与 BOLL 皆为「多组阵列」，取最近 10 组，顺序为旧→新。
        - 价格类字段使用该 symbol 的动态价格精度。
        """
        if not data:
            return None

        ind = data.get("indicators", {}) or {}
        df = data.get("dataframe")

        # 价格类指标（单值）：动态价格精度
        block: Dict[str, Any] = {
            "time_frame": interval,
            "ema20": self._round_price(symbol, ind.get("ema_20", 0.0)),
            "atr14": self._round_price(symbol, ind.get("atr_14", 0.0)),  # ATR 为价格距离，也用价格精度
        }

        # ===== RSI / MACD arrays（旧→新）=====
        rsi_arr, macd_arr, hist_arr = [], [], []
        if df is not None and len(df) >= 30 and "close" in df:
            closes = df["close"]

            # RSI（1 位小数）
            try:
                delta = closes.diff()
                gain = delta.where(delta > 0, 0).rolling(window=14, min_periods=14).mean()
                loss = (-delta.where(delta < 0, 0)).rolling(window=14, min_periods=14).mean()
                rs = gain / loss
                rsi_full = 100 - (100 / (1 + rs))
                rsi_arr = [self._round(x, 1) for x in rsi_full.tail(10).tolist()]
            except Exception:
                pass

            # MACD 与 Hist（4 位小数）
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

        # ===== KDJ 多组（旧→新）=====
        kdj_list = self._compute_kdj_series(df, n=9)
        block["kdj"] = kdj_list

        # ===== BOLL 多组（旧→新）=====
        boll_list = self._compute_boll_series(df, symbol, window=20)
        block["boll"] = boll_list

        # ===== OHLC（最近10根，旧→新；价格用动态价格精度）=====
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
        # block["ohlc"] = ohlc_list
        block["patterns"] = self._detect_candlestick_patterns(ohlc_list) if ohlc_list else []
        return block

    # ---------------------------
    # 整体：多币种 → JSON 载荷（dict）
    # ---------------------------
    def build_multi_symbol_analysis_payload(
        self,
        all_symbols_data: Dict[str, Any],
        account_summary: Optional[Dict[str, Any]] = None,
        decision_history: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        产出**JSON 载荷**（Python dict 可直接 json.dumps）
        结构：
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
              "decision_history": [ {... 旧→新 ...} ]
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

        # 帐户摘要
        if account_summary:
            payload["account"] = {
                "equity": self._get(account_summary, "equity", 0.0, 2),
                "available_balance": self._get(account_summary, "available_balance", 0.0, 2),
                "total_unrealized_pnl": self._get(account_summary, "total_unrealized_pnl", 0.0, 2),
            }

        # 历史决策按币种分组（旧→新）
        grouped_hist = self._group_history_by_symbol(decision_history, max_per_symbol=10)

        # 遍历币种
        for symbol, symbol_data in all_symbols_data.items():
            market_data = symbol_data.get("market_data", {}) or {}
            position = symbol_data.get("position")
            coin_name = symbol.replace("USDT", "")
            realtime = (market_data.get("realtime") or {})

            # 顶层行情（价格用动态价格精度）
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
                # 该币种的历史决策（旧→新）
                "decision_history": grouped_hist.get(symbol, []),
            }

            # 持仓（若有）— 数量用 qty 精度，价格用 price 精度
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

            # 各时间框架
            multi = market_data.get("multi_timeframe", {}) or {}
            for interval in self.default_intervals:
                if interval not in multi:
                    continue
                block = self._build_interval_block(interval, multi.get(interval) or {}, symbol)
                if block:
                    # 若希望每个 timeframe 也带 funding，可复制 symbol 层的 funding（可选）
                    block["funding"] = funding_rate
                    symbol_obj["market_data"].append(block)

            payload["symbols"].append(symbol_obj)

        return payload

    # ---------------------------
    # 文字提示：内嵌 JSON（给 DeepSeek）
    # ---------------------------
    def build_multi_symbol_analysis_prompt_json(
        self,
        all_symbols_data: Dict[str, Any],
        account_summary: Optional[Dict[str, Any]] = None,
        decision_history: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        产生**中文提示词** + 内嵌 **JSON 载荷**。
        模型请以该 JSON 为依据，回传每个币种的决策 JSON。
        """
        payload = self.build_multi_symbol_analysis_payload(
            all_symbols_data, account_summary, decision_history
        )
        payload_json = json.dumps(payload, ensure_ascii=False, indent=2)

        prompt = f"""
你是一位专业的日内交易员。以下提供多币种的结构化市场资料（JSON），
请逐一分析每个币种并输出**决策 JSON**，格式如下（币种键以实际输入为准）：

{{
  "BTCUSDT": {{
    "action": "BUY_OPEN" | "SELL_OPEN" | "CLOSE" | "HOLD" | "ADD_BUY_OPEN" | "ADD_SELL_OPEN | PARTIAL_CLOSE",
    "reason": "1-2句话说明决策理由（含关键指标与数值）",
    "confidence": 0.0 - 1.0,
    "leverage":  {self.config.get('trading', {}).get('default_leverage', 10)}-{self.config.get('trading', {}).get('max_leverage', 10)},
    "open_percent": 0-10,
    "reduce_percent" : 0-100,
    "take_profit_percent":  {self.config.get('risk', {}).get('take_profit_low', 10)}-{self.config.get('risk', {}).get('take_profit_high', 10)},
    "stop_loss_percent":  {self.config.get('risk', {}).get('stop_loss_low', 10)}-{self.config.get('risk', {}).get('stop_loss_high', 10)}
  }},
  "...": {{ ... }}
}}

說明：
──────────────────────────────
📊 **輸入的 JSON 結構（重點欄位）**
- `market` / `current_price` / `funding` / `open_interest`
- `position`：當前持倉（若有） 
- `market_data`：多時間框架（5m、15m、1h、4h、1D 等）
  - `atr14`: 波動幅度
  - `ema20`: ema20
  - `rsi`: 最近 10 筆（rsi 舊→新）
  - `macd`: 最近 10 筆 MACD 快線（舊→新）
  - `histogram`: 最近 10 筆 MACD 柱狀圖（舊→新）
  - `kdj`: 最近 10 筆 kdj
  - `pattern`: 近幾根 K 線辨識形態（舊→新）
  
#技術指標資料說明:
- time_frame:1d 用來判斷大方向,空頭趨勢盡量做空,多頭趨勢盡量做多
- time_frame:1h,3m 用來判斷是否開倉,也可用來判斷是否獲利了結/停損
- 可参考 market_data 内不同 time_frame 的 RSI/MACD/HIST/KDJ/BOLL 皆为「旧→新」序列）。
- 每个币种下方含有该币的 decision_history（旧→新），可用以对齐你的建议与既有持仓/历史。
- **decision_history（舊→新）**：請審視最近數筆紀錄，並遵循：
  1) 避免「來回打臉」：若上次剛開倉，除非出現**反向強訊號**（如 MACD 零軸反轉 + KDJ 交叉 + RSI 位階改變），否則傾向持有或減倉，而非立即反手  
  2) 若歷史連續錯向或 ATR 升高 ⇒ 優先**降槓桿/縮小倉位**  
  3) 若同方向連勝且多週期一致 ⇒ 可**小幅加槓桿/加倉**（不得超過上限）  
  4) **具體化理由**：在 reason 中說明「相對於最近一次操作的變化點」（例：「上次 BUY_OPEN 後，4h MACD 由正轉負且 KDJ 死亡交叉，決定 CLOSE」）
- 若本次建議與歷史方向相反，請在 reason 中**明確列出反轉依據**（指標交叉、零軸穿越、布林結構改變、關鍵位失守/站回）

#倉位说明：
- 每个币种单独决策，依市场状况 BUY_OPEN(作多)/SELL_OPEN(作空)/ADD_BUY_OPEN(加倉作多)/ADD_SELL_OPEN(加倉作空)
- 若判断风险较高或趋势不明确，可使用 HOLD。HOLD時無需提供leverage/open_percent/take_profit_percent/stop_loss_percent
- BUY_OPEN/SELL_OPEN 时务必提供合理止盈止损百分比。 
- ADD_BUY_OPEN/ADD_SELL_OPEN 為加倉,加倉時需同時提供新的止盈止损百分比,
- PARTIAL_CLOSE 為減倉, 只用來確保利潤,不用來減少損失 , 需帶入reduce_percent
- 我會根據你回傳的open_percent,leverage來開倉
  開倉所使用的保證金(isolatedMargin)為 equity*open_percent
  若所有艙位的isolatedMargin合超過equity的70%, 則不可開倉或加倉
- 不要只做多! 
- 若技術分析結果與市場情況相反,造成倉位浮虧的時候:
  請先把position物件內的 pnl_percent除以leverage (pnl_percent/leverage) 
  得到的數字超過 {self.config.get('risk', {}).get('position_tolerance', 10)}% 再考慮停損
  但若技術分析反轉(參照decision_history), 且信心足夠 , 可考慮停損
- 在考慮減倉時:
  請先把position物件內的 pnl_percent除以leverage (pnl_percent/leverage) 
  得到的數字超過 {self.config.get('risk', {}).get('reduce_if_over', 10)}% 再考慮鎖定利潤
  或是根據decision_history上一次的艙位,獲利減少超過{self.config.get('risk', {}).get('reduce_if_fallback', 10)}%時,再考慮鎖定利潤

#当前时间
{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

#市场资料 JSON（请据此做判断）
{payload_json}
""".strip()

        return prompt

    # （保留：仅在需要时使用）
    def _format_account_summary(self, account_summary: Dict[str, Any]) -> str:
        if not account_summary:
            return ""
        equity = account_summary.get("equity", 0)
        available = account_summary.get("available_balance", 0)
        unrealized_pnl = account_summary.get("total_unrealized_pnl", 0)
        return f"""
帐户馀额: {equity:.2f} USDT
可用馀额: {available:.2f} USDT
未实现损益: {unrealized_pnl:+.2f} USDT
""".strip()