# file: oms/main_oms.py

from .position import PositionManager
from .order_manager import OrderManager
from .account_manager import AccountManager
# [NEW] 引入 EVENT_ORDER_SUBMITTED
from event.type import EVENT_TRADE_UPDATE, EVENT_ORDER_UPDATE, EVENT_ORDERBOOK, EVENT_ORDER_SUBMITTED

class OMS:
    def __init__(self, engine, gateway, config):
        self.position = PositionManager(engine)
        self.order = OrderManager(engine, gateway)
        self.account = AccountManager(engine, self.position, self.order, config)
        
        self.engine = engine
        
        # 注册事件监听
        self.engine.register(EVENT_TRADE_UPDATE, self.on_trade)
        self.engine.register(EVENT_ORDER_UPDATE, self.on_order)
        self.engine.register(EVENT_ORDERBOOK, self.on_tick)
        
        # [NEW] 监听订单提交事件 (Post-Trade Recording)
        self.engine.register(EVENT_ORDER_SUBMITTED, self.on_order_submitted)

    def on_trade(self, event):
        trade = event.data
        fee = trade.price * trade.volume * 0.0005
        self.account.update_balance(0, fee)

    def on_order(self, event):
        self.order.on_order_update(event.data)
        self.account.calculate()

    def on_order_submitted(self, event):
        """收到策略发单成功的通知"""
        self.order.on_order_submitted(event)
        self.account.calculate() # 立即刷新保证金占用

    def on_tick(self, event):
        pass 

    def check_risk(self, notional):
        return self.account.check_margin(notional)

    def stop(self):
        self.order.stop()