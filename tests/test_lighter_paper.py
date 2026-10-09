import json
import time
import unittest
from unittest.mock import patch

from data.ref_data import (
    ContractInfo,
    parse_lighter_order_book_details,
    ref_data_manager,
)
from event.type import (
    EVENT_AGG_TRADE,
    EVENT_MARK_PRICE,
    EVENT_ORDERBOOK,
    TIF_GTX,
)
from gateway.lighter.market_data import (
    LighterMarketTranslator,
    resubscribe_book_frames,
    subscribe_frames,
    timestamp_ms,
)
from gateway.lighter.paper_gateway import LighterPaperGateway
from gateway.lighter.public_ws import STREAM_NAME, LighterPublicWs
from gateway.paper import paper_gateway_type
from infrastructure.config_schema import validate_composed_config
from infrastructure.venue import configured_venue
from strategy.execution_mode import resolve_execution_mode
from tests.test_paper_gateway import (
    DispatchingEngine,
    DummyPublicSession,
    make_gateway_config,
)
from tests.test_strategy_oms_coordination import (
    GLFTStrategy,
    PassiveQuoteOMS,
)

SYMBOL = "ETHUSDC"
NOW_MS = 1_790_000_000_000


def book_frame(kind, *, nonce, begin_nonce=None, bids=(), asks=(), ts=NOW_MS):
    order_book = {
        "code": 0,
        "bids": [{"price": p, "size": s} for p, s in bids],
        "asks": [{"price": p, "size": s} for p, s in asks],
        "nonce": nonce,
        "offset": 1,
    }
    if begin_nonce is not None:
        order_book["begin_nonce"] = begin_nonce
    return {
        "type": kind,
        "channel": "order_book:0",
        "order_book": order_book,
        "timestamp": ts,
    }


def eth_contract():
    return ContractInfo(
        symbol=SYMBOL,
        tick_size=0.01,
        step_size=0.0001,
        min_qty=0.005,
        min_notional=10.0,
        price_precision=2,
        qty_precision=4,
        market_id=0,
    )


class LighterReferenceDataTests(unittest.TestCase):
    def test_order_book_details_become_usdc_contracts(self):
        contracts = parse_lighter_order_book_details(
            {
                "code": 200,
                "order_book_details": [
                    {
                        "symbol": "ETH",
                        "market_id": 0,
                        "market_type": "perp",
                        "status": "active",
                        "min_base_amount": "0.0050",
                        "min_quote_amount": "10.000000",
                        "size_decimals": 4,
                        "price_decimals": 2,
                    },
                    {
                        "symbol": "OLD",
                        "market_id": 7,
                        "market_type": "perp",
                        "status": "inactive",
                        "min_base_amount": "1",
                        "min_quote_amount": "10",
                        "size_decimals": 0,
                        "price_decimals": 5,
                    },
                ],
            }
        )
        eth = contracts["ETHUSDC"]
        self.assertEqual(eth.market_id, 0)
        self.assertAlmostEqual(eth.tick_size, 0.01)
        self.assertAlmostEqual(eth.step_size, 0.0001)
        self.assertAlmostEqual(eth.min_qty, 0.005)
        self.assertAlmostEqual(eth.min_notional, 10.0)
        self.assertEqual(eth.status, "TRADING")
        self.assertFalse(eth.supports_rpi)
        self.assertEqual(contracts["OLDUSDC"].status, "BREAK")
        self.assertAlmostEqual(contracts["OLDUSDC"].step_size, 1.0)


