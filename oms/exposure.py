# file: oms/exposure.py
# [FIX-RISK] check_risk(): ?? worst-case ?????????

import math
from collections.abc import Iterator, Mapping
from collections import defaultdict
from dataclasses import dataclass
from types import MappingProxyType

from event.type import Side, PositionData
from data.cache import data_cache


class _ReadOnlyLedgerView(Mapping):
    """Read-only compatibility view over one owned exposure ledger.

    The old exposure API was backed by ``defaultdict(float)`` instances, so
    callers commonly used ``ledger[symbol]`` for a missing value.  A plain
    ``MappingProxyType`` would make that read raise ``KeyError`` and would
    encourage callers to reach into the store for a workaround.  This view
    preserves the zero-default read semantics while exposing no mutating
    operation; all writes remain methods on :class:`ExposureStore`.
    """

    __slots__ = ("_values",)

    def __init__(self, values: defaultdict) -> None:
        self._values = values

    def __getitem__(self, key: object) -> float:
        return self._values.get(key, 0.0)

    def __iter__(self) -> Iterator:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


@dataclass(frozen=True, slots=True)
class ExposureSnapshot:
    """Immutable copy of the complete exposure ledger."""

    net_positions: Mapping
    avg_prices: Mapping
    open_buy_qty: Mapping
    open_sell_qty: Mapping
    reduce_only_buy_qty: Mapping
    reduce_only_sell_qty: Mapping
    strategy_net_positions: Mapping
    strategy_avg_prices: Mapping
    strategy_open_buy_qty: Mapping
    strategy_open_sell_qty: Mapping


