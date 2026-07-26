import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "oms" / "engine.py"
OMS_IMPLEMENTATION = (
    ROOT / "oms" / "durability_manager.py",
    ROOT / "oms" / "exchange_event_processor.py",
    ROOT / "oms" / "initializer.py",
    ROOT / "oms" / "order_submission.py",
    ROOT / "oms" / "submit_settlement.py",
    ROOT / "oms" / "cancellation_manager.py",
    ROOT / "oms" / "account_truth.py",
    ROOT / "oms" / "lifecycle_controller.py",
    ROOT / "oms" / "rpi_calibration_runtime.py",
    ENGINE,
)
BASE_GATEWAY = ROOT / "gateway" / "base_gateway.py"
REST_API = ROOT / "gateway" / "binance" / "rest_api.py"
LIVE_GATEWAY = ROOT / "gateway" / "binance" / "gateway.py"
PAPER_GATEWAY = ROOT / "gateway" / "binance" / "paper_gateway.py"
SYSTEM_HEALTH = ROOT / "infrastructure" / "system_health.py"
MAIN = ROOT / "main.py"
RISK_SUPERVISOR = ROOT / "risk" / "independent_supervisor.py"
RPI_MANAGER = ROOT / "oms" / "rpi_calibration_manager.py"


def _oms_implementation_source() -> str:
    chunks = ["from __future__ import annotations\n"]
    for path in OMS_IMPLEMENTATION:
        source = path.read_text(encoding="utf-8").replace(
            "from __future__ import annotations\n",
            "",
            1,
        )
        chunks.append(f"\n# source: {path.name}\n{source}\n")
    return "".join(chunks)


def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _source_for(node: ast.AST, source: str) -> str:
    segment = ast.get_source_segment(source, node)
    assert segment is not None
    return segment


def _contains_marker(node: ast.AST, marker: str) -> bool:
    return any(
        isinstance(candidate, ast.Constant)
        and candidate.value == marker
        for candidate in ast.walk(node)
    )


def _span(node: ast.AST) -> int:
    return int(node.end_lineno or node.lineno) - int(node.lineno)


def _terminal_invalidation_count(node: ast.AST) -> int:
    count = 0
    for candidate in ast.walk(node):
        if not isinstance(candidate, ast.Call):
            continue
        if not isinstance(candidate.func, ast.Attribute):
            continue
        if candidate.func.attr != "_schedule_rpi_calibration_runtime_enforcement":
            continue
        if any(
            keyword.arg == "terminal_truth_changed"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in candidate.keywords
        ):
            count += 1
    return count


def _terminal_invalidation_calls(node: ast.AST) -> list[ast.Call]:
    return [
        candidate
        for candidate in ast.walk(node)
        if isinstance(candidate, ast.Call)
        and isinstance(candidate.func, ast.Attribute)
        and candidate.func.attr
        == "_schedule_rpi_calibration_runtime_enforcement"
        and any(
            keyword.arg == "terminal_truth_changed"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in candidate.keywords
        )
    ]


def _marker_line(node: ast.AST, marker: str) -> int:
    return min(
        candidate.lineno
        for candidate in ast.walk(node)
        if isinstance(candidate, ast.Constant)
        and candidate.value == marker
    )


