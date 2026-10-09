import json
import time
import unittest
from unittest.mock import patch

from data.ref_data import ContractInfo, parse_grvt_instruments, ref_data_manager
from event.type import (
    EVENT_AGG_TRADE,
    EVENT_EXCHANGE_ORDER_UPDATE,
    EVENT_MARK_PRICE,
    EVENT_ORDERBOOK,
    TIF_GTC,
    TIF_GTX,
    TIF_IOC,
    GatewayState,
    OrderRequest,
)
from gateway.grvt.market_data import (
    GrvtMarketTranslator,
    resubscribe_book_frames,
    subscribe_frames,
    timestamp_ms,
    trade_id_number,
)
from gateway.grvt.paper_gateway import GrvtPaperGateway
from gateway.grvt.public_ws import STREAM_NAME, GrvtPublicWs
from gateway.paper import paper_gateway_type
from infrastructure.config_scaling import load_config_document
from infrastructure.config_schema import validate_composed_config
from infrastructure.venue import configured_venue
from strategy.execution_mode import resolve_execution_mode
from tests.test_lighter_paper import FakeSnapshotWs, FakeWsApp
from tests.test_paper_gateway import SYMBOL as PAPER_SYMBOL
from tests.test_paper_gateway import (
    DispatchingEngine,
    DummyPublicSession,
    make_book,
    make_contract,
    make_gateway_config,
    wait_until,
)

SYMBOL = "ETHUSDT"
INSTRUMENT = "ETH_USDT_Perp"
NOW_NS = 1_790_000_000_000_000_000


def book_message(sequence, *, previous=0, bids=(), asks=(), event_ns=NOW_NS):
    return {
        "stream": "v1.book.d",
        "selector": f"{INSTRUMENT}@50",
        "sequence_number": str(sequence),
        "prev_sequence_number": str(previous),
        "feed": {
            "event_time": str(event_ns),
            "instrument": INSTRUMENT,
            "bids": [
                {"price": p, "size": s, "num_orders": 1} for p, s in bids
            ],
            "asks": [
                {"price": p, "size": s, "num_orders": 1} for p, s in asks
            ],
        },
    }


def trade_message(sequence, *, trade_id, taker_buys, event_ns=NOW_NS):
    return {
        "stream": "v1.trade",
        "selector": f"{INSTRUMENT}@50",
        "sequence_number": str(sequence),
        "prev_sequence_number": str(max(0, sequence - 1)),
        "feed": {
            "event_time": str(event_ns),
            "instrument": INSTRUMENT,
            "is_taker_buyer": taker_buys,
            "size": "0.2",
            "price": "2000.1",
            "mark_price": "2000.0",
            "index_price": "2000.0",
            "interest_rate": "0",
            "forward_price": "0",
            "trade_id": trade_id,
            "venue": "ORDERBOOK",
            "is_rpi": False,
        },
    }


def ticker_message(sequence=0, *, event_ns=NOW_NS):
    return {
        "stream": "v1.ticker.s",
        "selector": f"{INSTRUMENT}@500",
        "sequence_number": str(sequence),
        "prev_sequence_number": "0",
        "feed": {
            "event_time": str(event_ns),
            "instrument": INSTRUMENT,
            "mark_price": "2000.06",
            "index_price": "2000.0",
            "funding_rate": "0.01",
            "next_funding_time": str(NOW_NS + 3_600_000_000_000),
        },
    }


def eth_contract():
    return ContractInfo(
        symbol=SYMBOL,
        tick_size=0.01,
        step_size=0.01,
        min_qty=0.01,
        min_notional=0.0,
        price_precision=2,
        qty_precision=2,
        venue_symbol=INSTRUMENT,
    )


class GrvtReferenceDataTests(unittest.TestCase):
    def test_perpetual_instruments_become_contracts(self):
        contracts = parse_grvt_instruments(
            {
                "result": [
                    {
                        "instrument": INSTRUMENT,
                        "base": "ETH",
                        "quote": "USDT",
                        "kind": "PERPETUAL",
                        "tick_size": "0.01",
                        "min_size": "0.01",
                        "base_decimals": 9,
                        "quote_decimals": 6,
                    },
                    {
                        "instrument": "BTC_USDT_Fut_20Oct23",
                        "base": "BTC",
                        "quote": "USDT",
                        "kind": "FUTURE",
                        "tick_size": "0.1",
                        "min_size": "0.001",
                    },
                ]
            }
        )
        self.assertEqual(list(contracts), [SYMBOL])
        eth = contracts[SYMBOL]
        self.assertEqual(eth.venue_symbol, INSTRUMENT)
        self.assertAlmostEqual(eth.tick_size, 0.01)
        self.assertAlmostEqual(eth.step_size, 0.01)
        self.assertAlmostEqual(eth.min_qty, 0.01)
        self.assertEqual((eth.price_precision, eth.qty_precision), (2, 2))
        self.assertEqual(eth.status, "TRADING")


