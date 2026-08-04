import threading
import time

from data.orderbook import LocalOrderBook
from gateway.binance.book_controller import (
    BinanceOrderBookConfig,
    BinanceOrderBookController,
    BinanceOrderBookDependencies,
)
from gateway.binance.gateway import BinanceGateway


def _make_controller(
    *,
    resync_override=None,
    retry_sec=0.0,
    attempts=1,
):
    events = []
    published = []
    holder = {}

    def default_resync(symbol, **kwargs):
        return holder["controller"].resync_book(symbol, **kwargs)

    dependencies = BinanceOrderBookDependencies(
        create_orderbook=LocalOrderBook,
        fetch_snapshot=lambda _symbol: {
            "lastUpdateId": 100,
            "bids": [["100.0", "1.0"]],
            "asks": [["100.5", "1.0"]],
        },
        publish_orderbook=lambda data, generation: published.append(
            (data, generation)
        ),
        emit_health=events.append,
        resync_book=resync_override or default_resync,
        launch_recovery=lambda recovery: holder[
            "controller"
        ].launch_recovery(recovery),
        recover_orderbook=lambda symbol, generation, token: holder[
            "controller"
        ].recover_orderbook(symbol, generation, token),
        log_info=lambda _message: None,
        log_critical=lambda _message: None,
    )
    controller = BinanceOrderBookController(
        dependencies,
        BinanceOrderBookConfig(
            resync_max_attempts=attempts,
            resync_retry_sec=retry_sec,
            recovery_join_timeout_sec=0.5,
        ),
    )
    holder["controller"] = controller
    return controller, events, published


def test_controller_owns_tokens_and_rejects_stale_generations():
    controller, _events, _published = _make_controller()
    with controller.lock:
        generation = controller.begin_generation_locked(["BTCUSDT"])
        first = controller.begin_recovery_locked(
            "BTCUSDT",
            freeze_reason="FATAL_GAP",
            expected_generation=generation,
        )
        second = controller.begin_recovery_locked(
            "BTCUSDT",
            freeze_reason="FATAL_GAP",
            expected_generation=generation,
        )

        assert first == ("BTCUSDT", generation, 1, "FATAL_GAP")
        assert second == ("BTCUSDT", generation, 2, "FATAL_GAP")
        assert not controller.owns_recovery_locked(
            "BTCUSDT",
            generation,
            1,
        )
        assert controller.owns_recovery_locked(
            "BTCUSDT",
            generation,
            2,
        )
        assert not controller.release_recovery_locked(
            "BTCUSDT",
            generation,
            1,
        )

        next_generation = controller.begin_generation_locked(["BTCUSDT"])
        assert next_generation == generation + 1
        assert (
            controller.begin_recovery_locked(
                "BTCUSDT",
                freeze_reason="FATAL_GAP",
                expected_generation=generation,
            )
            is None
        )


def test_successful_recovery_orders_freeze_before_owned_clear():
    controller, events, published = _make_controller()
    with controller.lock:
        generation = controller.begin_generation_locked(["BTCUSDT"])

    assert controller.schedule_recovery(
        "BTCUSDT",
        freeze_reason="FATAL_GAP",
        expected_generation=generation,
    )
    assert controller.join_recovery_threads()

    assert events == [
        "FREEZE_SYMBOL:BTCUSDT:FATAL_GAP:1",
        "CLEAR_SYMBOL:BTCUSDT:ORDERBOOK_RESYNCED:1",
    ]
    assert controller.resyncing == set()
    assert len(published) == 1


def test_shutdown_interrupts_retry_worker_without_publishing_clear():
    first_attempt = threading.Event()

    def failed_resync(_symbol, **_kwargs):
        first_attempt.set()
        return False

    controller, events, _published = _make_controller(
        resync_override=failed_resync,
        retry_sec=10.0,
        attempts=3,
    )
    with controller.lock:
        generation = controller.begin_generation_locked(["BTCUSDT"])

    assert controller.schedule_recovery(
        "BTCUSDT",
        freeze_reason="FATAL_GAP",
        expected_generation=generation,
    )
    assert first_attempt.wait(timeout=0.5)

    started_at = time.perf_counter()
    with controller.lock:
        controller.begin_shutdown_locked()
    assert controller.join_recovery_threads()

    assert time.perf_counter() - started_at < 0.5
    assert controller.recovery_threads == set()
    assert events == ["FREEZE_SYMBOL:BTCUSDT:FATAL_GAP:1"]


def test_gateway_book_methods_are_thin_component_facades():
    calls = []

    class ComponentSpy:
        def process_delta(self, symbol, raw, *, expected_generation=None):
            calls.append(("process", symbol, raw, expected_generation))
            return "processed"

        def schedule_recovery(
            self,
            symbol,
            freeze_reason="",
            *,
            expected_generation=None,
        ):
            calls.append(
                ("schedule", symbol, freeze_reason, expected_generation)
            )
            return "scheduled"

        def resync_book(
            self,
            symbol,
            *,
            expected_generation=None,
            recovery_token=None,
        ):
            calls.append(
                ("resync", symbol, expected_generation, recovery_token)
            )
            return "resynced"

    gateway = BinanceGateway.__new__(BinanceGateway)
    gateway.__dict__["_order_book_component"] = ComponentSpy()

    assert (
        gateway._process_book(
            "BTCUSDT",
            {"u": 1},
            expected_generation=7,
        )
        == "processed"
    )
    assert (
        gateway._schedule_book_recovery(
            "BTCUSDT",
            "FATAL_GAP",
            expected_generation=7,
        )
        == "scheduled"
    )
    assert (
        gateway._resync_book(
            "BTCUSDT",
            expected_generation=7,
            recovery_token=9,
        )
        == "resynced"
    )
    assert calls == [
        ("process", "BTCUSDT", {"u": 1}, 7),
        ("schedule", "BTCUSDT", "FATAL_GAP", 7),
        ("resync", "BTCUSDT", 7, 9),
    ]


def test_legacy_state_attributes_are_views_of_component_owned_state():
    gateway = BinanceGateway.__new__(BinanceGateway)
    books = {"BTCUSDT": LocalOrderBook("BTCUSDT")}
    gateway.orderbooks = books
    gateway.book_resyncing = {"BTCUSDT"}

    component = gateway._order_books()
    assert component.orderbooks is books
    assert component.resyncing == {"BTCUSDT"}
    assert "orderbooks" not in gateway.__dict__
    assert "book_resyncing" not in gateway.__dict__
