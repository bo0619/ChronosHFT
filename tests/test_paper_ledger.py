import time
from collections import deque

from event.type import OrderRequest, TIF_GTX
from gateway.binance.paper_ledger import PaperLedger
from gateway.binance.paper_state import PaperOrder


SYMBOL = "SNDKUSDT"


def _make_order(client_oid: str, *, accept_seq: int = 1) -> PaperOrder:
    now = time.perf_counter()
    return PaperOrder(
        client_oid=client_oid,
        exchange_oid=f"exchange-{client_oid}",
        request=OrderRequest(
            symbol=SYMBOL,
            price=100.0,
            volume=1.0,
            side="BUY",
            time_in_force=TIF_GTX,
            post_only=True,
        ),
        accept_seq=accept_seq,
        created_ms=1,
        created_monotonic=now,
        update_ms=1,
        status="NEW",
        committed=True,
    )


class _Owner:
    def __init__(self, *orders):
        self._orders = {order.client_oid: order for order in orders}
        self._exchange_to_client = {
            order.exchange_oid: order.client_oid for order in orders
        }
        self._positions = {}
        self._books = {}
        self._balances = {"USDT": 1_000.0}
        self._trades = deque()
        self._event_sequence = 0
        self._paper_trade_sequence = 0
        self._worker_running = True
        self.balance_asset = "USDT"
        self.symbols = [SYMBOL]
        self.max_order_history = 10
        self.order_updates = []
        self.account_updates = []
        self.queue_removals = []
        self.worker_commands = []
        self.ledger = PaperLedger(self)

    @staticmethod
    def _reduce_only_fill_cap(order):
        return order.remaining

    @staticmethod
    def _fee_rate(_order, _is_maker):
        return 0.001

    def _apply_position_fill(self, symbol, side, quantity, price):
        return self.ledger.apply_position_fill(
            symbol,
            side,
            quantity,
            price,
        )

    @staticmethod
    def _quote_asset(_symbol):
        return "USDT"

    @staticmethod
    def _mark_price(_symbol):
        return 100.0

    def _emit_order_event(self, order, **kwargs):
        return self.ledger.emit_order_event(order, **kwargs)

    def _emit_account_update(self, symbol, **kwargs):
        return self.ledger.emit_account_update(symbol, **kwargs)

    def _remove_from_later_local_queue(self, order, removed_quantity):
        self.queue_removals.append((order.client_oid, removed_quantity))

    def _account_metrics(self):
        wallet = sum(self._balances.values())
        return {
            "wallet_balance": wallet,
            "available_balance": wallet,
            "available_by_asset": dict(self._balances),
        }

    def _submit_worker(self, kind, payload):
        self.worker_commands.append((kind, payload))
        return True

    def on_order_update(self, update):
        self.order_updates.append(update)

    def on_account_update(self, update):
        self.account_updates.append(update)


def test_position_fill_realizes_pnl_and_resets_basis_on_flip():
    owner = _Owner()

    assert owner.ledger.apply_position_fill(SYMBOL, "BUY", 2.0, 100.0) == 0.0
    position = owner._positions[SYMBOL]
    assert position.quantity == 2.0
    assert position.entry_price == 100.0

    realized = owner.ledger.apply_position_fill(SYMBOL, "SELL", 3.0, 110.0)

    assert realized == 20.0
    assert position.quantity == -1.0
    assert position.entry_price == 110.0


def test_fill_updates_ledger_before_publishing_order_and_account_events():
    order = _make_order("partial")
    owner = _Owner(order)

    assert owner.ledger.apply_fill(
        order,
        0.4,
        100.0,
        is_maker=True,
        fill_context={"fill_trigger": "at_price", "market_trade_id": 7},
    )

    assert order.status == "PARTIALLY_FILLED"
    assert order.cum_filled_qty == 0.4
    assert order.avg_price == 100.0
    assert owner._positions[SYMBOL].quantity == 0.4
    assert owner._balances["USDT"] == 999.96
    assert owner._trades[0]["commission"] == "0.04"
    assert owner._trades[0]["id"] == 1
    assert owner.order_updates[0].status == "PARTIALLY_FILLED"
    assert owner.order_updates[0].cum_filled_qty == 0.4
    assert owner.order_updates[0].market_trade_id == 7
    assert owner.account_updates[0].wallet_balance == 999.96


def test_terminal_order_pruning_preserves_active_orders():
    oldest = _make_order("oldest", accept_seq=1)
    newest = _make_order("newest", accept_seq=2)
    active = _make_order("active", accept_seq=3)
    oldest.status = "CANCELED"
    oldest.update_ms = 10
    newest.status = "FILLED"
    newest.update_ms = 20
    owner = _Owner(oldest, newest, active)
    owner.max_order_history = 2

    owner.ledger.prune_terminal_orders()

    assert set(owner._orders) == {"newest", "active"}
    assert "exchange-oldest" not in owner._exchange_to_client
    assert "exchange-newest" in owner._exchange_to_client


def test_full_account_publication_uses_worker_barrier():
    owner = _Owner()

    assert owner.ledger.emit_full_account_update("PAPER_BOOTSTRAP")
    assert owner.worker_commands == [
        ("emit_full_account", "PAPER_BOOTSTRAP")
    ]

    owner._worker_running = False
    assert not owner.ledger.emit_full_account_update("LATE")