def test_expired_permit_invalidates_terminal_proof_on_uncertain_order_truth():
    source = _oms_implementation_source()
    tree = ast.parse(source)
    apply_event = _function(tree, "_apply_event")

    unknown_branches = [
        node
        for node in ast.walk(apply_event)
        if isinstance(node, ast.If)
        and _contains_marker(node, "late_tombstone_truth_conflict")
        and _contains_marker(node, "unknown_order_update")
    ]
    assert unknown_branches
    smallest_unknown = min(unknown_branches, key=_span)
    unknown_calls = _terminal_invalidation_calls(smallest_unknown)
    assert len(unknown_calls) == 1
    assert unknown_calls[0].lineno < _marker_line(
        smallest_unknown,
        "late_tombstone_truth_conflict",
    )
    assert unknown_calls[0].lineno < _marker_line(
        smallest_unknown,
        "unknown_order_update",
    )

    all_calls = _terminal_invalidation_calls(apply_event)
    assert len(all_calls) == 2
    known_call = next(call for call in all_calls if call not in unknown_calls)
    for stale_marker in (
        "stale_update_ignored",
        "stale_exchange_time_update_ignored",
    ):
        assert known_call.lineno > _marker_line(apply_event, stale_marker)

    for uncertain_marker in (
        "exchange_oid_mismatch",
        "exchange_order_semantics_mismatch",
        "cum_fill_regression",
        "duplicate_execution_ignored",
        "unhandled_exchange_status",
        "invalid_transition",
    ):
        assert known_call.lineno < _marker_line(apply_event, uncertain_marker)

    normal_update_blocks = [
        node
        for node in ast.walk(apply_event)
        if isinstance(node, ast.If)
        and ast.unparse(node.test)
        == "order.status != previous_status or had_fill"
    ]
    assert len(normal_update_blocks) == 1
    assert known_call.lineno < normal_update_blocks[0].lineno


def test_execution_gap_invalidates_expired_permit_terminal_proof():
    source = _oms_implementation_source()
    tree = ast.parse(source)
    apply_event = _function(tree, "_apply_event")
    quarantine = _function(tree, "_quarantine_execution_gap_locked")

    known_call = max(
        _terminal_invalidation_calls(apply_event),
        key=lambda call: call.lineno,
    )
    quarantine_calls = [
        node
        for node in ast.walk(apply_event)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_quarantine_execution_gap_locked"
    ]
    assert len(quarantine_calls) == 1
    assert known_call.lineno < quarantine_calls[0].lineno
    assert _terminal_invalidation_count(quarantine) == 0


def test_only_stale_and_safe_tombstone_paths_keep_terminal_proof():
    source = _oms_implementation_source()
    tree = ast.parse(source)
    apply_event = _function(tree, "_apply_event")

    ignored_markers = (
        "late_duplicate_ignored",
        "stale_update_ignored",
        "stale_exchange_time_update_ignored",
    )
    for marker in ignored_markers:
        matching_nodes = [
            node
            for node in ast.walk(apply_event)
            if isinstance(node, ast.If) and _contains_marker(node, marker)
        ]
        assert matching_nodes, marker
        smallest = min(matching_nodes, key=_span)
        assert _terminal_invalidation_count(smallest) == 0, marker

    tombstone_duplicate = min(
        (
            node
            for node in ast.walk(apply_event)
            if isinstance(node, ast.If)
            and _contains_marker(node, "late_duplicate_ignored")
        ),
        key=_span,
    )
    tombstone_source = _source_for(tombstone_duplicate, source)
    for terminal_status in ("CANCELED", "EXPIRED", "REJECTED"):
        assert terminal_status in tombstone_source
    assert "update.cum_filled_qty <= 1e-9" in tombstone_source
    assert "update.filled_qty <= 1e-9" in tombstone_source


def test_all_gateway_order_sends_pass_the_post_prepare_final_fence():
    source = _oms_implementation_source()
    tree = ast.parse(source)
    dispatch = _function(tree, "_dispatch_gateway_order_with_final_fence")
    submit = _function(tree, "submit_order")
    internal = _function(tree, "_submit_internal_order")

    send_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "send_order"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "gateway"
    ]
    assert len(send_calls) == 2
    dispatch_nodes = set(ast.walk(dispatch))
    assert all(call in dispatch_nodes for call in send_calls)

    dispatch_source = _source_for(dispatch, source)
    assert "supports_outbound_send_guard" in dispatch_source
    assert "pre_send_guard=transport_pre_send_guard" in dispatch_source
    assert "outbound_transport_guard_unavailable" in dispatch_source

    for function in (submit, internal):
        function_source = _source_for(function, source)
        assert function_source.index("_record_submit_prepared_batch(") < (
            function_source.index("_dispatch_gateway_order_with_final_fence(")
        )

    internal_source = _source_for(internal, source)
    assert "allow_shutdown_emergency=allow_shutdown_emergency" in internal_source


