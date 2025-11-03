# at top
import time
import pandas as pd

from src.utils.indicators import (
    calculate_rsi,
    calculate_macd,
    calculate_ema,
    calculate_atr,
    calculate_sma,
    calculate_bollinger_bands,
)

class MarketDataManager:
    def __init__(self, client, *_, **__):
        # keep the wrapper and also expose the raw client if ever needed
        self.wrapper = client
        self.client = getattr(client, "client", client)  # raw python-binance client
        self.futures = True  # we’re on U本位 futures in this project

    # ---------- realtime snapshot (futures-first, wrapper-first) ----------
    def get_realtime_market_data(self, symbol: str) -> dict:
        symbol = symbol.upper()
        out = {"symbol": symbol, "timestamp": int(time.time() * 1000)}

        # 24h ticker / last price via wrapper (futures)
        try:
            t = self.wrapper.get_ticker(symbol)  # <- wrapper method
            # last price can be 'lastPrice' or 'price'
            lp = t.get("lastPrice") or t.get("price") or "0"
            out["price"] = float(lp)
            out["price_change_percent"] = float(t.get("priceChangePercent", 0.0))
            out["high_price"] = float(t.get("highPrice", 0.0))
            out["low_price"] = float(t.get("lowPrice", 0.0))
            # volume (contracts) and/or quoteVolume may exist
            out["volume"] = float(t.get("volume", 0.0))
        except Exception:
            out["price"] = 0.0
            out["price_change_percent"] = 0.0
            out["high_price"] = 0.0
            out["low_price"] = 0.0
            out["volume"] = 0.0

        # bid/ask — try futures book-ticker names that differ across versions
        try:
            if hasattr(self.client, "futures_orderbook_ticker"):
                oba = self.client.futures_orderbook_ticker(symbol=symbol)
            elif hasattr(self.client, "futures_book_ticker"):
                oba = self.client.futures_book_ticker(symbol=symbol)
            else:
                oba = self.client.get_orderbook_ticker(symbol=symbol)  # fallback spot
            out["bid"] = float(oba["bidPrice"])
            out["ask"] = float(oba["askPrice"])
        except Exception:
            p = out.get("price", 0.0)
            out["bid"] = p
            out["ask"] = p

        # mark price & funding rate (wrapper helpers)
        try:
            mp = self.client.futures_mark_price(symbol=symbol)
            out["mark_price"] = float(mp["markPrice"])
        except Exception:
            out["mark_price"] = out.get("price", 0.0)

        try:
            fr = self.wrapper.get_funding_rate(symbol)
            out["funding_rate"] = float(fr) if fr is not None else 0.0
        except Exception:
            out["funding_rate"] = 0.0

        # optional: open interest via wrapper
        try:
            oi = self.wrapper.get_open_interest(symbol)
            out["open_interest"] = float(oi) if oi is not None else None
        except Exception:
            pass

        return out

    # ---------- multi timeframe (use wrapper.get_klines) ----------
    def get_multi_timeframe_data(self, symbol: str, intervals, limit: int = 200) -> dict:
        symbol = symbol.upper()
        data = {}
        for iv in intervals:
            try:
                rows = self.wrapper.get_klines(symbol, iv, limit)
                cols = [
                    "open_time","open","high","low","close","volume",
                    "close_time","quote_volume","num_trades",
                    "taker_buy_base","taker_buy_quote","ignore",
                ]
                import pandas as pd
                df = pd.DataFrame(rows, columns=cols)
                for c in ("open","high","low","close","volume"):
                    df[c] = pd.to_numeric(df[c], errors="coerce")

                inds = self._compute_indicators(df)
                kl = [
                    {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]),
                    "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}
                    for r in rows
                ]
                # include dataframe for prompt_builder to pretty-print K lines
                data[iv] = {"klines": kl, "indicators": inds, "dataframe": df[["open","high","low","close","volume"]]}
            except Exception:
                data[iv] = {"klines": [], "indicators": zeros, "dataframe": None}
        return data

    def _compute_indicators(self, df: pd.DataFrame) -> dict:
        zeros = {
            "RSI": 0.0, "MACD": 0.0, "EMA20": 0.0, "EMA50": 0.0,
            "SMA20": 0.0, "SMA50": 0.0, "BOLL_UP": 0.0, "BOLL_MID": 0.0,
            "BOLL_LOW": 0.0, "ATR": 0.0,
            # snake_case the prompt expects:
            "rsi": 0.0, "macd": 0.0, "macd_signal": 0.0, "macd_histogram": 0.0,
            "ema_20": 0.0, "ema_50": 0.0, "sma_20": 0.0, "sma_50": 0.0,
            "bollinger_middle": 0.0, "bollinger_upper": 0.0, "bollinger_lower": 0.0,
            "atr_14": 0.0,
        }
        if df.empty:
            return zeros

        close = pd.to_numeric(df["close"], errors="coerce").dropna()
        high  = pd.to_numeric(df["high"],  errors="coerce").dropna()
        low   = pd.to_numeric(df["low"],   errors="coerce").dropna()
        if len(close) < 50 or len(high) < 15 or len(low) < 15:
            return zeros

        # compute via project helpers
        rsi = calculate_rsi(close, 14)
        macd_line, macd_signal, macd_hist = calculate_macd(close, 12, 26, 9)
        ema20 = calculate_ema(close, 20)
        ema50 = calculate_ema(close, 50)
        sma20 = calculate_sma(close, 20)
        sma50 = calculate_sma(close, 50)
        mid, up, lowb = calculate_bollinger_bands(close, 20, 2.0)
        atr = calculate_atr(high, low, close, 14)

        def nz(x): 
            try:
                return float(x) if x is not None and not pd.isna(x) else 0.0
            except Exception:
                return 0.0

        # build both naming schemes
        out = {
            # Uppercase set (you used earlier)
            "RSI": nz(rsi),
            "MACD": nz(macd_hist),           # single-number MACD → histogram
            "EMA20": nz(ema20),
            "EMA50": nz(ema50),
            "SMA20": nz(sma20),
            "SMA50": nz(sma50),
            "BOLL_UP": nz(up),
            "BOLL_MID": nz(mid),
            "BOLL_LOW": nz(lowb),
            "ATR": nz(atr),

            # Snake_case set (what prompt_builder.py reads)
            "rsi": nz(rsi),
            "macd": nz(macd_line),           # prompt expects 3 MACD pieces:
            "macd_signal": nz(macd_signal),  #   line, signal, histogram
            "macd_histogram": nz(macd_hist),
            "ema_20": nz(ema20),
            "ema_50": nz(ema50),
            "sma_20": nz(sma20),
            "sma_50": nz(sma50),
            "bollinger_middle": nz(mid),
            "bollinger_upper": nz(up),
            "bollinger_lower": nz(lowb),
            "atr_14": nz(atr),
        }
        return out