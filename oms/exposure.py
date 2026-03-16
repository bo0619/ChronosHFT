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

    def check_risk(
        self,
        symbol: str,
        side: Side,
        volume: float,
        max_pos_notional: float,
        max_account_gross_notional: float = 0.0,
        order_price: float = 0.0,
    ) -> tuple:
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

        new_buy_qty = volume if side == Side.BUY else 0.0
        new_sell_qty = volume if side == Side.SELL else 0.0

        worst_long = self._symbol_worst_long_qty(symbol, new_buy_qty)
        worst_short = self._symbol_worst_short_qty(symbol, new_sell_qty)
        max_exposure = max(abs(worst_long), abs(worst_short))
        potential_val = max_exposure * mark_price

        if potential_val > max_pos_notional:
            return False, (
                f"Exposure Limit: worst_long={worst_long:.4f} "
                f"worst_short={worst_short:.4f} "
                f"max_val={potential_val:.2f} > {max_pos_notional} "
                f"(Pos={self.net_positions[symbol]:.4f})"
            )

        if max_account_gross_notional > 0:
            gross_notional = self.estimate_account_gross_notional(
                symbol=symbol,
                side=side,
                volume=volume,
                order_price=order_price,
            )
            if gross_notional is None:
                return False, f"Account Gross Exposure unavailable for {symbol}"
            if gross_notional > max_account_gross_notional:
                return False, (
                    f"Account Gross Exposure: projected={gross_notional:.2f} "
                    f"> {max_account_gross_notional}"
                )

        return True, ""

    def estimate_account_gross_notional(
        self,
        symbol: str = "",
        side: Side = None,
        volume: float = 0.0,
        order_price: float = 0.0,
    ):
        target_symbol = (symbol or "").upper()
        tracked_symbols = set(self.net_positions.keys())
        tracked_symbols.update(self.open_buy_qty.keys())
        tracked_symbols.update(self.open_sell_qty.keys())
        if target_symbol:
            tracked_symbols.add(target_symbol)

        gross_notional = 0.0
        for tracked_symbol in tracked_symbols:
            add_buy = volume if tracked_symbol == target_symbol and side == Side.BUY else 0.0
            add_sell = volume if tracked_symbol == target_symbol and side == Side.SELL else 0.0
            max_exposure = self._symbol_worst_case_abs_qty(tracked_symbol, add_buy, add_sell)
            if max_exposure <= 1e-9:
                continue

            fallback_price = order_price if tracked_symbol == target_symbol else 0.0
            mark_price = self._get_price_for_risk(tracked_symbol, fallback_price)
            if mark_price <= 0:
                return None
            gross_notional += max_exposure * mark_price

        return gross_notional

    def _symbol_worst_case_abs_qty(
        self,
        symbol: str,
        add_buy_qty: float = 0.0,
        add_sell_qty: float = 0.0,
    ) -> float:
        worst_long = self._symbol_worst_long_qty(symbol, add_buy_qty)
        worst_short = self._symbol_worst_short_qty(symbol, add_sell_qty)
        return max(abs(worst_long), abs(worst_short))

    def _symbol_worst_long_qty(self, symbol: str, add_buy_qty: float = 0.0) -> float:
        current_pos = self.net_positions[symbol]
        return current_pos + self.open_buy_qty[symbol] + add_buy_qty

    def _symbol_worst_short_qty(self, symbol: str, add_sell_qty: float = 0.0) -> float:
        current_pos = self.net_positions[symbol]
        return current_pos - self.open_sell_qty[symbol] - add_sell_qty

    def _get_price_for_risk(self, symbol: str, fallback_price: float = 0.0) -> float:
        mark_price = data_cache.get_mark_price(symbol)
        if mark_price > 0:
            return mark_price

        bid_price, ask_price = data_cache.get_best_quote(symbol)
        if bid_price > 0 and ask_price > 0:
            return (bid_price + ask_price) / 2.0
        if bid_price > 0:
            return bid_price
        if ask_price > 0:
            return ask_price

        avg_price = abs(self.avg_prices[symbol] or 0.0)
        if avg_price > 0:
            return avg_price
        return fallback_price

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
