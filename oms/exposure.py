# file: oms/exposure.py
# [FIX-RISK] check_risk(): 单边 worst-case 导致双边敞口被低估

from collections import defaultdict
from event.type import Side, PositionData
from data.cache import data_cache


class ExposureManager:
    """
    [Single Source of Truth] 仓位与敞口管理器
    """

    def __init__(self):
        # 核心持仓状态（Symbol → float，正=多头，负=空头）
        self.net_positions = defaultdict(float)
        self.avg_prices    = defaultdict(float)

        # 挂单统计（Symbol → float，绝对量）
        self.open_buy_qty  = defaultdict(float)
        self.open_sell_qty = defaultdict(float)

    # ----------------------------------------------------------
    # 持仓更新（唯一入口）
    # ----------------------------------------------------------

    def on_fill(self, symbol: str, side: Side, qty: float, price: float):
        """成交更新：改变 Net Position 的唯一途径"""
        current_pos = self.net_positions[symbol]
        signed_qty  = qty if side == Side.BUY else -qty

        # 均价：同向加仓更新，反向减仓不变，反手重置
        is_increasing = (
            current_pos == 0
            or (current_pos > 0 and signed_qty > 0)
            or (current_pos < 0 and signed_qty < 0)
        )

        if is_increasing:
            total_val = abs(current_pos) * self.avg_prices[symbol] + qty * price
            new_total = abs(current_pos) + qty
            if new_total > 0:
                self.avg_prices[symbol] = total_val / new_total

        self.net_positions[symbol] += signed_qty

        # 清仓 / 反手处理
        if abs(self.net_positions[symbol]) < 1e-9:
            self.net_positions[symbol] = 0.0
            self.avg_prices[symbol]    = 0.0
        elif (current_pos > 0 > self.net_positions[symbol]
              or current_pos < 0 < self.net_positions[symbol]):
            self.avg_prices[symbol] = price

    # ----------------------------------------------------------
    # 挂单统计
    # ----------------------------------------------------------

    def update_open_orders(self, active_orders: dict):
        """全量重算挂单敞口（每次订单状态变化后调用）"""
        self.open_buy_qty.clear()
        self.open_sell_qty.clear()

        for order in active_orders.values():
            if not order.is_active():
                continue
            rem_vol = order.intent.volume - order.filled_volume
            if rem_vol <= 0:
                continue
            if order.intent.side == Side.BUY:
                self.open_buy_qty[order.intent.symbol]  += rem_vol
            else:
                self.open_sell_qty[order.intent.symbol] += rem_vol

    # ----------------------------------------------------------
    # 风险检查（双边 worst-case）
    # ----------------------------------------------------------

    def check_risk(self, symbol: str, side: Side, volume: float,
                   max_pos_notional: float) -> tuple:
        """
        [FIX-RISK] 双边 worst-case 敞口检查

        逻辑：
          做市商同时挂双边挂单，极端行情下只有一侧成交：
            最坏多头 = current_pos + open_buy_qty  + new_buy_vol
              （所有买单成交，卖单全撤）
            最坏空头 = current_pos - open_sell_qty - new_sell_vol
              （所有卖单成交，买单全撤）
          取两者绝对值的最大值作为风险敞口。

        参数：
          side   — 本次新单方向
          volume — 本次新单数量
        """
        mark_price = data_cache.get_mark_price(symbol)
        if mark_price <= 0:
            return False, f"MarkPrice unavailable for {symbol}"

        current_pos = self.net_positions[symbol]

        # 新单对两侧 worst-case 的贡献
        new_buy_qty  = volume if side == Side.BUY  else 0.0
        new_sell_qty = volume if side == Side.SELL else 0.0

        # [FIX-RISK] 双边 worst-case
        worst_long  = current_pos + self.open_buy_qty[symbol]  + new_buy_qty
        worst_short = current_pos - self.open_sell_qty[symbol] - new_sell_qty

        # 取绝对值最大的一侧
        max_exposure = max(abs(worst_long), abs(worst_short))
        potential_val = max_exposure * mark_price

        if potential_val > max_pos_notional:
            return False, (
                f"Exposure Limit: worst_long={worst_long:.4f} "
                f"worst_short={worst_short:.4f} "
                f"max_val={potential_val:.2f} > {max_pos_notional} "
                f"(Pos={current_pos:.4f})"
            )

        return True, ""

    # ----------------------------------------------------------
    # 工具方法
    # ----------------------------------------------------------

    def get_position_data(self, symbol: str) -> PositionData:
        return PositionData(
            symbol=symbol,
            volume=self.net_positions[symbol],
            price=self.avg_prices[symbol],
            pnl=0.0,
        )

    def force_sync(self, symbol: str, volume: float, price: float):
        """强制同步持仓（启动时从交易所拉取）"""
        self.net_positions[symbol] = volume
        self.avg_prices[symbol]    = price