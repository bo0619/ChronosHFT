import threading
import time
import uuid
from collections import deque
from datetime import datetime

from data.cache import data_cache
from data.ref_data import ref_data_manager
from infrastructure.logger import logger

from event.type import (
    CancelRequest,
    ExecutionPolicy,
    Event,
    ExchangeAccountUpdate,
    ExchangeOrderUpdate,
    LifecycleState,
    OMSCapabilityMode,
    OrderIntent,
    OrderRequest,
    OrderStatus,
    OrderSubmitResult,
    OrderSubmitted,
    Side,
    TradeData,
    EVENT_ORDER_SUBMITTED,
    EVENT_ORDER_UPDATE,
    EVENT_POSITION_UPDATE,
    EVENT_SYSTEM_HEALTH,
    EVENT_TRADE_UPDATE,
    TIF_GTX,
    TIF_IOC,
)

from .account_manager import AccountManager
from .exposure import ExposureManager
from .journal import OMSJournal
from .order import Order, TERMINAL_STATUSES
from .order_manager import OrderManager
from .sequence import SequenceValidator
from .validator import OrderValidator


class OMS:
    """Deterministic OMS for a single Binance perpetual account."""

    def __init__(self, event_engine, gateway, config):
        self.event_engine = event_engine
        self.gateway = gateway
        self.config = config

        self.state = LifecycleState.BOOTSTRAP

        self.event_log = []
        self.orders = {}
        self.exchange_id_map = {}
        self.symbol_guards = {}
        self.venue_guards = {}
        self.strategy_guards = {}
        self.strategy_symbol_guards = {}
        self.lock = threading.RLock()
        self.capability_mode = OMSCapabilityMode.READ_ONLY
        self.capability_reason = "startup_bootstrap"
        self.mode_override = None
        self.mode_override_reason = ""

        target_leverage = int(config.get("account", {}).get("leverage", 0) or 0)
        if target_leverage > 0:
            self.gateway.target_leverage = target_leverage
        target_margin_type = str(
            config.get("account", {}).get("margin_type", "CROSSED") or "CROSSED"
        ).upper()
        self.gateway.target_margin_type = target_margin_type
        target_position_mode = str(
            config.get("account", {}).get("position_mode", "ONE_WAY") or "ONE_WAY"
        ).upper()
        self.gateway.target_position_mode = target_position_mode

        self.max_pos_notional = (
            config.get("risk", {})
            .get("limits", {})
            .get("max_pos_notional", 2000.0)
        )
        self.max_account_gross_notional = (
            config.get("risk", {})
            .get("limits", {})
            .get("max_account_gross_notional", 0.0)
        )

        self.sequence = SequenceValidator()
        self.validator = OrderValidator(config)
        self.exposure = ExposureManager()
        self.account = AccountManager(event_engine, self.exposure, config)
        self.order_monitor = OrderManager(
            event_engine,
            gateway,
            self.trigger_reconcile,
            config.get("oms", {}),
        )

        self.journal = OMSJournal(config)
        self.TOMBSTONE_MAX = config.get("oms", {}).get("tombstone_max", 2000)
        self.terminated_oids = set()
        self.terminated_oid_queue = deque()
        self.reconcile_retry_scheduled = False
        self.manual_rearm_required = False
        self.last_freeze_reason = ""
        self.last_halt_reason = ""
        self.rebuild_summary = self.rebuild_from_log()
        self._apply_rebuild_summary()

        oms_cfg = config.get("oms", {})
        self.reconcile_min_interval_sec = float(oms_cfg.get("reconcile_min_interval_sec", 5.0))
        self.reconcile_api_failure_threshold = int(oms_cfg.get("reconcile_api_failure_threshold", 3))
        self.reconcile_api_cooldown_sec = float(oms_cfg.get("reconcile_api_cooldown_sec", 10.0))
        self.max_total_active_orders = int(oms_cfg.get("max_total_active_orders", 100) or 0)
        self.max_symbol_active_orders = int(oms_cfg.get("max_symbol_active_orders", 20) or 0)
        self.max_strategy_active_orders = int(oms_cfg.get("max_strategy_active_orders", 30) or 0)
        self.max_strategy_symbol_active_orders = int(
            oms_cfg.get("max_strategy_symbol_active_orders", 10) or 0
        )
        self.duplicate_intent_window_sec = max(
            0.0,
            float(oms_cfg.get("duplicate_intent_window_ms", 250.0) or 0.0) / 1000.0,
        )
        self.degraded_aggressive_to_passive = bool(
            oms_cfg.get("degraded_aggressive_to_passive", True)
        )
        self.emergency_flatten_cooldown_sec = max(
            0.0,
            float(oms_cfg.get("emergency_flatten_cooldown_sec", 5.0) or 0.0),
        )
        self.last_emergency_flatten_ts = {}
        self.last_reconcile_request_ts = 0.0
        self.last_reconcile_failure_ts = 0.0
        self.consecutive_reconcile_api_failures = 0

    def bootstrap(self):
        logger.info("OMS: Bootstrapping state...")
        self._audit("bootstrap_requested", recovered=self.rebuild_summary)
        if self.manual_rearm_required or self.state == LifecycleState.HALTED:
            self._sync_capability_mode("manual_rearm_required")
            self._refresh_read_only_account_snapshot()
            logger.error("[OMS] Bootstrap blocked: manual rearm required after recovered HALT")
            self._audit(
                "bootstrap_blocked",
                reason="manual_rearm_required",
                recovered=self.rebuild_summary,
            )
            return False

        if self.state == LifecycleState.FROZEN or self._has_active_guards():
            logger.warning("[OMS] Bootstrapping into guarded reconcile mode")
            self.state = LifecycleState.FROZEN
            self._sync_capability_mode("bootstrap_guarded")
            if not self.last_freeze_reason:
                self.last_freeze_reason = "Recovered guarded state"
            self._audit(
                "bootstrap_guarded",
                reason=self.last_freeze_reason,
                recovered=self.rebuild_summary,
            )
            self.trigger_reconcile("Recovered guarded state")
            return True

        self._perform_full_reset()
        return True

    def _refresh_read_only_account_snapshot(self):
        if not self.can_query_exchange():
            return False

        try:
            account = self.gateway.get_account_info()
        except Exception as exc:
            logger.warning(f"[OMS] Read-only account sync failed: {exc}")
            return False

        if not isinstance(account, dict) or not account:
            return False

        balances = {}
        for entry in account.get("assets", []) or []:
            asset = str(entry.get("asset", "") or "").upper()
            if not asset:
                continue
            available_balance = entry.get("availableBalance")
            balances[asset] = {
                "wallet_balance": float(entry.get("walletBalance", 0.0) or 0.0),
                "available_balance": (
                    float(available_balance or 0.0)
                    if available_balance is not None
                    else None
                ),
            }

        available_balance = account.get("availableBalance")
        self.account.force_sync(
            float(account.get("totalWalletBalance", self.account.balance) or self.account.balance),
            float(account.get("totalInitialMargin", self.account.used_margin) or 0.0),
            float(available_balance) if available_balance is not None else None,
            balances=balances or None,
        )
        self._audit(
            "read_only_account_sync",
            balance=self.account.balance,
            available=self.account.available,
            budget_available=self.account.budget_available,
            assets=sorted(balances.keys()),
        )
        return True

    def _apply_rebuild_summary(self):
        summary = self.rebuild_summary or {}
        self.symbol_guards = dict(summary.get("symbol_guards", {}))
        self.venue_guards = dict(summary.get("venue_guards", {}))
        self.strategy_guards = dict(summary.get("strategy_guards", {}))
        self.strategy_symbol_guards = {
            tuple(key.split("|", 1)): value
            for key, value in summary.get("strategy_symbol_guards", {}).items()
            if "|" in key
        }
        override_mode = str(summary.get("mode_override", "") or "")
        self.mode_override = OMSCapabilityMode(override_mode) if override_mode else None
        self.mode_override_reason = str(summary.get("mode_override_reason", "") or "")

        self.last_freeze_reason = str(summary.get("last_freeze_reason", "") or "")
        self.last_halt_reason = str(summary.get("last_halt_reason", "") or "")
        self.manual_rearm_required = bool(summary.get("manual_rearm_required", False))

        last_lifecycle = summary.get("last_lifecycle")
        dirty_shutdown = bool(summary.get("dirty_shutdown", False))
        if self.manual_rearm_required or last_lifecycle == LifecycleState.HALTED.value:
            self.state = LifecycleState.HALTED
            self.manual_rearm_required = True
            if not self.last_halt_reason:
                self.last_halt_reason = "Recovered halted state"
            self._sync_capability_mode("recovered_halted_state")
            return

        if dirty_shutdown:
            self.state = LifecycleState.FROZEN
            if not self.last_freeze_reason:
                self.last_freeze_reason = "Recovered unclean shutdown"
            self._sync_capability_mode("recovered_unclean_shutdown")
            return

        if self._has_active_guards() or last_lifecycle in {
            LifecycleState.FROZEN.value,
            LifecycleState.RECONCILING.value,
        }:
            self.state = LifecycleState.FROZEN
            if not self.last_freeze_reason:
                self.last_freeze_reason = "Recovered guarded state"
            self._sync_capability_mode("recovered_guarded_state")
            return

        self.state = LifecycleState.BOOTSTRAP
        self._sync_capability_mode("bootstrap")

    def _has_active_guards(self):
        return bool(
            self.symbol_guards
            or self.venue_guards
            or self.strategy_guards
            or self.strategy_symbol_guards
        )

    def _sync_capability_mode(self, reason: str = ""):
        previous_mode = getattr(self, "capability_mode", None)
        previous_reason = getattr(self, "capability_reason", "")
        base_mode = self._capability_mode_for_state()
        override_mode = self.mode_override
        next_mode = base_mode
        next_reason = reason or self.state.value.lower()
        if override_mode and self._mode_rank(override_mode) > self._mode_rank(base_mode):
            next_mode = override_mode
            next_reason = self.mode_override_reason or next_reason

        changed = previous_mode != next_mode or previous_reason != next_reason
        self.capability_mode = next_mode
        self.capability_reason = next_reason
        if changed:
            self._audit(
                "capability_mode_changed",
                mode=next_mode.value,
                reason=next_reason,
                previous_mode=previous_mode.value if previous_mode else "",
                previous_reason=previous_reason,
            )

    def _mode_rank(self, mode: OMSCapabilityMode) -> int:
        ranks = {
            OMSCapabilityMode.LIVE: 0,
            OMSCapabilityMode.DEGRADED: 1,
            OMSCapabilityMode.PASSIVE_ONLY: 2,
            OMSCapabilityMode.CANCEL_ONLY: 3,
            OMSCapabilityMode.READ_ONLY: 4,
            OMSCapabilityMode.LOCKDOWN: 5,
        }
        return ranks.get(mode, 99)

    def _capability_mode_for_state(self) -> OMSCapabilityMode:
        if self.state == LifecycleState.LIVE:
            return OMSCapabilityMode.LIVE
        if self.state in {LifecycleState.BOOTSTRAP, LifecycleState.RECONCILING}:
            return OMSCapabilityMode.READ_ONLY
        if self.state in {LifecycleState.FROZEN, LifecycleState.HALTED}:
            return OMSCapabilityMode.CANCEL_ONLY
        return OMSCapabilityMode.LOCKDOWN

    def _ensure_capability_mode_consistent(self):
        expected_mode = self._capability_mode_for_state()
        if self.mode_override and self._mode_rank(self.mode_override) > self._mode_rank(expected_mode):
            expected_mode = self.mode_override
        if self.capability_mode != expected_mode:
            self._sync_capability_mode(f"state_sync:{self.state.value}")

    def set_trading_mode(self, mode, reason: str):
        if isinstance(mode, str):
            mode = OMSCapabilityMode(mode)
        if mode not in {OMSCapabilityMode.DEGRADED, OMSCapabilityMode.PASSIVE_ONLY}:
            raise ValueError(f"Unsupported trading mode override: {mode}")
        if self.mode_override == mode and self.mode_override_reason == reason:
            return

        previous_mode = self.mode_override.value if self.mode_override else ""
        previous_reason = self.mode_override_reason
        self.mode_override = mode
        self.mode_override_reason = reason
        self._sync_capability_mode(reason)
        self._audit(
            "trading_mode_override_set",
            mode=mode.value,
            reason=reason,
            previous_mode=previous_mode,
            previous_reason=previous_reason,
        )

    def clear_trading_mode(self, reason: str = "", prefixes=()):
        if not self.mode_override:
            return False
        if prefixes and not any(self.mode_override_reason.startswith(prefix) for prefix in prefixes):
            return False

        previous_mode = self.mode_override.value
        previous_reason = self.mode_override_reason
        self.mode_override = None
        self.mode_override_reason = ""
        self._sync_capability_mode(reason or "trading_mode_cleared")
        self._audit(
            "trading_mode_override_cleared",
            reason=reason or previous_reason,
            previous_mode=previous_mode,
            previous_reason=previous_reason,
        )
        return True

    def can_query_exchange(self) -> bool:
        self._ensure_capability_mode_consistent()
        return self.capability_mode != OMSCapabilityMode.LOCKDOWN

    def can_cancel_orders(self) -> bool:
        self._ensure_capability_mode_consistent()
        return self.capability_mode in {
            OMSCapabilityMode.LIVE,
            OMSCapabilityMode.DEGRADED,
            OMSCapabilityMode.PASSIVE_ONLY,
            OMSCapabilityMode.CANCEL_ONLY,
        }

    def can_open_new_risk(self) -> bool:
        self._ensure_capability_mode_consistent()
        return self.capability_mode in {
            OMSCapabilityMode.LIVE,
            OMSCapabilityMode.DEGRADED,
            OMSCapabilityMode.PASSIVE_ONLY,
        }

    def get_capability_snapshot(self) -> dict:
        return {
            "mode": self.capability_mode.value,
            "reason": self.capability_reason,
            "override_mode": self.mode_override.value if self.mode_override else "",
            "override_reason": self.mode_override_reason,
            "can_query": self.can_query_exchange(),
            "can_cancel": self.can_cancel_orders(),
            "can_open_risk": self.can_open_new_risk(),
        }

    def _get_capability_block_reason(self, action: str) -> str:
        return (
            f"{action}_blocked:"
            f"{self.capability_mode.value}:{self.capability_reason or self.state.value}"
        )

    def query_account_info(self):
        if not self.can_query_exchange():
            self._audit("query_rejected", query="account", reason=self._get_capability_block_reason("query"))
            return None
        return self.gateway.get_account_info()

    def query_positions(self):
        if not self.can_query_exchange():
            self._audit("query_rejected", query="positions", reason=self._get_capability_block_reason("query"))
            return None
        return self.gateway.get_all_positions()

    def query_open_orders(self):
        if not self.can_query_exchange():
            self._audit("query_rejected", query="open_orders", reason=self._get_capability_block_reason("query"))
            return None
        return self.gateway.get_open_orders()

    def adapt_intent_for_trading_mode(self, intent: OrderIntent):
        self._ensure_capability_mode_consistent()
        if self.capability_mode == OMSCapabilityMode.PASSIVE_ONLY:
            if not intent.is_post_only:
                return None, "oms_mode_passive_only"
            return intent, ""

        if self.capability_mode == OMSCapabilityMode.DEGRADED and self.degraded_aggressive_to_passive:
            if not intent.is_post_only:
                adapted = OrderIntent(
                    strategy_id=intent.strategy_id,
                    symbol=intent.symbol,
                    side=intent.side,
                    price=intent.price,
                    volume=intent.volume,
                    order_type="LIMIT",
                    time_in_force=TIF_GTX,
                    is_post_only=True,
                    policy=ExecutionPolicy.PASSIVE,
                    tag=f"{intent.tag}|degraded" if intent.tag else "degraded",
                )
                self._audit(
                    "intent_degraded_to_passive",
                    strategy_id=intent.strategy_id,
                    symbol=intent.symbol,
                    side=intent.side.value,
                    original_order_type=intent.order_type,
                    original_tif=intent.time_in_force,
                )
                return adapted, ""
        return intent, ""

    def _estimate_emergency_price(self, symbol: str, side: Side) -> float:
        bid, ask = data_cache.get_best_quote(symbol)
        if side == Side.BUY and ask > 0:
            return ask
        if side == Side.SELL and bid > 0:
            return bid

        mark_price = data_cache.get_mark_price(symbol)
        if mark_price > 0:
            return mark_price

        last_trade = data_cache.get_last_trade_price(symbol)
        if last_trade > 0:
            return last_trade

        local_pos_price = abs(float(self.exposure.avg_prices.get(symbol, 0.0) or 0.0))
        return local_pos_price if local_pos_price > 0 else 1.0

    def _submit_internal_order(
        self,
        intent: OrderIntent,
        request: OrderRequest,
        client_oid: str,
        snapshot_source: str,
        audit_kind: str,
        **audit_extra,
    ) -> bool:
        order = Order(client_oid, intent)

        with self.lock:
            self.orders[client_oid] = order
            order.mark_submitting()
            self.exposure.update_open_orders(self.orders)
            self.account.calculate()
            self._record_order_snapshot(order, snapshot_source, **audit_extra)

        exchange_oid = self.gateway.send_order(request, client_oid)
        if exchange_oid:
            with self.lock:
                order.mark_pending_ack(exchange_oid)
                self.exchange_id_map[exchange_oid] = order
                self._record_order_snapshot(order, f"{snapshot_source}_ack", **audit_extra)
                self._emit_order_update(order)

            event_data = OrderSubmitted(request, client_oid, time.time())
            self.event_engine.put(Event(EVENT_ORDER_SUBMITTED, event_data))
            payload = {
                "client_oid": client_oid,
                "exchange_oid": exchange_oid,
                "symbol": intent.symbol,
                "side": intent.side.value,
                "price": intent.price,
                "volume": intent.volume,
            }
            payload.update(audit_extra)
            self._audit(audit_kind, **payload)
            return True

        with self.lock:
            order.mark_rejected_locally("gateway_send_failed")
            self._record_order_snapshot(order, f"{snapshot_source}_failed", **audit_extra)
            self._emit_order_update(order)
            self.orders.pop(client_oid, None)
            self.exposure.update_open_orders(self.orders)
            self.account.calculate()
        self._write_tombstone(order)
        payload = {
            "client_oid": client_oid,
            "symbol": intent.symbol,
            "reason": "gateway_send_failed",
        }
        payload.update(audit_extra)
        self._audit(f"{audit_kind}_failed", **payload)
        return False

    def emergency_reduce_only_flatten(self, reason: str, symbol: str = "") -> int:
        target_symbols = {symbol.upper()} if symbol else set()
        remote_positions = self.query_positions()
        positions = {}

        if remote_positions:
            for payload in remote_positions:
                remote_symbol = str(payload.get("symbol", "") or "").upper()
                if not remote_symbol:
                    continue
                if target_symbols and remote_symbol not in target_symbols:
                    continue
                positions[remote_symbol] = float(payload.get("positionAmt", 0.0) or 0.0)

        if not positions:
            with self.lock:
                for local_symbol, volume in self.exposure.net_positions.items():
                    local_symbol = local_symbol.upper()
                    if target_symbols and local_symbol not in target_symbols:
                        continue
                    if abs(volume) > 1e-9:
                        positions[local_symbol] = volume

        submitted = 0
        now = time.time()
        self._audit("emergency_flatten_requested", reason=reason, symbols=sorted(positions.keys()))
        for target_symbol, volume in positions.items():
            if abs(volume) <= 1e-9:
                continue

            last_sent = self.last_emergency_flatten_ts.get(target_symbol, 0.0)
            if now - last_sent < self.emergency_flatten_cooldown_sec:
                self._audit(
                    "emergency_flatten_suppressed",
                    reason=reason,
                    symbol=target_symbol,
                    cooldown_sec=self.emergency_flatten_cooldown_sec,
                )
                continue

            qty = ref_data_manager.round_qty(target_symbol, abs(volume))
            if qty <= 0:
                continue

            side = Side.SELL if volume > 0 else Side.BUY
            estimate_price = self._estimate_emergency_price(target_symbol, side)
            client_oid = f"EMERGENCY_{target_symbol}_{uuid.uuid4().hex[:16]}"
            intent = OrderIntent(
                "system_emergency",
                target_symbol,
                side,
                estimate_price,
                qty,
                order_type="MARKET",
                time_in_force=TIF_IOC,
                is_post_only=False,
                policy=ExecutionPolicy.AGGRESSIVE,
                tag=f"reduce_only_flatten:{reason}",
            )
            request = OrderRequest(
                symbol=target_symbol,
                price=estimate_price,
                volume=qty,
                side=side.value,
                order_type="MARKET",
                time_in_force=TIF_IOC,
                post_only=False,
                reduce_only=True,
            )
            if self._submit_internal_order(
                intent,
                request,
                client_oid,
                "emergency_flatten",
                "emergency_flatten_submitted",
                reason=reason,
                reduce_only=True,
            ):
                self.last_emergency_flatten_ts[target_symbol] = now
                submitted += 1

        return submitted

    def freeze_symbol(self, symbol: str, reason: str, cancel_active_orders: bool = True):
        if not symbol:
            return

        symbol = symbol.upper()
        previous_reason = self.symbol_guards.get(symbol, "")
        self.symbol_guards[symbol] = reason

        if previous_reason != reason:
            logger.error(f"[OMS] Symbol frozen {symbol}: {reason}")
            self._audit(
                "symbol_frozen",
                symbol=symbol,
                reason=reason,
                previous_reason=previous_reason,
            )
        else:
            self._audit("symbol_freeze_reasserted", symbol=symbol, reason=reason)

        if cancel_active_orders:
            self.cancel_all_orders(symbol)

    def clear_symbol_freeze(self, symbol: str, reason: str = ""):
        if not symbol:
            return False

        symbol = symbol.upper()
        previous_reason = self.symbol_guards.pop(symbol, "")
        if not previous_reason:
            return False

        logger.info(f"[OMS] Symbol restored {symbol}: {reason or previous_reason}")
        self._audit(
            "symbol_unfrozen",
            symbol=symbol,
            reason=reason or previous_reason,
            previous_reason=previous_reason,
        )
        return True

    def get_symbol_freeze_reason(self, symbol: str) -> str:
        if not symbol:
            return ""
        return self.symbol_guards.get(symbol.upper(), "")

    def freeze_venue(self, venue: str, reason: str, cancel_active_orders: bool = True):
        venue = (venue or getattr(self.gateway, "gateway_name", "UNKNOWN")).upper()
        previous_reason = self.venue_guards.get(venue, "")
        self.venue_guards[venue] = reason

        if previous_reason != reason:
            logger.error(f"[OMS] Venue frozen {venue}: {reason}")
            self._audit(
                "venue_frozen",
                venue=venue,
                reason=reason,
                previous_reason=previous_reason,
            )
        else:
            self._audit("venue_freeze_reasserted", venue=venue, reason=reason)

        if not cancel_active_orders:
            return

        try:
            for symbol in self.config.get("symbols", []):
                self.cancel_all_orders(symbol)
        except Exception:
            pass

    def clear_venue_freeze(self, venue: str, reason: str = ""):
        venue = (venue or getattr(self.gateway, "gateway_name", "UNKNOWN")).upper()
        previous_reason = self.venue_guards.pop(venue, "")
        if not previous_reason:
            return False

        logger.info(f"[OMS] Venue restored {venue}: {reason or previous_reason}")
        self._audit(
            "venue_unfrozen",
            venue=venue,
            reason=reason or previous_reason,
            previous_reason=previous_reason,
        )
        return True

    def get_venue_freeze_reason(self, venue: str = "") -> str:
        venue = (venue or getattr(self.gateway, "gateway_name", "UNKNOWN")).upper()
        return self.venue_guards.get(venue, "")

    def freeze_strategy(
        self,
        strategy_id: str,
        reason: str,
        symbol: str = "",
        cancel_active_orders: bool = True,
    ):
        strategy_id = (strategy_id or "").strip()
        if not strategy_id:
            return

        symbol = symbol.upper() if symbol else ""
        if symbol:
            key = (strategy_id, symbol)
            previous_reason = self.strategy_symbol_guards.get(key, "")
            self.strategy_symbol_guards[key] = reason
            payload = {
                "strategy_id": strategy_id,
                "symbol": symbol,
                "reason": reason,
                "previous_reason": previous_reason,
            }
            log_message = f"[OMS] Strategy frozen {strategy_id}/{symbol}: {reason}"
            audit_kind = "strategy_symbol_frozen"
        else:
            previous_reason = self.strategy_guards.get(strategy_id, "")
            self.strategy_guards[strategy_id] = reason
            payload = {
                "strategy_id": strategy_id,
                "reason": reason,
                "previous_reason": previous_reason,
            }
            log_message = f"[OMS] Strategy frozen {strategy_id}: {reason}"
            audit_kind = "strategy_frozen"

        if previous_reason != reason:
            logger.error(log_message)
            self._audit(audit_kind, **payload)
        else:
            self._audit("strategy_freeze_reasserted", **payload)

        if not cancel_active_orders:
            return

        self._cancel_orders_matching(
            lambda order: order.intent.strategy_id == strategy_id
            and (not symbol or order.intent.symbol == symbol)
        )

    def clear_strategy_freeze(self, strategy_id: str, symbol: str = "", reason: str = ""):
        strategy_id = (strategy_id or "").strip()
        if not strategy_id:
            return False

        symbol = symbol.upper() if symbol else ""
        if symbol:
            previous_reason = self.strategy_symbol_guards.pop((strategy_id, symbol), "")
        else:
            previous_reason = self.strategy_guards.pop(strategy_id, "")
        if not previous_reason:
            return False

        payload = {
            "strategy_id": strategy_id,
            "reason": reason or previous_reason,
            "previous_reason": previous_reason,
        }
        if symbol:
            payload["symbol"] = symbol
            logger.info(f"[OMS] Strategy restored {strategy_id}/{symbol}: {reason or previous_reason}")
        else:
            logger.info(f"[OMS] Strategy restored {strategy_id}: {reason or previous_reason}")
        self._audit("strategy_unfrozen", **payload)
        return True

    def get_strategy_freeze_reason(self, strategy_id: str, symbol: str = "") -> str:
        strategy_id = (strategy_id or "").strip()
        if not strategy_id:
            return ""

        symbol = symbol.upper() if symbol else ""
        if symbol:
            scoped_reason = self.strategy_symbol_guards.get((strategy_id, symbol), "")
            if scoped_reason:
                return scoped_reason
        return self.strategy_guards.get(strategy_id, "")

    def clear_transient_guards(self, prefixes=("truth_plane:",)):
        prefixes = tuple(prefixes or ())
        if not prefixes:
            return 0

        cleared = 0
        for symbol, reason in list(self.symbol_guards.items()):
            if any(reason.startswith(prefix) for prefix in prefixes):
                if self.clear_symbol_freeze(symbol, reason=f"transient guard cleared: {reason}"):
                    cleared += 1

        for venue, reason in list(self.venue_guards.items()):
            if any(reason.startswith(prefix) for prefix in prefixes):
                if self.clear_venue_freeze(venue, reason=f"transient guard cleared: {reason}"):
                    cleared += 1

        for strategy_id, reason in list(self.strategy_guards.items()):
            if any(reason.startswith(prefix) for prefix in prefixes):
                if self.clear_strategy_freeze(strategy_id, reason=f"transient guard cleared: {reason}"):
                    cleared += 1

        for (strategy_id, symbol), reason in list(self.strategy_symbol_guards.items()):
            if any(reason.startswith(prefix) for prefix in prefixes):
                if self.clear_strategy_freeze(
                    strategy_id,
                    symbol=symbol,
                    reason=f"transient guard cleared: {reason}",
                ):
                    cleared += 1

        return cleared

    def is_symbol_tradeable(self, symbol: str) -> bool:
        return self.can_open_new_risk() and not self.get_symbol_freeze_reason(symbol)

    def can_submit_for_strategy(self, strategy_id: str, symbol: str = "") -> bool:
        return self._get_order_block_reason(strategy_id, symbol) == ""

    def _cancel_orders_matching(self, predicate):
        with self.lock:
            client_oids = [
                order.client_oid
                for order in self.orders.values()
                if order.is_active() and predicate(order)
            ]
        for client_oid in client_oids:
            self.cancel_order(client_oid)

    def _get_order_block_reason(self, strategy_id: str = "", symbol: str = "") -> str:
        if not self.can_open_new_risk():
            return self._get_capability_block_reason("open_risk")

        venue_reason = self.get_venue_freeze_reason()
        if venue_reason:
            return f"venue_frozen:{venue_reason}"

        symbol_reason = self.get_symbol_freeze_reason(symbol)
        if symbol_reason:
            return f"symbol_frozen:{symbol_reason}"

        strategy_reason = self.get_strategy_freeze_reason(strategy_id, symbol)
        if strategy_reason:
            return f"strategy_frozen:{strategy_reason}"

        return ""

    def _get_submission_safety_reason_locked(self, intent: OrderIntent) -> str:
        total_active = 0
        symbol_active = 0
        strategy_active = 0
        strategy_symbol_active = 0
        now = time.time()

        for order in self.orders.values():
            if not order.is_active():
                continue

            total_active += 1
            same_symbol = order.intent.symbol == intent.symbol
            same_strategy = order.intent.strategy_id == intent.strategy_id
            if same_symbol:
                symbol_active += 1
            if same_strategy:
                strategy_active += 1
            if same_symbol and same_strategy:
                strategy_symbol_active += 1

            if self.duplicate_intent_window_sec <= 0:
                continue
            if now - float(getattr(order, "created_at", now)) > self.duplicate_intent_window_sec:
                continue
            if not same_symbol or not same_strategy:
                continue
            if order.intent.side != intent.side:
                continue
            if order.intent.order_type != intent.order_type:
                continue
            if order.intent.time_in_force != intent.time_in_force:
                continue
            if bool(order.intent.is_post_only) != bool(intent.is_post_only):
                continue
            if abs(order.intent.price - intent.price) > 1e-9:
                continue
            if abs(order.intent.volume - intent.volume) > 1e-9:
                continue
            return (
                "duplicate_active_intent:"
                f"{intent.strategy_id}:{intent.symbol}:{intent.side.value}"
            )

        if self.max_total_active_orders > 0 and total_active >= self.max_total_active_orders:
            return f"active_order_limit:total:{total_active}>={self.max_total_active_orders}"
        if self.max_symbol_active_orders > 0 and symbol_active >= self.max_symbol_active_orders:
            return f"active_order_limit:symbol:{symbol_active}>={self.max_symbol_active_orders}"
        if self.max_strategy_active_orders > 0 and strategy_active >= self.max_strategy_active_orders:
            return f"active_order_limit:strategy:{strategy_active}>={self.max_strategy_active_orders}"
        if (
            self.max_strategy_symbol_active_orders > 0
            and strategy_symbol_active >= self.max_strategy_symbol_active_orders
        ):
            return (
                "active_order_limit:strategy_symbol:"
                f"{strategy_symbol_active}>={self.max_strategy_symbol_active_orders}"
            )
        return ""

    def freeze_system(self, reason: str, cancel_active_orders: bool = False):
        if self.state == LifecycleState.HALTED:
            return

        previous_state = self.state
        self.state = LifecycleState.FROZEN
        self._sync_capability_mode(reason)
        self.last_freeze_reason = reason

        if previous_state != LifecycleState.FROZEN:
            logger.error(f"OMS FROZEN: {reason}")
            self._audit(
                "lifecycle",
                state=self.state.value,
                reason=reason,
                previous_state=previous_state.value,
            )
        else:
            logger.error(f"OMS still FROZEN: {reason}")
            self._audit("freeze_reasserted", reason=reason)

        if not cancel_active_orders:
            return

        self._audit(
            "freeze_cancel_all_requested",
            reason=reason,
            symbols=self.config.get("symbols", []),
        )
        try:
            for symbol in self.config["symbols"]:
                self.gateway.cancel_all_orders(symbol)
        except Exception:
            pass

    def halt_system(self, reason: str):
        if self.state == LifecycleState.HALTED:
            self.last_halt_reason = reason
            self.manual_rearm_required = True
            self._sync_capability_mode(reason)
            self._audit("halt_reasserted", reason=reason)
            return
        self.state = LifecycleState.HALTED
        self._sync_capability_mode(reason)
        self.manual_rearm_required = True
        self.last_halt_reason = reason
        self.last_freeze_reason = ""
        logger.critical(f"OMS HALTED: {reason}")
        self._audit(
            "lifecycle",
            state=self.state.value,
            reason=reason,
            manual_rearm_required=True,
        )
        self.event_engine.put(Event(EVENT_SYSTEM_HEALTH, f"HALT:{reason}"))
        try:
            for symbol in self.config["symbols"]:
                self.gateway.cancel_all_orders(symbol)
        except Exception:
            pass

    def rearm_system(self, reason: str = "manual"):
        if self.state != LifecycleState.HALTED or not self.manual_rearm_required:
            self._audit("rearm_ignored", reason=reason)
            return False

        logger.warning(f"OMS manual rearm requested: {reason}")
        self._audit(
            "rearm_requested",
            reason=reason,
            halted_reason=self.last_halt_reason,
        )
        self.state = LifecycleState.RECONCILING
        self._sync_capability_mode(f"manual_rearm:{reason}")
        self._audit(
            "lifecycle",
            state=self.state.value,
            reason=f"manual_rearm:{reason}",
        )
        self._perform_full_reset()
        if self.state == LifecycleState.LIVE:
            self.manual_rearm_required = False
            self.last_halt_reason = ""
            self._audit("rearm_completed", state=self.state.value, reason=reason)
            return True

        self.manual_rearm_required = True
        return False

    def stop(self):
        self._audit(
            "oms_stopped",
            state=self.state.value,
            manual_rearm_required=self.manual_rearm_required,
            symbol_guard_count=len(self.symbol_guards),
            venue_guard_count=len(self.venue_guards),
            strategy_guard_count=len(self.strategy_guards),
            strategy_symbol_guard_count=len(self.strategy_symbol_guards),
        )
        self.order_monitor.stop()

    def trigger_reconcile(self, reason: str, suspicious_oid: str = None):
        if self.state in [LifecycleState.RECONCILING, LifecycleState.HALTED]:
            return

        self.freeze_system(
            f"Awaiting reconcile: {reason}",
            cancel_active_orders=True,
        )

        now = time.monotonic()
        if self.last_reconcile_failure_ts and now - self.last_reconcile_failure_ts < self.reconcile_api_cooldown_sec:
            logger.warning(f"[OMS] Reconcile suppressed during API cooldown: {reason}")
            self._audit(
                "reconcile_suppressed",
                reason=reason,
                suspicious_oid=suspicious_oid,
                cooldown="api_failure",
            )
            self._schedule_reconcile_retry(reason, suspicious_oid=suspicious_oid)
            return

        if now - self.last_reconcile_request_ts < self.reconcile_min_interval_sec:
            logger.warning(f"[OMS] Reconcile suppressed by min interval: {reason}")
            self._audit(
                "reconcile_suppressed",
                reason=reason,
                suspicious_oid=suspicious_oid,
                cooldown="min_interval",
            )
            self._schedule_reconcile_retry(reason, suspicious_oid=suspicious_oid)
            return

        self.last_reconcile_request_ts = now
        logger.warning(f"OMS dirty: {reason}. State -> RECONCILING")
        self.state = LifecycleState.RECONCILING
        self._sync_capability_mode(reason)
        self._audit(
            "reconcile_requested",
            state=self.state.value,
            reason=reason,
            suspicious_oid=suspicious_oid,
        )
        threading.Thread(
            target=self._execute_reconcile,
            args=(suspicious_oid,),
            daemon=True,
        ).start()

    def _schedule_reconcile_retry(
        self,
        reason: str,
        suspicious_oid: str = None,
        delay_sec: float = None,
    ):
        if self.reconcile_retry_scheduled or self.state == LifecycleState.HALTED:
            return

        if delay_sec is None:
            now = time.monotonic()
            cooldown_remaining = 0.0
            if self.last_reconcile_failure_ts:
                cooldown_remaining = max(
                    0.0,
                    self.reconcile_api_cooldown_sec - (now - self.last_reconcile_failure_ts),
                )
            interval_remaining = max(
                0.0,
                self.reconcile_min_interval_sec - (now - self.last_reconcile_request_ts),
            )
            delay_sec = max(cooldown_remaining, interval_remaining, 0.05)

        delay_sec = max(delay_sec, 0.05)
        self.reconcile_retry_scheduled = True
        self._audit(
            "reconcile_retry_scheduled",
            reason=reason,
            suspicious_oid=suspicious_oid,
            delay_sec=delay_sec,
        )

        def _retry():
            time.sleep(delay_sec)
            self.reconcile_retry_scheduled = False
            if self.state != LifecycleState.FROZEN:
                return
            self.trigger_reconcile(reason, suspicious_oid=suspicious_oid)

        threading.Thread(target=_retry, daemon=True).start()

    def _execute_reconcile(self, suspicious_oid: str):
        self._audit("reconcile_started", suspicious_oid=suspicious_oid)
        try:
            remote_positions = self.query_positions()
            remote_orders = self.query_open_orders()

            if remote_positions is None or remote_orders is None:
                self.consecutive_reconcile_api_failures += 1
                self.last_reconcile_failure_ts = time.monotonic()
                attempt = self.consecutive_reconcile_api_failures
                self._audit(
                    "reconcile_api_unreachable",
                    failures=attempt,
                    suspicious_oid=suspicious_oid,
                )
                if attempt >= self.reconcile_api_failure_threshold:
                    self.halt_system("Reconcile API unreachable")
                else:
                    logger.error(
                        f"[Reconcile] API unreachable ({attempt}/{self.reconcile_api_failure_threshold}); "
                        "keeping FROZEN and backing off."
                    )
                    self.freeze_system("Reconcile API unreachable")
                    self._schedule_reconcile_retry(
                        "Reconcile API retry",
                        suspicious_oid=suspicious_oid,
                    )
                return

            self.consecutive_reconcile_api_failures = 0
            self.last_reconcile_failure_ts = 0.0

            with self.lock:
                remote_map = {
                    pos["symbol"]: float(pos["positionAmt"])
                    for pos in remote_positions
                    if float(pos["positionAmt"]) != 0
                }
                local_map = {
                    symbol: volume
                    for symbol, volume in self.exposure.net_positions.items()
                    if volume != 0
                }
                local_active_orders = self._collect_local_active_orders_locked()

            for symbol in set(remote_map) | set(local_map):
                if abs(local_map.get(symbol, 0.0) - remote_map.get(symbol, 0.0)) > 1e-6:
                    logger.error(
                        f"[Reconcile] Position mismatch {symbol}: "
                        f"Local={local_map.get(symbol, 0.0)}, Exch={remote_map.get(symbol, 0.0)}"
                    )
                    self._audit("reconcile_reset", case="position_mismatch", symbol=symbol)
                    self._perform_full_reset()
                    return

            remote_active_orders = self._normalize_remote_open_orders(remote_orders)
            if local_active_orders != remote_active_orders:
                self._audit(
                    "reconcile_reset",
                    case="open_order_mismatch",
                    local_active_orders=local_active_orders,
                    remote_active_orders=remote_active_orders,
                    suspicious_oid=suspicious_oid,
                )
                self._perform_full_reset()
                return

            remote_has_suspicious = False
            local_has_suspicious = False
            if suspicious_oid:
                remote_has_suspicious = any(
                    suspicious_oid in order["identifiers"]
                    for order in remote_active_orders
                )
                local_has_suspicious = any(
                    suspicious_oid in order["identifiers"]
                    for order in local_active_orders
                )

            if remote_has_suspicious and not local_has_suspicious:
                self._audit(
                    "reconcile_reset",
                    case="missing_local_order",
                    suspicious_oid=suspicious_oid,
                )
                self._perform_full_reset()
            else:
                self.state = LifecycleState.LIVE
                self._sync_capability_mode("reconcile_cleared")
                self.last_freeze_reason = ""
                self._audit("reconcile_cleared", state=self.state.value)
                logger.info("[Reconcile] False alarm. Resuming LIVE.")

        except Exception as exc:
            self.halt_system(f"Reconcile critical error: {exc}")

    def _perform_full_reset(self):
        logger.info("[OMS] Performing full state reset...")
        self._audit("full_reset_started", symbols=self.config.get("symbols", []))
        try:
            for symbol in self.config["symbols"]:
                self.gateway.cancel_all_orders(symbol)
            time.sleep(1.0)

            remote_orders = self.query_open_orders()
            account = self.query_account_info()
            positions = self.query_positions()
            if remote_orders is None or not account or positions is None:
                raise RuntimeError("API failed during reset")

            residual_orders = self._normalize_remote_open_orders(remote_orders)
            if residual_orders:
                raise RuntimeError(
                    f"remote open orders still present after cancel-all: {residual_orders}"
                )

            with self.lock:
                self.orders.clear()
                self.exchange_id_map.clear()
                self.sequence.reset()

                self.exposure.net_positions.clear()
                self.exposure.avg_prices.clear()
                self.exposure.open_buy_qty.clear()
                self.exposure.open_sell_qty.clear()

                for pos in positions:
                    amount = float(pos["positionAmt"])
                    if amount == 0:
                        continue
                    symbol = pos["symbol"]
                    self.exposure.force_sync(symbol, amount, float(pos["entryPrice"]))

                available_balance = account.get("availableBalance")
                self.account.force_sync(
                    float(account["totalWalletBalance"]),
                    float(account["totalInitialMargin"]),
                    float(available_balance) if available_balance is not None else None,
                )
                self.order_monitor.monitored_orders.clear()

            for symbol in self.config.get("symbols", []):
                self._emit_position_update(symbol)

            self.state = LifecycleState.LIVE
            self._sync_capability_mode("full_reset_completed")
            self.manual_rearm_required = False
            self.last_freeze_reason = ""
            self.last_halt_reason = ""
            self.reconcile_retry_scheduled = False
            self.clear_transient_guards(prefixes=("truth_plane:",))
            self._audit(
                "full_reset_completed",
                state=self.state.value,
                balance=self.account.balance,
                equity=self.account.equity,
                positions=dict(self.exposure.net_positions),
            )
            logger.info("OMS: Reset complete. System is CLEAN and LIVE.")

        except Exception as exc:
            self.halt_system(f"Reset failed: {exc}")

    def submit_order(self, intent: OrderIntent) -> OrderSubmitResult:
        client_oid = str(uuid.uuid4())
        original_intent = intent

        block_reason = self._get_order_block_reason(intent.strategy_id, intent.symbol)
        if block_reason:
            return self._reject_intent_locally(
                intent,
                client_oid,
                block_reason,
            )

        intent, mode_reject_reason = self.adapt_intent_for_trading_mode(intent)
        if mode_reject_reason:
            return self._reject_intent_locally(original_intent, client_oid, mode_reject_reason)

        valid, validation_reason = self.validator.validate_params(intent)
        if not valid:
            return self._reject_intent_locally(intent, client_oid, validation_reason)

        with self.lock:
            submission_safety_reason = self._get_submission_safety_reason_locked(intent)
        if submission_safety_reason:
            return self._reject_intent_locally(
                intent,
                client_oid,
                submission_safety_reason,
            )

        notional = intent.price * intent.volume
        if not self.account.check_margin(notional):
            return self._reject_intent_locally(
                intent,
                client_oid,
                "insufficient_margin",
                notional=notional,
                available=self.account.available,
            )

        ok, risk_reason = self.exposure.check_risk(
            intent.symbol,
            intent.side,
            intent.volume,
            self.max_pos_notional,
            self.max_account_gross_notional,
            intent.price,
        )
        if not ok:
            logger.warning(f"[OMS] Risk rejected: {risk_reason}")
            return self._reject_intent_locally(
                intent,
                client_oid,
                f"exposure_limit:{risk_reason}",
            )

        order = Order(client_oid, intent)

        with self.lock:
            self.orders[client_oid] = order
            order.mark_submitting()
            self.exposure.update_open_orders(self.orders)
            self.account.calculate()
            self._record_order_snapshot(order, "accepted")

        request = OrderRequest(
            symbol=intent.symbol,
            price=intent.price,
            volume=intent.volume,
            side=intent.side.value,
            order_type=intent.order_type,
            time_in_force=intent.time_in_force,
            post_only=intent.is_post_only,
        )

        exchange_oid = self.gateway.send_order(request, client_oid)

        if exchange_oid:
            with self.lock:
                order.mark_pending_ack(exchange_oid)
                self.exchange_id_map[exchange_oid] = order
                self._record_order_snapshot(order, "rest_ack")
                self._emit_order_update(order)

            event_data = OrderSubmitted(request, client_oid, time.time())
            self.event_engine.put(Event(EVENT_ORDER_SUBMITTED, event_data))
            self._audit(
                "order_submitted",
                client_oid=client_oid,
                exchange_oid=exchange_oid,
                symbol=intent.symbol,
                side=intent.side.value,
                price=intent.price,
                volume=intent.volume,
            )
            return OrderSubmitResult(
                accepted=True,
                client_oid=client_oid,
                state=self.state.value,
            )

        with self.lock:
            order.mark_rejected_locally("gateway_send_failed")
            self._record_order_snapshot(order, "send_failed")
            self._emit_order_update(order)
            self.orders.pop(client_oid, None)
            self.exposure.update_open_orders(self.orders)
            self.account.calculate()
        self._write_tombstone(order)
        self._audit(
            "order_rejected_locally",
            client_oid=client_oid,
            symbol=intent.symbol,
            reason="gateway_send_failed",
        )
        return OrderSubmitResult(
            accepted=False,
            client_oid=client_oid,
            reason="gateway_send_failed",
            state=self.state.value,
        )

    def _reject_intent_locally(self, intent: OrderIntent, client_oid: str, reason: str, **extra):
        order = Order(client_oid, intent)
        order.mark_rejected_locally(reason)
        with self.lock:
            self._record_order_snapshot(order, "intent_rejected", **extra)
            self._emit_order_update(order)
        self._write_tombstone(order)
        audit_payload = {
            "reason": reason,
            "intent": self._serialize_intent(intent),
            "client_oid": client_oid,
        }
        audit_payload.update(extra)
        self._audit("intent_rejected", **audit_payload)
        return OrderSubmitResult(
            accepted=False,
            client_oid=client_oid,
            reason=reason,
            state=self.state.value,
        )

    def cancel_order(self, client_oid: str):
        if not self.can_cancel_orders():
            self._audit(
                "cancel_rejected",
                client_oid=client_oid,
                reason=self._get_capability_block_reason("cancel"),
            )
            return False

        with self.lock:
            order = self.orders.get(client_oid)
            if not order or not order.is_active():
                return False
            target_id = order.exchange_oid if order.exchange_oid else client_oid
            try:
                order.mark_cancelling()
                self._record_order_snapshot(order, "cancel_requested")
                self._emit_order_update(order)
            except ValueError:
                pass
            request = CancelRequest(order.intent.symbol, target_id)

        self.gateway.cancel_order(request)
        self._audit(
            "cancel_submitted",
            client_oid=client_oid,
            target_id=target_id,
            symbol=request.symbol,
        )
        return True

    def cancel_all_orders(self, symbol: str):
        if not self.can_cancel_orders():
            self._audit(
                "cancel_all_rejected",
                symbol=symbol,
                reason=self._get_capability_block_reason("cancel"),
            )
            return False
        self._audit("cancel_all_submitted", symbol=symbol)
        self.gateway.cancel_all_orders(symbol)
        return True

    def on_exchange_update(self, event):
        self._append_and_process(event)

    def on_exchange_account_update(self, event):
        update: ExchangeAccountUpdate = event.data
        tracked_symbols = set(self.config.get("symbols", []))
        tracked_positions = {
            symbol: payload
            for symbol, payload in update.positions.items()
            if not tracked_symbols or symbol in tracked_symbols
        }

        with self.lock:
            self.account.sync_exchange_balance(
                update.wallet_balance,
                available=update.available_balance,
                asset=update.asset,
                balances=update.balances,
            )
            position_drift = self._collect_exchange_position_drift_locked(
                tracked_positions,
                tracked_symbols,
            )
            has_active_orders = self._has_active_orders_locked(tracked_symbols)

        if not position_drift:
            return

        self._audit(
            "exchange_account_position_drift",
            reason=update.reason,
            positions=position_drift,
        )

        if self.state in {LifecycleState.HALTED, LifecycleState.RECONCILING}:
            return

        if has_active_orders:
            logger.error(f"[OMS] Exchange position drift detected while orders are active: {position_drift}")
        else:
            logger.error(f"[OMS] Exchange position drift detected without active orders: {position_drift}")

        self.trigger_reconcile("Exchange account position drift")

    def _append_and_process(self, event):
        if event.type == "eExchangeOrderUpdate":
            update: ExchangeOrderUpdate = event.data
            if not self.sequence.check(update.seq):
                self._audit("sequence_gap", seq=update.seq)
                self.trigger_reconcile(f"Seq gap {update.seq}")
                return

        self.event_log.append(event)
        self._apply_event(event)

    def _apply_event(self, event):
        if event.type != "eExchangeOrderUpdate":
            return

        update: ExchangeOrderUpdate = event.data
        with self.lock:
            order = self.orders.get(update.client_oid)
            if not order and update.exchange_oid:
                order = self.exchange_id_map.get(update.exchange_oid)

            if not order:
                suspicious = update.client_oid or update.exchange_oid
                if suspicious in self.terminated_oids:
                    self._audit(
                        "late_duplicate_ignored",
                        suspicious_oid=suspicious,
                        exchange_status=update.status,
                    )
                    return
                self._audit(
                    "unknown_order_update",
                    client_oid=update.client_oid,
                    exchange_oid=update.exchange_oid,
                    status=update.status,
                )
                threading.Thread(
                    target=self.trigger_reconcile,
                    args=(f"Unknown Order {suspicious}", suspicious),
                    daemon=True,
                ).start()
                return

            if update.exchange_oid and order.exchange_oid and order.exchange_oid != update.exchange_oid:
                self._audit(
                    "exchange_oid_mismatch",
                    client_oid=order.client_oid,
                    local_exchange_oid=order.exchange_oid,
                    incoming_exchange_oid=update.exchange_oid,
                )
                threading.Thread(
                    target=self.trigger_reconcile,
                    args=(f"Exchange OID mismatch {order.client_oid}", order.client_oid),
                    daemon=True,
                ).start()
                return

            if update.seq and update.seq <= order.last_update_seq:
                self._audit(
                    "stale_update_ignored",
                    client_oid=order.client_oid,
                    seq=update.seq,
                    last_seq=order.last_update_seq,
                )
                return

            if update.cum_filled_qty + 1e-9 < order.filled_volume:
                self._audit(
                    "cum_fill_regression",
                    client_oid=order.client_oid,
                    incoming_cum=update.cum_filled_qty,
                    local_cum=order.filled_volume,
                )
                threading.Thread(
                    target=self.trigger_reconcile,
                    args=(f"Cum fill regression {order.client_oid}", order.client_oid),
                    daemon=True,
                ).start()
                return

            previous_status = order.status
            had_fill = False

            try:
                if update.status == "NEW":
                    order.mark_new(
                        exchange_oid=update.exchange_oid,
                        update_time=update.update_time,
                        seq=update.seq,
                    )
                    if update.exchange_oid:
                        self.exchange_id_map[update.exchange_oid] = order

                elif update.status == "CANCELED":
                    order.mark_cancelled(
                        update_time=update.update_time,
                        seq=update.seq,
                        exchange_status=update.status,
                    )
                    self._write_tombstone(order)

                elif update.status == "EXPIRED":
                    order.mark_expired(update_time=update.update_time, seq=update.seq)
                    self._write_tombstone(order)

                elif update.status == "REJECTED":
                    order.mark_rejected(
                        reason="exchange_rejected",
                        update_time=update.update_time,
                        seq=update.seq,
                        exchange_status=update.status,
                    )
                    self._write_tombstone(order)

                elif update.status in ["FILLED", "PARTIALLY_FILLED"]:
                    delta = update.cum_filled_qty - order.filled_volume
                    if delta > 1e-9:
                        had_fill = order.add_fill(
                            delta,
                            update.filled_price,
                            update_time=update.update_time,
                            seq=update.seq,
                            exchange_status=update.status,
                        )
                        local_realized_pnl = self.exposure.on_fill(
                            order.intent.symbol,
                            order.intent.side,
                            delta,
                            update.filled_price,
                        )
                        realized_pnl = (
                            update.realized_pnl
                            if update.realized_pnl is not None
                            else local_realized_pnl
                        )
                        fill_notional = delta * update.filled_price
                        fee = self._get_fill_commission(update, order, fill_notional)
                        self.account.update_balance(realized_pnl, fee)

                        trade_data = TradeData(
                            symbol=order.intent.symbol,
                            order_id=order.client_oid,
                            trade_id=f"T{int(update.update_time * 1000)}",
                            side=order.intent.side.value,
                            price=update.filled_price,
                            volume=delta,
                            datetime=datetime.now(),
                        )
                        self.event_engine.put(Event(EVENT_TRADE_UPDATE, trade_data))
                    else:
                        order.note_exchange_update(
                            exchange_status=update.status,
                            update_time=update.update_time,
                            seq=update.seq,
                            exchange_oid=update.exchange_oid,
                        )

                    if update.status == "FILLED":
                        order.mark_filled(update_time=update.update_time, seq=update.seq)
                        self._write_tombstone(order)

                else:
                    self._audit(
                        "unhandled_exchange_status",
                        client_oid=order.client_oid,
                        status=update.status,
                    )
                    order.note_exchange_update(
                        exchange_status=update.status,
                        update_time=update.update_time,
                        seq=update.seq,
                        exchange_oid=update.exchange_oid,
                    )
                    return

            except ValueError as exc:
                self._audit(
                    "invalid_transition",
                    client_oid=order.client_oid,
                    current_status=order.status.value,
                    incoming_status=update.status,
                    error=str(exc),
                )
                threading.Thread(
                    target=self.trigger_reconcile,
                    args=(f"Invalid transition {order.client_oid}", order.client_oid),
                    daemon=True,
                ).start()
                return

            self.order_monitor.on_order_update(order.client_oid, order.status)
            self.exposure.update_open_orders(self.orders)
            self.account.calculate()

            if order.status != previous_status or had_fill:
                self._record_order_snapshot(
                    order,
                    "exchange_update",
                    exchange_status=update.status,
                    seq=update.seq,
                    cum_filled_qty=update.cum_filled_qty,
                )
                self._emit_order_update(order)
                if had_fill:
                    self._emit_position_update(order.intent.symbol)

    def rebuild_from_log(self):
        records = self.journal.load()
        if not records:
            return {
                "records": 0,
                "recovered_orders": 0,
                "recovered_terminal_ids": 0,
                "last_lifecycle": None,
                "last_freeze_reason": "",
                "last_halt_reason": "",
                "manual_rearm_required": False,
                "symbol_guards": {},
                "venue_guards": {},
                "strategy_guards": {},
                "strategy_symbol_guards": {},
                "mode_override": "",
                "mode_override_reason": "",
                "clean_shutdown": True,
                "dirty_shutdown": False,
            }

        latest_order_records = {}
        last_lifecycle = None
        last_freeze_reason = ""
        last_halt_reason = ""
        manual_rearm_required = False
        symbol_guards = {}
        venue_guards = {}
        strategy_guards = {}
        strategy_symbol_guards = {}
        mode_override = ""
        mode_override_reason = ""
        clean_shutdown = records[-1].get("kind") == "oms_stopped"
        for record in records:
            payload = record.get("payload", {})
            kind = record.get("kind")
            if kind == "order_snapshot":
                client_oid = payload.get("client_oid")
                if client_oid:
                    latest_order_records[client_oid] = payload
            elif kind == "lifecycle":
                last_lifecycle = payload.get("state")
                reason = str(payload.get("reason", "") or "")
                if last_lifecycle == LifecycleState.FROZEN.value and reason:
                    last_freeze_reason = reason
                elif last_lifecycle == LifecycleState.HALTED.value:
                    if reason:
                        last_halt_reason = reason
                    manual_rearm_required = bool(payload.get("manual_rearm_required", True))
                elif last_lifecycle == LifecycleState.LIVE.value:
                    manual_rearm_required = False
                    last_halt_reason = ""
                    last_freeze_reason = ""
            elif kind in {"full_reset_completed", "reconcile_cleared", "rearm_completed"}:
                last_lifecycle = payload.get("state") or LifecycleState.LIVE.value
                if last_lifecycle == LifecycleState.LIVE.value:
                    manual_rearm_required = False
                    last_freeze_reason = ""
                    if kind == "rearm_completed":
                        last_halt_reason = ""
            elif kind in {"reconcile_requested", "reconcile_started", "full_reset_started"}:
                last_lifecycle = LifecycleState.RECONCILING.value
            elif kind in {"bootstrap_guarded", "freeze_reasserted"}:
                reason = str(payload.get("reason", "") or "")
                if reason:
                    last_freeze_reason = reason
                if last_lifecycle != LifecycleState.HALTED.value:
                    last_lifecycle = LifecycleState.FROZEN.value
            elif kind == "halt_reasserted":
                last_lifecycle = LifecycleState.HALTED.value
                reason = str(payload.get("reason", "") or "")
                if reason:
                    last_halt_reason = reason
                manual_rearm_required = True
            elif kind in {"symbol_frozen", "symbol_freeze_reasserted"}:
                symbol = str(payload.get("symbol", "") or "").upper()
                reason = str(payload.get("reason", "") or "")
                if symbol and reason:
                    symbol_guards[symbol] = reason
            elif kind == "symbol_unfrozen":
                symbol = str(payload.get("symbol", "") or "").upper()
                if symbol:
                    symbol_guards.pop(symbol, None)
            elif kind in {"venue_frozen", "venue_freeze_reasserted"}:
                venue = str(payload.get("venue", "") or "").upper()
                reason = str(payload.get("reason", "") or "")
                if venue and reason:
                    venue_guards[venue] = reason
            elif kind == "venue_unfrozen":
                venue = str(payload.get("venue", "") or "").upper()
                if venue:
                    venue_guards.pop(venue, None)
            elif kind in {"strategy_frozen", "strategy_freeze_reasserted"}:
                strategy_id = str(payload.get("strategy_id", "") or "").strip()
                symbol = str(payload.get("symbol", "") or "").upper()
                reason = str(payload.get("reason", "") or "")
                if strategy_id and reason:
                    if symbol:
                        strategy_symbol_guards[f"{strategy_id}|{symbol}"] = reason
                    else:
                        strategy_guards[strategy_id] = reason
            elif kind == "strategy_symbol_frozen":
                strategy_id = str(payload.get("strategy_id", "") or "").strip()
                symbol = str(payload.get("symbol", "") or "").upper()
                reason = str(payload.get("reason", "") or "")
                if strategy_id and symbol and reason:
                    strategy_symbol_guards[f"{strategy_id}|{symbol}"] = reason
            elif kind == "strategy_unfrozen":
                strategy_id = str(payload.get("strategy_id", "") or "").strip()
                symbol = str(payload.get("symbol", "") or "").upper()
                if strategy_id and symbol:
                    strategy_symbol_guards.pop(f"{strategy_id}|{symbol}", None)
                elif strategy_id:
                    strategy_guards.pop(strategy_id, None)
            elif kind == "trading_mode_override_set":
                mode_override = str(payload.get("mode", "") or "")
                mode_override_reason = str(payload.get("reason", "") or "")
            elif kind == "trading_mode_override_cleared":
                mode_override = ""
                mode_override_reason = ""
            elif kind == "oms_stopped":
                state = payload.get("state")
                if state:
                    last_lifecycle = state
                if payload.get("manual_rearm_required") is True:
                    manual_rearm_required = True

        with self.lock:
            self.terminated_oids.clear()
            self.terminated_oid_queue.clear()
            recovered_terminal_ids = 0
            for payload in latest_order_records.values():
                status = payload.get("status")
                if status in {state.value for state in TERMINAL_STATUSES}:
                    client_oid = payload.get("client_oid")
                    exchange_oid = payload.get("exchange_oid")
                    if client_oid:
                        self._remember_terminated_oid(client_oid)
                        recovered_terminal_ids += 1
                    if exchange_oid:
                        self._remember_terminated_oid(exchange_oid)
                        recovered_terminal_ids += 1

        summary = {
            "records": len(records),
            "recovered_orders": len(latest_order_records),
            "recovered_terminal_ids": recovered_terminal_ids,
            "last_lifecycle": last_lifecycle,
            "last_freeze_reason": last_freeze_reason,
            "last_halt_reason": last_halt_reason,
            "manual_rearm_required": manual_rearm_required,
            "symbol_guards": symbol_guards,
            "venue_guards": venue_guards,
            "strategy_guards": strategy_guards,
            "strategy_symbol_guards": strategy_symbol_guards,
            "mode_override": mode_override,
            "mode_override_reason": mode_override_reason,
            "clean_shutdown": clean_shutdown,
            "dirty_shutdown": not clean_shutdown,
        }
        if recovered_terminal_ids:
            logger.info(
                f"[OMS] Recovered {recovered_terminal_ids} terminal IDs from journal"
            )
        return summary

    def _normalize_remote_open_orders(self, remote_orders):
        tracked_symbols = set(self.config.get("symbols", []))
        normalized = []
        for order in remote_orders:
            symbol = order.get("symbol")
            if tracked_symbols and symbol not in tracked_symbols:
                continue
            identifiers = tuple(
                sorted(
                    oid
                    for oid in [
                        str(order.get("orderId")) if order.get("orderId") is not None else "",
                        order.get("clientOrderId") or "",
                    ]
                    if oid
                )
            )
            normalized.append(
                {
                    "symbol": symbol,
                    "identifiers": identifiers,
                    "side": order.get("side", ""),
                }
            )
        normalized.sort(key=lambda item: (item["symbol"], item["identifiers"], item["side"]))
        return normalized

    def _collect_local_active_orders_locked(self):
        tracked_symbols = set(self.config.get("symbols", []))
        normalized = []
        for order in self.orders.values():
            if not order.is_active():
                continue
            if tracked_symbols and order.intent.symbol not in tracked_symbols:
                continue
            identifiers = tuple(
                sorted(
                    oid
                    for oid in [order.client_oid, order.exchange_oid]
                    if oid
                )
            )
            normalized.append(
                {
                    "symbol": order.intent.symbol,
                    "identifiers": identifiers,
                    "side": order.intent.side.value,
                }
            )
        normalized.sort(key=lambda item: (item["symbol"], item["identifiers"], item["side"]))
        return normalized

    def _collect_exchange_position_drift_locked(self, exchange_positions, tracked_symbols=None):
        drift = {}
        symbols = set(tracked_symbols or [])
        symbols.update(exchange_positions.keys())
        symbols.update(
            symbol
            for symbol, volume in self.exposure.net_positions.items()
            if abs(volume) > 1e-6 and (not symbols or symbol in symbols)
        )

        for symbol in symbols:
            local_pos = self.exposure.net_positions.get(symbol, 0.0)
            payload = exchange_positions.get(symbol, {})
            exchange_pos = float(payload.get("volume", 0.0))
            if abs(local_pos - exchange_pos) > 1e-6:
                drift[symbol] = {
                    "local": local_pos,
                    "exchange": exchange_pos,
                    "entry_price": float(payload.get("entry_price", 0.0)),
                }
        return drift

    def _has_active_orders_locked(self, symbols=None):
        tracked_symbols = set(symbols or [])
        for order in self.orders.values():
            if not order.is_active():
                continue
            if tracked_symbols and order.intent.symbol not in tracked_symbols:
                continue
            return True
        return False

    def _extract_quote_asset(self, symbol: str) -> str:
        for suffix in ("USDT", "USDC", "BUSD", "FDUSD", "BTC", "ETH", "BNB"):
            if symbol.endswith(suffix):
                return suffix
        return ""

    def _get_fill_commission(self, update: ExchangeOrderUpdate, order: Order, fill_notional: float) -> float:
        if update.commission is None:
            return fill_notional * self._get_fee_rate(order, is_maker=update.is_maker)

        asset = (update.commission_asset or self._extract_quote_asset(order.intent.symbol)).upper()
        if asset in {"", "USDT", "USDC", "BUSD", "FDUSD"}:
            return update.commission

        logger.warning(
            f"[OMS] Unsupported commission asset {asset}; falling back to configured fee model"
        )
        return fill_notional * self._get_fee_rate(order, is_maker=update.is_maker)

    def _get_fee_rate(self, order: Order, is_maker: bool = None) -> float:
        fee_config = self.config.get("backtest", {})
        if is_maker is True:
            return fee_config.get("maker_fee", 0.0)
        if is_maker is False:
            return fee_config.get("taker_fee", 0.0005)
        if order.intent.is_post_only:
            return fee_config.get("maker_fee", 0.0)
        return fee_config.get("taker_fee", 0.0005)

    def _serialize_intent(self, intent: OrderIntent) -> dict:
        return {
            "strategy_id": intent.strategy_id,
            "symbol": intent.symbol,
            "side": intent.side.value,
            "price": intent.price,
            "volume": intent.volume,
            "order_type": intent.order_type,
            "time_in_force": intent.time_in_force,
            "is_post_only": intent.is_post_only,
            "policy": intent.policy.value,
            "tag": intent.tag,
        }

    def _audit(self, kind: str, **payload):
        payload.setdefault("state", self.state.value)
        payload.setdefault("capability_mode", self.capability_mode.value)
        payload.setdefault("capability_reason", self.capability_reason)
        payload.setdefault("mode_override", self.mode_override.value if self.mode_override else "")
        payload.setdefault("mode_override_reason", self.mode_override_reason)
        self.journal.append(kind, payload)

    def _record_order_snapshot(self, order: Order, source: str, **extra):
        payload = order.to_record()
        payload["source"] = source
        if extra:
            payload["extra"] = extra
        self.journal.append("order_snapshot", payload)

    def _emit_order_update(self, order: Order):
        self.event_engine.put(Event(EVENT_ORDER_UPDATE, order.to_snapshot()))

    def _emit_position_update(self, symbol: str):
        self.event_engine.put(
            Event(EVENT_POSITION_UPDATE, self.exposure.get_position_data(symbol))
        )

    def _remember_terminated_oid(self, oid: str):
        if not oid or oid in self.terminated_oids:
            return
        if len(self.terminated_oid_queue) >= self.TOMBSTONE_MAX:
            stale = self.terminated_oid_queue.popleft()
            self.terminated_oids.discard(stale)
        self.terminated_oid_queue.append(oid)
        self.terminated_oids.add(oid)

    def _write_tombstone(self, order: Order):
        self._remember_terminated_oid(order.client_oid)
        self._remember_terminated_oid(order.exchange_oid)