def test_submit_prepare_batch_is_outside_the_oms_state_lock():
    source = _oms_implementation_source()
    tree = ast.parse(source)
    for function_name in ("submit_order", "_submit_internal_order"):
        function = _function(tree, function_name)
        batch_calls = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_record_submit_prepared_batch"
        ]
        dispatch_calls = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr
            == "_dispatch_gateway_order_with_final_fence"
        ]
        assert len(batch_calls) == 1
        assert len(dispatch_calls) == 1
        assert batch_calls[0].lineno < dispatch_calls[0].lineno

        for with_node in (
            node for node in ast.walk(function) if isinstance(node, ast.With)
        ):
            holds_oms_lock = any(
                isinstance(item.context_expr, ast.Attribute)
                and isinstance(item.context_expr.value, ast.Name)
                and item.context_expr.value.id == "self"
                and item.context_expr.attr == "lock"
                for item in with_node.items
            )
            if holds_oms_lock:
                assert batch_calls[0] not in set(ast.walk(with_node))


def test_oms_uses_only_the_bounded_background_executor():
    tree = ast.parse(_oms_implementation_source())
    oms = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "OMS"
    )
    unmanaged = [
        node
        for node in ast.walk(oms)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "threading"
        and node.func.attr in {"Thread", "Timer"}
    ]
    assert unmanaged == []

    background_source = (
        ROOT / "oms" / "background_tasks.py"
    ).read_text(encoding="utf-8")
    for marker in (
        "max_pending_tasks",
        "safety_queue_capacity",
        "EMERGENCY_LANE",
        "emergency_queue_capacity",
        "self._pending_limits",
        "self._schedulers",
        "resubmit_after_current",
        "self.SAFETY_LANE",
        "self._scheduled",
        "rerun.run_at",
    ):
        assert marker in background_source

    background_tree = ast.parse(background_source)
    finish_handle = _function(background_tree, "_finish_handle")
    mark_done = next(
        node
        for node in ast.walk(finish_handle)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_mark_done"
    )
    condition_sections = [
        node
        for node in ast.walk(finish_handle)
        if isinstance(node, ast.With)
        and any(
            isinstance(item.context_expr, ast.Attribute)
            and item.context_expr.attr == "_condition"
            for item in node.items
        )
    ]
    assert any(mark_done in set(ast.walk(section)) for section in condition_sections)


def test_submit_cancel_is_coordinated_through_durable_settlement():
    source = _oms_implementation_source()
    tree = ast.parse(source)
    assert "_pre_dispatch_submission_oids" not in source

    final_fence = _function(tree, "_get_final_outbound_send_rejection_locked")
    final_fence_source = _source_for(final_fence, source)
    assert "client_oid in self._submit_cancel_requested_oids" in final_fence_source
    assert 'return "cancel_requested_before_transport", ""' in final_fence_source

    cancel = _function(tree, "cancel_order")
    cancel_source = _source_for(cancel, source)
    for marker in (
        "self._submit_settlement_inflight_oids",
        "self._submit_cancel_requested_oids",
        "cancel_queued_until_submit_settled",
        "OrderStatus.CANCELLING",
    ):
        assert marker in cancel_source

    finish = _function(tree, "_finish_submit_settlement")
    finish_source = _source_for(finish, source)
    assert 'task_key = f"post-submit-cancel:{client_oid}"' in finish_source
    assert finish_source.count("self.cancel_order(client_oid)") >= 2

    for function_name, committed_context in (
        ("submit_order", "submit_ack_committed"),
        ("_submit_internal_order", "internal_submit_ack_committed"),
    ):
        function = _function(tree, function_name)
        calls = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        ]
        commit = next(
            node
            for node in calls
            if node.func.attr == "_commit_gateway_submission"
        )
        finish_after_commit = next(
            node
            for node in calls
            if node.func.attr == "_finish_submit_settlement"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == committed_context
        )
        assert commit.lineno < finish_after_commit.lineno


