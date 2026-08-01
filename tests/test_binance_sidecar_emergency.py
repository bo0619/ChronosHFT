from unittest.mock import patch

from risk.binance_sidecar_emergency import BinanceSidecarEmergencyActions
from risk.binance_sidecar_truth import BinanceSidecarTruthReader


class _Response:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _Rest:
    def __init__(self):
        self.countdowns = []
        self.cancels = []
        self.orders = []
        self.open_orders = [{"symbol": "ETHUSDT"}]
        self.positions = []

    def get_open_orders(self, *, emergency=False):
        assert emergency
        return _Response(self.open_orders)

    def set_countdown_cancel_all(self, symbol, countdown_time_ms):
        self.countdowns.append((symbol, countdown_time_ms))
        return _Response({})

    def cancel_all_orders(self, symbol):
        self.cancels.append(symbol)
        return _Response({})

    def get_positions(self, *, emergency=False):
        assert emergency
        return _Response(self.positions)

    def new_order(self, request, client_oid):
        self.orders.append((request, client_oid))
        return _Response({})


class _Owner:
    def __init__(self):
        self.rest = _Rest()
        self.clock_forces = []

    def _ensure_exchange_clock(self, force=False):
        self.clock_forces.append(force)
        return True, ""

    @staticmethod
    def _response_payload(response, expected_type, label):
        return BinanceSidecarTruthReader.response_payload(
            response,
            expected_type,
            label,
        )


def test_cancel_includes_symbols_discovered_from_account_open_orders():
    owner = _Owner()
    actions = BinanceSidecarEmergencyActions(owner)

    assert actions.cancel(["BTCUSDT"], 5_000) == (True, "")

    assert owner.clock_forces == [True]
    assert owner.rest.countdowns == [
        ("BTCUSDT", 5_000),
        ("ETHUSDT", 5_000),
    ]
    assert owner.rest.cancels == ["BTCUSDT", "ETHUSDT"]


def test_flatten_submits_only_reduce_only_market_ioc_orders():
    owner = _Owner()
    owner.rest.positions = [
        {"symbol": "BTCUSDT", "positionAmt": "2"},
        {"symbol": "ETHUSDT", "positionAmt": "-3"},
        {"symbol": "SOLUSDT", "positionAmt": "0"},
    ]
    actions = BinanceSidecarEmergencyActions(owner)

    with (
        patch(
            "risk.binance_sidecar_emergency.time.time",
            return_value=1_000.0,
        ),
        patch(
            "risk.binance_sidecar_emergency.os.getpid",
            return_value=42,
        ),
    ):
        result = actions.flatten()

    assert result == (True, 2, "")
    assert owner.clock_forces == [True]
    requests = [request for request, _client_oid in owner.rest.orders]
    assert [(request.symbol, request.side, request.volume) for request in requests] == [
        ("BTCUSDT", "SELL", 2.0),
        ("ETHUSDT", "BUY", 3.0),
    ]
    assert all(request.reduce_only for request in requests)
    assert all(request.order_type == "MARKET" for request in requests)
    assert all(request.time_in_force == "IOC" for request in requests)
    assert all(
        client_oid.startswith("crsk-42-")
        for _request, client_oid in owner.rest.orders
    )
