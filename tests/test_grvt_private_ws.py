"""GRVT private websocket handling with a fake websocket app (no network)."""

import json

from gateway.grvt.private_ws import STREAMS, GrvtPrivateWs


class Rest:
    sub_account_id = "8289849667772468"

    def auth_headers(self):
        return {"Cookie": "gravity=abc", "X-Grvt-Account-Id": "ACC1", "Content-Type": "application/json"}


class FakeSocket:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(json.loads(text))


def client(**kwargs):
    seen, gaps, status = [], [], []
    ws = GrvtPrivateWs(rest=Rest(), on_message=lambda stream, feed: seen.append((stream, feed)),
                       on_gap=lambda *gap: gaps.append(gap), on_status=status.append, **kwargs)
    return ws, seen, gaps, status


def test_subscribes_every_stream_for_the_sub_account():
    ws, *_ = client()
    socket = FakeSocket()
    ws._on_open(socket)
    assert [frame["params"] for frame in socket.sent] == [
        {"stream": stream, "selectors": ["8289849667772468"]} for stream in STREAMS]
    ws_one, *_ = client(instrument="XRP_USDT_Perp")
    assert ws_one.selector == "8289849667772468-XRP_USDT_Perp"


def test_acks_mark_streams_and_feeds_are_delivered_in_order():
    ws, seen, gaps, _ = client()
    ws.handle({"jsonrpc": "2.0", "id": 3, "result": {"stream": "v1.fill", "subs": ["8289849667772468"]}})
    assert ws.subscribed == {"v1.fill"}
    for sequence in (1, 2):
        ws.handle({"stream": "v1.fill", "selector": "8289849667772468", "sequence_number": str(sequence),
                   "feed": {"trade_id": str(sequence), "fee": "-0.0005"}})
    assert [feed["trade_id"] for _, feed in seen] == ["1", "2"] and gaps == []


def test_a_sequence_gap_is_reported_but_the_message_still_delivered():
    ws, seen, gaps, _ = client()
    for sequence in (5, 6, 9):
        ws.handle({"stream": "v1.state", "sequence_number": str(sequence), "feed": {"n": sequence}})
    assert gaps == [("v1.state", 7, 9)] and len(seen) == 3


def test_reconnect_starts_sequences_afresh():
    ws, _, gaps, status = client()
    ws.handle({"stream": "v1.order", "sequence_number": "40", "feed": {}})
    ws._on_open(FakeSocket())
    ws.handle({"stream": "v1.order", "sequence_number": "1", "feed": {}})
    assert gaps == [] and status[-1] == "connected"


def test_auth_headers_reach_the_socket_and_errors_are_reported():
    created = {}

    class App:
        def __init__(self, url, header=None, **_handlers):
            created.update(url=url, header=header)

        def run_forever(self, **_kwargs):
            ws.stop.set()

    ws, _, _, status = client(app_factory=App)
    ws._run()
    assert created["url"] == "wss://trades.grvt.io/ws/full"
    assert created["header"] == ["Cookie: gravity=abc", "X-Grvt-Account-Id: ACC1"]
    ws.handle({"jsonrpc": "2.0", "id": 1, "error": {"code": 1000, "message": "unauthenticated"}})
    assert status[-1].startswith("error:")
