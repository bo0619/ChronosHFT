import copy
import json
import unittest
from unittest.mock import mock_open, patch

from infrastructure.config_scaling import load_root_config
from infrastructure.live_config_guard import validate_live_runtime_config
from infrastructure.paper_trade import apply_paper_trade_mode


def safe_live_config():
    return {
        "execution": {"mode": "live"},
        "symbols": ["BTCUSDT"],
        "oms": {
            "journal_enabled": True,
            "replay_journal_on_startup": True,
            "journal_path": "storage/oms/live_journal.jsonl",
            "single_writer_fence": {
                "enabled": True,
                "path": "storage/oms/live_journal.jsonl.lock",
            },
            "venue_dead_man_switch": {"enabled": True},
        },
        "risk": {
            "independent_supervisor": {
                "enabled": True,
                "flatten_enabled": True,
            },
            "cash_flow_truth": {
                "enabled": True,
                "require_snapshot": True,
                "max_snapshot_age_sec": 45.0,
            },
            "risk_control_heartbeat": {
                "enabled": True,
                "required_source": "independent_supervisor",
                "max_age_sec": 2.0,
            },
        },
    }


class LiveConfigGuardTests(unittest.TestCase):
    def test_safe_live_config_is_accepted_without_mutation(self):
        config = safe_live_config()
        before = copy.deepcopy(config)

        self.assertIs(validate_live_runtime_config(config), config)
        self.assertEqual(config, before)

    def test_paper_config_is_exempt_from_live_guards(self):
        config = apply_paper_trade_mode(
            {
                "execution": {"mode": "paper"},
                "paper_trade": {"enabled": True},
                "oms": {
                    "journal_enabled": True,
                    "venue_dead_man_switch": {"enabled": False},
                },
                "risk": {},
            }
        )

        self.assertIs(validate_live_runtime_config(config), config)

    def test_disabled_independent_safety_planes_are_rejected(self):
        cases = (
            (
                ("risk", "independent_supervisor", "enabled"),
                "independent_supervisor.enabled",
            ),
            (
                ("risk", "independent_supervisor", "flatten_enabled"),
                "flatten_enabled",
            ),
            (
                ("oms", "venue_dead_man_switch", "enabled"),
                "venue_dead_man_switch.enabled",
            ),
        )
        for path, expected in cases:
            with self.subTest(path=path):
                config = safe_live_config()
                target = config
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = False

                with self.assertRaisesRegex(ValueError, expected):
                    validate_live_runtime_config(config)

    def test_live_journal_must_be_enabled_and_replayed(self):
        cases = (
            ("journal_enabled", "journal_enabled"),
            ("replay_journal_on_startup", "replay_journal_on_startup"),
        )
        for field, expected in cases:
            with self.subTest(field=field):
                config = safe_live_config()
                config["oms"][field] = False

                with self.assertRaisesRegex(ValueError, expected):
                    validate_live_runtime_config(config)

    def test_live_journal_and_fence_cannot_use_paper_state_paths(self):
        cases = (
            (
                ("oms", "journal_path"),
                r"storage\paper\oms_journal.jsonl",
                "journal_path",
            ),
            (
                ("oms", "single_writer_fence", "path"),
                "C:/chronos/storage/PAPER/oms_journal.jsonl.lock",
                "single_writer_fence.path",
            ),
        )
        for path, value, expected in cases:
            with self.subTest(path=path):
                config = safe_live_config()
                target = config
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value

                with self.assertRaisesRegex(ValueError, expected):
                    validate_live_runtime_config(config)

    def test_cash_flow_truth_must_be_enabled_required_and_fresh(self):
        cases = (
            ("enabled", False, "cash_flow_truth.enabled"),
            ("require_snapshot", False, "require_snapshot"),
            ("max_snapshot_age_sec", 0.0, "max_snapshot_age_sec"),
        )
        for field, value, expected in cases:
            with self.subTest(field=field):
                config = safe_live_config()
                config["risk"]["cash_flow_truth"][field] = value

                with self.assertRaisesRegex(ValueError, expected):
                    validate_live_runtime_config(config)

    def test_heartbeat_must_be_independent_enabled_and_fresh(self):
        cases = (
            ("enabled", False, "risk_control_heartbeat.enabled"),
            ("required_source", "risk_manager", "required_source"),
            ("max_age_sec", float("nan"), "max_age_sec"),
        )
        for field, value, expected in cases:
            with self.subTest(field=field):
                config = safe_live_config()
                config["risk"]["risk_control_heartbeat"][field] = value

                with self.assertRaisesRegex(ValueError, expected):
                    validate_live_runtime_config(config)

    def test_root_loader_applies_guard_to_legacy_implicit_live_mode(self):
        payload = safe_live_config()
        payload.pop("execution")

        with patch("builtins.open", mock_open(read_data=json.dumps(payload))):
            loaded = load_root_config("config.json")

        self.assertEqual(loaded["symbols"], ["BTCUSDT"])

        payload["risk"]["independent_supervisor"]["enabled"] = False
        with patch("builtins.open", mock_open(read_data=json.dumps(payload))):
            with self.assertRaisesRegex(ValueError, "independent_supervisor.enabled"):
                load_root_config("config.json")

    def test_root_loader_keeps_paper_mode_exempt(self):
        payload = {
            "execution": {"mode": "paper"},
            "paper_trade": {"enabled": True},
            "symbols": ["BTCUSDT"],
        }

        with patch("builtins.open", mock_open(read_data=json.dumps(payload))):
            loaded = load_root_config("config.json")

        self.assertEqual(loaded["execution"]["mode"], "paper")
        self.assertFalse(loaded["risk"]["independent_supervisor"]["enabled"])


if __name__ == "__main__":
    unittest.main()