def test_rpi_permit_configuration_is_owned_by_composed_manager():
    engine_source = _oms_implementation_source()
    engine_tree = ast.parse(engine_source)
    oms = next(
        node
        for node in engine_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "OMS"
    )
    extracted_methods = {
        "_canonical_json",
        "_require_exact_mapping_keys",
        "_require_sha256",
        "_finite_decimal",
        "_positive_decimal",
        "_decimal_text",
        "_usdt_to_microu",
        "_parse_utc_exchange_ns",
        "_verify_rpi_calibration_permit_signature",
        "_load_rpi_calibration_config",
    }
    assert not any(
        isinstance(node, ast.FunctionDef) and node.name in extracted_methods
        for node in oms.body
    )
    assert "self.rpi_calibration_manager = RpiCalibrationManager(config)" in (
        engine_source
    )
    assert (
        "self._rpi_calibration = self.rpi_calibration_manager.runtime_config"
        in engine_source
    )

    manager_source = RPI_MANAGER.read_text(encoding="utf-8")
    manager_tree = ast.parse(manager_source)
    manager = next(
        node
        for node in manager_tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "RpiCalibrationManager"
    )
    manager_methods = {
        node.name
        for node in manager.body
        if isinstance(node, ast.FunctionDef)
    }
    assert extracted_methods <= manager_methods


def test_final_send_fence_rechecks_dynamic_gate_and_calibration_truth():
    source = _oms_implementation_source()
    tree = ast.parse(source)
    fence = _function(tree, "_get_final_outbound_send_rejection_locked")
    fence_source = _source_for(fence, source)

    required_markers = (
        "permit_epoch != self._outbound_gate_epoch",
        "self._stopped",
        "self._shutdown_requested",
        "not self._outbound_gate_open",
        "_get_order_block_reason",
        "OMSCapabilityMode.PASSIVE_ONLY",
        "OMSCapabilityMode.DEGRADED",
        "_get_clock_health_rejection_locked",
        "_get_venue_dead_man_switch_rejection_locked",
        "_get_risk_control_heartbeat_rejection_locked",
        "_get_margin_health_rejection_locked",
        "_get_self_trade_prevention_rejection_locked",
        "self._rpi_calibration_expired",
        "self._rpi_calibration_restart_rearm_blocked",
        "self._rpi_calibration_reservation_ids",
        "self._rpi_calibration_permit_activated",
        'self._rpi_calibration["not_before_ns"]',
        'self._rpi_calibration["expires_at_ns"]',
        "_observe_rpi_calibration_loss_locked",
        "self._rpi_calibration_effective_loss_cap_microu",
    )
    for marker in required_markers:
        assert marker in fence_source


def test_recovered_and_internal_orders_invalidate_expired_terminal_proof():
    source = _oms_implementation_source()
    tree = ast.parse(source)

    recovered = _function(tree, "_create_recovered_order")
    internal = _function(tree, "_submit_internal_order")
    assert _terminal_invalidation_count(recovered) == 1
    assert _terminal_invalidation_count(internal) == 1


