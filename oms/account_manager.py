# file: oms/account_manager.py

from datetime import datetime
from event.type import Event, EVENT_ACCOUNT_UPDATE, AccountData
from data.cache import data_cache # [NEW]

class AccountManager:
    """
    资金与保证金管理器
    Available = Equity - Used Margin
    Equity = Balance + Unrealized PnL (Mark Price)
    """
    def __init__(self, engine, exposure_manager, config):
        self.engine = engine
        self.exposure = exposure_manager # 只读 Exposure
        
        acc_conf = config.get("account", {})
        self.balance = acc_conf.get("initial_balance_usdt", 10000.0)
        self.leverage = acc_conf.get("leverage", 10)
        
        self.equity = self.balance
        self.available = 0.0
        self.used_margin = 0.0

    def update_balance(self, realized_pnl, commission):
        """只更新余额 (Realized)"""
        self.balance += (realized_pnl - commission)
        # 注意：这里不自动 calculate，由 OMS Engine 统一调度

    def calculate(self):
        """
        全量重算
        依赖：Exposure(Pos, OpenQty), DataCache(MarkPrice)
        """
        unrealized_pnl = 0.0
        pos_margin = 0.0
        order_margin = 0.0
        
        # 1. 遍历所有持仓 (计算 UPNL 和 仓位保证金)
        for symbol, pos_vol in self.exposure.net_positions.items():
            if pos_vol == 0: continue
            
            mark_price = data_cache.get_mark_price(symbol)
            if mark_price <= 0: continue # 价格未知，保守忽略或报错
            
            avg_price = self.exposure.avg_prices[symbol]
            
            # UPNL = (Mark - Avg) * Volume
            # 多头: (105 - 100) * 1 = 5
            # 空头: (95 - 100) * -1 = 5
            u_pnl = (mark_price - avg_price) * pos_vol
            unrealized_pnl += u_pnl
            
            # Pos Margin = Abs(Vol) * MarkPrice / Leverage
            pos_margin += (abs(pos_vol) * mark_price) / self.leverage

        # 2. 遍历所有挂单 (计算 挂单保证金)
        # Margin = (OpenBuyQty + OpenSellQty) * MarkPrice / Leverage
        # 注：币安双向持仓是单边保证金，单向持仓是净头寸+挂单。
        # 这里采用保守算法：所有挂单都占用保证金
        for symbol, qty in self.exposure.open_buy_qty.items():
            mp = data_cache.get_mark_price(symbol)
            order_margin += (qty * mp) / self.leverage
            
        for symbol, qty in self.exposure.open_sell_qty.items():
            mp = data_cache.get_mark_price(symbol)
            order_margin += (qty * mp) / self.leverage

        # 3. 汇总
        self.equity = self.balance + unrealized_pnl
        self.used_margin = pos_margin + order_margin
        
        # 可用资金不能为负
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
        # 这里假设 calculate 已经被及时调用过，直接用 cached available
        # 或者为了安全，这里也可以强制 calculate 一次，但耗性能
        required = notional_value / self.leverage
        return self.available >= required