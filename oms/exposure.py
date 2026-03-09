# file: oms/exposure.py
# [FIX-RISK] check_risk(): ?? worst-case ?????????

from collections import defaultdict
from event.type import Side, PositionData
from data.cache import data_cache


class ExposureManager:
    """
    [Single Source of Truth] ????????
    """

    def __init__(self):
        # ???????Symbol -> float??=????=???
        self.net_positions = defaultdict(float)
        self.avg_prices = defaultdict(float)

        # ?????Symbol -> float?????
        self.open_buy_qty = defaultdict(float)
        self.open_sell_qty = defaultdict(float)

    # ----------------------------------------------------------
    # ??????????
    # ----------------------------------------------------------

    def on_fill(self, symbol: str, side: Side, qty: float, price: float) -> float:
        """??????? Net Position ???????????? PnL"""
        current_pos = self.net_positions[symbol]
        avg_price = self.avg_prices[symbol]
        signed_qty = qty if side == Side.BUY else -qty
        next_pos = current_pos + signed_qty
        realized_pnl = 0.0

        # ?????????????????????
        is_increasing = (
            current_pos == 0
            or (current_pos > 0 and signed_qty > 0)
            or (current_pos < 0 and signed_qty < 0)
        )

        if is_increasing:
            total_val = abs(current_pos) * avg_price + qty * price
            new_total = abs(current_pos) + qty
            if new_total > 0:
                self.avg_prices[symbol] = total_val / new_total
        else:
            closing_qty = min(abs(current_pos), qty)
            if current_pos > 0:
                realized_pnl = (price - avg_price) * closing_qty
            else:
                realized_pnl = (avg_price - price) * closing_qty

        self.net_positions[symbol] = next_pos

        # ?? / ????
        if abs(self.net_positions[symbol]) < 1e-9:
            self.net_positions[symbol] = 0.0
            self.avg_prices[symbol] = 0.0
        elif current_pos > 0 > self.net_positions[symbol] or current_pos < 0 < self.net_positions[symbol]:
            self.avg_prices[symbol] = price

        return realized_pnl

    # ----------------------------------------------------------
    # ????
    # ----------------------------------------------------------

    def update_open_orders(self, active_orders: dict):
        """?????????????????????"""
        self.open_buy_qty.clear()
        self.open_sell_qty.clear()

        for order in active_orders.values():
            if not order.is_active():
                continue
            rem_vol = order.intent.volume - order.filled_volume
            if rem_vol <= 0:
                continue
            if order.intent.side == Side.BUY:
                self.open_buy_qty[order.intent.symbol] += rem_vol
            else:
                self.open_sell_qty[order.intent.symbol] += rem_vol

    # ----------------------------------------------------------
    # ??????? worst-case?
    # ----------------------------------------------------------

    def check_risk(self, symbol: str, side: Side, volume: float,
                   max_pos_notional: float) -> tuple:
        """
        [FIX-RISK] ?? worst-case ????

        ???
          ???????????????????????
            ???? = current_pos + open_buy_qty  + new_buy_vol
              ?????????????
            ???? = current_pos - open_sell_qty - new_sell_vol
              ?????????????
          ?????????????????

        ???
          side   - ??????
          volume - ??????
        """
        mark_price = data_cache.get_mark_price(symbol)
        if mark_price <= 0:
            return False, f"MarkPrice unavailable for {symbol}"

        current_pos = self.net_positions[symbol]

        # ????? worst-case ???
        new_buy_qty = volume if side == Side.BUY else 0.0
        new_sell_qty = volume if side == Side.SELL else 0.0

        # [FIX-RISK] ?? worst-case
        worst_long = current_pos + self.open_buy_qty[symbol] + new_buy_qty
        worst_short = current_pos - self.open_sell_qty[symbol] - new_sell_qty

        # ??????? ??
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
    # ????
    # ----------------------------------------------------------

    def get_position_data(self, symbol: str) -> PositionData:
        return PositionData(
            symbol=symbol,
            volume=self.net_positions[symbol],
            price=self.avg_prices[symbol],
            pnl=0.0,
        )

    def force_sync(self, symbol: str, volume: float, price: float):
        """?????????????????"""
        self.net_positions[symbol] = volume
        self.avg_prices[symbol] = price
