import unittest

from infrastructure.truth_monitor import TruthMonitor


def live_rpi_config():
    return {
        "execution": {"mode": "live"},
        "paper_trade": {"enabled": False},
        "symbols": ["XAUUSDT"],
        "oms": {
            "truth_monitor": {
                "rpi_commission_poll_interval_sec": 5.0,
                "rpi_commission_halt_threshold": 2,
                "rpi_commission_clean_polls_to_clear": 2,
            }
        },
        "strategy": {
            "primary_model": "glft",
            "registered_models": ["glft"],
            "execution_policy": "single_primary",
            "use_rpi": True,
            "use_rpi_for_glft": True,
            "rpi_fallback_to_gtx": False,
            "rpi_live_policy": {"require_zero_commission": True},
            "glft": {"use_rpi": True},
        },
        "backtest": {},
    }


class FakeOms:
    def __init__(self):
        self.gateway = type("Gateway", (), {"gateway_name": "BINANCE"})()
        self.freezes = []
        self.halts = []
        self.records = []
        self.clears = []
        self.guard = {}

    def freeze_venue(self, venue, reason, cancel_active_orders=True):
        self.freezes.append((venue, reason, cancel_active_orders))
        self.guard = {
            "commission_truth": {
                "reason": reason,
                "epoch": len(self.freezes),
            }
        }
        return len(self.freezes)

    def halt_system(self, reason):
        self.halts.append(reason)

    def record_rpi_commission_truth(
        self,
        rates,
        *,
        accepted,
        reason,
        source,
    ):
        self.records.append((rates, accepted, reason, source))

    def get_venue_freeze_owners(self, _venue):
        return self.guard

    def clear_venue_freeze(self, venue, **kwargs):
        self.clears.append((venue, kwargs))
        self.guard = {}
        return True


class FakeProvider:
    gateway_name = "BINANCE"

    def __init__(self, payload):
        self.payload = payload

    def get_commission_rate(self, _symbol):
        return self.payload


class FailingSnapshotProvider(FakeProvider):
    def get_account_info(self):
        raise TimeoutError("account snapshot timed out")

    def get_all_positions(self):
        raise AssertionError("position query must not follow account failure")

    def get_open_orders(self):
        raise AssertionError("order query must not follow account failure")


class HealthySnapshotProvider(FakeProvider):
    def get_account_info(self):
        return {"totalWalletBalance": "100"}

    def get_all_positions(self):
        return []

    def get_open_orders(self):
        return []


class FailingDownstreamOms(FakeOms):
    def sync_account_margin_health(self, account, *, snapshot_time):
        raise TimeoutError("margin truth update timed out")


def commission_payload(rpi_rate="0"):
    return {
        "symbol": "XAUUSDT",
        "makerCommissionRate": "0.0002",
        "takerCommissionRate": "0.0005",
        "rpiCommissionRate": rpi_rate,
    }


class RuntimeCommissionTruthTests(unittest.TestCase):
    def test_zero_rate_refreshes_fee_truth_without_guarding(self):
        config = live_rpi_config()
        oms = FakeOms()
        monitor = TruthMonitor(
            oms,
            FakeProvider(commission_payload()),
            config,
            start_thread=False,
        )

        self.assertTrue(
            monitor._poll_rpi_commission_truth(now_monotonic=100.0)
        )
        self.assertEqual(config["backtest"]["rpi_commission_rate"], 0.0)
        self.assertEqual(
            config["backtest"]["rpi_commission_rates"],
            {"XAUUSDT": 0.0},
        )
        self.assertEqual(oms.freezes, [])
        self.assertEqual(oms.halts, [])
        self.assertTrue(oms.records[-1][1])

    def test_unavailable_truth_freezes_then_halts(self):
        oms = FakeOms()
        monitor = TruthMonitor(
            oms,
            FakeProvider(None),
            live_rpi_config(),
            start_thread=False,
        )

        self.assertFalse(
            monitor._poll_rpi_commission_truth(now_monotonic=100.0)
        )
        self.assertEqual(len(oms.freezes), 1)
        self.assertEqual(oms.halts, [])

        self.assertFalse(
            monitor._poll_rpi_commission_truth(now_monotonic=106.0)
        )
        self.assertEqual(len(oms.freezes), 2)
        self.assertEqual(len(oms.halts), 1)

    def test_nonzero_rpi_rate_halts_immediately(self):
        oms = FakeOms()
        monitor = TruthMonitor(
            oms,
            FakeProvider(commission_payload("0.0001")),
            live_rpi_config(),
            start_thread=False,
        )

        self.assertFalse(
            monitor._poll_rpi_commission_truth(now_monotonic=100.0)
        )
        self.assertEqual(oms.freezes, [])
        self.assertEqual(len(oms.halts), 1)
        self.assertFalse(oms.records[-1][1])

    def test_two_clean_polls_clear_only_commission_guard(self):
        oms = FakeOms()
        provider = FakeProvider(None)
        monitor = TruthMonitor(
            oms,
            provider,
            live_rpi_config(),
            start_thread=False,
        )

        monitor._poll_rpi_commission_truth(now_monotonic=100.0)
        provider.payload = commission_payload()
        monitor._poll_rpi_commission_truth(now_monotonic=106.0)
        self.assertEqual(oms.clears, [])

        monitor._poll_rpi_commission_truth(now_monotonic=112.0)
        self.assertEqual(len(oms.clears), 1)
        self.assertEqual(
            oms.clears[0][1]["expected_owner"],
            "commission_truth",
        )

    def test_snapshot_exception_counts_toward_freeze_and_halt(self):
        config = live_rpi_config()
        config["oms"]["truth_monitor"].update(
            {
                "api_freeze_threshold": 1,
                "api_halt_threshold": 2,
            }
        )
        oms = FakeOms()
        monitor = TruthMonitor(
            oms,
            FailingSnapshotProvider(commission_payload()),
            config,
            start_thread=False,
        )

        self.assertFalse(monitor.poll_once())
        self.assertEqual(monitor.consecutive_api_failures, 1)
        self.assertTrue(oms.freezes[-1][1].startswith("truth_plane:"))
        self.assertEqual(oms.halts, [])

        self.assertFalse(monitor.poll_once())
        self.assertEqual(monitor.consecutive_api_failures, 2)
        self.assertEqual(oms.halts[-1], "Truth plane unavailable")

    def test_downstream_exception_is_not_cleared_before_halt(self):
        config = live_rpi_config()
        config["oms"]["truth_monitor"].update(
            {
                "api_freeze_threshold": 1,
                "api_halt_threshold": 2,
            }
        )
        oms = FailingDownstreamOms()
        monitor = TruthMonitor(
            oms,
            HealthySnapshotProvider(commission_payload()),
            config,
            start_thread=False,
        )

        self.assertFalse(monitor.poll_once())
        self.assertEqual(monitor.consecutive_api_failures, 1)

        self.assertFalse(monitor.poll_once())
        self.assertEqual(monitor.consecutive_api_failures, 2)
        self.assertEqual(oms.halts[-1], "Truth plane unavailable")


if __name__ == "__main__":
    unittest.main()
