import unittest
from unittest.mock import patch

from gateway.binance.ws_api import BinanceWsApi


class BinanceWebSocketRouteTests(unittest.TestCase):
    def test_production_streams_use_migrated_route_families(self):
        api = BinanceWsApi(lambda _message: None, lambda _error: None)

        with patch.object(api, "_start_thread") as start_thread:
            api.start_market_stream(["BTCUSDT"])
            api.start_user_stream("listen-key")

        self.assertEqual(
            start_thread.call_args_list[0].args,
            (
                "wss://fstream.binance.com/public/stream?"
                "streams=btcusdt@depth@100ms",
                "PublicWS",
            ),
        )
        self.assertEqual(
            start_thread.call_args_list[1].args,
            (
                "wss://fstream.binance.com/market/stream?"
                "streams=btcusdt@aggTrade/btcusdt@markPrice@1s",
                "MarketWS",
            ),
        )
        self.assertEqual(
            start_thread.call_args_list[2].args,
            (
                "wss://fstream.binance.com/private/ws/listen-key",
                "UserWS",
            ),
        )

    def test_testnet_keeps_legacy_combined_stream_layout(self):
        api = BinanceWsApi(
            lambda _message: None,
            lambda _error: None,
            testnet=True,
        )

        with patch.object(api, "_start_thread") as start_thread:
            api.start_market_stream(["ETHUSDT"])
            api.start_user_stream("test-listen-key")

        self.assertEqual(
            start_thread.call_args_list[0].args,
            (
                "wss://stream.binancefuture.com/stream?"
                "streams=ethusdt@depth@100ms",
                "PublicWS",
            ),
        )
        self.assertEqual(
            start_thread.call_args_list[1].args,
            (
                "wss://stream.binancefuture.com/stream?"
                "streams=ethusdt@aggTrade/ethusdt@markPrice@1s",
                "MarketWS",
            ),
        )
        self.assertEqual(
            start_thread.call_args_list[2].args,
            (
                "wss://stream.binancefuture.com/ws/test-listen-key",
                "UserWS",
            ),
        )

    def test_close_during_socket_construction_prevents_run_forever(self):
        api = BinanceWsApi(lambda _message: None, lambda _error: None)
        api.active = True
        instances = []

        class ClosingSocket:
            def __init__(self, *_args, **_kwargs):
                self.closed = False
                self.ran = False
                instances.append(self)
                api.close()

            def run_forever(self, **_kwargs):
                self.ran = True

            def close(self):
                self.closed = True

        with patch(
            "gateway.binance.ws_api.websocket.WebSocketApp",
            ClosingSocket,
        ):
            api._run("wss://example.invalid", "PublicWS")

        self.assertEqual(len(instances), 1)
        self.assertTrue(instances[0].closed)
        self.assertFalse(instances[0].ran)
        self.assertEqual(api.stream_apps, {})

    def test_late_open_after_close_is_rejected(self):
        api = BinanceWsApi(lambda _message: None, lambda _error: None)
        api.close()

        class LateSocket:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        socket = LateSocket()
        api._handle_open("UserWS", socket)

        self.assertTrue(socket.closed)
        self.assertFalse(api.connected_events["UserWS"].is_set())
        self.assertNotIn("UserWS", api.stream_apps)


if __name__ == "__main__":
    unittest.main()
