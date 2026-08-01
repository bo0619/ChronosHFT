import threading
from types import SimpleNamespace

from data.orderbook import LocalOrderBook
from event.type import EVENT_ORDERBOOK, EVENT_SYSTEM_HEALTH, OrderBookGapError
from gateway.binance.paper_book_sync import PaperBookSynchronizer


SYMBOL = "BTCUSDT"


class _EventEngine:
    def __init__(self):
        self.events = []

    def put(self, event):
        self.events.append(event)


class _Owner:
    def __init__(self):
        self.symbols = [SYMBOL]
        self.orderbooks = {SYMBOL: LocalOrderBook(SYMBOL)}
        self.ws_buffer = {SYMBOL: []}
        self.book_resyncing = set()
        self.book_recovery_generation = {}
        self.book_recovery_tokens = {}
        self._book_recovery_token = 0
        self._book_generation = 1
        self._book_lock = threading.RLock()
        self._book_recovery_threads = set()
        self._book_recovery_stop = threading.Event()
        self._last_ws_mark_received_monotonic = {}
        self.publish_depth_levels = 5
        self.emit_full_orderbook_events = False
        self.max_orderbook_levels_per_side = 100
        self.max_delta_levels_per_side = 100
        self.max_book_buffer = 100
        self.max_book_recovery_threads = 2
        self.book_recovery_join_timeout_sec = 0.5
        self.rest = SimpleNamespace(get_depth_snapshot=lambda _symbol: None)
        self.event_engine = _EventEngine()
        self.submitted = []
        self.published = []
        self.faults = []
        self.sync = PaperBookSynchronizer(self)

    def _book_generation_matches_locked(self, expected_generation):
        return self.sync.generation_matches_locked(expected_generation)

    def _book_generation_is_current(self, expected_generation):
        return self.sync.generation_is_current(expected_generation)

    def _owns_book_recovery_locked(
        self,
        symbol,
        generation,
        recovery_token,
    ):
        return self.sync.owns_recovery_locked(
            symbol,
            generation,
            recovery_token,
        )

    def _release_book_recovery_locked(
        self,
        symbol,
        generation,
        recovery_token,
    ):
        return self.sync.release_recovery_locked(
            symbol,
            generation,
            recovery_token,
        )

    def _begin_book_recovery_locked(
        self,
        symbol,
        freeze_reason="",
        *,
        expected_generation=None,
    ):
        return self.sync.begin_recovery_locked(
            symbol,
            freeze_reason,
            expected_generation=expected_generation,
        )

    def _launch_book_recovery(self, recovery):
        return self.sync.launch_recovery(recovery)

    def _run_book_recovery(self, symbol, generation, recovery_token):
        return self.sync.run_recovery(symbol, generation, recovery_token)

    def _recover_orderbook(self, symbol, generation, recovery_token):
        return self.sync.recover_orderbook(symbol, generation, recovery_token)

    def _resync_book(
        self,
        symbol,
        *,
        expected_generation=None,
        recovery_token=None,
    ):
        return self.sync.resync_book(
            symbol,
            expected_generation=expected_generation,
            recovery_token=recovery_token,
        )

    def _publish_book_update(self, generation, **kwargs):
        return self.sync.publish_update(generation, **kwargs)

    def _full_matching_book(self, book):
        return self.sync.full_matching_book(book)

    def _submit_worker(self, kind, payload):
        self.submitted.append((kind, payload))
        return True

    @staticmethod
    def _stamp_market_dispatch(data):
        data.dispatch_timestamp = 1.0
        data.dispatch_monotonic = 2.0

    def on_market_data(self, event_type, data):
        self.published.append((event_type, data))

    def _fault(self, reason):
        self.faults.append(reason)


def test_new_recovery_token_supersedes_old_owner():
    owner = _Owner()

    first = owner.sync.begin_recovery_locked(SYMBOL, "FATAL_GAP")
    second = owner.sync.begin_recovery_locked(SYMBOL, "FATAL_GAP")

    assert first == (SYMBOL, 1, 1, "FATAL_GAP")
    assert second == (SYMBOL, 1, 2, "FATAL_GAP")
    assert not owner.sync.release_recovery_locked(SYMBOL, 1, 1)
    assert owner.book_recovery_tokens[SYMBOL] == 2
    assert owner.sync.release_recovery_locked(SYMBOL, 1, 2)


def test_recovery_success_only_clears_owned_freeze():
    owner = _Owner()
    owner.sync.begin_recovery_locked(SYMBOL, "FATAL_GAP")
    calls = []
    owner._resync_book = lambda symbol, **kwargs: calls.append(
        (symbol, kwargs)
    ) or True

    owner.sync.recover_orderbook(SYMBOL, 1, 1)

    assert calls == [
        (
            SYMBOL,
            {"expected_generation": 1, "recovery_token": 1},
        )
    ]
    assert owner.book_resyncing == set()
    assert [event.type for event in owner.event_engine.events] == [
        EVENT_SYSTEM_HEALTH
    ]
    assert owner.event_engine.events[0].data == (
        "CLEAR_SYMBOL:BTCUSDT:ORDERBOOK_RESYNCED:1"
    )


def test_gap_claims_recovery_before_launch_callback():
    owner = _Owner()

    class _BrokenBook:
        @staticmethod
        def process_delta(_delta):
            raise OrderBookGapError("forced gap")

    owner.orderbooks[SYMBOL] = _BrokenBook()
    owner.ws_buffer[SYMBOL] = None
    launched = []
    owner._launch_book_recovery = launched.append

    owner.sync.process_delta(
        SYMBOL,
        {"U": 2, "u": 2, "pu": 0, "b": [], "a": []},
        expected_generation=1,
    )

    assert launched == [(SYMBOL, 1, 1, "FATAL_GAP")]
    assert owner.book_recovery_tokens[SYMBOL] == 1
    assert owner.ws_buffer[SYMBOL] == []


def test_publish_rejects_stale_generation_and_replaced_book():
    owner = _Owner()
    expected_book = owner.orderbooks[SYMBOL]
    event_book = SimpleNamespace()
    matching_book = object()

    assert not owner.sync.publish_update(
        0,
        symbol=SYMBOL,
        expected_book=expected_book,
        event_book=event_book,
        matching_book=matching_book,
    )
    owner.orderbooks[SYMBOL] = LocalOrderBook(SYMBOL)
    assert not owner.sync.publish_update(
        1,
        symbol=SYMBOL,
        expected_book=expected_book,
        event_book=event_book,
        matching_book=matching_book,
    )
    assert owner.submitted == []
    assert owner.published == []

    expected_book = owner.orderbooks[SYMBOL]
    assert owner.sync.publish_update(
        1,
        symbol=SYMBOL,
        expected_book=expected_book,
        event_book=event_book,
        matching_book=matching_book,
    )
    assert owner.submitted == [("book", (1, matching_book))]
    assert owner.published == [(EVENT_ORDERBOOK, event_book)]
