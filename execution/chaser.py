# file: execution/chaser.py

from .algo_base import AlgoTemplate

class ChaseAlgo(AlgoTemplate):
    """
    智能挂单 (Smart Limit / Pegging)
    始终跟随买一/卖一价，直到成交。
    """
    def __init__(self, algo_id, symbol, direction, total_vol, engine, strategy, max_chase_price=None):
        super().__init__(algo_id, symbol, direction, total_vol, engine, strategy)
        self.max_chase_price = max_chase_price # 比如买入时最高能追到多少
        self.current_oid = None
        self.last_price = 0

    def on_tick(self, ob):
        if self.finished: return
        
        # 目标价格：盘口最优价
        target_price = 0
        if self.direction == "BUY":
            target_price = ob.get_best_bid()[0]
            # 价格保护
            if self.max_chase_price and target_price > self.max_chase_price:
                return # 价格太高，不追了
        else:
            target_price = ob.get_best_ask()[0]
            if self.max_chase_price and target_price < self.max_chase_price:
                return 

        # 如果没有挂单，发新单
        if not self.current_oid:
            self._send_new(target_price)
            return

        # 如果有挂单，检查是否需要改单 (Chase)
        # 只有当价格变动超过一定阈值（比如一个Tick）才改单，防止频繁撤挂
        if abs(target_price - self.last_price) > 1e-8: # 简单浮点不等
            # 撤旧单
            self.strategy.cancel_order(self.current_oid)
            self.current_oid = None # 等待撤单成功回调后再发？HFT通常并发。
            # 这里简化：发新单
            self._send_new(target_price)

    def _send_new(self, price):
        left = self.total_vol - self.traded_vol
        if left <= 0: return
        
        if self.direction == "BUY":
            # 使用 PostOnly (GTX) 确保只做 Maker
            # 这里需要在 Strategy 增加支持 GTX 参数的接口，或者默认 Limit
            # 假设 strategy.buy 支持 kwargs
            oid = self.strategy.buy(self.symbol, price, left)
        else:
            oid = self.strategy.sell(self.symbol, price, left)
            
        if oid:
            self.current_oid = oid
            self.active_orders[oid] = left
            self.last_price = price