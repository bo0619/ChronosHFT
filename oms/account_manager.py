# file: oms/account_manager.py

from datetime import datetime
from event.type import Event, EVENT_ACCOUNT_UPDATE, AccountData
from data.cache import data_cache

class AccountManager:
    """
    资金与保证金管理器
    Available = Equity - Used Margin
    Equity = Balance + Unrealized PnL (Mark Price)
    """
    def __init__(self, engine, exposure_manager, config):
        self.engine = engine
        self.exposure = exposure_manager
        
        acc_conf = config.get("account", {})
        self.balance = acc_conf.get("initial_balance_usdt", 10000.0)
        self.leverage = acc_conf.get("leverage", 10)
        
        self.equity = self.balance
        self.used_margin = 0.0
        
        # [修复] 初始化时，默认可用资金 = 余额 (假设无持仓)
        # 防止启动时因为 available=0 导致无法下单
        self.available = self.balance 

    def force_sync(self, balance: float, used_margin: float):
        """
        [NEW] 强制同步账户资金状态
        """
        self.balance = balance
        self.used_margin = used_margin
        # 同步后立即重算权益和可用资金
        self.calculate()

    def update_balance(self, realized_pnl, commission):
        """只更新余额 (Realized)"""
        self.balance += (realized_pnl - commission)
        self.calculate() # 余额变了，立即重算

    def calculate(self):
        """
        全量重算
        依赖：Exposure(Pos, OpenQty), DataCache(MarkPrice)
        """
        unrealized_pnl = 0.0
        pos_margin = 0.0
        order_margin = 0.0
        
        # 1. 持仓 PnL 和 保证金
        for symbol, pos_vol in self.exposure.net_positions.items():
            if pos_vol == 0: continue
            
            mark_price = data_cache.get_mark_price(symbol)
            # 如果没有标记价格，暂时用持仓均价代替，或者跳过
            if mark_price <= 0: 
                mark_price = self.exposure.avg_prices[symbol]
            
            if mark_price > 0:
                avg_price = self.exposure.avg_prices[symbol]
                u_pnl = (mark_price - avg_price) * pos_vol
                unrealized_pnl += u_pnl
                pos_margin += (abs(pos_vol) * mark_price) / self.leverage

        # 2. 挂单 保证金
        for symbol, qty in self.exposure.open_buy_qty.items():
            mp = data_cache.get_mark_price(symbol)
            if mp <= 0: mp = data_cache.get_best_quote(symbol)[0] # Fallback to Bid1
            if mp > 0: order_margin += (qty * mp) / self.leverage
            
        for symbol, qty in self.exposure.open_sell_qty.items():
            mp = data_cache.get_mark_price(symbol)
            if mp <= 0: mp = data_cache.get_best_quote(symbol)[1] # Fallback to Ask1
            if mp > 0: order_margin += (qty * mp) / self.leverage

        # 3. 汇总
        self.equity = self.balance + unrealized_pnl
        self.used_margin = pos_margin + order_margin
        self.available = max(0.0, self.equity - self.used_margin)
        
        # 推送
        data = AccountData(
            balance=self.balance,
            equity=self.equity,
            available=self.available,
            used_margin=self.used_margin,
            datetime=datetime.now()
        )
        self.engine.put(Event(EVENT_ACCOUNT_UPDATE, data))

    def check_margin(self, notional_value):
        """预风控：检查可用资金"""
        required = notional_value / self.leverage
        # [优化] 如果可用资金为0，尝试强制刷新一次（可能是行情刚来）
        if self.available == 0:
            self.calculate()
            
        return self.available >= required