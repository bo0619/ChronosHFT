# file: strategy/demo_strategy.py

import time
from .base import StrategyTemplate
from event.type import OrderBook, TradeData, PositionData

class DemoStrategy(StrategyTemplate):
    def __init__(self, engine, gateway, risk_manager):
        super().__init__(engine, gateway, risk_manager, "DemoStrategy")
        self.triggered = False

    def on_orderbook(self, ob: OrderBook):
        bid_1_p, _ = ob.get_best_bid()
        if bid_1_p == 0: return

        if not self.triggered:
            self.log("=== [策略] 发现行情，挂单排队测试 ===")
            for i in range(5):
                # [修改] 挂在买一价 (不加价)，或者买一价下方
                # 这样才能真正测试 "排队" 逻辑
                price = bid_1_p 
                volume = 0.002
                self.buy(ob.symbol, price, volume)
            self.triggered = True

    def on_trade(self, trade: TradeData):
        self.log(f"!!! 收到成交回报 !!! ID={trade.trade_id} Price={trade.price}")

    def on_position(self, pos: PositionData):
        self.log(f"当前持仓: {pos.direction} Vol={pos.volume} AvgPrice={pos.price}")