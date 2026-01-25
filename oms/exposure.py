# file: oms/exposure.py

from collections import defaultdict
from event.type import Side, PositionData

class ExposureManager:
    """
    负责维护账户的实时敞口 (Exposure)
    Exposure = Held Position + Open Orders
    """
    def __init__(self):
        # Symbol -> float (正多负空)
        self.positions = defaultdict(float)
        self.avg_prices = defaultdict(float)
        
        # 挂单占用 (Open Interest)
        # Symbol -> float (挂买量, 挂卖量)
        self.open_buy_notional = defaultdict(float)
        self.open_sell_notional = defaultdict(float)

    def on_fill(self, symbol: str, side: Side, qty: float, price: float):
        """
        成交发生时更新持仓
        """
        current_pos = self.positions[symbol]
        
        # 1. 更新持仓均价和数量 (单向持仓逻辑)
        # Side.BUY -> 增加正数 (或减少负数)
        # Side.SELL -> 增加负数 (或减少正数)
        # 为了通用性，将 qty 转为带符号
        signed_qty = qty if side == Side.BUY else -qty
        
        # 判断是否是 "同向加仓"
        is_increasing = False
        if current_pos == 0:
            is_increasing = True
        elif current_pos > 0 and signed_qty > 0:
            is_increasing = True
        elif current_pos < 0 and signed_qty < 0:
            is_increasing = True
            
        if is_increasing:
            # 加仓：更新加权均价
            total_val = abs(current_pos) * self.avg_prices[symbol] + qty * price
            new_total = abs(current_pos) + qty
            if new_total > 0:
                self.avg_prices[symbol] = total_val / new_total
        
        # 更新数量
        self.positions[symbol] += signed_qty
        
        # 如果减仓减到0，重置均价
        if abs(self.positions[symbol]) < 1e-9:
            self.positions[symbol] = 0.0
            self.avg_prices[symbol] = 0.0
        # 如果反手了 (正变负 或 负变正)，均价重置为本次成交价
        elif (current_pos > 0 > self.positions[symbol]) or (current_pos < 0 < self.positions[symbol]):
            self.avg_prices[symbol] = price

    def update_open_orders(self, active_orders):
        """
        根据当前活跃订单重算挂单敞口
        active_orders: dict {client_oid: Order}
        """
        self.open_buy_notional.clear()
        self.open_sell_notional.clear()
        
        for order in active_orders.values():
            if not order.is_active(): continue
            
            # 剩余未成交量
            rem_vol = order.intent.volume - order.filled_volume
            if rem_vol <= 0: continue
            
            notional = rem_vol * order.intent.price
            
            if order.intent.side == Side.BUY:
                self.open_buy_notional[order.intent.symbol] += notional
            else:
                self.open_sell_notional[order.intent.symbol] += notional

    def check_risk(self, symbol, side, price, volume, max_pos_notional):
        """
        风控检查：预测成交后的持仓是否超限
        """
        current_pos = self.positions[symbol]
        
        # 预测净持仓价值
        # 这是一个保守估算：假设该笔订单成交后的总持仓价值
        future_pos = current_pos + volume if side == Side.BUY else current_pos - volume
        future_val = abs(future_pos) * price
        
        if future_val > max_pos_notional:
            return False, f"Max Position Limit: {future_val:.2f} > {max_pos_notional}"
            
        return True, ""

    def get_position_data(self, symbol: str) -> PositionData:
        """
        [NEW] 生成标准化的 PositionData 对象供 OMS 广播
        """
        return PositionData(
            symbol=symbol,
            volume=self.positions[symbol],
            price=self.avg_prices[symbol],
            pnl=0.0 # OMS 暂不计算 PnL，交由 UI 或 Accountant 处理
        )