class ExposureStore:
    """
    [Single Source of Truth] ????????
    """

    def __init__(self, market_cache=None):
        self._market_cache = market_cache or data_cache
        # The dictionaries below are the sole mutable storage cells.  Public
        # ledger names are read-only Mapping views declared just below.
        self._net_positions = defaultdict(float)
        self._avg_prices = defaultdict(float)
        self._open_buy_qty = defaultdict(float)
        self._open_sell_qty = defaultdict(float)
        self._reduce_only_buy_qty = defaultdict(float)
        self._reduce_only_sell_qty = defaultdict(float)
        self._strategy_net_positions = defaultdict(float)
        self._strategy_avg_prices = defaultdict(float)
        self._strategy_open_buy_qty = defaultdict(float)
        self._strategy_open_sell_qty = defaultdict(float)

        self._net_positions_view = _ReadOnlyLedgerView(self._net_positions)
        self._avg_prices_view = _ReadOnlyLedgerView(self._avg_prices)
        self._open_buy_qty_view = _ReadOnlyLedgerView(self._open_buy_qty)
        self._open_sell_qty_view = _ReadOnlyLedgerView(self._open_sell_qty)
        self._reduce_only_buy_qty_view = _ReadOnlyLedgerView(
            self._reduce_only_buy_qty
        )
        self._reduce_only_sell_qty_view = _ReadOnlyLedgerView(
            self._reduce_only_sell_qty
        )
        self._strategy_net_positions_view = _ReadOnlyLedgerView(
            self._strategy_net_positions
        )
        self._strategy_avg_prices_view = _ReadOnlyLedgerView(
            self._strategy_avg_prices
        )
        self._strategy_open_buy_qty_view = _ReadOnlyLedgerView(
            self._strategy_open_buy_qty
        )
        self._strategy_open_sell_qty_view = _ReadOnlyLedgerView(
            self._strategy_open_sell_qty
        )

    @property
    def net_positions(self) -> Mapping:
        return self._net_positions_view

    @property
    def avg_prices(self) -> Mapping:
        return self._avg_prices_view

    @property
    def open_buy_qty(self) -> Mapping:
        return self._open_buy_qty_view

    @property
    def open_sell_qty(self) -> Mapping:
        return self._open_sell_qty_view

    @property
    def reduce_only_buy_qty(self) -> Mapping:
        return self._reduce_only_buy_qty_view

    @property
    def reduce_only_sell_qty(self) -> Mapping:
        return self._reduce_only_sell_qty_view

    @property
    def strategy_net_positions(self) -> Mapping:
        return self._strategy_net_positions_view

    @property
    def strategy_avg_prices(self) -> Mapping:
        return self._strategy_avg_prices_view

    @property
    def strategy_open_buy_qty(self) -> Mapping:
        return self._strategy_open_buy_qty_view

    @property
    def strategy_open_sell_qty(self) -> Mapping:
        return self._strategy_open_sell_qty_view

    @staticmethod
    def _frozen_copy(values: Mapping) -> Mapping:
        return MappingProxyType(dict(values))

    def snapshot(self) -> ExposureSnapshot:
        """Return a detached immutable view for ports and telemetry."""

        return ExposureSnapshot(
            net_positions=self._frozen_copy(self._net_positions),
            avg_prices=self._frozen_copy(self._avg_prices),
            open_buy_qty=self._frozen_copy(self._open_buy_qty),
            open_sell_qty=self._frozen_copy(self._open_sell_qty),
            reduce_only_buy_qty=self._frozen_copy(
                self._reduce_only_buy_qty
            ),
            reduce_only_sell_qty=self._frozen_copy(
                self._reduce_only_sell_qty
            ),
            strategy_net_positions=self._frozen_copy(
                self._strategy_net_positions
            ),
            strategy_avg_prices=self._frozen_copy(
                self._strategy_avg_prices
            ),
            strategy_open_buy_qty=self._frozen_copy(
                self._strategy_open_buy_qty
            ),
            strategy_open_sell_qty=self._frozen_copy(
                self._strategy_open_sell_qty
            ),
        )

    def tracked_symbols(self) -> frozenset[str]:
        return frozenset(self._net_positions)

    def reset_account_ledgers(self, active_orders: Mapping) -> None:
        """Reset account truth and rebuild all order reservations."""

        if not isinstance(active_orders, Mapping):
            raise TypeError("active_orders must be a mapping")
        self._net_positions.clear()
        self._avg_prices.clear()
        self.update_open_orders(active_orders)

    @staticmethod
    def _strategy_key(raw_key: object) -> tuple[str, str]:
        if not isinstance(raw_key, tuple) or len(raw_key) != 2:
            raise ValueError(
                "strategy exposure keys must be (strategy_id, symbol)"
            )
        strategy_id, symbol = raw_key
        if not isinstance(strategy_id, str) or not strategy_id.strip():
            raise ValueError("strategy exposure strategy_id is invalid")
        if strategy_id != strategy_id.strip():
            raise ValueError("strategy exposure strategy_id is not canonical")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("strategy exposure symbol is invalid")
        canonical_symbol = symbol.strip().upper()
        if symbol != canonical_symbol:
            raise ValueError("strategy exposure symbol is not canonical")
        return strategy_id, canonical_symbol

    def restore_strategy_ledgers(
        self,
        positions: Mapping,
        average_prices: Mapping,
    ) -> None:
        """Validate then atomically replace replayed strategy exposure."""

        if not isinstance(positions, Mapping) or not isinstance(
            average_prices,
            Mapping,
        ):
            raise TypeError("strategy exposure ledgers must be mappings")
        if set(average_prices).difference(positions):
            raise ValueError(
                "strategy average-price ledger contains unknown positions"
            )

        restored_positions = defaultdict(float)
        restored_average_prices = defaultdict(float)
        for raw_key, raw_quantity in positions.items():
            key = self._strategy_key(raw_key)
            try:
                quantity = float(raw_quantity)
                average_price = float(average_prices.get(raw_key, 0.0))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "strategy exposure values must be numeric"
                ) from exc
            if (
                not math.isfinite(quantity)
                or not math.isfinite(average_price)
                or average_price < 0.0
                or (abs(quantity) > 1e-9 and average_price <= 0.0)
            ):
                raise ValueError("strategy exposure values are invalid")
            restored_positions[key] = quantity
            restored_average_prices[key] = (
                average_price if abs(quantity) > 1e-9 else 0.0
            )

        # Keep the owned dictionary identities stable so previously acquired
        # read-only views remain valid after a replay restore.
        self._strategy_net_positions.clear()
        self._strategy_net_positions.update(restored_positions)
        self._strategy_avg_prices.clear()
        self._strategy_avg_prices.update(restored_average_prices)
        self._strategy_open_buy_qty.clear()
        self._strategy_open_sell_qty.clear()

    def strategy_checkpoint_rows(self) -> tuple[dict, ...]:
        """Return deterministic JSON-safe strategy exposure rows."""

        return tuple(
            {
                "strategy_id": str(strategy_id),
                "symbol": str(symbol),
                "quantity": float(quantity),
                "average_price": float(
                    self._strategy_avg_prices.get(key, 0.0)
                ),
            }
            for key, quantity in sorted(
                self._strategy_net_positions.items(),
                key=lambda item: tuple(str(part) for part in item[0]),
            )
            for strategy_id, symbol in (key,)
        )

    # ----------------------------------------------------------
    # ??????????
    # ----------------------------------------------------------

    def on_fill(self, symbol: str, side: Side, qty: float, price: float) -> float:
        """??????? Net Position ???????????? PnL"""
        return self._apply_fill_to_ledger(
            self._net_positions,
            self._avg_prices,
            symbol,
            side,
            qty,
            price,
        )

    @staticmethod
    def _apply_fill_to_ledger(
        positions,
        average_prices,
        key,
        side: Side,
        qty: float,
        price: float,
    ) -> float:
        try:
            qty = float(qty)
            price = float(price)
        except (TypeError, ValueError) as exc:
            raise ValueError("Fill quantity and price must be numeric") from exc
        if (
            not math.isfinite(qty)
            or qty <= 0.0
            or not math.isfinite(price)
            or price <= 0.0
        ):
            raise ValueError(
                "Fill quantity and price must be finite and positive"
            )
        current_pos = positions[key]
        avg_price = average_prices[key]
        if (
            not math.isfinite(float(current_pos))
            or not math.isfinite(float(avg_price))
        ):
            raise ValueError(f"Exposure ledger is invalid for {key}")
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
                average_prices[key] = total_val / new_total
        else:
            closing_qty = min(abs(current_pos), qty)
            if current_pos > 0:
                realized_pnl = (price - avg_price) * closing_qty
            else:
                realized_pnl = (avg_price - price) * closing_qty

        positions[key] = next_pos

        # ?? / ????
        if abs(positions[key]) < 1e-9:
            positions[key] = 0.0
            average_prices[key] = 0.0
        elif current_pos > 0 > positions[key] or current_pos < 0 < positions[key]:
            average_prices[key] = price

        return realized_pnl

    def on_strategy_fill(
        self,
        strategy_id: str,
        symbol: str,
        side: Side,
        qty: float,
        price: float,
    ) -> float:
        key = (str(strategy_id or "exchange_recovery"), str(symbol or "").upper())
        return self._apply_fill_to_ledger(
            self._strategy_net_positions,
            self._strategy_avg_prices,
            key,
            side,
            qty,
            price,
        )

    def reconcile_strategy_position(
        self,
        symbol: str,
        account_position: float,
        price: float,
    ) -> float:
        symbol = str(symbol or "").upper()
        recovery_key = ("exchange_recovery", symbol)
        attributed = sum(
            position
            for (strategy_id, tracked_symbol), position in self._strategy_net_positions.items()
            if tracked_symbol == symbol and strategy_id != "exchange_recovery"
        )
        recovery_position = float(account_position) - attributed
        self._strategy_net_positions[recovery_key] = (
            0.0 if abs(recovery_position) < 1e-9 else recovery_position
        )
        self._strategy_avg_prices[recovery_key] = (
            abs(float(price or 0.0)) if abs(recovery_position) >= 1e-9 else 0.0
        )
        return self._strategy_net_positions[recovery_key]

    # ----------------------------------------------------------
    # ????
    # ----------------------------------------------------------

    def update_open_orders(self, active_orders: dict):
        """?????????????????????"""
        self._open_buy_qty.clear()
        self._open_sell_qty.clear()
        self._reduce_only_buy_qty.clear()
        self._reduce_only_sell_qty.clear()
        self._strategy_open_buy_qty.clear()
        self._strategy_open_sell_qty.clear()

        for order in active_orders.values():
            if not order.is_active():
                continue
            rem_vol = order.intent.volume - order.filled_volume
            if rem_vol <= 0:
                continue
            if order.intent.reduce_only and order.intent.side == Side.BUY:
                self._reduce_only_buy_qty[order.intent.symbol] += rem_vol
            elif order.intent.reduce_only:
                self._reduce_only_sell_qty[order.intent.symbol] += rem_vol
            elif order.intent.side == Side.BUY:
                self._open_buy_qty[order.intent.symbol] += rem_vol
                strategy_key = (
                    str(order.intent.strategy_id or "unattributed"),
                    order.intent.symbol,
                )
                self._strategy_open_buy_qty[strategy_key] += rem_vol
            else:
                self._open_sell_qty[order.intent.symbol] += rem_vol
                strategy_key = (
                    str(order.intent.strategy_id or "unattributed"),
                    order.intent.symbol,
                )
                self._strategy_open_sell_qty[strategy_key] += rem_vol

    def check_reduce_only(self, symbol: str, side: Side, volume: float) -> tuple:
        """Validate and reserve closes without allowing a position flip."""
        current_pos = float(self._net_positions[symbol] or 0.0)
        if abs(current_pos) <= 1e-9:
            return False, f"reduce_only_without_position:{symbol}"

        if current_pos > 0:
            if side != Side.SELL:
                return False, f"reduce_only_wrong_side:{symbol}:long_requires_sell"
            already_reserved = self._reduce_only_sell_qty[symbol]
            available = max(0.0, current_pos - already_reserved)
        else:
            if side != Side.BUY:
                return False, f"reduce_only_wrong_side:{symbol}:short_requires_buy"
            already_reserved = self._reduce_only_buy_qty[symbol]
            available = max(0.0, abs(current_pos) - already_reserved)

        if volume > available + 1e-9:
            return False, (
                f"reduce_only_exceeds_position:{symbol}:"
                f"requested={volume:.12g}>available={available:.12g}"
            )
        return True, ""

    def check_strategy_risk(
        self,
        strategy_id: str,
        symbol: str,
        side: Side,
        volume: float,
        max_strategy_gross_notional: float,
        max_strategy_symbol_notional: float,
        order_price: float = 0.0,
    ) -> tuple:
        strategy_id = str(strategy_id or "unattributed")
        symbol = str(symbol or "").upper()
        add_buy = volume if side == Side.BUY else 0.0
        add_sell = volume if side == Side.SELL else 0.0
        symbol_exposure = self._strategy_symbol_worst_case_abs_qty(
            strategy_id,
            symbol,
            add_buy,
            add_sell,
        )
        mark_price = self._get_price_for_risk(symbol, order_price)
        if mark_price <= 0.0:
            return False, f"Strategy Exposure price unavailable for {symbol}"
        symbol_notional = symbol_exposure * mark_price
        if symbol_notional > max_strategy_symbol_notional:
            return False, (
                f"Strategy Symbol Exposure: strategy={strategy_id} "
                f"symbol={symbol} projected={symbol_notional:.2f} "
                f"> {max_strategy_symbol_notional}"
            )

        gross_notional = self.estimate_strategy_gross_notional(
            strategy_id,
            symbol=symbol,
            side=side,
            volume=volume,
            order_price=order_price,
        )
        if gross_notional is None:
            return False, f"Strategy Gross Exposure unavailable for {strategy_id}"
        if gross_notional > max_strategy_gross_notional:
            return False, (
                f"Strategy Gross Exposure: strategy={strategy_id} "
                f"projected={gross_notional:.2f} > {max_strategy_gross_notional}"
            )
        return True, ""

    def estimate_strategy_gross_notional(
        self,
        strategy_id: str,
        symbol: str = "",
        side: Side = None,
        volume: float = 0.0,
        order_price: float = 0.0,
    ):
        strategy_id = str(strategy_id or "unattributed")
        target_symbol = str(symbol or "").upper()
        tracked_symbols = {
            tracked_symbol
            for tracked_strategy, tracked_symbol in self._strategy_net_positions
            if tracked_strategy == strategy_id
        }
        tracked_symbols.update(
            tracked_symbol
            for tracked_strategy, tracked_symbol in self._strategy_open_buy_qty
            if tracked_strategy == strategy_id
        )
        tracked_symbols.update(
            tracked_symbol
            for tracked_strategy, tracked_symbol in self._strategy_open_sell_qty
            if tracked_strategy == strategy_id
        )
        if target_symbol:
            tracked_symbols.add(target_symbol)

        gross_notional = 0.0
        for tracked_symbol in tracked_symbols:
            add_buy = (
                volume
                if tracked_symbol == target_symbol and side == Side.BUY
                else 0.0
            )
            add_sell = (
                volume
                if tracked_symbol == target_symbol and side == Side.SELL
                else 0.0
            )
            exposure = self._strategy_symbol_worst_case_abs_qty(
                strategy_id,
                tracked_symbol,
                add_buy,
                add_sell,
            )
            if exposure <= 1e-9:
                continue
            fallback_price = order_price if tracked_symbol == target_symbol else 0.0
            mark_price = self._get_price_for_risk(tracked_symbol, fallback_price)
            if mark_price <= 0.0:
                return None
            gross_notional += exposure * mark_price
        return gross_notional

    def _strategy_symbol_worst_case_abs_qty(
        self,
        strategy_id: str,
        symbol: str,
        add_buy_qty: float = 0.0,
        add_sell_qty: float = 0.0,
    ) -> float:
        key = (strategy_id, symbol)
        current_pos = self._strategy_net_positions[key]
        worst_long = current_pos + self._strategy_open_buy_qty[key] + add_buy_qty
        worst_short = current_pos - self._strategy_open_sell_qty[key] - add_sell_qty
        return max(abs(worst_long), abs(worst_short))

    def get_strategy_snapshot(self) -> dict:
        strategies = set()
        strategies.update(strategy for strategy, _symbol in self._strategy_net_positions)
        strategies.update(strategy for strategy, _symbol in self._strategy_open_buy_qty)
        strategies.update(strategy for strategy, _symbol in self._strategy_open_sell_qty)
        snapshot = {}
        for strategy_id in sorted(strategies):
            symbols = set()
            symbols.update(
                symbol
                for strategy, symbol in self._strategy_net_positions
                if strategy == strategy_id
            )
            symbols.update(
                symbol
                for strategy, symbol in self._strategy_open_buy_qty
                if strategy == strategy_id
            )
            symbols.update(
                symbol
                for strategy, symbol in self._strategy_open_sell_qty
                if strategy == strategy_id
            )
            snapshot[strategy_id] = {
                symbol: {
                    "position": self._strategy_net_positions[(strategy_id, symbol)],
                    "avg_price": self._strategy_avg_prices[(strategy_id, symbol)],
                    "open_buy_qty": self._strategy_open_buy_qty[(strategy_id, symbol)],
                    "open_sell_qty": self._strategy_open_sell_qty[(strategy_id, symbol)],
                }
                for symbol in sorted(symbols)
            }
        return snapshot

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
        max_concurrent_symbols: int = 0,
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
        mark_price = self._market_cache.get_mark_price(symbol)
        if mark_price <= 0:
            return False, f"MarkPrice unavailable for {symbol}"

        new_buy_qty = volume if side == Side.BUY else 0.0
        new_sell_qty = volume if side == Side.SELL else 0.0

        if max_concurrent_symbols > 0:
            active_symbols = self.get_active_risk_symbols()
            target_is_active = symbol in active_symbols
            target_would_be_active = (
                self._symbol_worst_case_abs_qty(
                    symbol,
                    new_buy_qty,
                    new_sell_qty,
                )
                > 1e-9
            )
            if (
                target_would_be_active
                and not target_is_active
                and len(active_symbols) >= max_concurrent_symbols
            ):
                return False, (
                    "Concurrent Symbol Limit: "
                    f"active={len(active_symbols)}>="
                    f"{max_concurrent_symbols} target={symbol}"
                )

        worst_long = self._symbol_worst_long_qty(symbol, new_buy_qty)
        worst_short = self._symbol_worst_short_qty(symbol, new_sell_qty)
        max_exposure = max(abs(worst_long), abs(worst_short))
        potential_val = max_exposure * mark_price

        if potential_val > max_pos_notional:
            return False, (
                f"Exposure Limit: worst_long={worst_long:.4f} "
                f"worst_short={worst_short:.4f} "
                f"max_val={potential_val:.2f} > {max_pos_notional} "
                f"(Pos={self._net_positions[symbol]:.4f})"
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
        tracked_symbols = set(self._net_positions.keys())
        tracked_symbols.update(self._open_buy_qty.keys())
        tracked_symbols.update(self._open_sell_qty.keys())
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

    def get_active_risk_symbols(self) -> set[str]:
        tracked_symbols = set(self._net_positions)
        tracked_symbols.update(self._open_buy_qty)
        tracked_symbols.update(self._open_sell_qty)
        return {
            symbol
            for symbol in tracked_symbols
            if self._symbol_worst_case_abs_qty(symbol) > 1e-9
        }

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
        current_pos = self._net_positions[symbol]
        return current_pos + self._open_buy_qty[symbol] + add_buy_qty

    def _symbol_worst_short_qty(self, symbol: str, add_sell_qty: float = 0.0) -> float:
        current_pos = self._net_positions[symbol]
        return current_pos - self._open_sell_qty[symbol] - add_sell_qty

    def _get_price_for_risk(self, symbol: str, fallback_price: float = 0.0) -> float:
        mark_price = self._market_cache.get_mark_price(symbol)
        if math.isfinite(mark_price) and mark_price > 0:
            return mark_price

        bid_price, ask_price = self._market_cache.get_best_quote(symbol)
        try:
            bid_price = float(bid_price)
            ask_price = float(ask_price)
        except (TypeError, ValueError):
            bid_price = ask_price = 0.0
        if not math.isfinite(bid_price):
            bid_price = 0.0
        if not math.isfinite(ask_price):
            ask_price = 0.0
        if bid_price > 0 and ask_price > 0:
            return (bid_price + ask_price) / 2.0
        if bid_price > 0:
            return bid_price
        if ask_price > 0:
            return ask_price

        avg_price = abs(float(self._avg_prices[symbol] or 0.0))
        if not math.isfinite(avg_price):
            avg_price = 0.0
        if avg_price > 0:
            return avg_price
        try:
            fallback_price = float(fallback_price)
        except (TypeError, ValueError):
            return 0.0
        return fallback_price if math.isfinite(fallback_price) else 0.0

    # ----------------------------------------------------------
    # ????
    # ----------------------------------------------------------

    def get_position_data(self, symbol: str) -> PositionData:
        return PositionData(
            symbol=symbol,
            volume=self._net_positions[symbol],
            price=self._avg_prices[symbol],
            pnl=0.0,
        )

    def force_sync(self, symbol: str, volume: float, price: float):
        """?????????????????"""
        symbol = str(symbol or "").upper().strip()
        try:
            volume = float(volume)
            price = float(price)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Position volume and price must be numeric"
            ) from exc
        if (
            not symbol
            or not math.isfinite(volume)
            or not math.isfinite(price)
            or price < 0.0
            or (abs(volume) > 1e-9 and price <= 0.0)
        ):
            raise ValueError(
                f"Invalid position truth for {symbol or 'UNKNOWN'}"
            )
        self._net_positions[symbol] = volume
        self._avg_prices[symbol] = price if abs(volume) > 1e-9 else 0.0


# Transitional import compatibility. Runtime composition uses ExposureStore.
ExposureManager = ExposureStore
