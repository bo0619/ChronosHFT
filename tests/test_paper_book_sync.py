import threading
import time
from types import SimpleNamespace

from data.orderbook import LocalOrderBook
from event.type import Event, OrderBookGapError, EVENT_ORDERBOOK, EVENT_SYSTEM_HEALTH
from gateway.binance.paper_book_sync import (
    PaperBookFeedConfig,
    PaperBookFeedPort,
    PaperBookFeedState,
    PaperBookSynchronizer,
)


SYMBOL = "BTCUSDT"


class _TrackedRLock:
    """RLock with deterministic current-thread ownership visibility."""

    def __init__(self):
        self._lock = threading.RLock()
        self._owner = None
        self._depth = 0

    def acquire(self, *args, **kwargs):
        acquired = self._lock.acquire(*args, **kwargs)
        if acquired:
            ident = threading.get_ident()
            if self._owner == ident:
                self._depth += 1
            else:
                self._owner = ident
                self._depth = 1
        return acquired

    def release(self):
        if self._owner != threading.get_ident():
            raise RuntimeError("lock released by non-owner")
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
        self._lock.release()

    def held_by_current_thread(self):
        return self._owner == threading.get_ident()

    def _is_owned(self):
        return self.held_by_current_thread()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_args):
        self.release()


class _EventEngine:
    def __init__(self):
        self.events = []

    def put(self, event):
        self.events.append(event)


class _Harness:
    def __init__(self):
        self.state = PaperBookFeedState(
            symbols=(SYMBOL,),
            orderbooks={SYMBOL: LocalOrderBook(SYMBOL)},
            ws_buffer={SYMBOL: []},
            generation=1,
            lock=_TrackedRLock(),
        )
        self.rest = SimpleNamespace(get_depth_snapshot=lambda _symbol: None)
        self.event_engine = _EventEngine()
        self.submitted = []
        self.published = []
        self.faults = []
        self.sync = PaperBookSynchronizer(
            self.state,
            PaperBookFeedPort(
                fetch_depth_snapshot=lambda symbol: self.rest.get_depth_snapshot(symbol),
                submit_worker=lambda kind, payload: self._submit_worker(kind, payload),
                stamp_market_dispatch=self._stamp_market_dispatch,
                publish_market_data=lambda event_type, data: self.on_market_data(
                    event_type,
                    data,
                ),
                publish_health=lambda reason: self._publish_health(reason),
                report_fault=lambda reason: self._fault(reason),
                launch_recovery=lambda recovery: self._launch_book_recovery(recovery),
                run_recovery=lambda symbol, generation, token: self._run_book_recovery(
                    symbol,
                    generation,
                    token,
                ),
                resync_book=lambda symbol, **kwargs: self._resync_book(
                    symbol,
                    **kwargs,
                ),
                publish_book_update=lambda generation, **kwargs: (
                    self._publish_book_update(generation, **kwargs)
                ),
            ),
            PaperBookFeedConfig(
                publish_depth_levels=5,
                emit_full_orderbook_events=False,
                max_orderbook_levels_per_side=100,
                max_delta_levels_per_side=100,
                max_book_buffer=100,
                max_book_recovery_threads=2,
                book_recovery_join_timeout_sec=0.5,
            ),
        )

    def _launch_book_recovery(self, recovery):
        return self.sync.launch_recovery(recovery)

    def _run_book_recovery(self, symbol, generation, recovery_token):
        return self.sync.run_recovery(symbol, generation, recovery_token)

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

    def _submit_worker(self, kind, payload):
        self.submitted.append((kind, payload))
        return True

    @staticmethod
    def _stamp_market_dispatch(data):
        data.dispatch_timestamp = 1.0
        data.dispatch_monotonic = 2.0

    def on_market_data(self, event_type, data):
        self.published.append((event_type, data))

    def _publish_health(self, reason):
        self.event_engine.put(Event(EVENT_SYSTEM_HEALTH, reason))

    def _fault(self, reason):
        self.faults.append(reason)


