import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
from oms.journal import (
    JournalCorruptionError,
    JournalMigrationRequiredError,
    JournalWriteError,
    OMSJournal,
    decode_legacy_journal,
)
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
    @staticmethod
    def _segment_path(path: str, index: int = 1) -> Path:
        return Path(f"{path}.v3-segments") / f"{index:020d}.seg"

    @staticmethod
    def _rewrite_first_record(path: str, mutate) -> None:
        segment_path = DurableJournalTests._segment_path(path)
        raw = segment_path.read_bytes()
        offset = len(OMSJournal.SEGMENT_MAGIC)
        header_length = OMSJournal.FRAME_LENGTH.unpack(
            raw[offset : offset + OMSJournal.FRAME_LENGTH.size]
        )[0]
        offset += OMSJournal.FRAME_LENGTH.size + header_length + OMSJournal.FRAME_CRC.size
        record_length = OMSJournal.FRAME_LENGTH.unpack(
            raw[offset : offset + OMSJournal.FRAME_LENGTH.size]
        )[0]
        frame_end = (
            offset
            + OMSJournal.FRAME_LENGTH.size
            + record_length
            + OMSJournal.FRAME_CRC.size
        )
        payload_start = offset + OMSJournal.FRAME_LENGTH.size
        record = json.loads(raw[payload_start : payload_start + record_length])
        mutate(record)
        replacement = OMSJournal._encode_frame(record)
        segment_path.write_bytes(raw[:offset] + replacement + raw[frame_end:])

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
            self.assertEqual(
                [record["seq"] for record in journal.iter_records()],
                [1, 2],
            )

    def test_tampered_record_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oms.jsonl")
            journal = OMSJournal(make_config(path))
            journal.append("order_snapshot", {"status": "NEW"})

            segment_path = self._segment_path(path)
            raw = bytearray(segment_path.read_bytes())
            raw[-1] ^= 0x01
            segment_path.write_bytes(raw)

            with self.assertRaises(JournalCorruptionError):
                OMSJournal(make_config(path))

    def test_truncated_tail_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oms.jsonl")
            journal = OMSJournal(make_config(path))
            journal.append("first", {"value": 1})
            with open(self._segment_path(path), "ab") as handle:
                handle.write(b"\x00\x00")

            with self.assertRaises(JournalCorruptionError):
                OMSJournal(make_config(path))

    def test_explicit_future_version_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oms.jsonl")
            journal = OMSJournal(make_config(path))
            journal.append("lifecycle", {"state": "LIVE"})
            self._rewrite_first_record(
                path,
                lambda record: record.__setitem__(
                    "version",
                    OMSJournal.RECORD_VERSION + 1,
                ),
            )

            with self.assertRaisesRegex(
                JournalCorruptionError,
                "Unsupported OMS journal version",
            ):
                OMSJournal(make_config(path))

    def test_legacy_record_cannot_smuggle_durable_envelope_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oms.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "seq": 1,
                            "kind": "lifecycle",
                            "payload": {"state": "LIVE"},
                        }
                    )
                    + "\n"
                )

            with self.assertRaisesRegex(
                JournalCorruptionError,
                "partial envelope",
            ):
                decode_legacy_journal(path)

    def test_runtime_rejects_v2_or_legacy_path_without_mutating_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oms.jsonl")
            Path(path).write_text('{"kind":"legacy","payload":{}}\n')
            before = Path(path).read_bytes()

            with self.assertRaises(JournalMigrationRequiredError):
                OMSJournal(make_config(path))

            self.assertEqual(Path(path).read_bytes(), before)
            self.assertFalse(Path(f"{path}.v3-manifest.json").exists())

    def test_checkpoint_bounds_tail_verification_and_preserves_head(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oms.jsonl")
            journal = OMSJournal(make_config(path))
            journal.append("first", {"value": 1})
            journal.append("second", {"value": 2})
            checkpoint = journal.commit_checkpoint({"records": 2})
            journal.append("first", {"value": 3})
            expected_hash = journal.health_snapshot()["last_hash"]

            restarted = OMSJournal(make_config(path))

            health = restarted.health_snapshot()
            self.assertEqual(health["next_seq"], 5)
            self.assertEqual(health["last_hash"], expected_hash)
            self.assertEqual(health["verified_start_seq"], checkpoint["anchor_seq"])

    def test_segment_rotation_preserves_chain(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oms.jsonl")
            config = make_config(path)
            config["oms"]["journal_segment_max_records"] = 1
            journal = OMSJournal(config)
            journal.append("first", {"value": 1})
            journal.append("second", {"value": 2})

            restarted = OMSJournal(config)
            records = restarted.read_all()
            self.assertEqual(len(records), 2)
            self.assertEqual(records[1]["prev_hash"], records[0]["hash"])
            self.assertEqual(restarted.health_snapshot()["segment_count"], 2)

    def test_checkpoint_startup_does_not_rescan_pre_anchor_segments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oms.jsonl")
            config = make_config(path)
            config["oms"]["journal_segment_max_records"] = 1
            journal = OMSJournal(config)
            journal.append("first", {"value": 1})
            journal.append("second", {"value": 2})
            checkpoint = journal.commit_checkpoint({"records": 2})

            old_segment = self._segment_path(path, 1)
            raw = bytearray(old_segment.read_bytes())
            raw[-1] ^= 0x01
            old_segment.write_bytes(raw)

            restarted = OMSJournal(config)
            self.assertEqual(
                restarted.health_snapshot()["verified_start_seq"],
                checkpoint["anchor_seq"],
            )
            with self.assertRaises(JournalCorruptionError):
                restarted.read_all()

    def test_low_disk_space_rejects_batch_before_open(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oms.jsonl")
            config = make_config(path)
            config["oms"]["journal_min_free_bytes"] = 1024
            journal = OMSJournal(config)

            with patch(
                "oms.journal.shutil.disk_usage",
                return_value=SimpleNamespace(free=1024),
            ):
                with self.assertRaisesRegex(
                    JournalWriteError,
                    "below the reserve",
                ):
                    journal.append("first", {"value": 1})

            health = journal.health_snapshot()
            self.assertEqual(health["space_rejection_count"], 1)
            self.assertEqual(health["next_seq"], 1)
            self.assertFalse(os.path.exists(path))

    def test_disk_space_check_failure_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oms.jsonl")
            config = make_config(path)
            config["oms"]["journal_min_free_bytes"] = 1
            journal = OMSJournal(config)

            with patch(
                "oms.journal.shutil.disk_usage",
                side_effect=OSError("disk unavailable"),
            ):
                with self.assertRaisesRegex(
                    JournalWriteError,
                    "verify free space",
                ):
                    journal.append("first", {"value": 1})

            health = journal.health_snapshot()
            self.assertEqual(health["space_check_failure_count"], 1)
            self.assertEqual(health["next_seq"], 1)

    def test_enospc_does_not_advance_hash_chain(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oms.jsonl")
            journal = OMSJournal(make_config(path))

            with patch(
                "builtins.open",
                side_effect=OSError(errno.ENOSPC, "disk full"),
            ):
                with self.assertRaises(JournalWriteError):
                    journal.append("first", {"value": 1})

            health = journal.health_snapshot()
            self.assertEqual(health["write_failure_count"], 1)
            self.assertEqual(health["next_seq"], 1)
            self.assertEqual(health["last_hash"], "")


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

    def test_checkpoint_restores_active_order_without_pre_anchor_replay(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oms.jsonl")
            crashed, _gateway = self._make_live_oms(path)
            order = Order(
                "checkpoint-active",
                OrderIntent("alpha", "BTCUSDT", Side.BUY, 100.0, 1.0),
            )
            order.mark_submitting()
            crashed.orders[order.client_oid] = order
            crashed.journal.append("order_snapshot", order.to_record())
            crashed.journal.commit_checkpoint(
                crashed.lifecycle_controller._shutdown_checkpoint_summary()
            )
            crashed.order_monitor.stop()

            recovered = OMS(DummyEngine(), LedgerGateway(), make_config(path))
            try:
                restored = recovered.orders[order.client_oid]
                self.assertEqual(restored.status, OrderStatus.SUBMIT_UNKNOWN)
                self.assertEqual(
                    recovered.rebuild_summary["records"],
                    crashed.journal.health_snapshot()["next_seq"] - 1,
                )
                self.assertGreater(
                    recovered.journal.health_snapshot()["verified_start_seq"],
                    0,
                )
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

    def test_clean_stop_marker_is_committed_after_owned_resources_stop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oms.jsonl")
            oms, _gateway = self._make_live_oms(path)
            self.assertTrue(oms.begin_shutdown("ordered_stop"))
            oms._shutdown_cancel_verified = True

            result = oms.stop(clean_shutdown=True, reason="ordered_stop")

            records = OMSJournal(make_config(path)).load()
            self.assertTrue(result["clean"])
            self.assertEqual(records[-2]["kind"], "checkpoint_committed")
            self.assertEqual(records[-1]["kind"], "oms_stopped")
            self.assertEqual(
                records[-1]["payload"]["shutdown_protocol_version"],
                3,
            )
            self.assertTrue(
                all(records[-1]["payload"]["components"].values())
            )

    def test_failed_resource_stop_cannot_commit_clean_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oms.jsonl")
            oms, _gateway = self._make_live_oms(path)
            self.assertTrue(oms.begin_shutdown("monitor_failure"))
            oms._shutdown_cancel_verified = True
            oms.order_monitor.stop = lambda: False

            result = oms.stop(
                clean_shutdown=True,
                reason="monitor_failure",
            )

            records = OMSJournal(make_config(path)).load()
            self.assertFalse(result["clean"])
            self.assertEqual(records[-1]["kind"], "shutdown_incomplete")
            rebuilt = OMS(
                DummyEngine(),
                LedgerGateway(),
                make_config(path),
            )
            try:
                self.assertFalse(rebuilt.rebuild_summary["clean_shutdown"])
            finally:
                rebuilt.order_monitor.stop()


if __name__ == "__main__":
    unittest.main()