def test_transport_guard_runs_at_the_actual_rest_send_boundary():
    rest_source = REST_API.read_text(encoding="utf-8")
    rest_tree = ast.parse(rest_source)
    request = _function(rest_tree, "request")
    new_order = _function(rest_tree, "new_order")

    guard_calls = [
        node
        for node in ast.walk(request)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_run_pre_send_guard"
    ]
    transport_calls = [
        node
        for node in ast.walk(request)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "send"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "session"
    ]
    assert len(guard_calls) == 1
    assert len(transport_calls) == 1
    assert guard_calls[0].lineno < transport_calls[0].lineno

    new_order_source = _source_for(new_order, rest_source)
    assert 'self.request(\n            "POST",\n            EP_ORDER' in new_order_source
    assert "pre_send_guard=tuple(guards)" in new_order_source
    assert "max_attempts=1" in new_order_source

    live_source = LIVE_GATEWAY.read_text(encoding="utf-8")
    live_send = _function(ast.parse(live_source), "send_order")
    assert "pre_send_guard=pre_send_guard" in _source_for(
        live_send,
        live_source,
    )
    assert "supports_outbound_send_guard = True" in live_source
    assert "supports_outbound_send_guard = True" in PAPER_GATEWAY.read_text(
        encoding="utf-8"
    )

    base_source = BASE_GATEWAY.read_text(encoding="utf-8")
    base_send = _function(ast.parse(base_source), "send_order")
    assert any(argument.arg == "pre_send_guard" for argument in base_send.args.kwonlyargs)


def test_expiry_and_terminal_enforcement_wait_for_submits_to_settle():
    source = _oms_implementation_source()
    tree = ast.parse(source)
    expire = _function(tree, "expire_rpi_calibration_permit")
    terminal = _function(tree, "_enforce_rpi_calibration_terminal_once")
    wait_risk = _function(tree, "_wait_for_outbound_risk_sends")
    wait_all = _function(tree, "_wait_for_outbound_order_sends")
    settlement_count = _function(tree, "_submit_settlement_count_locked")

    expire_source = _source_for(expire, source)
    assert expire_source.index("_wait_for_outbound_risk_sends(") < (
        expire_source.index("_cancel_orders_matching(")
    )
    assert "order.status != OrderStatus.SUBMITTING" in expire_source
    assert "order.status != OrderStatus.SUBMITTING" in _source_for(
        terminal,
        source,
    )
    for wait in (wait_risk, wait_all):
        wait_source = _source_for(wait, source)
        assert "settling" in wait_source
        assert "_submit_settlement_count_locked(" in wait_source
    assert "self._submit_settlement_inflight_oids" in _source_for(
        settlement_count,
        source,
    )


def test_submit_leases_outlive_transport_and_durable_local_settlement():
    source = _oms_implementation_source()
    tree = ast.parse(source)
    for function_name in ("_submit_internal_order", "submit_order"):
        function = _function(tree, function_name)
        dispatch_tries = [
            node
            for node in function.body
            if isinstance(node, ast.Try)
            and any(
                isinstance(candidate, ast.Call)
                and isinstance(candidate.func, ast.Attribute)
                and candidate.func.attr
                == "_dispatch_gateway_order_with_final_fence"
                for candidate in ast.walk(node)
            )
        ]
        assert len(dispatch_tries) == 1
        assert not dispatch_tries[0].finalbody

        result_calls = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_record_command_result"
        ]
        assert len(result_calls) == 1
        result_line = result_calls[0].lineno

        outcome_branches = [
            node
            for node in function.body
            if isinstance(node, ast.If)
            and "command.outcome == CommandOutcome." in ast.unparse(node.test)
        ]
        assert len(outcome_branches) == 2
        for branch in outcome_branches:
            snapshot_calls = [
                node
                for node in ast.walk(branch)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_record_order_snapshot"
            ]
            release_calls = [
                node
                for node in ast.walk(branch)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr
                == "_release_outbound_order_send_permit"
            ]
            assert len(snapshot_calls) == 1
            assert release_calls
            assert result_line < snapshot_calls[0].lineno
            assert all(
                snapshot_calls[0].lineno < call.lineno
                for call in release_calls
            )

        function_source = _source_for(function, source)
        rejected_snapshot = function_source.index(
            "transport_rejection_superseded = False"
        )
        rejected_release = function_source.index(
            "_release_outbound_order_send_permit(",
            rejected_snapshot,
        )
        assert function_source.index(
            "_record_order_snapshot(",
            rejected_snapshot,
        ) < rejected_release


