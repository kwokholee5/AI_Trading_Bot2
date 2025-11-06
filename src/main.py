"""
AI交易机器人主程序
整合所有模块，实现完整的交易流程
"""
import os
import sys
import time
import json
import tempfile  # ← 新增
from pathlib import Path  # ← 新增
from datetime import datetime
from typing import Dict, Any, Optional, List

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.binance_client import BinanceClient
from src.config.config_loader import ConfigLoader
from src.config.env_manager import EnvManager
from src.data.market_data import MarketDataManager
from src.data.position_data import PositionDataManager
from src.data.account_data import AccountDataManager
from src.trading.trade_executor import TradeExecutor
from src.trading.position_manager import PositionManager
from src.trading.risk_manager import RiskManager
from src.ai.deepseek_client import DeepSeekClient
from src.ai.prompt_builder import PromptBuilder
from src.ai.decision_parser import DecisionParser
from src.utils.symbol_filters import SymbolFilters
from src.utils.logger import init_all_loggers, get_logger
from src.utils.discord import notify_discord

class TradingBot:
    """交易机器人主类"""
    
    def __init__(self, config_path: str = 'config/trading_config.json'):
        self.logs = init_all_loggers()
        self.log_ai = get_logger("ai")
        self.log_trade = get_logger("trade")
        self.log_sys = get_logger("system")
        self.log_prompt = get_logger("prompt")
        self.log_debug = get_logger("debug")
        self.log_binance_client = get_logger("binance_client")
        """初始化交易机器人"""
        self.log_ai.info("=" * 60)
        self.log_ai.info("🚀 AI交易机器人启动中...")
        self.log_ai.info("=" * 60)
        
        # 加载配置
        self.config = ConfigLoader.load_trading_config(config_path)
        self.log_ai.info(f"✅ 配置加载完成")
        
        # 加载环境变量
        EnvManager.load_env_file('.env')
        self.log_ai.info(f"✅ 环境变量加载完成")
        
        # 初始化客户端
        self.client = self._init_binance_client()
        self.ai_client = self._init_ai_client()
        self.log_ai.info(f"✅ API客户端初始化完成")
        
        # 初始化管理器
        self.market_data = MarketDataManager(self.client)
        self.position_data = PositionDataManager(self.client)
        self.account_data = AccountDataManager(self.client)
        self.log_ai.info(f"✅ 数据管理器初始化完成")
        
        # 初始化交易执行器和风险管理器
        self.trade_executor = TradeExecutor(self.client, self.config)
        self.position_manager = PositionManager(self.client)
        self.risk_manager = RiskManager(self.config)
        self.log_ai.info(f"✅ 交易执行器初始化完成")
        
        # === 新增：本地歷史檔案設定 ===
        paths_cfg = self.config.get('paths', {})
        # 你也可以在 trading_config.json 裡設定:
        # "paths": {"state_dir": "./state", "history_file": "decision_history.jsonl", "max_history": 300}
        self.state_dir = Path(paths_cfg.get('state_dir', './state'))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.history_file = self.state_dir / paths_cfg.get('history_file', 'decision_history.jsonl')
        self.max_history: int = int(paths_cfg.get('max_history', 300))

        # AI组件
        symbols = ConfigLoader.get_trading_symbols(self.config)
        precision_map = self._build_precision_map(symbols)
        self.prompt_builder = PromptBuilder(self.config, precision_map)
        self.decision_parser = DecisionParser()
        self.log_ai.info(f"✅ AI组件初始化完成")
        
        # 状态追踪（從本地載入歷史）
        self.decision_history: List[Dict[str, Any]] = self._load_decision_history(self.history_file, self.max_history)
        self.trade_count = 0
        
        self.log_ai.info("=" * 60)
        self.log_ai.info("🎉 AI交易机器人启动成功！")
        self.log_ai.info("=" * 60)

    # === 新增：歷史檔案 I/O ===
    def _load_decision_history(self, path: Path, limit: int) -> List[Dict[str, Any]]:
        """
        從本地檔案載入決策歷史。
        支援 JSONL（每行一筆 JSON）或舊版 JSON 陣列格式。
        僅保留最後 limit 筆；若檔案不存在回傳空陣列。
        """
        if not path.exists():
            return []
        try:
            with open(path, 'r', encoding='utf-8') as f:
                first_char = f.read(1)
                f.seek(0)
                records: List[Dict[str, Any]] = []
                if first_char == '[':
                    # 舊版 JSON 陣列
                    data = json.load(f)
                    if isinstance(data, list):
                        records = [x for x in data if isinstance(x, dict)]
                else:
                    # JSONL
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            if isinstance(obj, dict):
                                records.append(obj)
                        except json.JSONDecodeError:
                            continue
                # 只保留最後 limit 筆
                return records[-limit:]
        except Exception as e:
            self.log_ai.info(f"⚠️ 載入歷史檔案失敗: {e}")
            return []

    def _append_history_jsonl(self, path: Path, record: Dict[str, Any]) -> None:
        """
        以 JSONL 方式追加一筆歷史到檔案。
        """
        try:
            with open(path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False))
                f.write('\n')
        except Exception as e:
            self.log_ai.info(f"⚠️ 寫入歷史檔案失敗: {e}")

    def _compact_history_file(self, path: Path, records: List[Dict[str, Any]]) -> None:
        """
        壓縮歷史檔案：只保留 records 的內容（通常是最後 N 筆），
        以臨時檔 + 原子替換確保安全。
        """
        try:
            tmp = path.with_suffix(path.suffix + '.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                for r in records:
                    f.write(json.dumps(r, ensure_ascii=False))
                    f.write('\n')
            os.replace(tmp, path)
        except Exception as e:
            self.log_ai.info(f"⚠️ 壓縮歷史檔案失敗: {e}")

    def _build_precision_map(self, symbols: list[str]) -> Dict[str, Dict[str, int]]:
        pm: Dict[str, Dict[str, int]] = {}
        for sym in symbols:
            f: SymbolFilters = self.client.get_symbol_filters(sym)  # 內含 tickSize/stepSize
            price_dp = PromptBuilder._decimals_from_step(getattr(f, "tickSize", None), default_dp=2)
            qty_dp = PromptBuilder._decimals_from_step(getattr(f, "stepSize", None), default_dp=4)
            pm[sym] = {"price_dp": price_dp, "qty_dp": qty_dp}
        return pm
    
    def _init_binance_client(self) -> BinanceClient:
        """初始化Binance客户端（正式网）"""
        api_key, api_secret = EnvManager.get_api_credentials()
        if not api_key or not api_secret:
            raise ValueError("API凭证未配置")
        
        return BinanceClient(api_key=api_key, api_secret=api_secret)
    
    def _init_ai_client(self) -> DeepSeekClient:
        """初始化DeepSeek客户端"""
        api_key = EnvManager.get_deepseek_key()
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY 未配置")
        
        model = self.config.get('ai', {}).get('model', 'deepseek-reasoner')
        return DeepSeekClient(api_key=api_key, model=model)
    
    def get_market_data_for_symbol(self, symbol: str) -> Dict[str, Any]:
        """获取单个币种的市场数据"""
        # 多周期K线
        intervals = ['3m', '1h' , '1d']
        multi_timeframe = self.market_data.get_multi_timeframe_data(symbol, intervals)
        
        # 实时行情
        realtime = self.market_data.get_realtime_market_data(symbol)
        
        return {
            'symbol': symbol,
            'realtime': realtime or {},
            'multi_timeframe': multi_timeframe
        }
    
    def analyze_all_symbols_with_ai(self, all_symbols_data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """使用AI一次性分析所有币种"""
        try:
            # 收集所有币种的持仓
            all_positions = {}
            for symbol in all_symbols_data.keys():
                position = self.position_data.get_current_position(symbol)
                if position:
                    all_positions[symbol] = position
            
            # 获取账户摘要
            account_summary = self.account_data.get_account_summary()
            
            # 获取历史决策
            history = self.decision_history[-300:] if self.decision_history else []
            # 构建多币种提示词
            prompt = self.prompt_builder.build_multi_symbol_analysis_prompt_json(all_symbols_data, account_summary , history)

            
            # 调用AI
            self.log_sys.info(f"\n🤖 调用AI一次性分析所有币种...")
            self.log_sys.info(f"\n{'='*60}")
            self.log_prompt.info(f"{'='*60}")
            self.log_prompt.info(prompt)
            self.log_prompt.info(f"{'='*60}\n")
            
            response = self.ai_client.analyze_and_decide(prompt)
            
            # 显示AI推理过程
            reasoning = self.ai_client.get_reasoning(response)
            
            if reasoning:
                self.log_ai.info(f"🧠 AI思维链（详细分析）")
                self.log_ai.info(f"{'='*60}")
                self.log_ai.info(reasoning)
                self.log_ai.info(f"{'='*60}\n")
            
            # 显示AI原始回复
            self.log_ai.info(f"🤖 AI原始回复:")
            self.log_ai.info(f"{'='*60}")
            self.log_ai.info(response['content'])
            self.log_ai.info(f"{'='*60}\n")
            
            # 解析决策
            decisions = self.decision_parser.parse_multi_symbol_response(response['content'])
            
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            content = f"🕒 Deepseek决策总结 @({now_str})\n"
            # 显示所有决策
            self.log_ai.info(f"📊 AI多币种决策总结:")
            self.log_ai.info(f"{'='*60}")
            all_hold = True  # 檢查用旗標
            for symbol, decision in decisions.items():
                action = decision.get('action', 'HOLD').upper()
                self.log_ai.info(f"   {symbol}: {decision['action']} - {decision['reason']}")
                content += (f"{symbol}: {decision['action']} \n")
                content += (f"{decision['reason']}\n")
                content += (f"{' '*20}\n")
                if action != "HOLD":
                    all_hold = False
                    
            self.log_ai.info(f"{'='*60}\n")
            if not all_hold:
                notify_discord(content)
            else:
                notify_discord(f"策略未改變,省略通知@{now_str}")
            return decisions
        except Exception as e:
            self.log_ai.info(f"❌ AI分析失败: {e}")
            import traceback
            traceback.print_exc()
            return {}
    
    def analyze_with_ai(self, symbol: str, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """使用AI分析并获取决策"""
        try:
            # 获取持仓
            position = self.position_data.get_current_position(symbol)
            
            # 获取历史决策（最近3条）
            history = [d for d in self.decision_history if d.get('symbol') == symbol][-3:]
            
            # 构建提示词
            prompt = self.prompt_builder.build_analysis_prompt(
                symbol=symbol,
                market_data=market_data,
                position=position,
                history=history
            )
            
            # 调用AI
            self.log_ai.info(f"\n🤖 调用AI分析 {symbol}...")
            response = self.ai_client.analyze_and_decide(prompt)
            
            # 解析决策
            decision = self.decision_parser.parse_ai_response(response['content'])
            
            # 显示AI推理过程
            reasoning = self.ai_client.get_reasoning(response)
            if reasoning:
                self.log_ai.info(f"\n💭 {symbol} AI推理:")
                self.log_ai.info(reasoning)
            
            # 显示决策
            self.log_ai.info(f"\n📊 {symbol} AI决策:")
            self.log_ai.info(f"   动作: {decision['action']}")
            self.log_ai.info(f"   信心: {decision['confidence']:.2f}")
            self.log_ai.info(f"   杠杆: {decision['leverage']}x")
            self.log_ai.info(f"   仓位: {decision['open_percent']}%")
            self.log_ai.info(f"   理由: {decision['reason']}")
            
            return decision
            
        except Exception as e:
            self.log_ai.info(f"❌ AI分析失败 {symbol}: {e}")
            return self.decision_parser._get_default_decision()
    
    def execute_decision(self, symbol: str, decision: Dict[str, Any], market_data: Dict[str, Any]):
        """执行AI决策"""
        action = decision.get('action', 'HOLD')
        confidence = decision.get('confidence', 0.5)
        
        # 确保 confidence 是数字
        if isinstance(confidence, str):
            conf_str = confidence.upper()
            if conf_str == 'HIGH':
                confidence = 0.8
            elif conf_str == 'MEDIUM':
                confidence = 0.6
            elif conf_str == 'LOW':
                confidence = 0.4
            else:
                confidence = 0.5
        
        # 如果信心度太低，不执行
        if confidence < 0.5 and action != 'CLOSE':
            self.log_ai.info(f"⚠️ {symbol} 信心度太低({confidence:.2f})，跳过执行")
            return
        
        try:
            # 获取账户信息
            account_summary = self.account_data.get_account_summary()
            if not account_summary:
                self.log_ai.info(f"⚠️ {symbol} 无法获取账户信息")
                return
            
            total_equity = account_summary['equity']
            
            # 获取当前价格
            current_price = market_data['realtime'].get('price', 0)
            if current_price == 0:
                self.log_ai.info(f"⚠️ {symbol} 无法获取当前价格")
                return
            
            if action == 'BUY_OPEN':
                # 开多仓
                self._open_long(symbol, decision, total_equity, current_price)
            
            elif action == 'ADD_BUY_OPEN':
                self._open_long(symbol, decision, total_equity, current_price)

            elif action == 'SELL_OPEN':
                # 开空仓
                self._open_short(symbol, decision, total_equity, current_price)
            
            elif action == 'ADD_SELL_OPEN':
                # 开空仓
                self._open_short(symbol, decision, total_equity, current_price)

            elif action == 'CLOSE':
                # 平仓
                self._close_position(symbol, decision)
                
            elif action == 'HOLD':
                # 持有
                self.log_ai.info(f"💤 {symbol} 保持现状")
                
            
            elif action == 'PARTIAL_CLOSE':
                pct = decision.get('reduce_percent')
                try:
                    pct = float(pct)
                except Exception:
                    pct = None
                if not pct or pct <= 0 or pct > 100:
                    self.log_ai.info(f"⚠️ {symbol} 部分減倉比例無效: {pct}")
                    return
                self.trade_executor.close_position_partial(symbol, pct / 100.0)


        except Exception as e:
            self.log_ai.info(f"❌ 执行决策失败 {symbol}: {e}")
    
    def _open_long(self, symbol: str, decision: Dict[str, Any], total_equity: float, current_price: float):
        """开多仓"""
        # 检查账户余额
        if total_equity <= 0:
            self.log_ai.info(f"⚠️ {symbol} 账户余额为0，无法开仓")
            self.log_ai.info(f"   请确保账户有足够的 USDT 余额")
            return
        
        # 检查是否已有持仓
        # position = self.position_data.get_current_position(symbol)
        # if position:
        #     self.log_ai.info(f"⚠️ {symbol} 已有持仓，无法开多仓")
        #     return
        
        # 计算仓位数量
        leverage = decision['leverage']
        open_percent = decision['open_percent'] / 100
        position_value = leverage * total_equity * open_percent
        quantity = position_value / current_price
        
        # 检查数量是否有效
        if quantity <= 0:
            self.log_ai.info(f"❌ {symbol} 计算出的数量无效: {quantity} (账户余额: {total_equity})")
            return
        
        # 风险检查
        ok, errors = self.risk_manager.check_all_risk_limits(
            symbol, quantity, current_price, total_equity, total_equity
        )
        if not ok:
            self.log_ai.info(f"❌ {symbol} 风控检查失败:")
            for err in errors:
                self.log_ai.info(f"   - {err}")
            return
        
        # 计算止盈止损价格
        take_profit = decision.get('take_profit')
        stop_loss = decision.get('stop_loss')
        
        # 执行开仓
        try:
            self.trade_executor.open_long(
                symbol=symbol,
                quantity=quantity,
                leverage=leverage,
                take_profit=take_profit,
                stop_loss=stop_loss
            )
            self.log_ai.info(f"✅ {symbol} 开多仓成功")
            self.trade_count += 1
        except Exception as e:
            self.log_ai.info(f"❌ {symbol} 开多仓失败: {e}")
    
    def _open_short(self, symbol: str, decision: Dict[str, Any], total_equity: float, current_price: float):
        """开空仓"""
        # 检查账户余额
        if total_equity <= 0:
            self.log_ai.info(f"⚠️ {symbol} 账户余额为0，无法开仓")
            self.log_ai.info(f"   请确保账户有足够的 USDT 余额")
            return
        
        # 检查是否已有持仓
        # position = self.position_data.get_current_position(symbol)
        # if position:
        #     self.log_ai.info(f"⚠️ {symbol} 已有持仓，无法开空仓")
        #     return
        
        # 计算仓位数量
        leverage = decision['leverage']
        open_percent = decision['open_percent'] / 100
        position_value = leverage * total_equity * open_percent
        quantity = position_value / current_price
        
        # 检查数量是否有效
        if quantity <= 0:
            self.log_ai.info(f"❌ {symbol} 计算出的数量无效: {quantity} (账户余额: {total_equity})")
            return
        
        # 风险检查
        ok, errors = self.risk_manager.check_all_risk_limits(
            symbol, quantity, current_price, total_equity, total_equity
        )
        if not ok:
            self.log_ai.info(f"❌ {symbol} 风控检查失败:")
            for err in errors:
                self.log_ai.info(f"   - {err}")
            return
        
        # 计算止盈止损价格
        take_profit = decision.get('take_profit')
        stop_loss = decision.get('stop_loss')
        
        # 执行开仓
        try:
            self.trade_executor.open_short(
                symbol=symbol,
                quantity=quantity,
                leverage=leverage,
                take_profit=take_profit,
                stop_loss=stop_loss
            )
            self.log_ai.info(f"✅ {symbol} 开空仓成功")
            self.trade_count += 1
        except Exception as e:
            self.log_ai.info(f"❌ {symbol} 开空仓失败: {e}")
    
    def _close_position(self, symbol: str, decision: Dict[str, Any]):
        """平仓"""
        try:
            self.trade_executor.close_position(symbol)
            self.log_ai.info(f"✅ {symbol} 平仓成功")
            self.trade_count += 1
        except Exception as e:
            self.log_ai.info(f"❌ {symbol} 平仓失败: {e}")
    
    def save_decision(self, symbol: str, decision: Dict[str, Any], market_data: Dict[str, Any] , position:Optional[Dict[str, Any]]):
        """保存决策历史（記憶體 + 檔案）"""
        p_obj: Dict[str, Any] = {}
        if position:
            p_obj = {
                "side": position.get("side") or ("LONG" if self._to_float(position.get("positionAmt"), 0.0) > 0 else "SHORT"),
                "positionAmt": self.prompt_builder ._round_qty(symbol, position.get("positionAmt", 0.0)),
                "entry_price": self.prompt_builder ._round_price(symbol, position.get("entry_price", 0.0)),
                "leverage": self.prompt_builder ._to_float(position.get("leverage"), 0.0),
                "unrealized_pnl": self.prompt_builder ._get(position, "unrealized_pnl", 0.0, 4),
                "pnl_percent": self.prompt_builder ._get(position, "pnl_percent", 0.0, 4),
                "isolatedMargin": self.prompt_builder ._get(position, "isolatedMargin", 0.0, 4),
                "take_profit": self.prompt_builder ._get(position, "take_profit", 0.0, 4),
                "stop_loss": self.prompt_builder ._get(position, "stop_loss", 0.0, 4),
            }
        decision_record = {
            'timestamp': datetime.now().isoformat(),
            'symbol': symbol,
            'action': decision['action'],
            'confidence': decision['confidence'],
            'leverage': decision['leverage'],
            'open_percent': decision.get('open_percent', 0),
            'reduce_percent': decision.get('reduce_percent', 0),
            'reason': decision['reason'],
            'price': market_data['realtime'].get('price', 0),
            'positionAfterExecution' : p_obj
        }
        # 先存記憶體
        self.decision_history.append(decision_record)
        # 僅保留最近 N 筆
        if len(self.decision_history) > self.max_history:
            self.decision_history = self.decision_history[-self.max_history:]
        # 追加到檔案（JSONL）
        self._append_history_jsonl(self.history_file, decision_record)
        # 如檔案過大（以筆數判斷），壓縮重寫
        try:
            # 簡易判斷：若筆數剛好超過 N，就做一次壓縮
            if len(self.decision_history) == self.max_history:
                self._compact_history_file(self.history_file, self.decision_history)
        except Exception as e:
            self.log_ai.info(f"⚠️ 壓縮歷史檔案時發生錯誤: {e}")
    
    def run_cycle(self):
        """执行一个交易周期"""
        self.log_ai.info("\n" + "=" * 60)
        self.log_ai.info(f"📅 交易周期 #{self.trade_count + 1} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.log_ai.info("=" * 60)
        
        # 获取交易币种列表
        symbols = ConfigLoader.get_trading_symbols(self.config)
        
        # 显示账户摘要
        account_summary = self.account_data.get_account_summary()
        if account_summary:
            self.log_ai.info(f"\n💰 账户信息:")
            self.log_ai.info(f"   总权益: {account_summary['equity']:.2f} USDT")
            self.log_ai.info(f"   未实现盈亏: {account_summary['total_unrealized_pnl']:.2f} USDT")
            self.log_ai.info(f"   保证金率: {account_summary['margin_ratio']:.2f}%")
        
        # 方式1：多币种一次性分析（优化）
        if len(symbols) > 1:
            # 收集所有币种的数据
            all_symbols_data = {}
            for symbol in symbols:
                market_data = self.get_market_data_for_symbol(symbol)
                position = self.position_data.get_current_position(symbol)
                all_symbols_data[symbol] = {
                    'market_data': market_data,
                    'position': position
                }
            
            # 一次性AI分析所有币种
            all_decisions = self.analyze_all_symbols_with_ai(all_symbols_data)
            
            # 执行每个币种的决策
            for symbol, decision in all_decisions.items():
                self.log_ai.info(f"\n--- {symbol} ---")
                market_data = all_symbols_data[symbol]['market_data']
                self.execute_decision(symbol, decision, market_data)
                position = self.position_data.get_current_position(symbol)
                self.save_decision(symbol, decision, market_data , position)
                
        else:
            # 方式2：单个币种分析（保持兼容）
            for symbol in symbols:
                self.log_ai.info(f"\n--- {symbol} ---")
                
                # 获取市场数据
                market_data = self.get_market_data_for_symbol(symbol)
                
                # AI分析
                decision = self.analyze_with_ai(symbol, market_data)
                
                # 保存决策
                self.save_decision(symbol, decision, market_data)
                
                # 执行决策
                self.execute_decision(symbol, decision, market_data)
    
    def run(self):
        """启动主循环"""
        schedule_config = ConfigLoader.get_schedule_config(self.config)
        interval_seconds = schedule_config['interval_seconds']
        
        self.log_ai.info(f"\n⏱️  交易周期: 每{interval_seconds}秒")
        self.log_ai.info(f"📊 交易币种: {', '.join(ConfigLoader.get_trading_symbols(self.config))}")
        self.log_ai.info(f"\n按 Ctrl+C 停止运行\n")
        
        try:
            while True:
                start_time = time.time()
                
                # 执行交易周期
                self.run_cycle()
                
                # 等待下一个周期
                elapsed = time.time() - start_time
                sleep_time = max(0, interval_seconds - elapsed)
                
                if sleep_time > 0:
                    self.log_ai.info(f"\n💤 等待 {sleep_time:.0f}秒...")
                    time.sleep(sleep_time)
                
        except KeyboardInterrupt:
            self.log_ai.info("\n\n⚠️ 收到中断信号，正在安全退出...")
            self.shutdown()
    
    def shutdown(self):
        """优雅关闭"""
        self.log_ai.info("\n" + "=" * 60)
        self.log_ai.info("🛑 交易机器人正在关闭...")
        self.log_ai.info("=" * 60)
        self.log_ai.info(f"✅ 本次运行交易次数: {self.trade_count}")
        self.log_ai.info(f"✅ 决策记录数量: {len(self.decision_history)}")
        self.log_ai.info("🎉 交易机器人已安全退出")
        self.log_ai.info("=" * 60)


def main():
    """主函数"""
    bot = TradingBot()
    bot.run()


if __name__ == '__main__':
    main()