"""Rebuild general OMS state from the durable journal."""

from __future__ import annotations

import math
from collections import OrderedDict, defaultdict
from itertools import chain

from infrastructure.logger import logger

from event.type import (
    LifecycleState,
    OMSCapabilityMode,
    OrderStatus,
    Side,
)

from .component import OMSComponent
from .execution_identity import retain_cursor_uncovered_execution_ids
from .journal import JournalCorruptionError
from .order import Order


class OMSJournalRebuilder(OMSComponent):
    """Own the deterministic journal-to-memory reconstruction pass."""

    def rebuild_from_log(self):
        stream_records = getattr(self.journal, "iter_records", None)
        records = iter(
            stream_records(respect_replay_policy=True)
            if callable(stream_records)
            else self.journal.load()
        )
        first_record = next(records, None)
        if first_record is None:
            calibration_replay = (
                self._new_rpi_calibration_replay_state()
            )
            return {
                "records": 0,
                "recovered_orders": 0,
                "recovered_active_orders": 0,
                "recovered_terminal_ids": 0,
                "pending_commands": 0,
                "last_lifecycle": None,
                "last_freeze_reason": "",
                "last_halt_reason": "",
                "manual_rearm_required": False,
                "symbol_guards": {},
                "symbol_guard_records": {},
                "venue_guards": {},
                "venue_guard_records": {},
                "strategy_guards": {},
                "strategy_symbol_guards": {},
                "mode_override": "",
                "mode_override_reason": "",
                "mode_constraint_generation": 0,
                "mode_constraints": {},
                "clean_shutdown": True,
                "dirty_shutdown": False,
                "trade_cursors": {},
                "trade_scan_end_ms": {},
                "untrusted_trade_cursor_symbols": [],
                "unverified_execution_symbols": [],
                "external_cash_flow_total": 0.0,
                "external_cash_flow_ids": [],
                "external_cash_flow_scan_end_ms": 0,
                "rpi_calibration": (
                    self._finalize_rpi_calibration_replay(
                        calibration_replay
                    )
                ),
            }

        records = chain((first_record,), records)
        latest_order_records = {}
        latest_order_record_indexes = {}
        terminal_order_oids = OrderedDict()
        pending_commands = {}
        pending_command_ids_by_client_oid = defaultdict(set)
        latest_command_results = {}
        executions_by_client_oid = {}
        replayed_execution_ids = set()
        strategy_positions = defaultdict(float)
        strategy_average_prices = defaultdict(float)
        deferred_strategy_executions = defaultdict(list)
        last_lifecycle = None
        last_freeze_reason = ""
        last_halt_reason = ""
        manual_rearm_required = False
        symbol_guards = {}
        symbol_guard_records = {}
        symbol_guard_epoch_counters = {}
        venue_guards = {}
        venue_guard_records = {}
        venue_guard_epoch_counters = {}
        strategy_guards = {}
        strategy_symbol_guards = {}
        mode_override = ""
        mode_override_reason = ""
        mode_constraint_generation = 0
        mode_constraints = {}
        trade_cursors = {}
        trade_scan_end_ms = {}
        untrusted_trade_cursor_symbols = set()
        unverified_execution_symbols = set()
        external_cash_flow_total = 0.0
        external_cash_flow_ids = set()
        external_cash_flow_scan_end_ms = 0
        calibration_replay = self._new_rpi_calibration_replay_state()
        record_count = 0
        last_record_kind = ""

        def replay_strategy_execution(
            execution_payload,
            execution_record_index,
            intent_payload=None,
        ) -> bool:
            intent = intent_payload if isinstance(intent_payload, dict) else {}
            execution_id = str(
                execution_payload.get("execution_id", "") or ""
            )
            strategy_id = str(
                execution_payload.get("strategy_id", "")
                or intent.get("strategy_id", "")
                or "exchange_recovery"
            )
            symbol = str(
                execution_payload.get("symbol", "")
                or intent.get("symbol", "")
            ).upper()
            side_value = str(
                execution_payload.get("side", "")
                or intent.get("side", "")
            )
            if not symbol or not side_value:
                return False
            try:
                side = Side(side_value)
                fill_qty = float(
                    execution_payload.get("fill_qty", 0.0) or 0.0
                )
                fill_price = float(
                    execution_payload.get("fill_price", 0.0) or 0.0
                )
            except (TypeError, ValueError) as exc:
                raise JournalCorruptionError(
                    f"Malformed strategy execution {execution_id}: {exc}"
                ) from exc
            if (
                fill_qty <= 0.0
                or fill_price <= 0.0
                or not math.isfinite(fill_qty)
                or not math.isfinite(fill_price)
            ):
                raise JournalCorruptionError(
                    "Invalid strategy execution values for "
                    f"{execution_id} at journal record "
                    f"{execution_record_index}"
                )
            self.exposure._apply_fill_to_ledger(
                strategy_positions,
                strategy_average_prices,
                (strategy_id, symbol),
                side,
                fill_qty,
                fill_price,
            )
            return True

        terminal_statuses = {
            OrderStatus.FILLED.value,
            OrderStatus.CANCELLED.value,
            OrderStatus.REJECTED.value,
            OrderStatus.REJECTED_LOCALLY.value,
            OrderStatus.EXPIRED.value,
        }
        tombstone_limit = max(1, int(self.TOMBSTONE_MAX or 1))
        for record_index, record in enumerate(records):
            record_count = record_index + 1
            payload = record.get("payload", {})
            kind = record.get("kind")
            last_record_kind = kind
            if self._replay_rpi_calibration_record(
                kind,
                payload,
                calibration_replay,
                record_index,
            ):
                continue
            if kind == "order_snapshot":
                client_oid = payload.get("client_oid")
                if client_oid:
                    latest_order_records[client_oid] = payload
                    latest_order_record_indexes[client_oid] = record_index
                    latest_command_results.pop(client_oid, None)
                    executions_by_client_oid.pop(client_oid, None)
                    pending_ids = pending_command_ids_by_client_oid.get(
                        client_oid,
                        (),
                    )
                    for command_id in tuple(pending_ids):
                        pending = pending_commands.get(command_id)
                        if pending is not None:
                            pending["snapshot_index"] = record_index
                            pending["snapshot_status"] = str(
                                payload.get("status", "") or ""
                            )

                    deferred = deferred_strategy_executions.pop(
                        client_oid,
                        (),
                    )
                    intent_payload = payload.get("intent", {})
                    for execution in deferred:
                        if not replay_strategy_execution(
                            execution["payload"],
                            execution["index"],
                            intent_payload,
                        ):
                            raise JournalCorruptionError(
                                "Execution record cannot resolve order intent "
                                f"for {client_oid} at journal record "
                                f"{execution['index']}"
                            )

                    status = str(payload.get("status", "") or "")
                    if status in terminal_statuses:
                        terminal_order_oids[client_oid] = None
                        terminal_order_oids.move_to_end(client_oid)
                        while len(terminal_order_oids) > tombstone_limit:
                            expired_oid, _ = terminal_order_oids.popitem(
                                last=False
                            )
                            latest_order_records.pop(expired_oid, None)
                            latest_order_record_indexes.pop(expired_oid, None)
                            latest_command_results.pop(expired_oid, None)
                            executions_by_client_oid.pop(expired_oid, None)
                    else:
                        terminal_order_oids.pop(client_oid, None)
            elif kind == "command_prepared":
                command_id = str(payload.get("command_id", "") or "")
                if command_id:
                    client_oid = str(payload.get("client_oid", "") or "")
                    order_snapshot = latest_order_records.get(client_oid, {})
                    entry = {
                        "index": record_index,
                        "payload": payload,
                        "client_oid": client_oid,
                        "snapshot_index": latest_order_record_indexes.get(
                            client_oid,
                            -1,
                        ),
                        "snapshot_status": str(
                            order_snapshot.get("status", "") or ""
                        ),
                    }
                    pending_commands[command_id] = entry
                    if client_oid:
                        pending_command_ids_by_client_oid[client_oid].add(
                            command_id
                        )
            elif kind == "command_result":
                command_id = str(payload.get("command_id", "") or "")
                prepared = pending_commands.pop(command_id, None)
                if prepared is not None:
                    prepared_client_oid = prepared["client_oid"]
                    pending_ids = pending_command_ids_by_client_oid.get(
                        prepared_client_oid
                    )
                    if pending_ids is not None:
                        pending_ids.discard(command_id)
                        if not pending_ids:
                            pending_command_ids_by_client_oid.pop(
                                prepared_client_oid,
                                None,
                            )
                client_oid = str(payload.get("client_oid", "") or "")
                if client_oid:
                    latest_command_results[client_oid] = {
                        "index": record_index,
                        "payload": payload,
                    }
            elif kind == "execution_record":
                client_oid = str(payload.get("client_oid", "") or "")
                try:
                    recorded_trade_id = int(payload.get("trade_id", -1))
                except (TypeError, ValueError):
                    recorded_trade_id = -1
                if (
                    recorded_trade_id < 0
                    and float(payload.get("fill_qty", 0.0) or 0.0) > 0.0
                ):
                    symbol = str(payload.get("symbol", "") or "").upper()
                    if symbol:
                        unverified_execution_symbols.add(symbol)
                execution_id = str(payload.get("execution_id", "") or "")
                if not execution_id:
                    raise JournalCorruptionError(
                        "Execution record without execution_id during "
                        f"strategy replay at journal record {record_index}"
                    )
                execution = {"index": record_index, "payload": payload}
                if client_oid:
                    executions_by_client_oid.setdefault(client_oid, []).append(
                        execution
                    )
                if execution_id in replayed_execution_ids:
                    continue
                replayed_execution_ids.add(execution_id)
                order_payload = latest_order_records.get(client_oid, {})
                if not replay_strategy_execution(
                    payload,
                    record_index,
                    order_payload.get("intent", {}),
                ):
                    deferred_strategy_executions[client_oid].append(execution)
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
                    owner = str(
                        payload.get("owner", "")
                        or self._symbol_guard_owner(reason)
                    )
                    epoch = int(payload.get("epoch", 0) or 0)
                    if epoch <= 0:
                        epoch = int(symbol_guard_epoch_counters.get(symbol, 0)) + 1
                    symbol_guard_epoch_counters[symbol] = max(
                        epoch,
                        int(symbol_guard_epoch_counters.get(symbol, 0)),
                    )
                    records_for_symbol = symbol_guard_records.setdefault(symbol, {})
                    existing_epoch = int(
                        (records_for_symbol.get(owner) or {}).get("epoch", 0)
                        or 0
                    )
                    if epoch >= existing_epoch:
                        records_for_symbol[owner] = {
                            "reason": reason,
                            "epoch": epoch,
                        }
                    newest = max(
                        records_for_symbol.values(),
                        key=lambda record: int(record.get("epoch", 0) or 0),
                    )
                    symbol_guards[symbol] = str(newest.get("reason", "") or "")
            elif kind in {"symbol_unfrozen", "symbol_guard_cleared"}:
                symbol = str(payload.get("symbol", "") or "").upper()
                if symbol:
                    records_for_symbol = symbol_guard_records.get(symbol, {})
                    owner = str(payload.get("owner", "") or "")
                    previous_reason = str(
                        payload.get("previous_reason", "") or ""
                    )
                    epoch = int(payload.get("epoch", 0) or 0)
                    if owner:
                        owner_record = records_for_symbol.get(owner)
                        if owner_record and (
                            epoch <= 0
                            or int(owner_record.get("epoch", 0) or 0) == epoch
                        ) and (
                            not previous_reason
                            or str(owner_record.get("reason", "") or "")
                            == previous_reason
                        ):
                            records_for_symbol.pop(owner, None)
                    elif previous_reason:
                        matching_owners = [
                            candidate_owner
                            for candidate_owner, record in records_for_symbol.items()
                            if str(record.get("reason", "") or "") == previous_reason
                            and (
                                epoch <= 0
                                or int(record.get("epoch", 0) or 0) == epoch
                            )
                        ]
                        for candidate_owner in matching_owners:
                            records_for_symbol.pop(candidate_owner, None)
                    else:
                        # Backward compatibility for journals written before
                        # owner-aware guards existed.
                        records_for_symbol.clear()
                    if records_for_symbol:
                        newest = max(
                            records_for_symbol.values(),
                            key=lambda record: int(record.get("epoch", 0) or 0),
                        )
                        symbol_guards[symbol] = str(
                            newest.get("reason", "") or ""
                        )
                    else:
                        symbol_guard_records.pop(symbol, None)
                        symbol_guards.pop(symbol, None)
            elif kind in {"venue_frozen", "venue_freeze_reasserted"}:
                venue = str(payload.get("venue", "") or "").upper()
                reason = str(payload.get("reason", "") or "")
                if venue and reason:
                    owner = str(
                        payload.get("owner", "")
                        or self._venue_guard_owner(reason)
                    )
                    epoch = int(payload.get("epoch", 0) or 0)
                    if epoch <= 0:
                        epoch = int(venue_guard_epoch_counters.get(venue, 0)) + 1
                    venue_guard_epoch_counters[venue] = max(
                        epoch,
                        int(venue_guard_epoch_counters.get(venue, 0)),
                    )
                    records_for_venue = venue_guard_records.setdefault(venue, {})
                    existing_epoch = int(
                        (records_for_venue.get(owner) or {}).get("epoch", 0)
                        or 0
                    )
                    if epoch >= existing_epoch:
                        records_for_venue[owner] = {
                            "reason": reason,
                            "epoch": epoch,
                        }
                    newest = max(
                        records_for_venue.values(),
                        key=lambda record: int(record.get("epoch", 0) or 0),
                    )
                    venue_guards[venue] = str(newest.get("reason", "") or "")
            elif kind in {"venue_unfrozen", "venue_guard_cleared"}:
                venue = str(payload.get("venue", "") or "").upper()
                if venue:
                    records_for_venue = venue_guard_records.get(venue, {})
                    owner = str(payload.get("owner", "") or "")
                    previous_reason = str(
                        payload.get("previous_reason", "") or ""
                    )
                    epoch = int(payload.get("epoch", 0) or 0)
                    if owner:
                        owner_record = records_for_venue.get(owner)
                        if owner_record and (
                            epoch <= 0
                            or int(owner_record.get("epoch", 0) or 0) == epoch
                        ) and (
                            not previous_reason
                            or str(owner_record.get("reason", "") or "")
                            == previous_reason
                        ):
                            records_for_venue.pop(owner, None)
                    elif previous_reason:
                        matching_owners = [
                            candidate_owner
                            for candidate_owner, record in records_for_venue.items()
                            if str(record.get("reason", "") or "")
                            == previous_reason
                            and (
                                epoch <= 0
                                or int(record.get("epoch", 0) or 0) == epoch
                            )
                        ]
                        for candidate_owner in matching_owners:
                            records_for_venue.pop(candidate_owner, None)
                    else:
                        records_for_venue.clear()
                    if records_for_venue:
                        newest = max(
                            records_for_venue.values(),
                            key=lambda record: int(record.get("epoch", 0) or 0),
                        )
                        venue_guards[venue] = str(
                            newest.get("reason", "") or ""
                        )
                    else:
                        venue_guard_records.pop(venue, None)
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
                constraint_key = str(
                    payload.get("constraint_key", "")
                    or self._mode_constraint_key(mode_override_reason)
                )
                if mode_override and mode_override_reason:
                    if "constraint_generation" in payload:
                        try:
                            constraint_generation = int(
                                payload["constraint_generation"]
                            )
                        except (TypeError, ValueError) as exc:
                            raise JournalCorruptionError(
                                "Invalid explicit mode constraint "
                                "generation at journal record "
                                f"{record_index}"
                            ) from exc
                        if (
                            constraint_generation
                            <= mode_constraint_generation
                        ):
                            raise JournalCorruptionError(
                                "Non-monotonic mode constraint generation "
                                f"at journal record {record_index}: "
                                f"{constraint_generation}<="
                                f"{mode_constraint_generation}"
                            )
                    else:
                        constraint_generation = (
                            mode_constraint_generation + 1
                        )
                    mode_constraint_generation = constraint_generation
                    mode_constraints[constraint_key] = {
                        "mode": mode_override,
                        "reason": mode_override_reason,
                        "generation": constraint_generation,
                    }
            elif kind == "trading_mode_override_cleared":
                cleared_keys = payload.get("cleared_constraint_keys", []) or []
                cleared_generations = payload.get(
                    "cleared_constraint_generations",
                    {},
                )
                if not isinstance(cleared_generations, dict):
                    cleared_generations = {}
                if cleared_keys:
                    for constraint_key in cleared_keys:
                        constraint_key = str(constraint_key)
                        current = mode_constraints.get(constraint_key)
                        expected_generation = cleared_generations.get(
                            constraint_key
                        )
                        if (
                            expected_generation is not None
                            and current is not None
                            and int(current.get("generation", 0) or 0)
                            != int(expected_generation)
                        ):
                            continue
                        mode_constraints.pop(constraint_key, None)
                else:
                    previous_reason = str(payload.get("previous_reason", "") or "")
                    if previous_reason:
                        mode_constraints.pop(
                            self._mode_constraint_key(previous_reason),
                            None,
                        )
                    else:
                        mode_constraints.clear()
                if mode_constraints:
                    _selected_key, selected = max(
                        mode_constraints.items(),
                        key=lambda item: (
                            self._mode_rank(OMSCapabilityMode(item[1]["mode"])),
                            item[0],
                        ),
                    )
                    mode_override = selected["mode"]
                    mode_override_reason = selected["reason"]
                else:
                    mode_override = ""
                    mode_override_reason = ""
            elif kind == "oms_stopped":
                state = payload.get("state")
                if state:
                    last_lifecycle = state
                if payload.get("manual_rearm_required") is True:
                    manual_rearm_required = True
            elif kind == "trade_cursor_advanced":
                symbol = str(payload.get("symbol", "") or "").upper()
                trade_id = int(payload.get("trade_id", -1))
                source = str(payload.get("source", "") or "")
                if symbol and source == "user_stream":
                    untrusted_trade_cursor_symbols.add(symbol)
                elif symbol:
                    trade_cursors[symbol] = max(trade_cursors.get(symbol, -1), trade_id)
            elif kind == "trade_scan_completed":
                symbol = str(payload.get("symbol", "") or "").upper()
                end_time_ms = int(payload.get("end_time_ms", 0))
                if symbol:
                    trade_scan_end_ms[symbol] = max(
                        trade_scan_end_ms.get(symbol, 0),
                        end_time_ms,
                    )
            elif kind == "external_cash_flow_record":
                income_id = str(payload.get("income_id", "") or "")
                if income_id in external_cash_flow_ids:
                    continue
                try:
                    cash_flow_amount = float(
                        payload.get("amount", 0.0) or 0.0
                    )
                except (TypeError, ValueError) as exc:
                    raise JournalCorruptionError(
                        "Invalid external cash-flow amount at journal "
                        f"record {record_index}"
                    ) from exc
                if not math.isfinite(cash_flow_amount):
                    raise JournalCorruptionError(
                        "Non-finite external cash-flow amount at journal "
                        f"record {record_index}"
                    )
                external_cash_flow_ids.add(income_id)
                external_cash_flow_total += cash_flow_amount
                if not math.isfinite(external_cash_flow_total):
                    raise JournalCorruptionError(
                        "External cash-flow total overflow at journal "
                        f"record {record_index}"
                    )
            elif kind == "cash_flow_scan_completed":
                external_cash_flow_scan_end_ms = max(
                    external_cash_flow_scan_end_ms,
                    int(payload.get("end_time_ms", 0) or 0),
                )

        if deferred_strategy_executions:
            unresolved = next(iter(deferred_strategy_executions.values()))[0]
            raise JournalCorruptionError(
                "Execution record cannot resolve symbol/side at journal "
                f"record {unresolved['index']}"
            )

        ambiguous_statuses = {
            OrderStatus.SUBMITTING.value,
            OrderStatus.SUBMIT_UNKNOWN.value,
            OrderStatus.CANCELLING.value,
            OrderStatus.CANCEL_UNKNOWN.value,
        }
        pending_command_count = sum(
            prepared["snapshot_index"] <= prepared["index"]
            or prepared["snapshot_status"] in ambiguous_statuses
            for prepared in pending_commands.values()
        )
        retained_execution_ids = retain_cursor_uncovered_execution_ids(
            replayed_execution_ids,
            trade_cursors,
        )
        compacted_execution_id_count = (
            len(replayed_execution_ids) - len(retained_execution_ids)
        )

        with self.lock:
            self.orders.clear()
            self.exchange_id_map.clear()
            self.execution_ids.clear()
            self.terminated_oids.clear()
            self.terminated_oid_queue.clear()
            self.exposure.strategy_net_positions.clear()
            self.exposure.strategy_avg_prices.clear()
            self.exposure.strategy_open_buy_qty.clear()
            self.exposure.strategy_open_sell_qty.clear()
            self.exposure.strategy_net_positions.update(strategy_positions)
            self.exposure.strategy_avg_prices.update(strategy_average_prices)
            self.execution_ids.update(retained_execution_ids)
            recovered_terminal_ids = 0
            recovered_active_orders = 0
            for client_oid, payload in latest_order_records.items():
                try:
                    order = Order.from_record(payload)
                except (KeyError, TypeError, ValueError) as exc:
                    raise JournalCorruptionError(
                        f"Invalid order snapshot for {client_oid}: {exc}"
                    ) from exc

                trailing_result = latest_command_results.get(client_oid)
                if (
                    trailing_result
                    and trailing_result["index"] > latest_order_record_indexes[client_oid]
                ):
                    self._apply_recovered_command_result(
                        order,
                        trailing_result["payload"],
                    )

                for execution in executions_by_client_oid.get(client_oid, []):
                    self._apply_recovered_execution(
                        order,
                        execution["payload"],
                    )

                # PREPARED/SUBMITTING and PREPARED/CANCELLING are deliberately
                # ambiguous after a process crash. They must be queried by the
                # durable idempotency key and never blindly resent.
                if order.status == OrderStatus.SUBMITTING:
                    order.mark_submit_unknown("recovered_inflight_submit")
                elif order.status == OrderStatus.CANCELLING:
                    order.mark_cancel_unknown("recovered_inflight_cancel")

                if order.is_active():
                    self.orders[order.client_oid] = order
                    if order.exchange_oid:
                        self.exchange_id_map[order.exchange_oid] = order
                    self.order_monitor.recover_order(order)
                    recovered_active_orders += 1
                    continue

                if order.is_terminal():
                    if order.client_oid:
                        self._remember_terminated_oid(order.client_oid)
                        recovered_terminal_ids += 1
                    if order.exchange_oid:
                        self._remember_terminated_oid(order.exchange_oid)
                        recovered_terminal_ids += 1

            self.execution_ids.intersection_update(retained_execution_ids)
            self.exposure.update_open_orders(self.orders)
            self.account.calculate()

        clean_shutdown = last_record_kind == "oms_stopped"
        summary = {
            "records": record_count,
            "recovered_orders": len(latest_order_records),
            "recovered_active_orders": recovered_active_orders,
            "recovered_terminal_ids": recovered_terminal_ids,
            "pending_commands": pending_command_count,
            "retained_execution_ids": len(retained_execution_ids),
            "compacted_execution_ids": compacted_execution_id_count,
            "last_lifecycle": last_lifecycle,
            "last_freeze_reason": last_freeze_reason,
            "last_halt_reason": last_halt_reason,
            "manual_rearm_required": manual_rearm_required,
            "symbol_guards": symbol_guards,
            "symbol_guard_records": symbol_guard_records,
            "venue_guards": venue_guards,
            "venue_guard_records": venue_guard_records,
            "strategy_guards": strategy_guards,
            "strategy_symbol_guards": strategy_symbol_guards,
            "mode_override": mode_override,
            "mode_override_reason": mode_override_reason,
            "mode_constraint_generation": mode_constraint_generation,
            "mode_constraints": mode_constraints,
            "clean_shutdown": clean_shutdown,
            "dirty_shutdown": not clean_shutdown,
            "trade_cursors": trade_cursors,
            "trade_scan_end_ms": trade_scan_end_ms,
            "untrusted_trade_cursor_symbols": sorted(
                untrusted_trade_cursor_symbols
            ),
            "unverified_execution_symbols": sorted(
                unverified_execution_symbols
            ),
            "external_cash_flow_total": external_cash_flow_total,
            "external_cash_flow_ids": sorted(external_cash_flow_ids),
            "external_cash_flow_scan_end_ms": external_cash_flow_scan_end_ms,
            "rpi_calibration": self._finalize_rpi_calibration_replay(
                calibration_replay,
                dirty_shutdown=not clean_shutdown,
            ),
        }
        if recovered_terminal_ids:
            logger.info(
                f"[OMS] Recovered {recovered_terminal_ids} terminal IDs from journal"
            )
        return summary
