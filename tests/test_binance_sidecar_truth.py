from unittest.mock import patch

from risk.binance_sidecar_truth import BinanceSidecarTruthReader


class _Response:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _Rest:
    def __init__(self, positions):
        self.positions = list(positions)

    @staticmethod
    def get_account():
        return _Response({"totalWalletBalance": "1000"})

    def get_positions(self):
        return _Response(self.positions.pop(0))


class _Owner:
    def __init__(self, positions):
        self.rest = _Rest(positions)
        self.symbols = ("BTCUSDT",)
        self.daily_loss_enabled = False
        self.clock_offset_ms = 0.0
        self.clock_phase_error_ms = 0.0
        self.clock_rtt_ms = 1.0
        self.clock_uncertainty_ms = 0.5
        self.clock_offset_dispersion_ms = 0.0
        self.cash_flow_poll_interval_sec = 30.0
        self._last_cash_flow_poll_monotonic = 0.0
        self._cached_external_cash_flow_total = 7.0
        self._cash_flow_cache_initialized = False
        self.cash_flow_result = (True, 0.0, "")
        self.reader = BinanceSidecarTruthReader(self)

    @staticmethod
    def _ensure_exchange_clock(force=False):
        return True, ""

    @staticmethod
    def _response_payload(response, expected_type, label):
        return BinanceSidecarTruthReader.response_payload(
            response,
            expected_type,
            label,
        )

    @staticmethod
    def _position_risk_fingerprint(positions):
        return BinanceSidecarTruthReader.position_risk_fingerprint(
            positions
        )

    @staticmethod
    def _get_open_orders_snapshot():
        return True, [], ""

    @staticmethod
    def _get_funding_observations():
        return True, {}, ""

    def _get_daily_external_cash_flow(self):
        return self.cash_flow_result


def test_position_fingerprint_normalizes_equivalent_numeric_strings():
    first = [{"symbol": "BTCUSDT", "positionAmt": "1.00"}]
    second = [{"symbol": "btcusdt", "positionAmt": "1"}]

    assert BinanceSidecarTruthReader.position_risk_fingerprint(
        first
    ) == BinanceSidecarTruthReader.position_risk_fingerprint(second)


def test_snapshot_fails_closed_when_position_changes_during_order_query():
    before = [{"symbol": "BTCUSDT", "positionAmt": "1"}]
    after = [{"symbol": "BTCUSDT", "positionAmt": "2"}]
    owner = _Owner([before, after])

    ok, snapshot, reason = owner.reader.get_risk_snapshot()

    assert not ok
    assert snapshot == {}
    assert reason == (
        "snapshot_inconsistent:positions_changed_during_open_orders_query"
    )


def test_failed_cash_flow_refresh_does_not_replace_last_good_cache():
    positions = [[{"symbol": "BTCUSDT", "positionAmt": "0"}]] * 2
    owner = _Owner(positions)
    owner.cash_flow_result = (False, 0.0, "income_history_status=503")

    with patch(
        "risk.binance_sidecar_truth.time.perf_counter",
        return_value=100.0,
    ):
        result = owner.reader.get_cached_daily_external_cash_flow()

    assert result == (False, 0.0, "income_history_status=503")
    assert owner._cached_external_cash_flow_total == 7.0
    assert owner._last_cash_flow_poll_monotonic == 0.0
    assert not owner._cash_flow_cache_initialized
