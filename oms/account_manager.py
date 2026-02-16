# file: oms/account_manager.py

from datetime import datetime
from event.type import Event, EVENT_ACCOUNT_UPDATE, AccountData
from data.cache import data_cache

class AccountManager:
    def __init__(self, engine, exposure_manager, config):
        self.engine = engine
        self.exposure = exposure_manager
        
        acc_conf = config.get("account", {})
        self.balance = acc_conf.get("initial_balance_usdt", 10000.0)
        self.leverage = acc_conf.get("leverage", 10)
        
        self.equity = self.balance
        self.used_margin = 0.0
        self.available = self.balance 

    def force_sync(self, balance: float, used_margin: float):
        """强制同步 (来自交易所真值)"""
        self.balance = balance
        # 注意：这里我们暂时信任交易所的 used_margin，但在下一次 calculate 时会用本地逻辑覆盖
        # 为了平滑，我们可以先赋值
        self.used_margin = used_margin
        self.equity = balance # 初始假设
        self.calculate()

    def update_balance(self, realized_pnl, commission):
        self.balance += (realized_pnl - commission)
        self.calculate()

    def calculate(self):
        """
        全量重算资金与保证金
        """
        unrealized_pnl = 0.0
        pos_margin = 0.0
        order_margin = 0.0
        
        # 1. 计算持仓 PnL 和 保证金
        for symbol, pos_vol in self.exposure.net_positions.items():
            if pos_vol == 0: continue
            
            # 优先使用 MarkPrice，其次用 DataCache 最新盘口，最后用持仓均价
            mark_price = data_cache.get_mark_price(symbol)
            if mark_price <= 0:
                mark_price, _ = data_cache.get_best_quote(symbol)
            if mark_price <= 0:
                mark_price = self.exposure.avg_prices[symbol]
            
            if mark_price > 0:
                avg_price = self.exposure.avg_prices[symbol]
                # PnL
                u_pnl = (mark_price - avg_price) * pos_vol
                unrealized_pnl += u_pnl
                
                # Margin = Notional / Leverage
                pos_margin += (abs(pos_vol) * mark_price) / self.leverage

        # 2. 计算挂单保证金
        for symbol, qty in self.exposure.open_buy_qty.items():
            mp = self._get_price_safely(symbol)
            if mp > 0: order_margin += (qty * mp) / self.leverage
            
        for symbol, qty in self.exposure.open_sell_qty.items():
            mp = self._get_price_safely(symbol)
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

    def _get_price_safely(self, symbol):
        """获取估值价格的辅助函数"""
        p = data_cache.get_mark_price(symbol)
        if p <= 0: p, _ = data_cache.get_best_quote(symbol)
        return p

    def check_margin(self, notional_value):
        required = notional_value / self.leverage
        if self.available == 0: self.calculate() # 惰性重算
        return self.available >= required

    def get_margin_ratio(self):
        """
        [NEW] 获取当前保证金率 (0.0 ~ 1.0+)
        Used / Equity
        """
        if self.equity <= 0: return 0.0
        return self.used_margin / self.equity