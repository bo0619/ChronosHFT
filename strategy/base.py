import math

from event.type import (
    AccountData,
    Event,
    LifecycleState,
    OrderBook,
    OrderIntent,
    OrderStateSnapshot,
    OrderStatus,
    PositionData,
    Side,
    TIF_GTX,
    TIF_RPI,
    TradeData,
    EVENT_LOG,
)
from data.ref_data import ref_data_manager
from infrastructure.commission_truth import resolve_passive_fee_rate
from infrastructure.paper_trade import is_paper_trade


class StrategyTemplate:
    """
    Base strategy that only talks to OMS.
    """

    def __init__(self, engine, oms, name="Strategy"):
        self.engine = engine
        self.oms = oms
        self.name = name

        self.pos = 0.0
        self.active_orders = {}
        self.orders_cancelling = set()
        self.latest_account = None
        self.last_system_health = ""
        self.last_submit_reject_reason = ""
        self.last_submit_reject_oid = ""
        self.last_submit_reject_by_symbol = {}
        self._rpi_fallback_warned_routes = set()
        self.lot_multiplier = 1.0
        self.target_order_notional = 0.0
        self.max_pos_usdt = 0.0
        self.fixed_order_quantity = 0.0

    @staticmethod
    def _positive_finite(value, default: float = 0.0) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return float(default)
        if not math.isfinite(parsed) or parsed <= 0.0:
            return float(default)
        return parsed

    @staticmethod
    def _nonnegative_finite(value, default: float = 0.0) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return float(default)
        if not math.isfinite(parsed) or parsed < 0.0:
            return float(default)
        return parsed

    def configure_quote_sizing(self, strategy_config: dict | None):
        """Load quote sizing while keeping legacy lot_multiplier configs valid."""
        config = strategy_config if isinstance(strategy_config, dict) else {}
        order_sizing = config.get("order_sizing", {})
        if not isinstance(order_sizing, dict):
            raise ValueError("strategy.order_sizing must be an object")
        sizing_mode = str(
            order_sizing.get("mode", "notional") or "notional"
        ).lower()
        if sizing_mode not in {"notional", "fixed_quantity"}:
            raise ValueError(
                "strategy.order_sizing.mode must be notional or fixed_quantity"
            )
        if sizing_mode == "fixed_quantity":
            if not is_paper_trade(getattr(self.oms, "config", {}) or {}):
                raise ValueError("fixed_quantity order sizing is Paper-only")
            self.fixed_order_quantity = self._positive_finite(
                order_sizing.get("fixed_quantity", 0.0)
            )
            if self.fixed_order_quantity <= 0.0:
                raise ValueError(
                    "strategy.order_sizing.fixed_quantity must be positive and finite"
                )
        else:
            self.fixed_order_quantity = 0.0
        self.lot_multiplier = self._positive_finite(
            config.get("lot_multiplier", 1.0),
            1.0,
        )
        scaling = config.get("capital_scaling", {})
        if not isinstance(scaling, dict):
            scaling = {}
        scaled_notional_fallback = (
            self._positive_finite(scaling.get("target_order_notional", 0.0))
            * self._positive_finite(config.get("capital_multiplier", 1.0), 1.0)
        )
        self.target_order_notional = self._positive_finite(
            config.get(
                "target_order_notional",
                config.get(
                    "order_notional_usdt",
                    config.get("order_notional", scaled_notional_fallback),
                ),
            )
        )
        self.max_pos_usdt = self._positive_finite(
            config.get("max_pos_usdt", 0.0)
        )

    def calculate_quote_volume(
        self,
        symbol: str,
        price: float,
        *,
        side: Side | None = None,
        current_position: float = 0.0,
        reference_price: float | None = None,
    ) -> float:
        """Return a step-rounded quote size within order and inventory notionals."""
        info = ref_data_manager.get_info(symbol)
        if info is None:
            return 0.0

        try:
            price = float(price)
            reference_price = float(
                price if reference_price is None else reference_price
            )
            current_position = float(current_position)
        except (TypeError, ValueError):
            return 0.0
        if (
            not math.isfinite(price)
            or not math.isfinite(reference_price)
            or not math.isfinite(current_position)
            or price <= 0.0
            or reference_price <= 0.0
        ):
            return 0.0

        min_notional = max(5.0, float(info.min_notional or 0.0))
        fixed_quantity = self.fixed_order_quantity > 0.0
        explicit_notional = self.target_order_notional > 0.0
        if fixed_quantity:
            target_qty = self.fixed_order_quantity
        else:
            if explicit_notional:
                target_notional = self.target_order_notional
            else:
                target_notional = min_notional * 1.1 * self.lot_multiplier
            if self.max_pos_usdt > 0.0:
                target_notional = min(target_notional, self.max_pos_usdt)
            target_qty = target_notional / price
        min_qty = max(0.0, float(info.min_qty or 0.0))
        if min_qty > target_qty:
            if fixed_quantity or explicit_notional or self.max_pos_usdt > 0.0:
                return 0.0
            target_qty = min_qty

        if self.max_pos_usdt > 0.0 and side in {Side.BUY, Side.SELL}:
            current_notional = current_position * reference_price
            if side == Side.BUY:
                remaining_notional = self.max_pos_usdt - current_notional
            else:
                remaining_notional = self.max_pos_usdt + current_notional
            capacity_qty = max(0.0, remaining_notional) / reference_price
            if fixed_quantity and capacity_qty + 1e-12 < target_qty:
                return 0.0
            target_qty = min(target_qty, capacity_qty)

        rounded_qty = ref_data_manager.round_qty(symbol, target_qty)
        if fixed_quantity and not math.isclose(
            rounded_qty,
            target_qty,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            return 0.0
        if rounded_qty < min_qty or rounded_qty <= 0.0:
            return 0.0
        if rounded_qty * price + 1e-9 < min_notional:
            return 0.0
        return rounded_qty

    def on_orderbook(self, orderbook: OrderBook):
        raise NotImplementedError

    def on_trade(self, trade: TradeData):
        pass

    def on_order(self, snapshot: OrderStateSnapshot):
        terminal_statuses = {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.REJECTED_LOCALLY,
            OrderStatus.EXPIRED,
        }
        if snapshot.status in {OrderStatus.REJECTED, OrderStatus.REJECTED_LOCALLY}:
            reason = snapshot.error_msg or snapshot.status.value.lower()
            self.last_submit_reject_reason = reason
            self.last_submit_reject_oid = snapshot.client_oid
            self.last_submit_reject_by_symbol[snapshot.symbol] = reason
        if snapshot.status in terminal_statuses:
            self.active_orders.pop(snapshot.client_oid, None)
            self.orders_cancelling.discard(snapshot.client_oid)

    def on_position(self, pos: PositionData):
        self.pos = pos.volume

    def on_account_update(self, account: AccountData):
        self.latest_account = account

    def on_system_health(self, message):
        if not isinstance(message, str):
            message = str(message)
        self.last_system_health = message

    def on_submit_rejected(self, intent: OrderIntent, reason: str, client_oid: str = ""):
        self.last_submit_reject_reason = reason
        self.last_submit_reject_oid = client_oid or ""
        self.last_submit_reject_by_symbol[intent.symbol] = reason

    def can_submit_orders(self, symbol: str = "") -> bool:
        if hasattr(self.oms, "can_submit_for_strategy"):
            return bool(self.oms.can_submit_for_strategy(self.name, symbol))
        if symbol and hasattr(self.oms, "is_symbol_tradeable"):
            return bool(self.oms.is_symbol_tradeable(symbol))
        return getattr(self.oms, "state", None) == LifecycleState.LIVE

    def log(self, msg):
        self.engine.put(Event(EVENT_LOG, f"[{self.name}] {msg}"))

    def resolve_passive_time_in_force(
        self,
        symbol: str,
        *,
        use_rpi: bool,
        fallback_to_gtx: bool = True,
        route: str = "passive_quote",
    ) -> str:
        """Resolve a passive quote to RPI or GTX using live exchangeInfo data."""
        if not use_rpi:
            return TIF_GTX
        if ref_data_manager.supports_rpi(symbol):
            return TIF_RPI
        if not fallback_to_gtx:
            return TIF_RPI

        symbol = str(symbol or "").upper()
        warning_key = (symbol, str(route or "passive_quote"))
        if warning_key not in self._rpi_fallback_warned_routes:
            self._rpi_fallback_warned_routes.add(warning_key)
            self.log(
                f"{symbol} has no RPI permission in exchangeInfo; "
                f"{warning_key[1]} falls back to GTX"
            )
        return TIF_GTX

    def passive_fee_rate(self, symbol: str, time_in_force: str) -> float:
        """Return the final passive fee rate for the selected venue route."""
        config = getattr(self.oms, "config", {}) or {}
        fee_config = dict(config.get("backtest", {}) or {})
        paper_config = config.get("paper_trade", {}) or {}
        if is_paper_trade(config):
            fee_config.update(paper_config)
        return resolve_passive_fee_rate(
            maker_rate=fee_config.get("maker_fee", 0.0),
            symbol=symbol,
            is_rpi=str(time_in_force or "").upper() == TIF_RPI,
            rpi_commission_rates=fee_config.get("rpi_commission_rates", {}),
            default_rpi_commission_rate=fee_config.get(
                "rpi_commission_rate",
                0.0,
            ),
        )

    def passive_round_trip_fee_bps(
        self,
        symbol: str,
        time_in_force: str,
    ) -> float:
        return max(0.0, self.passive_fee_rate(symbol, time_in_force) * 20000.0)

    def send_intent(self, intent: OrderIntent):
        intent.price = ref_data_manager.round_price(intent.symbol, intent.price)
        intent.volume = ref_data_manager.round_qty(intent.symbol, intent.volume)

        if hasattr(self.oms, "adapt_intent_for_trading_mode"):
            adapted_intent, reject_reason = self.oms.adapt_intent_for_trading_mode(intent)
            if reject_reason:
                self.on_submit_rejected(intent, reject_reason)
                return None
            intent = adapted_intent

        info = ref_data_manager.get_info(intent.symbol)
        if info:
            notional = intent.price * intent.volume
            min_notional = max(info.min_notional, 5.0)
            if notional < min_notional:
                self.on_submit_rejected(intent, "min_notional")
                return None

        submit_result = self.oms.submit_order(intent)
        if isinstance(submit_result, str):
            client_oid = submit_result
            if client_oid:
                self.active_orders[client_oid] = intent
            return client_oid

        client_oid = getattr(submit_result, "client_oid", "") if submit_result else ""
        accepted = bool(getattr(submit_result, "accepted", False)) if submit_result else False
        if accepted and client_oid:
            self.active_orders[client_oid] = intent
            return client_oid

        reject_reason = getattr(submit_result, "reason", "submit_rejected") if submit_result else "submit_rejected"
        self.on_submit_rejected(intent, reject_reason, client_oid)
        return None

    def entry_long(self, symbol, price, volume):
        intent = OrderIntent(self.name, symbol, Side.BUY, price, volume)
        return self.send_intent(intent)

    def exit_long(self, symbol, price, volume):
        intent = OrderIntent(self.name, symbol, Side.SELL, price, volume, reduce_only=True)
        return self.send_intent(intent)

    def entry_short(self, symbol, price, volume):
        intent = OrderIntent(self.name, symbol, Side.SELL, price, volume)
        return self.send_intent(intent)

    def exit_short(self, symbol, price, volume):
        intent = OrderIntent(self.name, symbol, Side.BUY, price, volume, reduce_only=True)
        return self.send_intent(intent)

    def buy(self, symbol, price, volume):
        return self.entry_long(symbol, price, volume)

    def sell(self, symbol, price, volume):
        return self.entry_short(symbol, price, volume)

    def cancel_order(self, client_oid: str):
        if client_oid not in self.active_orders:
            return False
        if client_oid in self.orders_cancelling:
            return False

        self.orders_cancelling.add(client_oid)
        try:
            accepted = bool(self.oms.cancel_order(client_oid))
        except BaseException:
            self.orders_cancelling.discard(client_oid)
            raise
        if not accepted:
            self.orders_cancelling.discard(client_oid)
        return accepted

    def cancel_all(self, symbol: str):
        to_cancel = {
            oid for oid, intent in self.active_orders.items() if intent.symbol == symbol
        }
        newly_cancelling = to_cancel.difference(self.orders_cancelling)
        self.orders_cancelling.update(newly_cancelling)
        try:
            accepted = bool(self.oms.cancel_all_orders(symbol))
        except BaseException:
            self.orders_cancelling.difference_update(newly_cancelling)
            raise
        if not accepted:
            self.orders_cancelling.difference_update(newly_cancelling)
        return accepted
