import sys
import types
import unittest
from unittest.mock import Mock, patch

if "requests" not in sys.modules:
    requests_module = types.ModuleType("requests")

    class Request:
        def __init__(self, method, url, params=None, headers=None):
            self.method = method
            self.url = url
            self.params = params or {}
            self.headers = headers or {}

    requests_module.Request = Request
    requests_module.Session = lambda: None
    requests_module.get = lambda *args, **kwargs: None
    sys.modules["requests"] = requests_module

from data.ref_data import ContractInfo, ref_data_manager
from event.type import OrderIntent, OrderRequest, Side, TIF_GTX, TIF_RPI
from gateway.binance.constants import (
    EP_COMMISSION_RATE,
    EP_COUNTDOWN_CANCEL_ALL,
    EP_INCOME,
    EP_ORDER,
    EP_POSITION_RISK,
    EP_RPI_DEPTH,
)
from gateway.binance.rest_api import BinanceRestApi
from oms.engine import OMS
from oms.order import Order
from oms.validator import OrderValidator
from scripts.list_binance_rpi_contracts import (
    render_csv,
    render_json,
    select_rpi_contracts,
)


class DummySession:
    def prepare_request(self, req):
        return req

    def send(self, _prepped, timeout=None):
        raise RuntimeError("proxy_down")


class DummyResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        return self.payload


class SequenceSession:
    def __init__(self, responses):
        self.responses = list(responses)

    def prepare_request(self, req):
        return req

    def send(self, _prepped, timeout=None):
        return self.responses.pop(0)


class DummyRequest:
    def __init__(self, method, url, params=None, headers=None):
        self.method = method
        self.url = url
        self.params = params or {}
        self.headers = headers or {}


