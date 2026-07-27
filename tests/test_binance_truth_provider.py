import unittest

from gateway.binance.truth_provider import BinanceTruthSnapshotProvider


class DummyResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeRestApi:
    def __init__(self, *_args, **_kwargs):
        self.open_orders = [
            {"symbol": "BTCUSDT", "orderId": 1},
            {"symbol": "SOLUSDT", "orderId": 2},
        ]
        self.open_order_queries = []
        self.open_order_emergency = []

    def get_open_orders(self, symbol=None, *, emergency=False):
        normalized = str(symbol or "").upper()
        self.open_order_queries.append(normalized)
        self.open_order_emergency.append(bool(emergency))
        if not normalized:
            return DummyResponse(self.open_orders)
        return DummyResponse(
            [
                row
                for row in self.open_orders
                if str(row.get("symbol", "")).upper() == normalized
            ]
        )


class BinanceTruthProviderRateTests(unittest.TestCase):
    def test_open_orders_use_periodic_full_audit_then_scoped_queries(self):
        provider = BinanceTruthSnapshotProvider(
            "key",
            "secret",
            session=object(),
            rest_api_cls=FakeRestApi,
            symbols=("BTCUSDT", "ETHUSDT"),
            full_open_orders_audit_interval_sec=60.0,
        )

        first_rows = provider.get_open_orders()
        self.assertEqual(len(first_rows), 2)

        provider.rest.open_orders = [
            {"symbol": "BTCUSDT", "orderId": 3},
        ]
        second_rows = provider.get_open_orders()

        self.assertEqual(second_rows, provider.rest.open_orders)
        self.assertEqual(
            provider.rest.open_order_queries,
            ["", "BTCUSDT", "ETHUSDT", "SOLUSDT"],
        )

    def test_emergency_open_orders_forces_account_wide_audit(self):
        provider = BinanceTruthSnapshotProvider(
            "key",
            "secret",
            session=object(),
            rest_api_cls=FakeRestApi,
            symbols=("BTCUSDT",),
            full_open_orders_audit_interval_sec=60.0,
        )

        provider.get_open_orders()
        rows = provider.get_open_orders(emergency=True)

        self.assertEqual(len(rows), 2)
        self.assertEqual(provider.rest.open_order_queries, ["", ""])
        self.assertEqual(
            provider.rest.open_order_emergency,
            [False, True],
        )


if __name__ == "__main__":
    unittest.main()