class LighterTranslatorTests(unittest.TestCase):
    def setUp(self):
        self.translator = LighterMarketTranslator({0: SYMBOL})

    def test_snapshot_maps_nonce_to_last_update_id(self):
        snapshot, records = self.translator.translate(
            book_frame(
                "subscribed/order_book",
                nonce=100,
                bids=[("2000.00", "1.5")],
                asks=[("2000.10", "2.0")],
            )
        )
        self.assertEqual(records, [])
        self.assertEqual(snapshot.symbol, SYMBOL)
        self.assertEqual(snapshot.snapshot["lastUpdateId"], 100)
        self.assertEqual(snapshot.snapshot["bids"], [["2000.00", "1.5"]])

    def test_update_maps_begin_nonce_to_previous_final_id(self):
        _, [record] = self.translator.translate(
            book_frame(
                "update/order_book",
                nonce=105,
                begin_nonce=100,
                asks=[("2000.10", "0")],
            )
        )
        self.assertEqual(record.stream, "ethusdc@depth")
        self.assertEqual(record.data["pu"], 100)
        self.assertEqual(record.data["U"], 101)
        self.assertEqual(record.data["u"], 105)
        self.assertEqual(record.data["a"], [["2000.10", "0"]])
        self.assertEqual(record.data["E"], NOW_MS)

    def test_trades_set_taker_side_and_skip_subscription_replay(self):
        frame = {
            "channel": "trade:0",
            "nonce": 9,
            "trades": [
                {
                    "trade_id": 12,
                    "price": "2000.10",
                    "size": "0.3",
                    "is_maker_ask": True,
                    "timestamp": NOW_MS,
                },
                {
                    "trade_id": 11,
                    "price": "2000.00",
                    "size": "0.1",
                    "is_maker_ask": False,
                    "timestamp": NOW_MS,
                },
            ],
            "type": "subscribed/trade",
        }
        self.assertEqual(self.translator.translate(frame), (None, []))
        frame["type"] = "update/trade"
        _, records = self.translator.translate(frame)
        self.assertEqual([r.data["a"] for r in records], [11, 12])
        # Maker bid -> seller was the taker -> maker is buyer.
        self.assertTrue(records[0].data["m"])
        self.assertFalse(records[1].data["m"])
        self.assertEqual(records[1].stream, "ethusdc@aggtrade")

    def test_market_stats_becomes_mark_record(self):
        _, [record] = self.translator.translate(
            {
                "channel": "market_stats:0",
                "market_stats": {
                    "market_id": 0,
                    "mark_price": "2000.05",
                    "index_price": "2000.00",
                    "current_funding_rate": "0.0001",
                },
                "timestamp": NOW_MS,
                "type": "update/market_stats",
            }
        )
        self.assertEqual(record.stream, "ethusdc@markprice")
        self.assertEqual(record.data["p"], "2000.05")
        self.assertEqual(record.data["T"], 0)

    def test_unknown_market_is_ignored(self):
        frame = book_frame("update/order_book", nonce=2, begin_nonce=1)
        frame["channel"] = "order_book:42"
        self.assertEqual(self.translator.translate(frame), (None, []))
        self.assertEqual(self.translator.unknown_market_ids, {42})

    def test_timestamp_units(self):
        self.assertEqual(timestamp_ms(1_790_000_000), NOW_MS)
        self.assertEqual(timestamp_ms(NOW_MS), NOW_MS)
        self.assertEqual(timestamp_ms(NOW_MS * 1000), NOW_MS)

    def test_subscription_frames(self):
        self.assertEqual(
            [f["channel"] for f in subscribe_frames([0])],
            ["order_book/0", "trade/0", "market_stats/0"],
        )
        self.assertEqual(
            [f["type"] for f in resubscribe_book_frames(0)],
            ["unsubscribe", "subscribe"],
        )


class FakeWsApp:
    def __init__(self, on_send=None):
        self.sent = []
        self.on_send = on_send

    def send(self, payload):
        self.sent.append(json.loads(payload))
        if self.on_send is not None:
            self.on_send(json.loads(payload))

    def close(self):
        return None


class LighterPublicWsTests(unittest.TestCase):
    def make_ws(self):
        self.records = []
        self.errors = []
        ws = LighterPublicWs(
            lambda stream, data: self.records.append((stream, data)),
            self.errors.append,
            market_ids={SYMBOL: 0},
        )
        ws.active = True
        return ws

    def test_open_subscribes_and_ping_gets_pong(self):
        ws = self.make_ws()
        app = FakeWsApp()
        ws._handle_open(STREAM_NAME, app)
        self.assertEqual(len(app.sent), 3)
        self.assertTrue(ws.wait_until_connected(timeout_sec=0.1))
        ws._on_raw_message(json.dumps({"type": "ping"}))
        self.assertEqual(app.sent[-1], {"type": "pong"})

    def test_fetch_snapshot_uses_pending_then_resubscribes(self):
        ws = self.make_ws()

        def deliver(frame):
            if frame["type"] == "subscribe":
                ws._on_raw_message(
                    json.dumps(
                        book_frame(
                            "subscribed/order_book",
                            nonce=200,
                            bids=[("1999.00", "1")],
                            asks=[("2001.00", "1")],
                        )
                    )
                )

        app = FakeWsApp(on_send=deliver)
        ws._handle_open(STREAM_NAME, app)
        # The initial subscribe already delivered one snapshot.
        first = ws.fetch_snapshot(SYMBOL, timeout_sec=0.1)
        self.assertEqual(first["lastUpdateId"], 200)
        app.sent.clear()
        second = ws.fetch_snapshot(SYMBOL, timeout_sec=0.5)
        self.assertEqual(second["lastUpdateId"], 200)
        self.assertEqual(
            [f["type"] for f in app.sent],
            ["unsubscribe", "subscribe"],
        )

    def test_malformed_frame_reports_handler_failure(self):
        ws = self.make_ws()
        ws._on_raw_message(
            json.dumps({"type": "update/order_book", "channel": "order_book:0"})
        )
        self.assertEqual(self.errors[0]["kind"], "handler_failure")


class FakeSnapshotWs:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def fetch_snapshot(self, symbol, timeout_sec=10.0):
        return self.snapshot

    def close(self):
        return True