def test_fail_closed_and_stop_seal_every_new_order_before_draining():
    source = _oms_implementation_source()
    tree = ast.parse(source)
    acquire = _function(tree, "_acquire_outbound_order_send_permit_locked")
    fence = _function(tree, "_get_final_outbound_send_rejection_locked")
    latch = _function(tree, "_latch_journal_failure_locked")
    fail_closed = _function(tree, "_fail_closed_on_journal_error")
    stop = _function(tree, "stop")

    assert "_outbound_all_order_seal_reason" in _source_for(acquire, source)
    assert "_outbound_all_order_seal_reason" in _source_for(fence, source)

    latch_source = _source_for(latch, source)
    assert latch_source.index("_outbound_all_order_seal_reason = reason") < (
        latch_source.index("_close_outbound_gate_locked(")
    )
    assert 'hold="journal_failure"' in latch_source

    fail_source = _source_for(fail_closed, source)
    assert fail_source.index("_latch_journal_failure(") < fail_source.index(
        "_wait_for_outbound_order_sends("
    )
    assert fail_source.index("_wait_for_outbound_order_sends(") < (
        fail_source.index("_cancel_all_orders_unchecked(")
    )
    assert "_account_cancel_symbols()" in fail_source

    stop_source = _source_for(stop, source)
    assert stop_source.index("self._stopped = True") < stop_source.index(
        "_wait_for_outbound_order_sends("
    )


def test_submit_journal_failures_seal_before_releasing_outbound_lease():
    source = _oms_implementation_source()
    tree = ast.parse(source)
    for function_name in ("_submit_internal_order", "submit_order"):
        function = _function(tree, function_name)
        handlers = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.ExceptHandler)
            and isinstance(node.type, ast.Name)
            and node.type.id == "JournalError"
        ]
        assert len(handlers) == 5
        for handler in handlers:
            latch_calls = [
                node
                for node in ast.walk(handler)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_latch_journal_failure"
            ]
            release_calls = [
                node
                for node in ast.walk(handler)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr
                == "_release_outbound_order_send_permit"
            ]
            assert len(latch_calls) == 1
            assert release_calls
            assert all(
                latch_calls[0].lineno < release.lineno
                for release in release_calls
            )


def test_halt_handoff_reaches_sidecar_and_has_stale_parent_fallback():
    health_source = SYSTEM_HEALTH.read_text(encoding="utf-8")
    health = _function(ast.parse(health_source), "handle_system_health_event")
    health_segment = _source_for(health, health_source)
    assert health_segment.index("resume_shutdown_guard(") < (
        health_segment.index("trigger_kill_switch(")
    )
    assert 'message.startswith("HALT:")' in health_segment
    assert 'supervisor_result.get("persisted") is True' in health_segment

    main_source = MAIN.read_text(encoding="utf-8")
    assert (
        "handle_system_health_event(\n"
        "            e,\n"
        "            risk_controller,\n"
        "            oms_system,\n"
        "            risk_supervisor,"
    ) in main_source

    supervisor_source = RISK_SUPERVISOR.read_text(encoding="utf-8")
    supervisor_tree = ast.parse(supervisor_source)
    resume = _function(supervisor_tree, "resume_shutdown_guard")
    send_heartbeat = _function(supervisor_tree, "_send_heartbeat")
    assert "suspend_parent_heartbeat(" in _source_for(
        resume,
        supervisor_source,
    )
    assert "parent_heartbeat_suspended_reason" in _source_for(
        send_heartbeat,
        supervisor_source,
    )