class GrvtTranslatorTests(unittest.TestCase):
    def setUp(self):
        self.translator = GrvtMarketTranslator({INSTRUMENT: SYMBOL})
        self.translator.reset()

    def test_first_book_message_is_the_snapshot(self):
        snapshot, records = self.translator.translate(
            book_message(
                41,
                previous=40,
                bids=[("2000.0", "1.5")],
                asks=[("2000.1", "2")],
            )
        )
        self.assertEqual(records, [])
        self.assertEqual(snapshot.symbol, SYMBOL)
        self.assertEqual(snapshot.snapshot["lastUpdateId"], 41)
        self.assertEqual(snapshot.snapshot["bids"], [["2000.0", "1.5"]])
        self.assertEqual(snapshot.snapshot["E"], NOW_NS // 1_000_000)

    def test_delta_maps_previous_sequence_to_pu(self):
        self.translator.translate(book_message(0))
        snapshot, records = self.translator.translate(
            book_message(5, previous=4, bids=[("2000.0", "0")])
        )
        self.assertIsNone(snapshot)
        data = records[0].data
        self.assertEqual(records[0].stream, "ethusdt@depth")
        self.assertEqual((data["U"], data["u"], data["pu"]), (5, 5, 4))
        # Size zero removes the level, as in Binance depth updates.
        self.assertEqual(data["b"], [["2000.0", "0"]])

    def test_sequence_zero_is_a_fresh_snapshot(self):
        self.translator.translate(book_message(0))
        self.translator.translate(book_message(1, previous=0))
        snapshot, records = self.translator.translate(book_message(0))
        self.assertIsNotNone(snapshot)
        self.assertEqual(records, [])

    def test_trades_drop_replay_and_set_maker_side(self):
        self.assertEqual(
            self.translator.translate(
                trade_message(0, trade_id="9-1", taker_buys=True)
            ),
            (None, []),
        )
        _, records = self.translator.translate(
            trade_message(3, trade_id="12-2", taker_buys=True)
        )
        data = records[0].data
        self.assertEqual(records[0].stream, "ethusdt@aggtrade")
        self.assertEqual(data["a"], 12 * 100_000 + 2)
        self.assertFalse(data["m"])
        _, records = self.translator.translate(
            trade_message(4, trade_id="13-1", taker_buys=False)
        )
        self.assertTrue(records[0].data["m"])

    def test_ticker_becomes_mark_with_fractional_funding(self):
        _, records = self.translator.translate(ticker_message())
        data = records[0].data
        self.assertEqual(records[0].stream, "ethusdt@markprice")
        self.assertEqual(data["p"], "2000.06")
        self.assertEqual(data["i"], "2000.0")
        self.assertAlmostEqual(float(data["r"]), 0.0001)
        self.assertEqual(data["T"], NOW_NS // 1_000_000 + 3_600_000)

    def test_unknown_instrument_is_ignored(self):
        message = book_message(0)
        message["feed"]["instrument"] = "SOL_USDT_Perp"
        self.assertEqual(self.translator.translate(message), (None, []))
        self.assertIn("SOL_USDT_Perp", self.translator.unknown_instruments)

    def test_ids_and_timestamps(self):
        self.assertEqual(timestamp_ms(NOW_NS), NOW_NS // 1_000_000)
        self.assertEqual(timestamp_ms(1_790_000_000_000), 1_790_000_000_000)
        self.assertEqual(trade_id_number("77"), 7_700_000)
        self.assertEqual(trade_id_number("bad"), -1)

    def test_subscription_frames(self):
        frames = subscribe_frames([INSTRUMENT])
        self.assertEqual(
            [(f["method"], f["params"]["stream"]) for f in frames],
            [
                ("subscribe", "v1.book.d"),
                ("subscribe", "v1.trade"),
                ("subscribe", "v1.ticker.s"),
            ],
        )
        self.assertEqual(frames[0]["params"]["selectors"], [f"{INSTRUMENT}@50"])
        self.assertTrue(all(f["jsonrpc"] == "2.0" for f in frames))
        self.assertEqual(
            [f["method"] for f in resubscribe_book_frames(INSTRUMENT)],
            ["unsubscribe", "subscribe"],
        )


class GrvtPublicWsTests(unittest.TestCase):
    def make_ws(self):
        self.records = []
        self.errors = []
        ws = GrvtPublicWs(
            lambda stream, data: self.records.append((stream, data)),
            self.errors.append,
            instruments={SYMBOL: INSTRUMENT},
        )
        ws.active = True
        return ws

    def test_open_subscribes_and_acks_are_ignored(self):
        ws = self.make_ws()
        app = FakeWsApp()
        ws._handle_open(STREAM_NAME, app)
        self.assertEqual(len(app.sent), 3)
        self.assertTrue(ws.wait_until_connected(timeout_sec=0.1))
        ws._on_raw_message(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "result": {"stream": "v1.book.d", "subs": []},
                    "id": 1,
                }
            )
        )
        self.assertEqual((self.records, self.errors), ([], []))
        ws._on_raw_message(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": 1003, "message": "bad selector"},
                    "id": 2,
                }
            )
        )
        self.assertEqual(self.errors[0]["kind"], "venue_error")

    def test_fetch_snapshot_resubscribes_for_a_fresh_book(self):
        ws = self.make_ws()

        def deliver(frame):
            if frame["method"] == "subscribe" and frame["params"]["stream"] == "v1.book.d":
                ws._on_raw_message(
                    json.dumps(
                        book_message(
                            0,
                            bids=[("1999.0", "1")],
                            asks=[("2001.0", "1")],
                        )
                    )
                )

        app = FakeWsApp(on_send=deliver)
        ws._handle_open(STREAM_NAME, app)
        first = ws.fetch_snapshot(SYMBOL, timeout_sec=0.1)
        self.assertEqual(first["bids"], [["1999.0", "1"]])
        app.sent.clear()
        second = ws.fetch_snapshot(SYMBOL, timeout_sec=0.5)
        self.assertEqual(second["asks"], [["2001.0", "1"]])
        self.assertEqual(
            [f["method"] for f in app.sent],
            ["unsubscribe", "subscribe"],
        )

    def test_malformed_frame_reports_handler_failure(self):
        ws = self.make_ws()
        message = book_message(0)
        message["feed"]["bids"] = "oops"
        ws._on_raw_message(json.dumps(message))
        self.assertEqual(self.errors[0]["kind"], "handler_failure")