class LighterPaperGatewayTests(unittest.TestCase):
    def setUp(self):
        self.session_patch = patch(
            "gateway.binance.paper_gateway.requests.Session",
            side_effect=DummyPublicSession,
        )
        self.session_patch.start()
        self.contracts_patch = patch.dict(
            ref_data_manager.contracts,
            {SYMBOL: eth_contract()},
            clear=True,
        )
        self.contracts_patch.start()
        self.engine = DispatchingEngine()
        config = make_gateway_config()
        config["symbols"] = [SYMBOL]
        config["execution"]["venue"] = "lighter"
        self.gateway = LighterPaperGateway(self.engine, config)

    def tearDown(self):
        try:
            if self.gateway._worker_running:
                self.gateway.close()
        finally:
            self.contracts_patch.stop()
            self.session_patch.stop()

    def record(self, frame):
        translator = LighterMarketTranslator({0: SYMBOL})
        _, records = translator.translate(frame)
        for record in records:
            self.gateway._on_lighter_record(
                record.stream,
                record.data,
                expected_generation=self.generation,
            )

    def test_snapshot_and_continuous_updates_publish_books(self):
        gateway = self.gateway
        self.assertEqual(gateway.gateway_name, "LIGHTER_PAPER")
        self.assertEqual(gateway.balance_asset, "USDC")
        self.assertEqual(gateway._market_ids(), {SYMBOL: 0})
        gateway._start_worker()
        self.generation = gateway._reset_public_books()
        now_ms = int(time.time() * 1000)
        gateway.ws = FakeSnapshotWs(
            LighterMarketTranslator({0: SYMBOL})
            .translate(
                book_frame(
                    "subscribed/order_book",
                    nonce=100,
                    bids=[("2000.00", "1.0")],
                    asks=[("2000.10", "1.0")],
                    ts=now_ms,
                )
            )[0]
            .snapshot
        )
        self.assertTrue(
            gateway._resync_book(SYMBOL, expected_generation=self.generation)
        )
        self.record(
            book_frame(
                "update/order_book",
                nonce=103,
                begin_nonce=100,
                bids=[("2000.05", "0.5")],
                ts=now_ms,
            )
        )
        self.record(
            {
                "channel": "market_stats:0",
                "market_stats": {
                    "mark_price": "2000.06",
                    "index_price": "2000.00",
                    "current_funding_rate": "0.0",
                },
                "timestamp": now_ms,
                "type": "update/market_stats",
            }
        )
        self.record(
            {
                "channel": "trade:0",
                "trades": [
                    {
                        "trade_id": 1,
                        "price": "2000.10",
                        "size": "0.2",
                        "is_maker_ask": True,
                        "timestamp": now_ms,
                    }
                ],
                "type": "update/trade",
            }
        )
        books = [
            e.data for e in self.engine.events if e.type == EVENT_ORDERBOOK
        ]
        self.assertEqual(books[-1].best_bid_price, 2000.05)
        self.assertEqual(books[-1].best_ask_price, 2000.10)
        self.assertTrue(
            any(e.type == EVENT_MARK_PRICE for e in self.engine.events)
        )
        self.assertTrue(
            any(e.type == EVENT_AGG_TRADE for e in self.engine.events)
        )
        self.assertTrue(gateway._wait_for_initial_marks(self.generation))

        # A skipped nonce is a gap: the book stops publishing until resync.
        published = len(books)
        with patch.object(gateway, "_launch_book_recovery") as recovery:
            self.record(
                book_frame(
                    "update/order_book",
                    nonce=110,
                    begin_nonce=105,
                    bids=[("2000.07", "0.5")],
                    ts=now_ms,
                )
            )
        recovery.assert_called_once()
        books = [
            e.data for e in self.engine.events if e.type == EVENT_ORDERBOOK
        ]
        self.assertEqual(len(books), published)


class LighterVenueConfigTests(unittest.TestCase):
    def test_venue_defaults_to_binance(self):
        self.assertEqual(configured_venue({}), "binance")
        self.assertEqual(
            configured_venue({"execution": {"venue": "lighter"}}),
            "lighter",
        )
        with self.assertRaises(ValueError):
            configured_venue({"execution": {"venue": "okx"}})

    def test_paper_gateway_factory_picks_lighter(self):
        self.assertIs(
            paper_gateway_type({"execution": {"venue": "lighter"}}),
            LighterPaperGateway,
        )

    def test_lighter_is_paper_only(self):
        with self.assertRaisesRegex(Exception, "Paper-only"):
            validate_composed_config(
                {"execution": {"mode": "live", "venue": "lighter"}}
            )

    def test_lighter_market_mode_and_post_only_route(self):
        self.assertEqual(
            resolve_execution_mode(
                {"execution_modes": {"lighter": "market"}},
                "lighter",
            ),
            "market",
        )
        oms = PassiveQuoteOMS()
        oms.config["execution"]["venue"] = "lighter"
        strategy = GLFTStrategy(
            DispatchingEngine(),
            oms,
            strategy_config={"use_rpi": True},
        )
        self.assertEqual(strategy.execution_venue, "lighter")
        self.assertEqual(
            strategy.resolve_passive_time_in_force(SYMBOL, use_rpi=True),
            TIF_GTX,
        )
