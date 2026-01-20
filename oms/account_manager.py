# file: oms/account_manager.py

from datetime import datetime
from event.type import Event, EVENT_ACCOUNT_UPDATE, AccountData

class AccountManager:
    """
    资产与保证金管理器
    Available = Balance + UnrealizedPnL - PositionMargin - OpenOrderMargin
    """
    def __init__(self, engine, position_manager, order_manager, config):
        self.engine = engine
        self.pm = position_manager
        self.om = order_manager
        
        acc_conf = config.get("account", {})
        self.balance = acc_conf.get("initial_balance_usdt", 10000.0)
        self.leverage = acc_conf.get("leverage", 10)
        
        self.equity = self.balance
        self.available = 0.0
        self.used_margin = 0.0

    def update_balance(self, realized_pnl, commission):
        """成交后更新余额"""
        self.balance += (realized_pnl - commission)
        self.calculate()

    def calculate(self):
        """核心计算逻辑：实时计算权益和可用资金"""
        # 1. 计算未结盈亏 (Unrealized PnL) 和 持仓占用保证金
        unrealized_pnl = 0.0
        pos_margin = 0.0
        
        # 从 PositionManager 获取最新持仓
        for symbol, pos in self.pm.positions.items():
            # 这里的 price 应该是 mark_price，但在 OMS 里我们暂时用 entry_price 近似，
            # 或者需要从 DataCache 获取最新 MarkPrice (更精准)
            # 为解耦，这里暂时忽略 PnL 对保证金的动态增加 (保守计算)，只计算占用
            
            # Position Margin = Value / Leverage
            # Value = abs(volume) * entry_price (近似)
            pos_value = abs(pos.volume) * pos.price
            pos_margin += pos_value / self.leverage
            
            # 简单估算 PnL (仅用于展示 Equity，不用于 Margin 释放，保守风控)
            # 若要用于 Margin，需要实时 MarkPrice
            # unrealized_pnl += pos.pnl 

        # 2. 计算挂单占用保证金 (Open Order Margin)
        open_order_val = self.om.get_open_orders_cost()
        order_margin = open_order_val / self.leverage
        
        # 3. 汇总
        self.used_margin = pos_margin + order_margin
        
        # 在保守模式下，Equity 不计入浮盈 (防止浮盈加仓导致爆仓)，但计入浮亏
        # 这里简化：Equity = Balance
        self.equity = self.balance + unrealized_pnl
        
        self.available = self.equity - self.used_margin
        
        # 推送事件
        data = AccountData(
            balance=self.balance,
            equity=self.equity,
            available=self.available,
            used_margin=self.used_margin,
            datetime=datetime.now()
        )
        self.engine.put(Event(EVENT_ACCOUNT_UPDATE, data))

    def check_margin(self, notional_value):
        """
        预风控检查：是否有足够保证金下单
        """
        self.calculate() # 强制刷新一次
        required_margin = notional_value / self.leverage
        return self.available >= required_margin