class GrvtPaperGatewayTests(unittest.TestCase):
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
        config["execution"]["venue"] = "grvt"
        self.gateway = GrvtPaperGateway(self.engine, config)
        self.translator = GrvtMarketTranslator({INSTRUMENT: SYMBOL})

    def tearDown(self):
        try:
            if self.gateway._worker_running:
                self.gateway.close()
        finally:
            self.contracts_patch.stop()
            self.session_patch.stop()

    def record(self, message):
        _, records = self.translator.translate(message)
        for record in records:
            self.gateway._on_venue_record(
                record.stream,
                record.data,
                expected_generation=self.generation,
            )

    def test_snapshot_and_continuous_updates_publish_books(self):
        gateway = self.gateway
        self.assertEqual(gateway.gateway_name, "GRVT_PAPER")
        self.assertEqual(gateway.balance_asset, "USDT")
        self.assertEqual(gateway._instruments(), {SYMBOL: INSTRUMENT})
        gateway._start_worker()
        self.generation = gateway._reset_public_books()
        now_ns = time.time_ns()
        gateway.ws = FakeSnapshotWs(
            self.translator.translate(
                book_message(
                    0,
                    bids=[("2000.00", "1.0")],
                    asks=[("2000.10", "1.0")],
                    event_ns=now_ns,
                )
            )[0].snapshot
        )
        self.assertTrue(
            gateway._resync_book(SYMBOL, expected_generation=self.generation)
        )
        self.record(
            book_message(
                1,
                previous=0,
                bids=[("2000.05", "0.5")],
                event_ns=now_ns,
            )
        )
        self.record(ticker_message(event_ns=now_ns))
        self.record(trade_message(7, trade_id="5-1", taker_buys=True, event_ns=now_ns))
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

        # A skipped sequence number is a gap: publishing stops until resync.
        published = len(books)
        with patch.object(gateway, "_launch_book_recovery") as recovery:
            self.record(
                book_message(
                    9,
                    previous=8,
                    bids=[("2000.07", "0.5")],
                    event_ns=now_ns,
                )
            )
        recovery.assert_called_once()
        books = [
            e.data for e in self.engine.events if e.type == EVENT_ORDERBOOK
        ]
        self.assertEqual(len(books), published)