def test_new_recovery_token_supersedes_old_owner():
    harness = _Harness()

    with harness.state.lock:
        first = harness.sync.begin_recovery_locked(SYMBOL, "FATAL_GAP")
        second = harness.sync.begin_recovery_locked(SYMBOL, "FATAL_GAP")

    assert first == (SYMBOL, 1, 1, "FATAL_GAP")
    assert second == (SYMBOL, 1, 2, "FATAL_GAP")
    assert not harness.sync.release_recovery_locked(SYMBOL, 1, 1)
    assert harness.state.recovery_tokens[SYMBOL] == 2
    assert harness.sync.release_recovery_locked(SYMBOL, 1, 2)


def test_recovery_success_only_clears_owned_freeze():
    harness = _Harness()
    with harness.state.lock:
        harness.sync.begin_recovery_locked(SYMBOL, "FATAL_GAP")
    calls = []
    harness._resync_book = lambda symbol, **kwargs: calls.append(
        (symbol, kwargs)
    ) or True

    harness.sync.recover_orderbook(SYMBOL, 1, 1)

    assert calls == [
        (
            SYMBOL,
            {"expected_generation": 1, "recovery_token": 1},
        )
    ]
    assert harness.state.resyncing == set()
    assert [event.type for event in harness.event_engine.events] == [
        EVENT_SYSTEM_HEALTH
    ]
    assert harness.event_engine.events[0].data == (
        "CLEAR_SYMBOL:BTCUSDT:ORDERBOOK_RESYNCED:1"
    )


def test_gap_claims_recovery_before_launch_callback():
    harness = _Harness()

    class _BrokenBook:
        @staticmethod
        def process_delta(_delta):
            raise OrderBookGapError("forced gap")

    harness.state.orderbooks[SYMBOL] = _BrokenBook()
    harness.state.ws_buffer[SYMBOL] = None
    launched = []
    harness._launch_book_recovery = launched.append

    harness.sync.process_delta(
        SYMBOL,
        {"U": 2, "u": 2, "pu": 0, "b": [], "a": []},
        expected_generation=1,
    )

    assert launched == [(SYMBOL, 1, 1, "FATAL_GAP")]
    assert harness.state.recovery_tokens[SYMBOL] == 1
    assert harness.state.ws_buffer[SYMBOL] == []


def test_publish_rejects_stale_generation_and_replaced_book():
    harness = _Harness()
    expected_book = harness.state.orderbooks[SYMBOL]
    event_book = SimpleNamespace()
    matching_book = object()

    assert not harness.sync.publish_update(
        0,
        symbol=SYMBOL,
        expected_book=expected_book,
        event_book=event_book,
        matching_book=matching_book,
    )
    harness.state.orderbooks[SYMBOL] = LocalOrderBook(SYMBOL)
    assert not harness.sync.publish_update(
        1,
        symbol=SYMBOL,
        expected_book=expected_book,
        event_book=event_book,
        matching_book=matching_book,
    )
    assert harness.submitted == []
    assert harness.published == []

    expected_book = harness.state.orderbooks[SYMBOL]
    assert harness.sync.publish_update(
        1,
        symbol=SYMBOL,
        expected_book=expected_book,
        event_book=event_book,
        matching_book=matching_book,
    )
    assert harness.submitted == [("book", (1, matching_book))]
    assert harness.published == [(EVENT_ORDERBOOK, event_book)]


