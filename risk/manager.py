import time
from collections import defaultdict, deque

from data.cache import data_cache
from event.type import (
    Event,
    OrderRequest,
    EVENT_ACCOUNT_UPDATE,
    EVENT_LOG,
    EVENT_MARK_PRICE,
    EVENT_ORDERBOOK,
    EVENT_ORDER_UPDATE,
)
from infrastructure.logger import logger


class RiskManager:
    def __init__(self, engine, config: dict, oms=None, gateway=None):
        self.engine = engine
        self.oms = oms
        self.gateway = gateway
        self.config = config.get("risk", {})

        self.active = self.config.get("active", True)
        self.kill_switch_triggered = False
        self.kill_reason = ""

        limits = self.config.get("limits", {})
        self.max_order_qty = limits.get("max_order_qty", 1000.0)
        self.max_order_notional = limits.get("max_order_notional", 5000.0)
        self.max_pos_notional = limits.get("max_pos_notional", 20000.0)
        self.max_daily_loss = limits.get("max_daily_loss", 500.0)
        self.max_drawdown_pct = limits.get("max_drawdown_pct", 0.0)

        sanity = self.config.get("price_sanity", {})
        self.max_deviation_pct = sanity.get("max_deviation_pct", 0.05)

        tech = self.config.get("tech_health", {})
        self.max_latency_ms = tech.get("max_latency_ms", 1000)
        self.max_orders_per_sec = tech.get("max_order_count_per_sec", 20)
        self.consecutive_error_limit = max(1, int(tech.get("consecutive_error_limit", 10)))

        black_swan = self.config.get("black_swan", {})
        self.volatility_halt_threshold = black_swan.get("volatility_halt_threshold", 0.05)

        self.order_history = deque()
        self.initial_equity = 0.0
        self.peak_equity = 0.0
        self.latency_breach_count = 0
        self.latency_breach_by_symbol = defaultdict(int)
        self.latency_recovery_by_symbol = defaultdict(int)
        self.divergence_breach_by_symbol = defaultdict(int)
        self.divergence_recovery_by_symbol = defaultdict(int)
        self.frozen_symbols = {}
        self.symbol_freeze_recovery_updates = max(
            1,
            int(tech.get("symbol_freeze_recovery_updates", self.consecutive_error_limit)),
        )
        self.max_frozen_symbols_before_kill = int(tech.get("max_frozen_symbols_before_kill", 0))

        self.engine.register(EVENT_ORDER_UPDATE, self.on_order_update)
        self.engine.register(EVENT_MARK_PRICE, self.on_mark_price)
        self.engine.register(EVENT_ACCOUNT_UPDATE, self.on_account_update)
        self.engine.register(EVENT_ORDERBOOK, self.on_orderbook)

    def check_order(self, req: OrderRequest) -> bool:
        if self.kill_switch_triggered:
            return False
        if not self.active:
            return True

        now = time.time()
        while self.order_history and self.order_history[0] < now - 1.0:
            self.order_history.popleft()
        if len(self.order_history) >= self.max_orders_per_sec:
            self._log_warn("Order rate limit exceeded")
            return False

        if req.volume > self.max_order_qty:
            self._log_warn(f"Order volume {req.volume} > {self.max_order_qty}")
            return False

        notional = req.price * req.volume
        if notional > self.max_order_notional:
            self._log_warn(f"Order notional {notional:.2f} > {self.max_order_notional}")
            return False

        mark_price = data_cache.get_mark_price(req.symbol)
        if mark_price > 0:
            deviation = abs(req.price - mark_price) / mark_price
            if deviation > self.max_deviation_pct:
                self._log_warn(f"Order price deviation {deviation:.2%} > {self.max_deviation_pct:.2%}")
                return False

        if self.oms:
            current_vol = self.oms.exposure.net_positions.get(req.symbol, 0.0)
            new_notional = (abs(current_vol) + req.volume) * req.price
            if new_notional > self.max_pos_notional:
                self._log_warn(f"Projected position {new_notional:.2f} > {self.max_pos_notional}")
                return False
            if not self.oms.account.check_margin(notional):
                return False

        self.order_history.append(now)
        return True

    def on_mark_price(self, event: Event):
        if self.kill_switch_triggered or not self.active:
            return

        data = event.data
        if data.index_price <= 0 or self.volatility_halt_threshold <= 0:
            return

        symbol = getattr(data, "symbol", "").upper()
        divergence = abs(data.mark_price - data.index_price) / data.index_price
        if divergence > self.volatility_halt_threshold:
            if symbol:
                self.divergence_breach_by_symbol[symbol] += 1
                self.divergence_recovery_by_symbol[symbol] = 0
            self._log_warn(
                f"Mark/index divergence {divergence:.2%} > {self.volatility_halt_threshold:.2%} "
                f"({self.divergence_breach_by_symbol[symbol]}/{self.consecutive_error_limit}) {data.symbol}"
            )
            if symbol and self.divergence_breach_by_symbol[symbol] >= self.consecutive_error_limit:
                self._freeze_symbol(
                    symbol,
                    f"divergence:{divergence:.2%}>{self.volatility_halt_threshold:.2%}",
                )
            return

        if symbol:
            self.divergence_breach_by_symbol[symbol] = 0
            self._recover_symbol_if_stable(symbol, prefix="divergence:")

    def on_orderbook(self, event: Event):
        if self.kill_switch_triggered or not self.active:
            return

        orderbook = event.data
        symbol = getattr(orderbook, "symbol", "").upper()
        exchange_ts = float(getattr(orderbook, "exchange_timestamp", 0.0) or 0.0)
        received_ts = float(getattr(orderbook, "received_timestamp", 0.0) or 0.0)
        reference_ts = exchange_ts or received_ts or orderbook.datetime.timestamp()
        latency_ms = max(0.0, (time.time() - reference_ts) * 1000.0)
        if latency_ms > self.max_latency_ms:
            self.latency_breach_count += 1
            if symbol:
                self.latency_breach_by_symbol[symbol] += 1
                self.latency_recovery_by_symbol[symbol] = 0
            self._log_warn(
                f"Market data latency {latency_ms:.1f}ms > {self.max_latency_ms}ms "
                f"({self.latency_breach_count}/{self.consecutive_error_limit})"
            )
            if symbol and self.latency_breach_by_symbol[symbol] >= self.consecutive_error_limit:
                self._freeze_symbol(
                    symbol,
                    f"latency:{latency_ms:.1f}ms>{self.max_latency_ms}ms",
                )
            if self.latency_breach_count >= self.consecutive_error_limit and not symbol:
                self.trigger_kill_switch(
                    f"Market data latency {latency_ms:.1f}ms exceeded {self.max_latency_ms}ms "
                    f"for {self.latency_breach_count} consecutive updates"
                )
        else:
            self.latency_breach_count = 0
            if symbol:
                self.latency_breach_by_symbol[symbol] = 0
                self._recover_symbol_if_stable(symbol, prefix="latency:")

    def on_account_update(self, event: Event):
        if self.kill_switch_triggered or not self.active:
            return

        account = event.data
        if self.initial_equity == 0:
            self.initial_equity = account.equity
        self.peak_equity = max(self.peak_equity, account.equity)

        drawdown = self.initial_equity - account.equity
        if self.max_daily_loss > 0 and drawdown > self.max_daily_loss:
            self.trigger_kill_switch(f"Daily loss limit breached: -{drawdown:.2f}")
            return

        if self.max_drawdown_pct > 0 and self.peak_equity > 0:
            drawdown_pct = max(0.0, (self.peak_equity - account.equity) / self.peak_equity)
            if drawdown_pct > self.max_drawdown_pct:
                self.trigger_kill_switch(
                    f"Drawdown {drawdown_pct:.2%} > {self.max_drawdown_pct:.2%}"
                )

    def on_order_update(self, event: Event):
        return None

    def trigger_kill_switch(self, reason: str):
        if self.kill_switch_triggered:
            return

        self.kill_switch_triggered = True
        self.kill_reason = reason
        logger.critical(f"KILL SWITCH TRIGGERED: {reason}")

        if self.gateway:
            symbols = set()
            if self.oms:
                symbols.update(self.oms.config.get("symbols", []))
                symbols.update(self.oms.exposure.net_positions.keys())
            for symbol in symbols:
                try:
                    self.gateway.cancel_all_orders(symbol)
                except Exception as exc:
                    logger.error(f"[KillSwitch] cancel_all_orders({symbol}) failed: {exc}")

        if self.oms:
            try:
                self.oms.halt_system(f"KillSwitch: {reason}")
            except Exception as exc:
                logger.error(f"[KillSwitch] oms.halt_system failed: {exc}")

    def _freeze_symbol(self, symbol: str, reason: str):
        if not symbol:
            return

        symbol = symbol.upper()
        existing_reason = self.frozen_symbols.get(symbol, "")
        self.frozen_symbols[symbol] = reason
        if existing_reason == reason:
            return

        logger.error(f"[Risk] Symbol circuit breaker {symbol}: {reason}")
        self._log_warn(f"Symbol frozen {symbol}: {reason}")
        if self.oms and hasattr(self.oms, "freeze_symbol"):
            try:
                self.oms.freeze_symbol(symbol, reason, cancel_active_orders=True)
            except Exception as exc:
                logger.error(f"[Risk] oms.freeze_symbol({symbol}) failed: {exc}")
        self._maybe_escalate_symbol_freeze(reason)

    def _recover_symbol_if_stable(self, symbol: str, prefix: str):
        frozen_reason = self.frozen_symbols.get(symbol, "")
        if not frozen_reason.startswith(prefix):
            return

        if prefix == "latency:":
            self.latency_recovery_by_symbol[symbol] += 1
            stable_updates = self.latency_recovery_by_symbol[symbol]
        else:
            self.divergence_recovery_by_symbol[symbol] += 1
            stable_updates = self.divergence_recovery_by_symbol[symbol]

        if stable_updates < self.symbol_freeze_recovery_updates:
            return

        self.frozen_symbols.pop(symbol, None)
        logger.info(f"[Risk] Symbol circuit breaker cleared {symbol}: {prefix}recovered")
        self._log_warn(f"Symbol restored {symbol}: {prefix}recovered")
        if self.oms and hasattr(self.oms, "clear_symbol_freeze"):
            try:
                self.oms.clear_symbol_freeze(
                    symbol,
                    reason=f"{prefix.rstrip(':')} recovered after {stable_updates} healthy updates",
                )
            except Exception as exc:
                logger.error(f"[Risk] oms.clear_symbol_freeze({symbol}) failed: {exc}")

        self.latency_recovery_by_symbol[symbol] = 0
        self.divergence_recovery_by_symbol[symbol] = 0

    def _tracked_symbols(self):
        symbols = set(self.frozen_symbols.keys())
        if self.oms:
            symbols.update(self.oms.config.get("symbols", []))
            symbols.update(getattr(self.oms.exposure, "net_positions", {}).keys())
        return {symbol for symbol in symbols if symbol}

    def _maybe_escalate_symbol_freeze(self, trigger_reason: str):
        tracked_symbols = self._tracked_symbols()
        if not tracked_symbols:
            return

        threshold = self.max_frozen_symbols_before_kill
        if threshold <= 0:
            threshold = len(tracked_symbols) if len(tracked_symbols) > 1 else 0
        if threshold <= 0:
            return

        frozen_count = len({symbol for symbol in tracked_symbols if symbol in self.frozen_symbols})
        if frozen_count >= threshold:
            self.trigger_kill_switch(
                f"Symbol circuit breakers exhausted ({frozen_count}/{len(tracked_symbols)}): {trigger_reason}"
            )

    def _log_warn(self, msg: str):
        self.engine.put(Event(EVENT_LOG, f"[Risk] {msg}"))
