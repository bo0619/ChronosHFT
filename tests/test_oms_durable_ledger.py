import json
import os
import tempfile
import unittest

from event.type import (
    CommandOutcome,
    ExchangeOrderUpdate,
    LifecycleState,
    OrderIntent,
    OrderRequest,
    OrderStatus,
    Side,
)
from oms.engine import OMS
from oms.journal import JournalCorruptionError, JournalWriteError, OMSJournal
from oms.order import Order


class DummyEngine:
    def __init__(self):
        self.events = []

    def put(self, event):
        self.events.append(event)


class DummyResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class LedgerGateway:
    gateway_name = "BINANCE"

    def __init__(self):
        self.sent_orders = []
        self.cancelled_symbols = []

    def send_order(self, request, client_oid):
        self.sent_orders.append((request, client_oid))
        return "exchange-1"

    def cancel_order(self, _request):
        return DummyResponse(200, {"status": "CANCELED"})

    def cancel_all_orders(self, symbol):
        self.cancelled_symbols.append(symbol)
        return DummyResponse()

    def get_account_info(self):
        return {
            "totalWalletBalance": "1000",
            "totalInitialMargin": "0",
            "availableBalance": "1000",
        }

    def get_all_positions(self):
        return []

    def get_open_orders(self):
        return []

    def get_order(self, _symbol, _order_id):
        return {"_query_status": "NOT_FOUND", "code": "-2013"}

    def get_user_trades(self, _symbol, **_kwargs):
        return []


def make_config(journal_path):
    return {
        "symbols": ["BTCUSDT"],
        "account": {"initial_balance_usdt": 1000.0, "leverage": 10},
        "backtest": {"maker_fee": 0.0, "taker_fee": 0.0},
        "oms": {
            "journal_enabled": True,
            "journal_fsync": True,
            "journal_integrity_check": True,
            "replay_journal_on_startup": True,
            "journal_path": journal_path,
            "ack_timeout_sec": 1e12,
            "unknown_recheck_sec": 1e12,
            "cancel_timeout_sec": 1e12,
            "active_order_audit_interval_sec": 1e12,
        },
        "risk": {"limits": {"max_pos_notional": 5000.0}},
    }