def test_position_and_rest_snapshot_uncertainty_invalidate_terminal_proof_first():
    source = _oms_implementation_source()
    tree = ast.parse(source)
    account_update = _function(tree, "on_exchange_account_update")
    account_calls = _terminal_invalidation_calls(account_update)
    assert len(account_calls) == 1
    assert account_calls[0].lineno < min(
        node.lineno
        for node in ast.walk(account_update)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "force_sync"
    )
    assert account_calls[0].lineno < _marker_line(
        account_update,
        "stale_exchange_account_update_ignored",
    )

    order_snapshot = _function(tree, "_apply_exchange_order_snapshot")
    snapshot_calls = _terminal_invalidation_calls(order_snapshot)
    assert len(snapshot_calls) == 2
    for marker in (
        "unhandled_order_snapshot_status",
        "order_snapshot_trade_truth_missing",
    ):
        branches = [
            node
            for node in ast.walk(order_snapshot)
            if isinstance(node, ast.If) and _contains_marker(node, marker)
        ]
        assert branches
        branch = min(branches, key=_span)
        branch_calls = _terminal_invalidation_calls(branch)
        assert len(branch_calls) == 1
        assert branch_calls[0].lineno < _marker_line(branch, marker)


def test_terminal_verified_is_published_only_after_durable_audit():
    source = _oms_implementation_source()
    tree = ast.parse(source)
    terminal = _function(tree, "_enforce_rpi_calibration_terminal_once")
    terminal_source = _source_for(terminal, source)
    audit_calls = [
        node
        for node in ast.walk(terminal)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_audit"
        and _contains_marker(
            node,
            "rpi_calibration_terminal_convergence_verified",
        )
    ]
    publish_assignments = [
        node
        for node in ast.walk(terminal)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Attribute)
            and target.attr == "_rpi_calibration_terminal_verified"
            for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
        and node.value.value is True
    ]
    assert len(audit_calls) == 1
    assert len(publish_assignments) == 1
    assert audit_calls[0].lineno < publish_assignments[0].lineno
    assert "terminal_verification_commit_pending" in terminal_source


def test_sidecar_quiesce_keeps_exchange_truth_and_can_take_over():
    source = RISK_SUPERVISOR.read_text(encoding="utf-8")
    tree = ast.parse(source)
    quiesced = _function(tree, "_step_quiesced")
    quiesced_source = _source_for(quiesced, source)

    assert quiesced_source.index("_service_exchange_risk(") < (
        quiesced_source.index("parent_age =")
    )
    for required_call in (
        "_exchange_snapshot_valid(",
        "_account_truth_counts(",
        "_update_parent_stale_state(",
        "_takeover_from_quiesce(",
    ):
        assert required_call in quiesced_source
    for takeover_condition in (
        "not parent_healthy",
        "not exchange_valid",
        "open_order_count or nonzero_position_count",
    ):
        assert takeover_condition in quiesced_source
    assert quiesced_source.index("_takeover_from_quiesce(") < (
        quiesced_source.index("return self.step(")
    )
    assert "exchange_serviced=True" in quiesced_source


def test_sidecar_quiesce_takeover_latches_kill_before_persistence():
    source = RISK_SUPERVISOR.read_text(encoding="utf-8")
    tree = ast.parse(source)
    takeover = _function(tree, "_takeover_from_quiesce")
    persist_calls = [
        node
        for node in ast.walk(takeover)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_persist_durable_state"
    ]
    assert len(persist_calls) == 1
    persist_line = persist_calls[0].lineno

    assignments = {}
    for node in ast.walk(takeover):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Attribute):
                assignments[target.attr] = node

    for attribute in (
        "quiesced",
        "quiesce_reason",
        "quiesced_at",
        "quiesce_snapshot_sequence",
        "kill_latched",
        "kill_reason",
        "stage",
        "flat_verification_count",
        "last_verified_snapshot_sequence",
        "last_cancel_attempt_at",
        "last_flatten_attempt_at",
    ):
        assert attribute in assignments
        assert assignments[attribute].lineno < persist_line

    takeover_source = _source_for(takeover, source)
    assert "self.quiesced = False" in takeover_source
    assert "self.kill_latched = True" in takeover_source
    assert 'self.stage = "FLATTENING"' in takeover_source
    assert "force=True" in takeover_source