def test_external_effect_ports_never_run_under_book_lock():
    harness = _Harness()
    observations = []
    expected_book = harness.state.orderbooks[SYMBOL]

    def observe(name, result=None):
        observations.append(
            (name, harness.state.lock.held_by_current_thread())
        )
        return result

    harness._submit_worker = lambda *_args: observe("submit", True)
    harness.on_market_data = lambda *_args: observe("market")
    assert harness.sync.publish_update(
        1,
        symbol=SYMBOL,
        expected_book=expected_book,
        event_book=SimpleNamespace(),
        matching_book=object(),
    )

    with harness.state.lock:
        harness.sync.begin_recovery_locked(SYMBOL, "FATAL_GAP")
    harness._resync_book = lambda *_args, **_kwargs: False
    harness._fault = lambda _reason: observe("fault")
    harness.sync.recover_orderbook(SYMBOL, 1, 1)

    with harness.state.lock:
        harness.sync.begin_recovery_locked(SYMBOL, "FATAL_GAP")
    harness._resync_book = lambda *_args, **_kwargs: True
    harness._publish_health = lambda _reason: observe("health")
    harness.sync.recover_orderbook(SYMBOL, 1, 2)

    assert observations == [
        ("submit", False),
        ("market", False),
        ("fault", False),
        ("health", False),
    ]


def test_lifecycle_reset_waits_for_dispatch_lease_without_holding_book_lock():
    harness = _Harness()
    callback_entered = threading.Event()
    release_callback = threading.Event()
    reset_completed = threading.Event()
    results = []

    def blocked_submit(*_args):
        assert not harness.state.lock.held_by_current_thread()
        callback_entered.set()
        release_callback.wait(timeout=1.0)
        return True

    harness._submit_worker = blocked_submit
    publisher = threading.Thread(
        target=lambda: results.append(
            harness.sync.publish_update(
                1,
                symbol=SYMBOL,
                expected_book=harness.state.orderbooks[SYMBOL],
                event_book=None,
                matching_book=object(),
            )
        )
    )
    resetter = threading.Thread(
        target=lambda: (
            harness.sync.reset_books([SYMBOL]),
            reset_completed.set(),
        )
    )

    publisher.start()
    assert callback_entered.wait(timeout=1.0)
    resetter.start()
    assert not reset_completed.wait(timeout=0.05)

    release_callback.set()
    publisher.join(timeout=1.0)
    resetter.join(timeout=1.0)

    assert not publisher.is_alive()
    assert not resetter.is_alive()
    assert results == [True]
    assert reset_completed.is_set()
    assert harness.state.generation == 2


def test_fault_in_dispatch_joins_waiting_lifecycle_transition_without_deadlock():
    harness = _Harness()
    callback_entered = threading.Event()
    run_fault = threading.Event()
    reset_completed = threading.Event()
    invalidation_results = []

    def faulting_submit(*_args):
        callback_entered.set()
        run_fault.wait(timeout=1.0)
        invalidation_results.append(harness.sync.invalidate_lifecycle())
        return False

    harness._submit_worker = faulting_submit
    publisher = threading.Thread(
        target=lambda: harness.sync.publish_update(
            1,
            symbol=SYMBOL,
            expected_book=harness.state.orderbooks[SYMBOL],
            event_book=None,
            matching_book=object(),
        )
    )
    resetter = threading.Thread(
        target=lambda: (
            harness.sync.reset_books([SYMBOL]),
            reset_completed.set(),
        )
    )

    publisher.start()
    assert callback_entered.wait(timeout=1.0)
    resetter.start()
    deadline = time.monotonic() + 1.0
    while not harness.state.lifecycle_transition and time.monotonic() < deadline:
        time.sleep(0.001)
    assert harness.state.lifecycle_transition

    run_fault.set()
    publisher.join(timeout=1.0)
    resetter.join(timeout=1.0)

    assert not publisher.is_alive()
    assert not resetter.is_alive()
    assert reset_completed.is_set()
    assert invalidation_results == [2]
    assert harness.state.generation == 3


def test_component_has_explicit_state_instead_of_gateway_owner_proxy():
    harness = _Harness()

    assert harness.sync.state is harness.state
    assert not hasattr(harness.sync, "_owner")