class GrvtSpeedBumpTests(unittest.TestCase):
    """GRVT delays every order that is not post-only by 25ms."""

    TAKER_MS = 120.0

    def setUp(self):
        self.session_patch = patch(
            "gateway.binance.paper_gateway.requests.Session",
            side_effect=DummyPublicSession,
        )
        self.session_patch.start()
        self.contract_patch = patch(
            "gateway.binance.paper_gateway.ref_data_manager.get_info",
            return_value=make_contract(),
        )
        self.contract_patch.start()
        self.engine = DispatchingEngine()
        config = make_gateway_config()
        config["execution"]["venue"] = "grvt"
        config["paper_trade"]["taker_order_delay_ms"] = self.TAKER_MS
        gateway = GrvtPaperGateway(self.engine, config)
        gateway._start_worker()
        gateway.active = True
        gateway._accepting_orders = True
        gateway.state = GatewayState.READY
        gateway._call_worker(
            "book",
            (gateway._book_feed_state.generation, make_book()),
        )
        self.gateway = gateway

    def tearDown(self):
        try:
            if self.gateway._worker_running:
                self.gateway.close()
        finally:
            self.contract_patch.stop()
            self.session_patch.stop()

    def statuses(self, client_oid):
        return [
            event.data.status
            for event in self.engine.events
            if event.type == EVENT_EXCHANGE_ORDER_UPDATE
            and event.data.client_oid == client_oid
        ]

    def submit(self, client_oid, **fields):
        request = OrderRequest(symbol=PAPER_SYMBOL, volume=0.1, **fields)
        self.gateway.send_order(request, client_oid)
        started = time.perf_counter()
        self.assertTrue(self.gateway.commit_order_submission(client_oid))
        return started

    def test_defaults_match_grvt(self):
        gateway = GrvtPaperGateway(self.engine, make_gateway_config())
        self.assertEqual(gateway.maker_order_delay_sec, 0.0)
        self.assertAlmostEqual(gateway.taker_order_delay_sec, 0.025)

    def test_only_post_only_orders_skip_the_speed_bump(self):
        delay = self.gateway._order_delay_sec
        taker = self.TAKER_MS / 1000.0
        cases = [
            (dict(price=100.0, side="BUY", time_in_force=TIF_GTX), 0.0),
            (dict(price=101.0, side="BUY", order_type="MARKET"), taker),
            (dict(price=101.0, side="BUY", time_in_force=TIF_IOC), taker),
            # A resting limit that is not post-only is still delayed.
            (dict(price=100.0, side="BUY", time_in_force=TIF_GTC), taker),
        ]
        for fields, expected in cases:
            request = OrderRequest(symbol=PAPER_SYMBOL, volume=0.1, **fields)
            self.assertAlmostEqual(delay(request), expected, msg=fields)

    def test_post_only_rests_immediately(self):
        self.submit("maker", price=100.0, side="BUY", time_in_force=TIF_GTX)
        self.assertEqual(self.statuses("maker"), ["NEW"])

    def test_market_order_fills_after_the_delay(self):
        started = self.submit(
            "taker", price=101.0, side="BUY", order_type="MARKET"
        )
        self.assertEqual(self.statuses("taker"), [])
        self.assertTrue(
            wait_until(lambda: "FILLED" in self.statuses("taker"), timeout=2.0)
        )
        self.assertGreaterEqual(
            time.perf_counter() - started,
            self.TAKER_MS / 1000.0,
        )


class GrvtVenueConfigTests(unittest.TestCase):
    def test_paper_gateway_factory_picks_grvt(self):
        self.assertIs(
            paper_gateway_type({"execution": {"venue": "grvt"}}),
            GrvtPaperGateway,
        )

    def test_grvt_is_paper_only(self):
        with self.assertRaisesRegex(Exception, "Paper-only"):
            validate_composed_config(
                {"execution": {"mode": "live", "venue": "grvt"}}
            )

    def test_grvt_execution_mode(self):
        self.assertEqual(
            resolve_execution_mode(
                {"execution_modes": {"grvt": "market"}},
                "grvt",
            ),
            "market",
        )
        self.assertEqual(resolve_execution_mode({}, "grvt"), "post_only")

    def test_grvt_profile_quotes_post_only(self):
        config = load_config_document("config.grvt.json")
        self.assertEqual(configured_venue(config), "grvt")
        self.assertEqual(
            resolve_execution_mode(config["strategy"], "grvt"),
            "post_only",
        )


if __name__ == "__main__":
    unittest.main()
