# file: oms/exposure.py

from collections import defaultdict
from event.type import Side, PositionData
from data.cache import data_cache # [NEW] 引入价格源

class ExposureManager:
    """
    [Single Source of Truth] 仓位与敞口管理器
    职责：
    1. 维护 Net Position (唯一真理)
    2. 维护 Open Orders Quantity (挂单统计)
    3. 计算系统性风险 (考虑并发挂单的影响)
    """
    def __init__(self):
        # 核心状态
        # Symbol -> float (正=多, 负=空)
        self.net_positions = defaultdict(float)
        self.avg_prices = defaultdict(float)
        
        # 挂单统计 (只存数量，不存金额，金额由实时价格计算)
        # Symbol -> float (绝对值，只增不减)
        self.open_buy_qty = defaultdict(float)
        self.open_sell_qty = defaultdict(float)

    def on_fill(self, symbol: str, side: Side, qty: float, price: float):
        """
        成交更新：这是改变 Net Position 的唯一途径
        """
        current_pos = self.net_positions[symbol]
        signed_qty = qty if side == Side.BUY else -qty
        
        # 1. 均价计算 (同向加仓更新，反向减仓不变，反手重置)
        is_increasing = False
        if current_pos == 0: is_increasing = True
        elif current_pos > 0 and signed_qty > 0: is_increasing = True
        elif current_pos < 0 and signed_qty < 0: is_increasing = True
            
        if is_increasing:
            total_val = abs(current_pos) * self.avg_prices[symbol] + qty * price
            new_total = abs(current_pos) + qty
            if new_total > 0:
                self.avg_prices[symbol] = total_val / new_total
        
        # 2. 更新持仓
        self.net_positions[symbol] += signed_qty
        
        # 3. 反手/清仓处理
        if abs(self.net_positions[symbol]) < 1e-9:
            self.net_positions[symbol] = 0.0
            self.avg_prices[symbol] = 0.0
        elif (current_pos > 0 > self.net_positions[symbol]) or (current_pos < 0 < self.net_positions[symbol]):
            self.avg_prices[symbol] = price

    def update_open_orders(self, active_orders):
        """
        全量重算挂单敞口
        """
        self.open_buy_qty.clear()
        self.open_sell_qty.clear()
        
        for order in active_orders.values():
            if not order.is_active(): continue
            
            rem_vol = order.intent.volume - order.filled_volume
            if rem_vol <= 0: continue
            
            if order.intent.side == Side.BUY:
                self.open_buy_qty[order.intent.symbol] += rem_vol
            else:
                self.open_sell_qty[order.intent.symbol] += rem_vol

    def check_risk(self, symbol, side, volume, max_pos_notional):
        """
        [系统级风控] 
        检查：(当前持仓 + 同方向所有挂单 + 本次新单) * MarkPrice 是否超限
        """
        mark_price = data_cache.get_mark_price(symbol)
        if mark_price <= 0:
            return False, f"MarkPrice unavailable for {symbol}"

        current_pos = self.net_positions[symbol]
        
        # 计算潜在最大持仓 (Worst-case Scenario)
        # 如果是买单：假设所有 Buy Pending 都成交
        # 如果是卖单：假设所有 Sell Pending 都成交
        if side == Side.BUY:
            # 潜在多头 = 当前持仓 + 已挂买单 + 新买单
            # 注意：如果当前是空头(-10)，挂买单(+5)是减仓，其实风险降低。
            # 但为了防止 "反向瞬间翻仓" 导致的风险，我们直接按代数和计算，取绝对值最大情况
            potential_pos = current_pos + self.open_buy_qty[symbol] + volume
        else:
            potential_pos = current_pos - self.open_sell_qty[symbol] - volume
            
        potential_val = abs(potential_pos) * mark_price
        
        if potential_val > max_pos_notional:
            return False, f"Exposure Limit: {potential_val:.2f} > {max_pos_notional} (Pos:{current_pos} + Pending)"
            
        return True, ""

    def get_position_data(self, symbol: str) -> PositionData:
        """生成标准数据供 UI/Strategy 使用"""
        return PositionData(
            symbol=symbol,
            volume=self.net_positions[symbol],
            price=self.avg_prices[symbol],
            pnl=0.0 # PnL 由 AccountManager 或 UI 计算，Exposure 只管量
        )