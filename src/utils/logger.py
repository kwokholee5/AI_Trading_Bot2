# src/utils/logger.py
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_DIR = Path("./logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

_DEFAULT_FMT = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

_CONSOLE_FMT = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

def _build_file_handler(filename: str, level=logging.INFO) -> logging.Handler:
    h = TimedRotatingFileHandler(
        LOG_DIR / filename,
        when="midnight",      # 每日輪替
        interval=1,
        backupCount=14,       # 保留天數
        encoding="utf-8",
        utc=False             # 依系統時區（你在台灣就用本地時間）
    )
    h.setLevel(level)
    h.setFormatter(_DEFAULT_FMT)
    return h

def _build_console_handler(level=logging.INFO) -> logging.Handler:
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(_CONSOLE_FMT)
    return ch

def _ensure_handlers(logger: logging.Logger, handlers: list[logging.Handler]):
    # 避免重覆加 handler
    exists = {(type(h), getattr(h, "baseFilename", None)) for h in logger.handlers}
    for h in handlers:
        sig = (type(h), getattr(h, "baseFilename", None))
        if sig not in exists:
            logger.addHandler(h)
            exists.add(sig)

def init_all_loggers() -> dict[str, logging.Logger]:
    """
    建立/初始化三個 logger：ai / trade / system
    - 每個 logger 各自寫入對應檔案
    - Root/Console 依然會輸出
    重覆呼叫是安全的（不會重覆裝 handler）
    """
    logging.captureWarnings(True)  # 也抓到 warnings 模組訊息
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    _ensure_handlers(root, [_build_console_handler(logging.INFO)])

    ai_logger = logging.getLogger("ai")
    ai_logger.setLevel(logging.INFO)
    _ensure_handlers(ai_logger, [_build_file_handler("ai.log", logging.INFO)])

    trade_logger = logging.getLogger("trade")
    trade_logger.setLevel(logging.INFO)
    _ensure_handlers(trade_logger, [_build_file_handler("trade.log", logging.INFO)])

    system_logger = logging.getLogger("system")
    system_logger.setLevel(logging.INFO)
    _ensure_handlers(system_logger, [_build_file_handler("system.log", logging.INFO)])

    prompt_logger = logging.getLogger("prompt")
    prompt_logger.setLevel(logging.INFO)
    _ensure_handlers(prompt_logger, [_build_file_handler("prompt.log", logging.INFO)])
    
    debug_logger = logging.getLogger("debug")
    debug_logger.setLevel(logging.INFO)
    _ensure_handlers(debug_logger, [_build_file_handler("debug.log", logging.INFO)])

    binance_client = logging.getLogger("binance_client")
    binance_client.setLevel(logging.INFO)
    _ensure_handlers(binance_client, [_build_file_handler("binance_client.log", logging.INFO)])

    hedge = logging.getLogger("hedge")
    hedge.setLevel(logging.INFO)
    _ensure_handlers(hedge, [_build_file_handler("hedge.log", logging.INFO)])
    
    return {"hedge" : hedge ,"ai": ai_logger, "trade": trade_logger, "system": system_logger , "prompt": prompt_logger , "debug" : debug_logger , "binance_client" : binance_client}

def get_logger(name: str) -> logging.Logger:
    """
    需要時取用既有 logger（保險起見若未初始化會快速建置必要 handler）
    """
    m = {
        "ai": ("ai.log",),
        "trade": ("trade.log",),
        "system": ("system.log",),
        "prompt": ("prompt.log"),
        "debug" : ("debug.log"),
        "binance_client" : ("binance_client.log"),
        "hedge" :("hedge.log")
    }
    logger = logging.getLogger(name)
    if not logger.handlers:
        # 最小化處理，防漏 init
        files = m.get(name)
        if files:
            _ensure_handlers(logger, [_build_file_handler(files[0], logging.INFO)])
        _ensure_handlers(logging.getLogger(), [_build_console_handler(logging.INFO)])
        logger.setLevel(logging.INFO)
    return logger