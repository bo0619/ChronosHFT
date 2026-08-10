from __future__ import annotations

import io
import json

from ui.dashboard_admin_actions import (
    handle_dashboard_admin_action,
    serve_dashboard_admin_post,
)


class DummyOwner:
    def __init__(self, oms=None, risk_manager=None, risk_supervisor=None):
        self._components = {
            "oms": oms,
            "risk_manager": risk_manager,
            "risk_supervisor": risk_supervisor,
        }
        self.publish_calls: list[bool] = []
        self.snapshot = {"meta": {"sequence": 1}, "system": {}}

    def publish_snapshot(self, force: bool = False):
        self.publish_calls.append(force)
        return True

    def get_snapshot(self):
        return self.snapshot

    def _valid_host_header(self, _header: str) -> bool:
        return True


class DummyHeaders(dict):
    def get_content_type(self):
        return self.get("Content-Type", "")


class DummyHandler:
    def __init__(self, headers: dict[str, str], body: bytes = b"{}"):
        self.headers = DummyHeaders(headers)
        self.path = "/api/admin/action"
        self.rfile = io.BytesIO(body)
        self.sent = None

    def _send(self, payload, content_type, status, extra_headers=None):
        self.sent = {
            "payload": payload,
            "content_type": content_type,
            "status": status,
            "extra_headers": extra_headers or {},
        }


def test_flatten_all_runs_gate_flatten_halt_in_order():
    calls: list[tuple] = []

    class FakeOms:
        def close_outbound_gate(self, reason, wait=True):
            calls.append(("close_outbound_gate", reason, wait))
            return True

        def emergency_reduce_only_flatten(self, reason, symbol=""):
            calls.append(("emergency_reduce_only_flatten", reason, symbol))
            return 3

        def halt_system(self, reason):
            calls.append(("halt_system", reason))
            return True

    owner = DummyOwner(oms=FakeOms())

    response, status = handle_dashboard_admin_action(
        owner,
        {"action": "flatten_all", "reason": "dashboard_flatten_all"},
    )

    assert status == 200
    assert response["accepted"] is True
    assert response["submitted"] == 3
    assert response["halted"] is True
    assert calls == [
        ("close_outbound_gate", "dashboard_flatten_all", True),
        ("emergency_reduce_only_flatten", "dashboard_flatten_all", ""),
        ("halt_system", "dashboard_flatten_all"),
    ]
    assert owner.publish_calls == [True]


def test_flatten_symbol_runs_flatten_then_freeze():
    calls: list[tuple] = []

    class FakeOms:
        def emergency_reduce_only_flatten(self, reason, symbol=""):
            calls.append(("emergency_reduce_only_flatten", reason, symbol))
            return 1

        def freeze_symbol(self, symbol, reason, cancel_active_orders=True):
            calls.append(("freeze_symbol", symbol, reason, cancel_active_orders))
            return True

    owner = DummyOwner(oms=FakeOms())

    response, status = handle_dashboard_admin_action(
        owner,
        {
            "action": "flatten_symbol",
            "reason": "dashboard_symbol_flatten",
            "symbol": "BTCUSDT",
        },
    )

    assert status == 200
    assert response["accepted"] is True
    assert response["submitted"] == 1
    assert response["frozen"] is True
    assert calls == [
        ("emergency_reduce_only_flatten", "dashboard_symbol_flatten", "BTCUSDT"),
        ("freeze_symbol", "BTCUSDT", "dashboard_symbol_flatten", True),
    ]
    assert owner.publish_calls == [True]


def test_rearm_uses_coordinated_rearm_flow():
    calls: list[tuple] = []

    class FakeOms:
        def rearm_system(self, reason):
            calls.append(("oms_rearm_system", reason))
            return True

    class FakeRiskManager:
        def can_operator_rearm(self):
            calls.append(("can_operator_rearm",))
            return True

        def acknowledge_operator_rearm(self):
            calls.append(("acknowledge_operator_rearm",))
            return True

    class FakeSupervisor:
        def prepare_rearm(self, reason):
            calls.append(("prepare_rearm", reason))
            return {"accepted": True, "token": "token-1"}

        def commit_rearm(self, token):
            calls.append(("commit_rearm", token))
            return {"accepted": True}

    owner = DummyOwner(
        oms=FakeOms(),
        risk_manager=FakeRiskManager(),
        risk_supervisor=FakeSupervisor(),
    )

    response, status = handle_dashboard_admin_action(
        owner,
        {"action": "rearm", "reason": "operator_verified_flat"},
    )

    assert status == 200
    assert response["accepted"] is True
    assert response["result"]["reason"] == "coordinated_rearm_completed"
    assert calls == [
        ("can_operator_rearm",),
        ("prepare_rearm", "operator_verified_flat"),
        ("oms_rearm_system", "operator_verified_flat"),
        ("commit_rearm", "token-1"),
        ("acknowledge_operator_rearm",),
    ]
    assert owner.publish_calls == [True]


def test_rearm_refusal_includes_flat_verification_diagnostic():
    class FakeOms:
        def rearm_system(self, reason):
            raise AssertionError("rearm_system must not run before flat verification")

    class FakeRiskManager:
        def can_operator_rearm(self):
            return False

    owner = DummyOwner(oms=FakeOms(), risk_manager=FakeRiskManager())
    owner.snapshot = {
        "system": {
            "oms": {
                "state": "HALTED",
                "manual_rearm_required": True,
                "capability_mode": "CANCEL_ONLY",
                "capability": {
                    "venue_dead_man_switch": {
                        "valid": False,
                        "reason": "renewal_stale:1042.984s>45.000s",
                    },
                    "risk_control_heartbeat": {
                        "valid": False,
                        "reason": "risk_live_loop",
                    },
                },
            },
        },
        "risk": {
            "status": {
                "kill_switch_triggered": True,
                "kill_state": "FAILED",
                "kill_reason": "SystemHealth: PAPER_DMS_TRIGGERED:CLUSDT",
            },
        },
    }

    response, status = handle_dashboard_admin_action(
        owner,
        {"action": "rearm", "reason": "dashboard_rearm"},
    )

    assert status == 409
    assert response["accepted"] is False
    assert response["diagnostic"]["code"] == "paper_dms_triggered"
    assert "Paper DMS" in response["diagnostic"]["summary"]
    assert response["diagnostic"]["details"]["kill_state"] == "FAILED"
    assert "PAPER_DMS_TRIGGERED:CLUSDT" in response["diagnostic"]["details"]["kill_reason"]
    assert "DMS" in response["diagnostic"]["details"]["blocking_hint"]
    assert owner.publish_calls == [True]


def test_admin_post_requires_dashboard_action_header():
    owner = DummyOwner()
    handler = DummyHandler({"Host": "127.0.0.1:8765", "Content-Type": "application/json"})

    serve_dashboard_admin_post(owner, handler)

    assert handler.sent is not None
    assert handler.sent["status"] == 403
    payload = json.loads(handler.sent["payload"].decode("utf-8"))
    assert payload["error"] == "forbidden"
    assert "missing dashboard action header" in payload["message"]
