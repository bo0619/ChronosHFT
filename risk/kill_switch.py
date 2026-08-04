"""Durable kill-switch supervision independent of RiskManager."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

from infrastructure.logger import logger
from risk.state_repository import RESUMABLE_KILL_STATES


@dataclass(frozen=True, slots=True)
class KillSwitchConfig:
    verify_interval_sec: float
    verify_timeout_sec: float
    flatten_retry_sec: float
    empty_snapshots_required: int


@dataclass(slots=True)
class KillSwitchRuntimeState:
    empty_order_snapshots: int = 0
    empty_flat_snapshots: int = 0
    last_accepted_snapshot_at: float = 0.0
    state_lock: threading.RLock = field(default_factory=threading.RLock)
    verification_lock: threading.Lock = field(default_factory=threading.Lock)
    supervisor_thread: threading.Thread | None = None
    supervisor_lock: threading.Lock = field(default_factory=threading.Lock)


class KillSwitchField:
    """Expose one declared controller field through the compatibility facade."""

    def __init__(self, attribute: str):
        self.attribute = attribute

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return getattr(instance.kill_switch, self.attribute)

    def __set__(self, instance, value) -> None:
        setattr(instance.kill_switch, self.attribute, value)


class KillSwitchMethod:
    """Bind a RiskManager method name to its kill-switch controller."""

    def __init__(self, method_name: str | None = None):
        self.method_name = method_name

    def __set_name__(self, owner, name: str) -> None:
        if self.method_name is None:
            self.method_name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return getattr(instance.kill_switch, self.method_name)

    def __set__(self, instance, value) -> None:
        setattr(instance.kill_switch, self.method_name, value)


class RiskKillSwitchController:
    """Own the durable kill sequence, verification state and worker."""

    RESUMABLE_KILL_STATES = RESUMABLE_KILL_STATES

    def __init__(
        self,
        *,
        oms,
        gateway,
        risk_state_repository,
        root_config: dict,
        frozen_symbols: dict,
        config: KillSwitchConfig,
    ) -> None:
        self.oms = oms
        self.gateway = gateway
        self.risk_state_repository = risk_state_repository
        self.root_config = root_config
        self.frozen_symbols = frozen_symbols
        self.config = config
        self.runtime = KillSwitchRuntimeState()

    @property
    def kill_switch_triggered(self) -> bool:
        return self.risk_state_repository.state.kill_switch_triggered

    @kill_switch_triggered.setter
    def kill_switch_triggered(self, value: bool) -> None:
        self.risk_state_repository.state.kill_switch_triggered = value

    @property
    def kill_state(self) -> str:
        return self.risk_state_repository.state.kill_state

    @kill_state.setter
    def kill_state(self, value: str) -> None:
        self.risk_state_repository.state.kill_state = value

    @property
    def kill_reason(self) -> str:
        return self.risk_state_repository.state.kill_reason

    @kill_reason.setter
    def kill_reason(self, value: str) -> None:
        self.risk_state_repository.state.kill_reason = value

    @property
    def kill_verify_interval_sec(self) -> float:
        return self.config.verify_interval_sec

    @property
    def kill_verify_timeout_sec(self) -> float:
        return self.config.verify_timeout_sec

    @property
    def kill_flatten_retry_sec(self) -> float:
        return self.config.flatten_retry_sec

    @property
    def kill_empty_snapshots_required(self) -> int:
        return self.config.empty_snapshots_required

    @property
    def _kill_empty_order_snapshots(self) -> int:
        return self.runtime.empty_order_snapshots

    @_kill_empty_order_snapshots.setter
    def _kill_empty_order_snapshots(self, value: int) -> None:
        self.runtime.empty_order_snapshots = value

    @property
    def _kill_empty_flat_snapshots(self) -> int:
        return self.runtime.empty_flat_snapshots

    @_kill_empty_flat_snapshots.setter
    def _kill_empty_flat_snapshots(self, value: int) -> None:
        self.runtime.empty_flat_snapshots = value

    @property
    def _kill_last_accepted_snapshot_at(self) -> float:
        return self.runtime.last_accepted_snapshot_at

    @_kill_last_accepted_snapshot_at.setter
    def _kill_last_accepted_snapshot_at(self, value: float) -> None:
        self.runtime.last_accepted_snapshot_at = value

    @property
    def _kill_state_lock(self):
        return self.runtime.state_lock

    @property
    def _kill_verification_lock(self):
        return self.runtime.verification_lock

    @property
    def _kill_supervisor_thread(self):
        return self.runtime.supervisor_thread

    @_kill_supervisor_thread.setter
    def _kill_supervisor_thread(self, value) -> None:
        self.runtime.supervisor_thread = value

    @property
    def _kill_supervisor_lock(self):
        return self.runtime.supervisor_lock

    def _refresh_rearm_state(self):
        if not self.kill_switch_triggered or self.oms is None:
            return
        if self.kill_state != "FLAT_VERIFIED":
            return
        state = getattr(getattr(self.oms, "state", None), "value", "")
        manual_rearm_required = bool(
            getattr(self.oms, "manual_rearm_required", False)
        )
        if state != "LIVE" or manual_rearm_required:
            return
        self.kill_switch_triggered = False
        self.kill_reason = ""
        self.kill_state = "ARMED"
        self._reset_kill_empty_snapshots()
        self.risk_state_repository.persist("oms_manual_rearm_completed")

    def can_operator_rearm(self) -> bool:
        return bool(
            not self.kill_switch_triggered
            or self.kill_state == "FLAT_VERIFIED"
        )

    def acknowledge_operator_rearm(self) -> bool:
        self._refresh_rearm_state()
        return not self.kill_switch_triggered

    def resume_kill_switch_supervision(self) -> bool:
        """Resume an interrupted durable kill sequence after dependencies are ready."""
        if not self.kill_switch_triggered:
            return False
        if self.kill_state in {"ARMED", "FLAT_VERIFIED"}:
            return False
        if self.kill_state not in self.RESUMABLE_KILL_STATES:
            logger.critical(
                f"[KillSwitch] Invalid recovered state {self.kill_state!r}; "
                "resuming fail-closed"
            )
            self._set_kill_state("FAILED", "invalid_recovered_kill_state")

        recovered_state = self.kill_state
        if recovered_state == "FAILED":
            with self._kill_state_lock:
                self._reset_kill_empty_snapshots()
                self.kill_state = "TRIGGERED"
            self.risk_state_repository.persist(
                "kill_supervision_retry_after_failure"
            )

        logger.critical(
            f"[KillSwitch] Resuming interrupted sequence from {recovered_state}: "
            f"{self.kill_reason or 'recovered kill switch'}"
        )
        self.risk_state_repository.persist("kill_supervision_resumed")
        if not self._verify_kill_state_safely(allow_flatten=True):
            self._start_kill_supervisor()
        return True

    def trigger_kill_switch(self, reason: str):
        with self._kill_state_lock:
            if self.kill_switch_triggered:
                return
            self._reset_kill_empty_snapshots()
            self.kill_switch_triggered = True
            self.kill_reason = reason
            self.kill_state = "TRIGGERED"
        self.risk_state_repository.persist("kill_triggered")
        logger.critical(f"KILL SWITCH TRIGGERED: {reason}")

        if self.oms:
            try:
                self.oms.halt_system(f"KillSwitch: {reason}")
            except Exception as exc:
                logger.error(f"[KillSwitch] oms.halt_system failed: {exc}")

        try:
            self._retry_mass_cancel()
            self._set_kill_state("CANCEL_PENDING", "mass_cancel_requested")
        except Exception as exc:
            logger.error(
                "[KillSwitch] initial mass cancel failed unexpectedly: "
                f"{type(exc).__name__}:{exc}"
            )
        finally:
            if not self._verify_kill_state_safely(allow_flatten=True):
                self._start_kill_supervisor()

    def restart_kill_switch_after_truth_drift(self, reason: str) -> bool:
        """Re-arm kill supervision when a supposedly flat account drifts."""
        reason = str(reason or "account truth drift after flat verification")
        with self._kill_state_lock:
            self._reset_kill_empty_snapshots()
            self.kill_switch_triggered = True
            self.kill_reason = reason
            self.kill_state = "TRIGGERED"
        self.risk_state_repository.persist(
            "kill_restarted_after_truth_drift"
        )
        logger.critical(
            "KILL SWITCH RESTARTED AFTER ACCOUNT TRUTH DRIFT: "
            f"{reason}"
        )

        if self.oms:
            try:
                self.oms.halt_system(f"KillSwitchRestart: {reason}")
            except Exception as exc:
                logger.error(
                    "[KillSwitch] OMS halt failed during truth-drift restart: "
                    f"{exc}"
                )

        try:
            self._retry_mass_cancel()
            self._set_kill_state(
                "CANCEL_PENDING",
                "truth_drift_mass_cancel_requested",
            )
        except Exception as exc:
            logger.error(
                "[KillSwitch] truth-drift mass cancel failed unexpectedly: "
                f"{type(exc).__name__}:{exc}"
            )
        finally:
            if not self._verify_kill_state_safely(allow_flatten=True):
                self._start_kill_supervisor()
        return True

    def _set_kill_state(self, state: str, reason: str):
        with self._kill_state_lock:
            if self.kill_state == state:
                return
            self.kill_state = state
        self.risk_state_repository.persist(reason)
        logger.warning(f"[KillSwitch] State -> {state}: {reason}")

    def _start_kill_supervisor(self):
        with self._kill_supervisor_lock:
            if self._kill_supervisor_thread and self._kill_supervisor_thread.is_alive():
                return
            self._kill_supervisor_thread = threading.Thread(
                target=self._supervise_kill_switch,
                daemon=True,
                name="RiskKillSupervisor",
            )
            self._kill_supervisor_thread.start()

    def _supervise_kill_switch(self):
        deadline = time.perf_counter() + self.kill_verify_timeout_sec
        next_flatten_at = time.perf_counter() + self.kill_flatten_retry_sec
        timeout_reported = False
        while self.kill_switch_triggered:
            now = time.perf_counter()
            allow_flatten = now >= next_flatten_at
            if self._verify_kill_state_safely(allow_flatten=allow_flatten):
                return
            if allow_flatten:
                next_flatten_at = now + self.kill_flatten_retry_sec
            if now >= deadline and not timeout_reported:
                self._set_kill_state(
                    "FAILED",
                    "kill_verification_timeout_continuing",
                )
                logger.critical(
                    "[KillSwitch] Initial flat-state verification timed "
                    f"out after {self.kill_verify_timeout_sec:.1f}s; "
                    "cancellation and flatten supervision will continue"
                )
                timeout_reported = True
            time.sleep(self.kill_verify_interval_sec)

    def _verify_kill_state_safely(self, allow_flatten: bool) -> bool:
        with self._kill_verification_lock:
            try:
                return bool(
                    self._verify_kill_state_once(allow_flatten=allow_flatten)
                )
            except Exception as exc:
                self._reset_kill_empty_snapshots()
                logger.error(
                    "[KillSwitch] verification attempt failed; "
                    "supervision remains active: "
                    f"{type(exc).__name__}:{exc}"
                )
                return False

    def _verify_kill_state_once(self, allow_flatten: bool) -> bool:
        open_orders = self._query_kill_open_orders()
        if open_orders is None:
            self._reset_kill_empty_snapshots()
            if allow_flatten:
                self._retry_mass_cancel()
            return False

        local_order_truth = self._query_local_kill_order_truth()
        if local_order_truth is None:
            self._reset_kill_empty_snapshots()
            self._set_kill_state("CANCEL_PENDING", "local_order_truth_unknown")
            if allow_flatten:
                self._retry_mass_cancel(remote_orders=open_orders)
            return False

        local_active_orders = local_order_truth["active_orders"]
        order_sends_inflight = local_order_truth["order_sends_inflight"]
        if open_orders:
            self._reset_kill_empty_snapshots()
            self._set_kill_state("CANCEL_PENDING", "open_orders_remain")
            if allow_flatten:
                self._retry_mass_cancel(
                    remote_orders=open_orders,
                    local_orders=local_active_orders,
                )
            return False
        if local_active_orders or order_sends_inflight:
            self._reset_kill_empty_snapshots()
            self._set_kill_state(
                "CANCEL_PENDING",
                "local_orders_or_sends_remain",
            )
            if allow_flatten:
                self._retry_mass_cancel(local_orders=local_active_orders)
            return False

        now = time.perf_counter()
        if (
            self._kill_last_accepted_snapshot_at > 0.0
            and now - self._kill_last_accepted_snapshot_at
            < self.kill_verify_interval_sec
        ):
            self._set_kill_state(
                "CANCEL_PENDING",
                "empty_snapshot_interval_pending",
            )
            return False
        self._kill_last_accepted_snapshot_at = now
        self._kill_empty_order_snapshots = min(
            self.kill_empty_snapshots_required,
            self._kill_empty_order_snapshots + 1,
        )
        positions = self._query_kill_positions()
        if positions is None:
            self._kill_empty_flat_snapshots = 0
            return False
        if positions:
            self._kill_empty_flat_snapshots = 0
            self._set_kill_state("FLATTENING", "nonzero_positions_remain")
            if (
                allow_flatten
                and self.oms
                and hasattr(self.oms, "emergency_reduce_only_flatten")
            ):
                try:
                    self.oms.emergency_reduce_only_flatten(
                        f"KillSwitch: {self.kill_reason}"
                    )
                except Exception as exc:
                    logger.error(f"[KillSwitch] emergency flatten failed: {exc}")
            return False

        self._kill_empty_flat_snapshots = min(
            self.kill_empty_snapshots_required,
            self._kill_empty_flat_snapshots + 1,
        )
        required = self.kill_empty_snapshots_required
        if self._kill_empty_order_snapshots < required:
            self._set_kill_state(
                "CANCEL_PENDING",
                "empty_order_snapshot_confirmation_pending",
            )
            return False
        self._set_kill_state("CANCEL_VERIFIED", "orders_cancelled_confirmed")
        if self._kill_empty_flat_snapshots < required:
            return False

        self._set_kill_state(
            "FLAT_VERIFIED",
            "orders_cancelled_and_positions_flat_confirmed",
        )
        return True

    def _reset_kill_empty_snapshots(self):
        self._kill_empty_order_snapshots = 0
        self._kill_empty_flat_snapshots = 0
        self._kill_last_accepted_snapshot_at = 0.0

    def _query_local_kill_order_truth(self):
        if self.oms is None:
            return {
                "active_orders": [],
                "order_sends_inflight": 0,
            }
        query = getattr(self.oms, "get_local_order_truth_snapshot", None)
        if not callable(query):
            logger.error("[KillSwitch] OMS local order truth snapshot is unavailable")
            return None
        try:
            snapshot = query()
        except Exception as exc:
            logger.error(
                "[KillSwitch] OMS local order truth snapshot failed: "
                f"{type(exc).__name__}:{exc}"
            )
            return None
        if not isinstance(snapshot, dict):
            logger.error(
                "[KillSwitch] OMS local order truth snapshot returned invalid "
                f"container: {type(snapshot).__name__}"
            )
            return None

        active_orders = snapshot.get("active_orders")
        order_sends_inflight = snapshot.get("order_sends_inflight")
        if not isinstance(active_orders, (list, tuple)):
            logger.error(
                "[KillSwitch] OMS local active-order truth is invalid: "
                f"{type(active_orders).__name__}"
            )
            return None
        if (
            isinstance(order_sends_inflight, bool)
            or not isinstance(order_sends_inflight, int)
            or order_sends_inflight < 0
        ):
            logger.error(
                "[KillSwitch] OMS outbound send truth is invalid: "
                f"{order_sends_inflight!r}"
            )
            return None
        for index, payload in enumerate(active_orders):
            if not isinstance(payload, dict):
                logger.error(
                    "[KillSwitch] OMS local active-order row is invalid at "
                    f"index {index}: {type(payload).__name__}"
                )
                return None
            symbol = str(payload.get("symbol", "") or "").upper().strip()
            if not symbol:
                logger.error(
                    "[KillSwitch] OMS local active-order row has no symbol at "
                    f"index {index}"
                )
                return None
        return {
            "active_orders": list(active_orders),
            "order_sends_inflight": order_sends_inflight,
        }

    def _query_kill_open_orders(self):
        query = getattr(self.gateway, "get_open_orders", None)
        if not callable(query):
            return None
        try:
            remote_orders = (
                query(emergency=True)
                if bool(
                    getattr(
                        self.gateway,
                        "supports_emergency_query_priority",
                        False,
                    )
                )
                else query()
            )
        except Exception as exc:
            logger.error(f"[KillSwitch] open-order verification failed: {exc}")
            return None
        if not isinstance(remote_orders, (list, tuple)):
            logger.error(
                "[KillSwitch] open-order verification returned invalid "
                f"container: {type(remote_orders).__name__}"
            )
            return None
        try:
            for index, payload in enumerate(remote_orders):
                if not isinstance(payload, dict):
                    logger.error(
                        "[KillSwitch] open-order verification returned "
                        "invalid row at index "
                        f"{index}: {type(payload).__name__}"
                    )
                    return None
                if "symbol" not in payload:
                    logger.error(
                        "[KillSwitch] open-order verification row is missing "
                        f"symbol at index {index}"
                    )
                    return None
                symbol = str(payload["symbol"] or "").upper().strip()
                if not symbol:
                    logger.error(
                        "[KillSwitch] open-order verification row contains "
                        f"an empty symbol at index {index}"
                    )
                    return None
        except Exception as exc:
            logger.error(
                "[KillSwitch] open-order verification payload could not be "
                f"validated: {type(exc).__name__}:{exc}"
            )
            return None
        return remote_orders

    def _query_kill_positions(self):
        query = getattr(self.gateway, "get_all_positions", None)
        if not callable(query):
            return None
        try:
            remote_positions = (
                query(emergency=True)
                if bool(
                    getattr(
                        self.gateway,
                        "supports_emergency_query_priority",
                        False,
                    )
                )
                else query()
            )
        except Exception as exc:
            logger.error(f"[KillSwitch] position verification failed: {exc}")
            return None
        if remote_positions is None:
            return None

        try:
            return self._collect_nonzero_kill_positions(remote_positions)
        except Exception as exc:
            logger.error(
                "[KillSwitch] position verification payload could not be "
                f"validated: {type(exc).__name__}:{exc}"
            )
            return None

    def _collect_nonzero_kill_positions(self, remote_positions):
        if not isinstance(remote_positions, (list, tuple)):
            logger.error(
                "[KillSwitch] position verification returned invalid "
                f"container: {type(remote_positions).__name__}"
            )
            return None

        nonzero = set()
        for index, payload in enumerate(remote_positions):
            if not isinstance(payload, dict):
                logger.error(
                    "[KillSwitch] position verification returned invalid "
                    f"row at index {index}: {type(payload).__name__}"
                )
                return None
            if "symbol" not in payload or "positionAmt" not in payload:
                logger.error(
                    "[KillSwitch] position verification row is missing "
                    f"required fields at index {index}"
                )
                return None
            try:
                symbol = str(payload["symbol"] or "").upper().strip()
                amount = float(payload["positionAmt"])
            except (TypeError, ValueError, OverflowError) as exc:
                logger.error(
                    "[KillSwitch] position verification row is invalid "
                    f"at index {index}: {type(exc).__name__}"
                )
                return None
            except Exception as exc:
                logger.error(
                    "[KillSwitch] position verification row could not be "
                    f"decoded at index {index}: {type(exc).__name__}"
                )
                return None
            if not symbol or not math.isfinite(amount):
                logger.error(
                    "[KillSwitch] position verification row contains "
                    f"non-finite or empty values at index {index}"
                )
                return None
            if amount != 0.0:
                nonzero.add(symbol)

        if self.oms:
            try:
                exposure = getattr(self.oms, "exposure", None)
                local_positions = getattr(exposure, "net_positions", None)
            except Exception as exc:
                logger.error(
                    "[KillSwitch] local position ledger is unavailable: "
                    f"{type(exc).__name__}:{exc}"
                )
                return None
            if not isinstance(local_positions, dict):
                logger.error(
                    "[KillSwitch] local position ledger is invalid: "
                    f"{type(local_positions).__name__}"
                )
                return None
            for raw_symbol, raw_volume in local_positions.items():
                try:
                    symbol = str(raw_symbol or "").upper().strip()
                    volume = float(raw_volume)
                except (TypeError, ValueError, OverflowError) as exc:
                    logger.error(
                        "[KillSwitch] local position ledger entry is invalid: "
                        f"{type(exc).__name__}"
                    )
                    return None
                except Exception as exc:
                    logger.error(
                        "[KillSwitch] local position ledger entry could not "
                        f"be decoded: {type(exc).__name__}"
                    )
                    return None
                if not symbol or not math.isfinite(volume):
                    logger.error(
                        "[KillSwitch] local position ledger contains "
                        "non-finite or empty values"
                    )
                    return None
                if volume != 0.0:
                    nonzero.add(symbol)
        return nonzero

    def _collect_kill_cancel_symbols(self, remote_orders=None, local_orders=None):
        symbols = set()

        try:
            root_config = getattr(self, "root_config", None)
            configured = (
                root_config.get("symbols")
                if isinstance(root_config, dict)
                else None
            )
            if not isinstance(configured, (list, tuple, set)):
                raise TypeError(
                    f"invalid root symbols: {type(configured).__name__}"
                )
            symbols.update(configured)
        except Exception as exc:
            logger.error(
                "[KillSwitch] root symbol discovery failed: "
                f"{type(exc).__name__}:{exc}"
            )

        try:
            frozen_symbols = getattr(self, "frozen_symbols", {})
            if isinstance(frozen_symbols, dict):
                symbols.update(frozen_symbols.keys())
            else:
                logger.error(
                    "[KillSwitch] frozen symbol ledger is invalid: "
                    f"{type(frozen_symbols).__name__}"
                )
        except Exception as exc:
            logger.error(
                "[KillSwitch] frozen symbol ledger is unavailable: "
                f"{type(exc).__name__}:{exc}"
            )

        if self.oms:
            try:
                oms_config = getattr(self.oms, "config", None)
                configured = (
                    oms_config.get("symbols")
                    if isinstance(oms_config, dict)
                    else None
                )
                if not isinstance(configured, (list, tuple, set)):
                    raise TypeError(
                        f"invalid configured symbols: {type(configured).__name__}"
                    )
                symbols.update(configured)
            except Exception as exc:
                logger.error(
                    "[KillSwitch] configured symbol discovery failed: "
                    f"{type(exc).__name__}:{exc}"
                )
            try:
                exposure = getattr(self.oms, "exposure", None)
                positions = getattr(exposure, "net_positions", None)
                if not isinstance(positions, dict):
                    raise TypeError(
                        f"invalid position ledger: {type(positions).__name__}"
                    )
                symbols.update(positions.keys())
            except Exception as exc:
                logger.error(
                    "[KillSwitch] position symbol discovery failed: "
                    f"{type(exc).__name__}:{exc}"
                )
            try:
                get_known_symbols = getattr(
                    self.oms,
                    "get_known_account_order_symbols",
                    None,
                )
                if not callable(get_known_symbols):
                    raise TypeError("known account symbol snapshot unavailable")
                known_symbols = get_known_symbols()
                if not isinstance(known_symbols, (list, tuple, set)):
                    raise TypeError(
                        "invalid known account symbols: "
                        f"{type(known_symbols).__name__}"
                    )
                symbols.update(known_symbols)
            except Exception as exc:
                logger.error(
                    "[KillSwitch] known account symbol discovery failed: "
                    f"{type(exc).__name__}:{exc}"
                )

        for source_name, orders in (
            ("remote", remote_orders),
            ("local", local_orders),
        ):
            if orders is None:
                continue
            if not isinstance(orders, (list, tuple)):
                logger.error(
                    f"[KillSwitch] {source_name} cancel symbol source is invalid: "
                    f"{type(orders).__name__}"
                )
                continue
            for index, payload in enumerate(orders):
                if not isinstance(payload, dict):
                    logger.error(
                        f"[KillSwitch] {source_name} cancel symbol row is invalid "
                        f"at index {index}: {type(payload).__name__}"
                    )
                    continue
                symbols.add(payload.get("symbol", ""))

        normalized = set()
        for raw_symbol in symbols:
            try:
                symbol = str(raw_symbol or "").upper().strip()
            except Exception as exc:
                logger.error(
                    "[KillSwitch] cancel symbol could not be decoded: "
                    f"{type(exc).__name__}:{exc}"
                )
                continue
            if symbol:
                normalized.add(symbol)
        return normalized

    def _retry_mass_cancel(self, remote_orders=None, local_orders=None):
        if not self.gateway:
            return
        symbols = self._collect_kill_cancel_symbols(
            remote_orders=remote_orders,
            local_orders=local_orders,
        )
        for symbol in symbols:
            try:
                self.gateway.cancel_all_orders(symbol)
            except Exception as exc:
                logger.error(f"[KillSwitch] cancel retry failed for {symbol}: {exc}")