def test_sidecar_orphan_exit_requires_fresh_post_stale_flat_proof():
    source = RISK_SUPERVISOR.read_text(encoding="utf-8")
    tree = ast.parse(source)
    step = _function(tree, "step")
    keep_running_assignments = [
        node
        for node in ast.walk(step)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "keep_running"
            for target in node.targets
        )
    ]
    assert len(keep_running_assignments) == 1
    exit_expression = ast.unparse(keep_running_assignments[0].value)
    for required_truth in (
        "exchange_valid",
        "open_order_count == 0",
        "nonzero_position_count == 0",
        "self.flat_verification_count >= self.flat_verification_checks",
        "self.last_verified_snapshot_sequence == self.risk_snapshot_sequence",
        "self.last_verified_snapshot_sequence > self.parent_stale_snapshot_sequence",
    ):
        assert required_truth in exit_expression

    parent_state = _function(tree, "_update_parent_stale_state")
    parent_source = _source_for(parent_state, source)
    assert (
        "self.parent_stale_snapshot_sequence = (\n"
        "                self.risk_snapshot_sequence\n"
        "            )"
    ) in parent_source
    assert "self.flat_verification_count = 0" in parent_source
    assert "self.last_verified_snapshot_sequence = 0" in parent_source

    step_source = _source_for(step, source)
    invalidation_start = step_source.index(
        'self.stage == "FLAT_VERIFIED"'
    )
    invalidation_end = step_source.index(
        "if self.stage != \"FLAT_VERIFIED\"",
        invalidation_start,
    )
    invalidation = step_source[invalidation_start:invalidation_end]
    assert "not exchange_valid" in invalidation
    assert "self.flat_verification_count = 0" in invalidation
    assert "self.last_verified_snapshot_sequence = 0" in invalidation
    assert (
        "parent_healthy\n"
        "                            or self.risk_snapshot_sequence\n"
        "                            > self.parent_stale_snapshot_sequence"
    ) in step_source


def test_sidecar_stop_waits_for_newer_independent_flat_snapshot():
    source = RISK_SUPERVISOR.read_text(encoding="utf-8")
    tree = ast.parse(source)
    stop = _function(tree, "_complete_stop_request")
    stop_source = _source_for(stop, source)

    assert "_service_exchange_risk(" in stop_source
    assert "_exchange_snapshot_valid(" in stop_source
    assert "_account_truth_counts(" in stop_source
    assert (
        "self.risk_snapshot_sequence\n"
        "                <= self.quiesce_snapshot_sequence"
    ) in stop_source
    assert "return None" in stop_source
    assert stop_source.count("_takeover_from_quiesce(") >= 2
    assert "stop_without_cancel_requires_quiesced" in stop_source
    assert "stop_after_cancel_requires_fresh_quiesce" in stop_source

    quiesced = _function(tree, "_step_quiesced")
    quiesced_source = _source_for(quiesced, source)
    takeover_start = quiesced_source.index("if takeover_reason:")
    recursive_step = quiesced_source.index(
        "return self.step(",
        takeover_start,
    )
    takeover_segment = quiesced_source[takeover_start:recursive_step]
    assert "if self.stop_requested:" in takeover_segment
    assert "self._set_stop_result(" in takeover_segment
    assert "self.stop_requested = False" in takeover_segment

    snapshot_valid = _function(tree, "_exchange_snapshot_valid")
    snapshot_source = _source_for(snapshot_valid, source)
    for required_field in (
        "self.exchange_healthy",
        "self.risk_snapshot_sequence > 0",
        "self.last_exchange_success_at",
        "self.risk_snapshot_captured_monotonic",
        "self.exchange_max_age_sec",
    ):
        assert required_field in snapshot_source