class DurableJournalTests(unittest.TestCase):
    def test_records_have_monotonic_sequence_and_hash_chain(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oms.jsonl")
            journal = OMSJournal(make_config(path))

            self.assertEqual(journal.append("first", {"value": 1}), 1)
            self.assertEqual(journal.append("second", {"value": 2}), 2)

            records = journal.load()
            self.assertEqual([record["seq"] for record in records], [1, 2])
            self.assertEqual(records[0]["prev_hash"], "")
            self.assertEqual(records[1]["prev_hash"], records[0]["hash"])
            self.assertEqual(journal.health_snapshot()["next_seq"], 3)

    def test_tampered_record_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oms.jsonl")
            journal = OMSJournal(make_config(path))
            journal.append("order_snapshot", {"status": "NEW"})

            with open(path, "r", encoding="utf-8") as handle:
                record = json.loads(handle.readline())
            record["payload"]["status"] = "FILLED"
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")

            with self.assertRaises(JournalCorruptionError):
                OMSJournal(make_config(path))

    def test_truncated_tail_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oms.jsonl")
            journal = OMSJournal(make_config(path))
            journal.append("first", {"value": 1})
            with open(path, "a", encoding="utf-8") as handle:
                handle.write('{"version":2,"seq":2')

            with self.assertRaises(JournalCorruptionError):
                OMSJournal(make_config(path))


class DurableCommandRecoveryTests(unittest.TestCase):
    def _make_live_oms(self, path, gateway=None):
        gateway = gateway or LedgerGateway()
        oms = OMS(DummyEngine(), gateway, make_config(path))
        oms.state = LifecycleState.LIVE
        oms._sync_capability_mode("test_live")
        oms.validator.validate_params = lambda _intent: (True, "")
        oms.exposure.check_risk = lambda *_args, **_kwargs: (True, "")
        return oms, gateway

    def test_submit_is_prepared_before_gateway_send(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oms.jsonl")
            oms, gateway = self._make_live_oms(path)
            observed_kinds = []
            original_send = gateway.send_order

            def inspect_before_send(request, client_oid):
                observed_kinds.extend(record["kind"] for record in oms.journal.load())
                return original_send(request, client_oid)

            gateway.send_order = inspect_before_send
            try:
                result = oms.submit_order(
                    OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 1.0)
                )
                self.assertTrue(result.accepted)
                self.assertIn("command_prepared", observed_kinds)
                self.assertNotIn("command_result", observed_kinds)

                kinds = [record["kind"] for record in oms.journal.load()]
                prepared_index = kinds.index("command_prepared")
                result_index = kinds.index("command_result")
                self.assertLess(prepared_index, result_index)
            finally:
                oms.stop()

    def test_prepared_without_result_recovers_as_submit_unknown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oms.jsonl")
            crashed, _gateway = self._make_live_oms(path)
            order = Order(
                "crash-before-result",
                OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 1.0),
            )
            order.mark_submitting()
            request = OrderRequest("BTCUSDT", 100.0, 1.0, "BUY")
            crashed._record_order_snapshot(order, "accepted")
            crashed._record_command_prepared(
                f"SUBMIT:{order.client_oid}",
                "SUBMIT",
                order,
                request,
            )
            crashed.order_monitor.stop()

            recovered = OMS(DummyEngine(), LedgerGateway(), make_config(path))
            try:
                restored = recovered.orders[order.client_oid]
                self.assertEqual(restored.status, OrderStatus.SUBMIT_UNKNOWN)
                self.assertEqual(recovered.state, LifecycleState.FROZEN)
                self.assertEqual(recovered.rebuild_summary["pending_commands"], 1)
                self.assertEqual(recovered.rebuild_summary["recovered_active_orders"], 1)
                self.assertIn(order.client_oid, recovered.order_monitor.monitored_orders)
            finally:
                recovered.stop()

    def test_ack_result_without_snapshot_recovers_exchange_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oms.jsonl")
            crashed, _gateway = self._make_live_oms(path)
            order = Order(
                "crash-after-ack",
                OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 1.0),
            )
            order.mark_submitting()
            request = OrderRequest("BTCUSDT", 100.0, 1.0, "BUY")
            command_id = f"SUBMIT:{order.client_oid}"
            crashed._record_order_snapshot(order, "accepted")
            crashed._record_command_prepared(command_id, "SUBMIT", order, request)
            crashed._record_command_result(
                command_id,
                "SUBMIT",
                order,
                CommandOutcome.ACKNOWLEDGED,
                exchange_oid="exchange-ack",
            )
            crashed.order_monitor.stop()

            recovered = OMS(DummyEngine(), LedgerGateway(), make_config(path))
            try:
                restored = recovered.orders[order.client_oid]
                self.assertEqual(restored.status, OrderStatus.PENDING_ACK)
                self.assertEqual(restored.exchange_oid, "exchange-ack")
                self.assertIs(recovered.exchange_id_map["exchange-ack"], restored)
            finally:
                recovered.stop()

    def test_execution_record_finishes_fill_replay_after_crash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oms.jsonl")
            crashed, _gateway = self._make_live_oms(path)
            order = Order(
                "crash-after-execution",
                OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 1.0),
            )
            order.mark_submitting()
            order.mark_pending_ack("exchange-fill")
            order.mark_new("exchange-fill", update_time=1.0)
            crashed._record_order_snapshot(order, "exchange_new")
            update = ExchangeOrderUpdate(
                client_oid=order.client_oid,
                exchange_oid=order.exchange_oid,
                symbol="BTCUSDT",
                status="PARTIALLY_FILLED",
                filled_qty=0.4,
                filled_price=101.0,
                cum_filled_qty=0.4,
                update_time=2.0,
                trade_id=42,
                commission=0.01,
                commission_asset="USDT",
            )
            crashed._record_execution(order, update, fill_qty=0.4, fee=0.01)
            crashed.order_monitor.stop()

            recovered = OMS(DummyEngine(), LedgerGateway(), make_config(path))
            try:
                restored = recovered.orders[order.client_oid]
                self.assertEqual(restored.status, OrderStatus.PARTIALLY_FILLED)
                self.assertAlmostEqual(restored.filled_volume, 0.4)
                self.assertAlmostEqual(restored.avg_price, 101.0)
                self.assertIn("BINANCE:BTCUSDT:42", recovered.execution_ids)
                self.assertEqual(recovered.state, LifecycleState.FROZEN)
            finally:
                recovered.stop()

    def test_prepare_failure_prevents_send_and_halts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oms.jsonl")
            oms, gateway = self._make_live_oms(path)
            original_append_batch = oms.journal.append_batch

            def fail_command_prepare(records):
                records = list(records)
                if any(kind == "command_prepared" for kind, _payload in records):
                    raise JournalWriteError("simulated disk failure")
                return original_append_batch(records)

            oms.journal.append_batch = fail_command_prepare
            try:
                result = oms.submit_order(
                    OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 1.0)
                )
                self.assertFalse(result.accepted)
                self.assertEqual(result.reason, "durable_journal_unavailable")
                self.assertEqual(gateway.sent_orders, [])
                self.assertEqual(oms.state, LifecycleState.HALTED)
                self.assertTrue(oms.manual_rearm_required)
            finally:
                oms.journal.append_batch = original_append_batch
                oms.stop()


if __name__ == "__main__":
    unittest.main()