class RestApiThrottleTests(unittest.TestCase):
    def test_failed_endpoint_enters_cooldown(self):
        api = BinanceRestApi("key", "secret", DummySession(), testnet=True)
        api.max_retries = 1
        api.retry_backoff_sec = 0.01
        api.request("GET", EP_POSITION_RISK, signed=True)
        self.assertGreater(api.endpoint_cooldown_until.get(EP_POSITION_RISK, 0.0), 0.0)

    def test_income_history_maps_public_arguments_to_exchange_parameters(self):
        api = BinanceRestApi("key", "secret", DummySession(), testnet=True)

        with patch.object(api, "request", return_value=DummyResponse(200, [])) as request:
            response = api.get_income_history(
                symbol="BTCUSDT",
                income_type="TRANSFER",
                start_time=1000,
                end_time=2000,
                page=2,
                limit=1000,
            )

        self.assertEqual(response.status_code, 200)
        request.assert_called_once_with(
            "GET",
            EP_INCOME,
            {
                "symbol": "BTCUSDT",
                "incomeType": "TRANSFER",
                "startTime": 1000,
                "endTime": 2000,
                "page": 2,
                "limit": 1000,
            },
            signed=True,
        )

    def test_new_order_sends_explicit_exchange_stp_mode(self):
        api = BinanceRestApi("key", "secret", DummySession(), testnet=True)
        order = OrderRequest(
            symbol="BTCUSDT",
            price=100.0,
            volume=0.1,
            side="BUY",
            self_trade_prevention_mode="EXPIRE_MAKER",
        )

        with patch.object(api, "request", return_value=DummyResponse(200, {})) as request:
            response = api.new_order(order, "client-1")

        self.assertEqual(response.status_code, 200)
        request.assert_called_once_with(
            "POST",
            EP_ORDER,
            {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "LIMIT",
                "quantity": 0.1,
                "newClientOrderId": "client-1",
                "price": 100.0,
                "timeInForce": "GTC",
                "selfTradePreventionMode": "EXPIRE_MAKER",
            },
            signed=True,
            pre_send_guard=(),
            max_attempts=1,
        )

    def test_rpi_order_preserves_tif_and_omits_ineligible_exchange_stp(self):
        api = BinanceRestApi("key", "secret", DummySession(), testnet=True)
        order = OrderRequest(
            symbol="LTCUSDT",
            price=100.0,
            volume=0.1,
            side="BUY",
            time_in_force="rpi",
            self_trade_prevention_mode="EXPIRE_MAKER",
        )

        with patch.object(api, "request", return_value=DummyResponse(200, {})) as request:
            response = api.new_order(order, "rpi-client-1")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(order.post_only)
        self.assertEqual(order.time_in_force, TIF_RPI)
        request.assert_called_once_with(
            "POST",
            EP_ORDER,
            {
                "symbol": "LTCUSDT",
                "side": "BUY",
                "type": "LIMIT",
                "quantity": 0.1,
                "newClientOrderId": "rpi-client-1",
                "price": 100.0,
                "timeInForce": "RPI",
            },
            signed=True,
            pre_send_guard=(),
            max_attempts=1,
        )

    def test_legacy_post_only_order_is_normalized_to_gtx_before_send(self):
        api = BinanceRestApi("key", "secret", DummySession(), testnet=True)
        order = OrderRequest(
            symbol="BTCUSDT",
            price=100.0,
            volume=0.1,
            side="BUY",
            time_in_force="GTC",
            post_only=True,
        )

        with patch.object(api, "request", return_value=DummyResponse(200, {})) as request:
            api.new_order(order)

        self.assertEqual(order.time_in_force, "GTX")
        self.assertEqual(request.call_args.args[2]["timeInForce"], "GTX")

    def test_rpi_depth_is_diagnostic_unsigned_request_with_fixed_limit(self):
        api = BinanceRestApi("key", "secret", DummySession(), testnet=True)

        with patch.object(
            api,
            "request",
            return_value=DummyResponse(200, {"bids": [], "asks": []}),
        ) as request:
            payload = api.get_rpi_depth("ltcusdt")

        self.assertEqual(payload, {"bids": [], "asks": []})
        request.assert_called_once_with(
            "GET",
            EP_RPI_DEPTH,
            {"symbol": "LTCUSDT", "limit": 1000},
            signed=False,
        )
        self.assertGreaterEqual(api.endpoint_intervals[EP_RPI_DEPTH], 1.0)

        with self.assertRaisesRegex(ValueError, "only supports limit=1000"):
            api.get_rpi_depth("LTCUSDT", limit=100)

    def test_commission_rate_exposes_account_specific_rpi_rate(self):
        api = BinanceRestApi("key", "secret", DummySession(), testnet=True)

        with patch.object(api, "request", return_value=DummyResponse(200, {})) as request:
            response = api.get_commission_rate("ltcusdt")

        self.assertEqual(response.status_code, 200)
        request.assert_called_once_with(
            "GET",
            EP_COMMISSION_RATE,
            {"symbol": "LTCUSDT"},
            signed=True,
        )
        self.assertGreaterEqual(api.endpoint_intervals[EP_COMMISSION_RATE], 1.0)

    def test_rpi_market_order_fails_at_rest_boundary(self):
        api = BinanceRestApi("key", "secret", DummySession(), testnet=True)
        order = OrderRequest(
            symbol="LTCUSDT",
            price=100.0,
            volume=0.1,
            side="BUY",
            order_type="MARKET",
            time_in_force="RPI",
        )

        with self.assertRaisesRegex(ValueError, "RPI requires a LIMIT order"):
            api.new_order(order)

    def test_countdown_cancel_all_maps_dead_man_switch_parameters(self):
        api = BinanceRestApi("key", "secret", DummySession(), testnet=True)

        with patch.object(api, "request", return_value=DummyResponse(200, {})) as request:
            response = api.set_countdown_cancel_all("btcusdt", 120_000)

        self.assertEqual(response.status_code, 200)
        request.assert_called_once_with(
            "POST",
            EP_COUNTDOWN_CANCEL_ALL,
            {
                "symbol": "BTCUSDT",
                "countdownTime": 120_000,
            },
            signed=True,
        )

    @patch("gateway.binance.rest_api.requests.Request", DummyRequest)
    @patch(
        "gateway.binance.rest_api.time_service.synchronize_now",
        return_value=True,
    )
    def test_timestamp_error_resyncs_and_retries(self, sync_mock):
        session = SequenceSession(
            [
                DummyResponse(400, {"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."}),
                DummyResponse(200, {"ok": True}),
            ]
        )
        api = BinanceRestApi("key", "secret", session, testnet=True)
        api.max_retries = 2

        response = api.request("GET", EP_POSITION_RISK, signed=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(sync_mock.call_count, 1)

    @patch("gateway.binance.rest_api.requests.Request", DummyRequest)
    def test_order_clock_guard_runs_after_throttle_before_send(self):
        session = SequenceSession([DummyResponse(200, {"orderId": 1})])
        api = BinanceRestApi("key", "secret", session, testnet=True)
        api.order_clock_guard = lambda: (
            False,
            "CLOCK_UNHEALTHY",
            "calibration stale",
        )
        order = OrderRequest(
            symbol="BTCUSDT",
            price=100.0,
            volume=0.1,
            side="BUY",
        )

        response = api.new_order(order, "clock-final-gate")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "CLOCK_UNHEALTHY")
        self.assertEqual(len(session.responses), 1)

    @patch("gateway.binance.rest_api.requests.Request", DummyRequest)
    def test_order_clock_guard_allows_reduce_only(self):
        session = SequenceSession([DummyResponse(200, {"orderId": 1})])
        api = BinanceRestApi("key", "secret", session, testnet=True)
        api.order_clock_guard = lambda: (
            False,
            "CLOCK_UNHEALTHY",
            "calibration stale",
        )
        order = OrderRequest(
            symbol="BTCUSDT",
            price=100.0,
            volume=0.1,
            side="SELL",
            reduce_only=True,
        )

        response = api.new_order(order, "clock-reduce-only")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(session.responses), 0)


class RPICoreIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.original_contracts = dict(ref_data_manager.contracts)
        ref_data_manager.contracts = {
            "LTCUSDT": self._contract("LTCUSDT", supports_rpi=True),
            "SOLUSDT": self._contract("SOLUSDT", supports_rpi=False),
        }

    def tearDown(self):
        ref_data_manager.contracts = self.original_contracts

    @staticmethod
    def _contract(symbol: str, *, supports_rpi: bool) -> ContractInfo:
        permissions = frozenset({"RPI"}) if supports_rpi else frozenset()
        return ContractInfo(
            symbol=symbol,
            tick_size=0.01,
            step_size=0.001,
            min_qty=0.001,
            min_notional=5.0,
            price_precision=2,
            qty_precision=3,
            status="TRADING",
            permissions=permissions,
        )

    def test_exchange_info_permission_sets_drive_rpi_capability(self):
        payload = {
            "symbols": [
                {
                    "symbol": "LTCUSDT",
                    "status": "TRADING",
                    "permissionSets": ["GRID", "RPI"],
                    # The current API does not enumerate RPI in timeInForce.
                    "timeInForce": ["GTC", "IOC", "GTX"],
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {
                            "filterType": "LOT_SIZE",
                            "stepSize": "0.001",
                            "minQty": "0.001",
                        },
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                },
                {
                    "symbol": "SOLUSDT",
                    "status": "TRADING",
                    "permissionSets": ["GRID"],
                    "timeInForce": ["GTC", "IOC", "GTX", "RPI"],
                    "filters": [],
                },
            ]
        }
        response = Mock()
        response.json.return_value = payload

        with patch("data.ref_data.requests.get", return_value=response):
            ref_data_manager.init(testnet=False)

        self.assertTrue(ref_data_manager.supports_rpi("ltcusdt"))
        self.assertFalse(ref_data_manager.supports_rpi("SOLUSDT"))

    def test_rpi_listing_script_filters_and_renders_supported_contracts(self):
        symbols = [
            {
                "symbol": "LTCUSDT",
                "contractType": "PERPETUAL",
                "status": "TRADING",
                "baseAsset": "LTC",
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
                "permissionSets": [["GRID", "RPI"]],
            },
            {
                "symbol": "SOLUSDT",
                "contractType": "PERPETUAL",
                "status": "TRADING",
                "baseAsset": "SOL",
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
                "permissionSets": ["GRID"],
            },
            {
                "symbol": "OLDUSDT",
                "contractType": "PERPETUAL",
                "status": "SETTLING",
                "baseAsset": "OLD",
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
                "permissionSets": ["RPI"],
            },
        ]

        contracts = select_rpi_contracts(symbols)

        self.assertEqual([contract["symbol"] for contract in contracts], ["LTCUSDT"])
        self.assertIn("LTCUSDT", render_csv(contracts))
        rendered_json = render_json(
            contracts,
            endpoint="https://example.test/exchangeInfo",
            server_time=1_700_000_000_000,
        )
        self.assertIn('"count": 1', rendered_json)
        self.assertIn('"symbol": "LTCUSDT"', rendered_json)

    def test_rpi_model_and_validator_are_fail_closed(self):
        validator = OrderValidator({})
        supported = OrderIntent(
            "alpha", "LTCUSDT", Side.BUY, 100.0, 0.1, time_in_force="rpi"
        )
        unsupported = OrderIntent(
            "alpha", "SOLUSDT", Side.BUY, 100.0, 0.1, time_in_force=TIF_RPI
        )
        unknown = OrderIntent(
            "alpha", "UNKNOWN", Side.BUY, 100.0, 0.1, time_in_force=TIF_RPI
        )

        self.assertTrue(supported.is_rpi)
        self.assertTrue(supported.is_post_only)
        self.assertEqual(validator.validate_params(supported), (True, ""))
        self.assertEqual(
            validator.validate_params(unsupported),
            (False, "rpi_unsupported_symbol:SOLUSDT"),
        )
        self.assertEqual(
            validator.validate_params(unknown),
            (False, "rpi_capability_unknown:UNKNOWN"),
        )

    def test_legacy_post_only_is_gtx_but_conflicting_tif_is_rejected(self):
        legacy = OrderIntent(
            "alpha",
            "LTCUSDT",
            Side.BUY,
            100.0,
            0.1,
            time_in_force="GTC",
            is_post_only=True,
        )
        conflicting = OrderIntent(
            "alpha",
            "LTCUSDT",
            Side.BUY,
            100.0,
            0.1,
            time_in_force="IOC",
            is_post_only=True,
        )

        self.assertEqual(legacy.time_in_force, TIF_GTX)
        self.assertEqual(
            Order("legacy", legacy).to_record()["intent"]["time_in_force"],
            TIF_GTX,
        )
        self.assertEqual(conflicting.time_in_force, "IOC")
        self.assertEqual(
            OrderValidator({}).validate_params(conflicting),
            (False, "post_only_incompatible_time_in_force:IOC"),
        )

    def test_recovery_and_snapshot_preserve_rpi_semantics(self):
        oms = object.__new__(OMS)
        oms.orders = {}
        oms.exchange_id_map = {}
        oms._rpi_calibration = {"enabled": False}
        oms._audit = lambda *args, **kwargs: None
        order = oms._create_recovered_order(
            {
                "symbol": "LTCUSDT",
                "orderId": 42,
                "clientOrderId": "rpi-recovered",
                "side": "BUY",
                "origQty": "0.1",
                "price": "100",
                "type": "LIMIT",
                "timeInForce": "RPI",
                "reduceOnly": False,
            }
        )

        snapshot = order.to_snapshot()
        self.assertEqual(order.intent.time_in_force, TIF_RPI)
        self.assertTrue(snapshot.is_post_only)
        self.assertTrue(snapshot.is_rpi)

    def test_local_stp_skips_unmatchable_rpi_api_pair(self):
        oms = object.__new__(OMS)
        oms.self_trade_prevention_enabled = True
        oms.local_self_cross_check_enabled = True
        resting = Order(
            "rpi-resting",
            OrderIntent(
                "alpha",
                "LTCUSDT",
                Side.SELL,
                100.0,
                0.1,
                time_in_force=TIF_RPI,
            ),
        )
        resting.mark_submitting()
        resting.mark_new(exchange_oid="exchange-rpi-resting")
        oms.orders = {resting.client_oid: resting}
        incoming = OrderIntent(
            "alpha", "LTCUSDT", Side.BUY, 101.0, 0.1, time_in_force="IOC"
        )

        self.assertEqual(
            oms._get_self_trade_prevention_rejection_locked(incoming),
            "",
        )

    def test_rpi_fee_fallback_uses_final_symbol_rate(self):
        oms = object.__new__(OMS)
        oms.config = {
            "backtest": {
                "maker_fee": 0.0002,
                "taker_fee": 0.0005,
                "rpi_commission_rate": 0.0001,
                "rpi_commission_rates": {"LTCUSDT": 0.00015},
            }
        }
        order = Order(
            "rpi-fee",
            OrderIntent(
                "alpha",
                "LTCUSDT",
                Side.BUY,
                100.0,
                0.1,
                time_in_force=TIF_RPI,
            ),
        )

        self.assertAlmostEqual(oms._get_fee_rate(order, is_maker=True), 0.00015)


if __name__ == "__main__":
    unittest.main()
