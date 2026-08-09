from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from risk.binance_sidecar_truth import BinanceSidecarTruthReader
from risk.sidecar_state_store import SidecarStateStore


class _Response:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _IncomeRest:
    def __init__(self, rows, *, fail_page: int = 0):
        self.rows = list(rows)
        self.fail_page = int(fail_page)
        self.calls = []

    def get_income_history(self, *, start_time, end_time, page, limit):
        self.calls.append((start_time, end_time, page, limit))
        if page == self.fail_page:
            return _Response({"msg": "interrupted"}, 503)
        matching = [
            row
            for row in self.rows
            if int(start_time) <= int(row["time"]) <= int(end_time)
        ]
        offset = (int(page) - 1) * int(limit)
        return _Response(matching[offset : offset + int(limit)])


class _Owner:
    def __init__(self, rest, ledger, deployment_start_ms: int, now_epoch: float):
        self.rest = rest
        self.cash_flow_ledger = ledger
        self.cash_flow_deployment_start_ms = int(deployment_start_ms)
        self.cash_flow_overlap_ms = 86_400_000
        self.cash_flow_max_pages = 3
        self.cash_flow_income_types = {"TRANSFER"}
        self.cash_flow_assets = {"USDT"}
        self.cash_flow_poll_interval_sec = 30.0
        self._now_epoch = float(now_epoch)
        self._monotonic = lambda: 100.0
        self._wall_time = lambda: self._now_epoch
        self._last_cash_flow_poll_monotonic = 0.0
        self._cached_external_cash_flow_total = 0.0
        self._cached_daily_external_cash_flow_total = 0.0
        self._cached_deployment_external_cash_flow_total = 0.0
        self._deployment_cash_flow_carry = 0.0
        self._cash_flow_cache_day = ""
        self._cash_flow_cache_generation = 0
        self._cash_flow_cache_complete_through_ms = 0
        self._cash_flow_cache_initialized = False

    def _corrected_epoch_at(self, _observed_monotonic: float):
        return self._now_epoch

    @staticmethod
    def _response_payload(response, expected_type, label: str):
        payload = response.json()
        if response.status_code != 200:
            return False, None, f"{label}_status={response.status_code}"
        if not isinstance(payload, expected_type):
            return False, None, f"{label}_payload_invalid"
        return True, payload, ""

    @staticmethod
    def _income_identity(row: dict) -> str:
        return BinanceSidecarTruthReader.income_identity(row)

    def _get_daily_external_cash_flow(self):
        return BinanceSidecarTruthReader(self).get_daily_external_cash_flow()


def _store(root: Path, deployment_start_ms: int) -> SidecarStateStore:
    SidecarStateStore.provision(
        root,
        account_scope_id="account-a",
        deployment_id="deployment-a",
        genesis_id="genesis-a",
        initial_payload={
            "schema_version": 2,
            "kill_latched": True,
            "stage": "KILL",
            "cash_flow_deployment_start_ms": deployment_start_ms,
        },
    )
    store = SidecarStateStore(
        root,
        account_scope_id="account-a",
        deployment_id="deployment-a",
        genesis_id="genesis-a",
        writer_id="writer-a",
    )
    store.open_recover()
    return store


def _row(transaction_id: int, timestamp_ms: int, amount: float) -> dict:
    return {
        "incomeType": "TRANSFER",
        "tranId": transaction_id,
        "asset": "USDT",
        "income": str(amount),
        "time": timestamp_ms,
    }


def test_utc_rollover_and_late_cash_flows_use_durable_horizons(
    tmp_path: Path,
) -> None:
    deployment_start = int(
        datetime(2026, 7, 19, tzinfo=timezone.utc).timestamp() * 1000
    )
    day_one_now = datetime(2026, 7, 20, 23, 59, tzinfo=timezone.utc)
    day_one_midday = int(
        datetime(2026, 7, 20, 12, tzinfo=timezone.utc).timestamp() * 1000
    )
    day_two_now = datetime(2026, 7, 21, 0, 1, tzinfo=timezone.utc)
    day_two_event = int(
        datetime(2026, 7, 21, 0, 0, 30, tzinfo=timezone.utc).timestamp()
        * 1000
    )
    rows = [
        _row(1, deployment_start + 1_000, 50.0),
        _row(2, day_one_midday, 100.0),
    ]
    store = _store(tmp_path, deployment_start)
    rest = _IncomeRest(rows)
    owner = _Owner(rest, store, deployment_start, day_one_now.timestamp())
    reader = BinanceSidecarTruthReader(owner)

    ok, first, reason = reader.get_cached_external_cash_flow_truth()
    assert ok, reason
    assert first.daily_external_cash_flow_total == 100.0
    assert first.deployment_external_cash_flow_total == 150.0
    assert first.ledger_generation == 1

    rest.rows.extend(
        [
            _row(3, day_one_midday + 1_000, 25.0),
            _row(4, day_two_event, -10.0),
        ]
    )
    owner._now_epoch = day_two_now.timestamp()
    ok, second, reason = reader.get_cached_external_cash_flow_truth()

    assert ok, reason
    assert second.risk_day == "2026-07-21"
    assert second.daily_external_cash_flow_total == -10.0
    assert second.deployment_external_cash_flow_total == 165.0
    assert second.ledger_generation == 2
    assert store.cash_flow_cursor()["generation"] == 2
    store.close()


def test_pagination_failure_does_not_advance_durable_cursor(
    tmp_path: Path,
) -> None:
    deployment_start = 1_700_000_000_000
    now_ms = deployment_start + 10_000
    rows = [
        _row(index, deployment_start + index, 1.0)
        for index in range(1, 1_001)
    ]
    store = _store(tmp_path, deployment_start)
    owner = _Owner(
        _IncomeRest(rows, fail_page=2),
        store,
        deployment_start,
        now_ms / 1000.0,
    )
    owner.cash_flow_max_pages = 2

    ok, truth, reason = BinanceSidecarTruthReader(
        owner
    ).get_cached_external_cash_flow_truth()

    assert not ok
    assert truth is None
    assert reason == "income_history_status=503"
    assert store.cash_flow_cursor()["generation"] == 0
    assert store.cash_flow_total(deployment_start, now_ms) == 0.0
    store.close()
