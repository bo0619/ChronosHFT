import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import mock_open, patch

from infrastructure.config_scaling import (
    CONFIG_MANIFEST_SCHEMA,
    load_config_document,
    load_root_config,
    normalize_root_config_preapproval,
)
from infrastructure.live_config_guard import (
    validate_live_account_equity_truth,
    validate_live_flat_start_truth,
    validate_live_runtime_config,
    validate_live_state_path_bindings,
)
from infrastructure.paper_trade import apply_paper_trade_mode
from strategy.model_readiness import strategy_policy_sha256


def safe_live_config():
    return {
        "execution": {"mode": "live"},
        "paper_trade": {"enabled": False},
        "testnet": False,
        "api_key_env": "BINANCE_API_KEY",
        "api_secret_env": "BINANCE_API_SECRET",
        "api_key": "primary-key",
        "api_secret": "primary-secret",
        "symbols": ["BTCUSDT"],
        "record_data": True,
        "live_launch": {
            "stage": "canary",
            "deployment_id": "canary-2026-07-23-001",
            "declared_account_equity_usdt": 10000.0,
            "max_deployed_capital_usdt": 100.0,
            "max_deployment_loss_usdt": 5.0,
            "deployment_loss_reduce_only_fraction": 0.8,
            "rpi_only": True,
        },
        "alert": {
            "active": True,
            "transport": "https_webhook",
            "webhook_url_env": "CHRONOSHFT_ALERT_WEBHOOK_URL",
            "minimum_level": "WARNING",
            "queue_capacity": 128,
            "connect_timeout_sec": 1.0,
            "read_timeout_sec": 2.0,
            "max_attempts": 3,
            "retry_backoff_sec": 0.5,
            "startup_probe_required": True,
            "startup_probe_timeout_sec": 12.0,
            "runtime_fail_closed": True,
            "recovery_probe_interval_sec": 30.0,
            "shutdown_flush_timeout_sec": 3.0,
            "failure_spool_path": (
                "storage/live/canary-2026-07-23-001/"
                "external_alert_failures.jsonl"
            ),
            "failure_spool_fsync": True,
        },
        "system": {
            "market_data": {
                "environment": "production",
                "testnet": False,
            },
            "time_sync": {
                "startup_required": True,
                "require_healthy_for_trading": True,
            },
            "admin_control": {
                "path": "storage/live/canary-2026-07-23-001/admin",
                "command_ttl_sec": 10.0,
                "session_max_age_sec": 2.0,
            },
            "web_dashboard": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 8765,
            },
            "evidence_recorder": {
                "enabled": True,
                "path": (
                    "storage/live/canary-2026-07-23-001/"
                    "market_evidence.jsonl"
                ),
                "queue_capacity": 8192,
                "max_batch_records": 256,
                "fsync_interval_sec": 1.0,
                "close_timeout_sec": 15.0,
                "single_writer_fence": {
                    "enabled": True,
                    "path": (
                        "storage/live/canary-2026-07-23-001/"
                        "market_evidence.jsonl.lock"
                    ),
                },
            },
        },
        "account": {
            "configuration_mode": "VERIFY_ONLY",
            "leverage": 1,
            "margin_type": "ISOLATED",
            "trading_budget_total": 100.0,
            "trading_budget_by_asset": {"USDT": 100.0},
        },
        "oms": {
            "journal_enabled": True,
            "replay_journal_on_startup": True,
            "journal_fsync": True,
            "journal_integrity_check": True,
            "max_total_active_orders": 2,
            "max_symbol_active_orders": 2,
            "max_strategy_active_orders": 2,
            "max_strategy_symbol_active_orders": 2,
            "journal_path": (
                "storage/live/canary-2026-07-23-001/oms_journal.jsonl"
            ),
            "truth_monitor": {
                "rpi_commission_poll_interval_sec": 30.0,
                "rpi_commission_halt_threshold": 2,
                "rpi_commission_clean_polls_to_clear": 2,
            },
            "single_writer_fence": {
                "enabled": True,
                "path": (
                    "storage/live/canary-2026-07-23-001/"
                    "oms_journal.jsonl.lock"
                ),
            },
            "venue_dead_man_switch": {"enabled": True},
        },
        "risk": {
            "active": True,
            "limits": {
                "max_order_notional": 8.0,
                "max_pos_notional": 8.0,
                "max_account_gross_notional": 8.0,
                "max_daily_loss": 2.0,
            },
            "market_data_freshness": {
                "enabled": True,
                "require_mark_price": True,
                "require_book": True,
                "max_mark_age_ms": 3000.0,
                "max_book_age_ms": 1500.0,
            },
            "funding_guard": {
                "enabled": True,
                "require_snapshot": True,
                "max_snapshot_age_ms": 3000.0,
                "pre_funding_reduce_only_sec": 600.0,
                "post_funding_hold_sec": 120.0,
                "max_abs_funding_rate": 0.0005,
                "max_next_funding_horizon_sec": 32400.0,
                "recovery_updates": 5,
            },
            "margin_health": {
                "enabled": True,
                "require_snapshot": True,
                "max_snapshot_age_sec": 15.0,
            },
            "independent_supervisor": {
                "enabled": True,
                "api_key_env": "BINANCE_RISK_API_KEY",
                "api_secret_env": "BINANCE_RISK_API_SECRET",
                "api_key": "risk-key",
                "api_secret": "risk-secret",
                "flatten_enabled": True,
                "state_required": True,
                "state_fsync": True,
                "state_path": (
                    "storage/live/canary-2026-07-23-001/"
                    "risk_supervisor_state.json"
                ),
                "daily_loss_enabled": True,
                "clock_sync_enabled": True,
                "liquidation_proximity_enabled": True,
                "require_liquidation_price": True,
                "max_open_orders": 2,
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
            "strategy_risk_budgets": {
                "enabled": True,
                "require_explicit_strategy": True,
                "budgets": {
                    "GLFT_MultiScale": {
                        "max_gross_notional": 8.0,
                        "max_symbol_notional": 8.0,
                    }
                },
            },
        },
        "strategy": {
            "primary_model": "glft",
            "registered_models": ["glft"],
            "execution_policy": "single_primary",
            "use_rpi": True,
            "rpi_fallback_to_gtx": False,
            "rpi_live_policy": {"require_zero_commission": True},
            "target_order_notional": 8.0,
            "max_pos_usdt": 8.0,
            "model_readiness": {
                "enabled": True,
                "min_volatility_samples": 50,
                "min_model_samples": 30,
                "models": {
                    "glft": {
                        "min_volatility_samples": 50,
                        "min_model_samples": 30,
                    }
                },
                "live_approval": {
                    "manifest_path": "approval.json",
                    "min_data_duration_sec": 604800,
                    "min_oos_samples": 10000,
                    "trusted_signers": {},
                },
            },
            "glft": {
                "gamma": 0.1,
                "cycle_interval": 1.0,
                "alpha": {"enabled": False},
                "target_inventory_notional_usdt": 0.0,
                "inventory_lot_notional_usdt": 8.0,
                "execution": {"min_spread_bps": 5.0},
                "calibrator": {
                    "window": 1000,
                    "min_samples": 50,
                    "initial_sigma_bps": 10.0,
                    "sigma_max_bps": 100.0,
                    "max_tick_gap_sec": 2.0,
                },
                "rpi_intensity": {
                    "min_sample_count": 30,
                    "min_depth_level_count": 3,
                    "min_total_exposure_seconds": 60.0,
                    "min_fill_count": 10,
                    "min_depth_span_bps": 0.5,
                },
            },
        },
    }


def safe_rpi_calibration_config():
    config = safe_live_config()
    live_launch = config["live_launch"]
    live_launch.update(
        {
            "stage": "rpi_calibration_canary",
            "max_deployed_capital_usdt": 50.0,
            "max_deployment_loss_usdt": 2.0,
            "calibration_permit_path": "permit.json",
            "target_deployment_config_path": "target.json",
            "calibration_permit_trusted_signers": {
                "operator-1": {
                    "algorithm": "ED25519",
                    "public_key_base64": "placeholder",
                }
            },
        }
    )
    config["account"]["trading_budget_total"] = 50.0
    config["account"]["trading_budget_by_asset"] = {"USDT": 50.0}
    config["oms"].update(
        {
            "max_total_active_orders": 1,
            "max_symbol_active_orders": 1,
            "max_strategy_active_orders": 1,
            "max_strategy_symbol_active_orders": 1,
        }
    )
    config["risk"]["limits"].update(
        {
            "max_order_notional": 8.0,
            "max_pos_notional": 8.0,
            "max_account_gross_notional": 8.0,
            "max_daily_loss": 1.0,
        }
    )
    config["risk"]["strategy_risk_budgets"]["budgets"][
        "GLFT_MultiScale"
    ].update(
        {
            "max_gross_notional": 8.0,
            "max_symbol_notional": 8.0,
        }
    )
    config["risk"]["independent_supervisor"]["max_open_orders"] = 1
    config["strategy"]["target_order_notional"] = 8.0
    config["strategy"]["max_pos_usdt"] = 8.0
    config["strategy"]["glft"]["inventory_lot_notional_usdt"] = 8.0
    config["strategy"]["glft"]["cycle_interval"] = 10.0
    digest_a = "a" * 64
    digest_b = "b" * 64
    config["_validated_rpi_calibration_permit"] = {
        "permit": {
            "permit_id": "permit-001",
            "deployment_id": live_launch["deployment_id"],
            "stage": "rpi_calibration_canary",
            "venue": "BINANCE_USDM",
            "symbol": config["symbols"][0],
            "model": "glft",
            "issued_at_utc": "2026-07-24T00:00:00Z",
            "not_before_utc": "2026-07-24T00:00:00Z",
            "expires_at_utc": "2026-07-24T01:00:00Z",
            "calibration_config_sha256": digest_a,
            "target_deployment_config_sha256": digest_b,
            "policy": {
                "fixed_depths_bps": [0.5, 1.0, 1.5],
                "order_ttl_sec": 5.0,
                "min_order_interval_sec": 10.0,
                "max_active_orders": 1,
                "max_order_count": 10,
                "min_order_notional_usdt": 5.0,
                "max_order_notional_usdt": 8.0,
                "max_cumulative_submitted_notional_usdt": 50.0,
                "max_calibration_loss_usdt": 2.0,
            },
        },
        "permit_sha256": "c" * 64,
        "calibration_config_sha256": digest_a,
        "target_deployment_config_sha256": digest_b,
    }
    return config


def safe_rpi_target_config():
    config = safe_rpi_calibration_config()
    config.pop("_validated_rpi_calibration_permit", None)
    live_launch = config["live_launch"]
    live_launch["stage"] = "canary"
    for field in (
        "calibration_permit_path",
        "target_deployment_config_path",
        "calibration_permit_trusted_signers",
    ):
        live_launch.pop(field, None)

    deployment_root = f"storage/live/{live_launch['deployment_id']}/"

    def relocate_target_state(value):
        if isinstance(value, dict):
            return {
                key: relocate_target_state(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [relocate_target_state(item) for item in value]
        if isinstance(value, str) and value.startswith(deployment_root):
            return deployment_root + "target/" + value[len(deployment_root):]
        return value

    return relocate_target_state(config)


def validate_rpi_calibration_guard(config):
    wrapper = copy.deepcopy(config["_validated_rpi_calibration_permit"])
    with patch(
        "infrastructure.rpi_calibration_permit."
        "load_and_validate_rpi_calibration_permit",
        return_value=wrapper,
    ):
        return validate_live_runtime_config(
            config,
            config_path="config.json",
        )


class LiveConfigGuardTests(unittest.TestCase):
    def setUp(self):
        self.alert_environment = patch.dict(
            os.environ,
            {
                "CHRONOSHFT_ALERT_WEBHOOK_URL": (
                    "https://alerts.invalid/chronoshft-secret"
                )
            },
        )
        self.alert_environment.start()
        self.addCleanup(self.alert_environment.stop)

    def test_safe_live_config_is_accepted_without_mutation(self):
        config = safe_live_config()
        before = copy.deepcopy(config)

        self.assertIs(validate_live_runtime_config(config), config)
        self.assertEqual(config, before)

    def test_live_dashboard_and_admin_control_are_fail_closed(self):
        cases = (
            (
                ("system", "web_dashboard", "enabled"),
                False,
                "web_dashboard.enabled",
            ),
            (
                ("system", "web_dashboard", "host"),
                "0.0.0.0",
                "explicit loopback",
            ),
            (
                ("system", "web_dashboard", "port"),
                0,
                "port must be an integer",
            ),
            (
                ("system", "admin_control", "command_ttl_sec"),
                31.0,
                "command_ttl_sec",
            ),
            (
                ("system", "admin_control", "session_max_age_sec"),
                10.0,
                "session_max_age_sec",
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

        config = safe_live_config()
        config["system"]["admin_control"]["path"] = (
            "storage/live/shared/admin"
        )
        with self.assertRaisesRegex(ValueError, "contain deployment_id"):
            validate_live_runtime_config(config)

    def test_live_external_alert_channel_is_fail_closed_and_env_only(self):
        cases = (
            ("active", False, "alert.active"),
            ("transport", "telegram", "alert.transport"),
            ("minimum_level", "ERROR", "minimum_level"),
            (
                "startup_probe_required",
                False,
                "startup_probe_required",
            ),
            ("runtime_fail_closed", False, "runtime_fail_closed"),
            ("failure_spool_fsync", False, "failure_spool_fsync"),
            ("queue_capacity", 31, "queue_capacity"),
            ("max_attempts", 4, "max_attempts"),
            (
                "startup_probe_timeout_sec",
                10.0,
                "bounded request and retry budget",
            ),
        )
        for field, value, expected in cases:
            with self.subTest(field=field):
                config = safe_live_config()
                config["alert"][field] = value
                with self.assertRaisesRegex(ValueError, expected):
                    validate_live_runtime_config(config)

        config = safe_live_config()
        config["alert"]["webhook_url"] = (
            "https://must-not-appear.invalid/secret"
        )
        with self.assertRaisesRegex(
            ValueError,
            "webhook_url must not be present",
        ):
            validate_live_runtime_config(config)

    def test_live_external_alert_endpoint_must_be_populated_https(self):
        config = safe_live_config()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                ValueError,
                "environment variable must be populated",
            ):
                validate_live_runtime_config(config)

        with patch.dict(
            os.environ,
            {"CHRONOSHFT_ALERT_WEBHOOK_URL": "http://alerts.invalid/hook"},
        ):
            with self.assertRaisesRegex(
                ValueError,
                "must be a valid HTTPS endpoint",
            ):
                validate_live_runtime_config(config)

    def test_live_external_alert_spool_is_deployment_bound_and_distinct(self):
        config = safe_live_config()
        config["alert"]["failure_spool_path"] = (
            "storage/live/other-deployment/external_alert_failures.jsonl"
        )
        with self.assertRaisesRegex(ValueError, "contain deployment_id"):
            validate_live_runtime_config(config)

    def test_live_evidence_recorder_is_required_bounded_and_disjoint(self):
        cases = (
            (
                ("record_data",),
                False,
                "record_data must be true",
            ),
            (
                ("system", "evidence_recorder", "enabled"),
                False,
                "evidence_recorder.enabled",
            ),
            (
                ("system", "evidence_recorder", "queue_capacity"),
                1023,
                "queue_capacity",
            ),
            (
                ("system", "evidence_recorder", "max_batch_records"),
                1025,
                "max_batch_records",
            ),
            (
                ("system", "evidence_recorder", "fsync_interval_sec"),
                5.01,
                "fsync_interval_sec",
            ),
            (
                ("system", "evidence_recorder", "close_timeout_sec"),
                30.01,
                "close_timeout_sec",
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

        config = safe_live_config()
        config["system"]["evidence_recorder"]["path"] = (
            config["oms"]["journal_path"]
        )
        config["system"]["evidence_recorder"]["single_writer_fence"][
            "path"
        ] = f"{config['oms']['journal_path']}.lock"
        with self.assertRaisesRegex(ValueError, "different files"):
            validate_live_runtime_config(config)

        config = safe_live_config()
        config["alert"]["failure_spool_path"] = config["oms"]["journal_path"]
        with self.assertRaisesRegex(
            ValueError,
            "must differ from oms.journal_path",
        ):
            validate_live_runtime_config(config)

    def test_safe_rpi_calibration_config_is_accepted_without_mutation(self):
        config = safe_rpi_calibration_config()
        before = copy.deepcopy(config)

        self.assertIs(validate_rpi_calibration_guard(config), config)
        self.assertEqual(config, before)

    def test_live_canary_requires_conservative_funding_guard(self):
        cases = (
            ("enabled", False, "funding_guard.enabled"),
            ("require_snapshot", False, "funding_guard.require_snapshot"),
            ("max_snapshot_age_ms", 3000.1, "max_snapshot_age_ms"),
            (
                "pre_funding_reduce_only_sec",
                299.9,
                "pre_funding_reduce_only_sec",
            ),
            ("post_funding_hold_sec", 59.9, "post_funding_hold_sec"),
            ("max_abs_funding_rate", 0.0005001, "max_abs_funding_rate"),
            (
                "max_next_funding_horizon_sec",
                32400.1,
                "max_next_funding_horizon_sec",
            ),
            ("recovery_updates", 2, "recovery_updates"),
        )
        for field, value, expected in cases:
            with self.subTest(field=field):
                config = safe_live_config()
                config["risk"]["funding_guard"][field] = value
                with self.assertRaisesRegex(ValueError, expected):
                    validate_live_runtime_config(config)

        config = safe_live_config()
        config["risk"]["funding_guard"].update(
            {
                "pre_funding_reduce_only_sec": 32400.0,
                "max_next_funding_horizon_sec": 32400.0,
            }
        )
        with self.assertRaisesRegex(ValueError, "must exceed"):
            validate_live_runtime_config(config)

    def test_rpi_calibration_guard_requires_independent_permit_revalidation(self):
        config = safe_rpi_calibration_config()
        with self.assertRaisesRegex(ValueError, "exact config_path"):
            validate_live_runtime_config(config)

        revalidated = copy.deepcopy(
            config["_validated_rpi_calibration_permit"]
        )
        revalidated["permit"]["permit_id"] = "permit-different"
        with patch(
            "infrastructure.rpi_calibration_permit."
            "load_and_validate_rpi_calibration_permit",
            return_value=revalidated,
        ):
            with self.assertRaisesRegex(ValueError, "wrapper does not match"):
                validate_live_runtime_config(
                    config,
                    config_path="config.json",
                )

    def test_rpi_calibration_hard_caps_fail_closed(self):
        cases = (
            (
                ("live_launch", "max_deployed_capital_usdt"),
                50.01,
                "0.5% of declared account equity",
            ),
            (
                ("live_launch", "max_deployment_loss_usdt"),
                2.01,
                "max_deployment_loss_usdt",
            ),
            (
                ("account", "leverage"),
                2,
                "account.leverage=1",
            ),
            (
                ("risk", "limits", "max_order_notional"),
                8.01,
                "max_order_notional",
            ),
            (
                ("risk", "limits", "max_daily_loss"),
                1.01,
                "max_daily_loss",
            ),
            (
                ("oms", "max_total_active_orders"),
                2,
                "max_total_active_orders",
            ),
        )
        for path, value, expected in cases:
            with self.subTest(path=path):
                config = safe_rpi_calibration_config()
                target = config
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                with self.assertRaisesRegex(ValueError, expected):
                    validate_rpi_calibration_guard(config)

    def test_preapproval_normalization_discards_raw_trust_products(self):
        config = safe_live_config()
        config["_validated_rpi_calibration_permit"] = {"forged": True}
        config["_validated_calibration"] = {"forged": True}
        config["strategy"]["_validated_calibration"] = {"forged": True}
        config["strategy"]["_validated_rpi_calibration_permit"] = {
            "forged": True
        }
        config["strategy"]["glft"]["_validated_calibration"] = {
            "forged": True
        }
        config["strategy"]["glft"]["_validated_rpi_calibration_permit"] = {
            "forged": True
        }

        normalized = normalize_root_config_preapproval(config)

        self.assertNotIn("_validated_calibration", normalized)
        self.assertNotIn("_validated_rpi_calibration_permit", normalized)
        self.assertNotIn(
            "_validated_calibration",
            normalized["strategy"],
        )
        self.assertNotIn(
            "_validated_rpi_calibration_permit",
            normalized["strategy"],
        )
        self.assertNotIn(
            "_validated_calibration",
            normalized["strategy"]["glft"],
        )
        self.assertNotIn(
            "_validated_rpi_calibration_permit",
            normalized["strategy"]["glft"],
        )

    def test_calibration_and_target_fixtures_share_glft_policy(self):
        calibration = normalize_root_config_preapproval(
            safe_rpi_calibration_config()
        )
        target = normalize_root_config_preapproval(safe_rpi_target_config())

        self.assertEqual(
            strategy_policy_sha256(calibration, "glft"),
            strategy_policy_sha256(target, "glft"),
        )

    def test_config_manifest_merges_single_owner_fragments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "execution.json").write_text(
                json.dumps({"execution": {"mode": "paper"}}),
                encoding="utf-8",
            )
            (root / "system.json").write_text(
                json.dumps({"system": {"log_level": "INFO"}}),
                encoding="utf-8",
            )
            (root / "dashboard.json").write_text(
                json.dumps({"system": {"web_dashboard": {"port": 8765}}}),
                encoding="utf-8",
            )
            manifest = root / "config.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema": CONFIG_MANIFEST_SCHEMA,
                        "includes": [
                            "execution.json",
                            "system.json",
                            "dashboard.json",
                        ],
                    }
                ),
                encoding="utf-8",
            )

            loaded = load_config_document(manifest)

        self.assertEqual(loaded["execution"]["mode"], "paper")
        self.assertEqual(loaded["system"]["log_level"], "INFO")
        self.assertEqual(loaded["system"]["web_dashboard"]["port"], 8765)

    def test_config_manifest_rejects_duplicate_leaf_and_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "one.json").write_text(
                json.dumps({"system": {"log_level": "INFO"}}),
                encoding="utf-8",
            )
            (root / "two.json").write_text(
                json.dumps({"system": {"log_level": "DEBUG"}}),
                encoding="utf-8",
            )
            manifest = root / "config.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema": CONFIG_MANIFEST_SCHEMA,
                        "includes": ["one.json", "two.json"],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate config field"):
                load_config_document(manifest)

            manifest.write_text(
                json.dumps(
                    {
                        "schema": CONFIG_MANIFEST_SCHEMA,
                        "includes": ["../outside.json"],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "manifest directory"):
                load_config_document(manifest)

    def test_fragmented_live_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "live.json").write_text(
                json.dumps(
                    {
                        "execution": {"mode": "live"},
                        "live_launch": {"stage": "canary"},
                        "symbols": ["BTCUSDT"],
                    }
                ),
                encoding="utf-8",
            )
            manifest = root / "config.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema": CONFIG_MANIFEST_SCHEMA,
                        "includes": ["live.json"],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Paper-only"):
                load_root_config(manifest)

    def test_resolved_state_paths_reject_parent_traversal_and_aliases(self):
        deployment_id = safe_live_config()["live_launch"]["deployment_id"]
        traversal = safe_live_config()
        journal = (
            f"storage/live/{deployment_id}/../shared/oms_journal.jsonl"
        )
        traversal["oms"]["journal_path"] = journal
        traversal["oms"]["single_writer_fence"]["path"] = f"{journal}.lock"
        with self.assertRaisesRegex(ValueError, r"\.\."):
            validate_live_state_path_bindings(traversal)

        aliased = safe_live_config()
        aliased["risk"]["independent_supervisor"]["state_path"] = (
            aliased["oms"]["journal_path"]
        )
        with self.assertRaisesRegex(ValueError, "different files"):
            validate_live_state_path_bindings(aliased)

    def test_account_truth_caps_use_conservative_real_equity(self):
        config = safe_live_config()
        snapshot = {
            "canTrade": True,
            "multiAssetsMargin": False,
            "totalWalletBalance": "10000.01",
            "totalMarginBalance": "10000.01",
            "availableBalance": "1000",
        }
        self.assertIs(
            validate_live_account_equity_truth(config, snapshot),
            snapshot,
        )

        snapshot["totalMarginBalance"] = "9999.99"
        with self.assertRaisesRegex(
            ValueError,
            "declared_account_equity_usdt",
        ):
            validate_live_account_equity_truth(config, snapshot)

        snapshot["totalMarginBalance"] = "10000.01"
        snapshot["availableBalance"] = "99.99"
        with self.assertRaisesRegex(ValueError, "availableBalance"):
            validate_live_account_equity_truth(config, snapshot)

    def test_live_flat_start_truth_rejects_any_order_or_position(self):
        positions = [
            {"symbol": "BTCUSDT", "positionAmt": "0"},
            {"symbol": "ETHUSDT", "positionAmt": "0.000"},
        ]
        self.assertEqual(
            validate_live_flat_start_truth(positions, []),
            {"position_row_count": 2, "open_order_count": 0},
        )

        with self.assertRaisesRegex(ValueError, "zero exchange open orders"):
            validate_live_flat_start_truth(
                positions,
                [{"symbol": "BTCUSDT", "orderId": 1}],
            )
        for nonzero in ("1e-18", "-0.00000001"):
            with self.subTest(nonzero=nonzero):
                with self.assertRaisesRegex(ValueError, "exact zero positions"):
                    validate_live_flat_start_truth(
                        [{"symbol": "BTCUSDT", "positionAmt": nonzero}],
                        [],
                    )

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
            ("journal_fsync", "journal_fsync"),
            ("journal_integrity_check", "journal_integrity_check"),
        )
        for field, expected in cases:
            with self.subTest(field=field):
                config = safe_live_config()
                config["oms"][field] = False

                with self.assertRaisesRegex(ValueError, expected):
                    validate_live_runtime_config(config)

    def test_mainnet_clock_and_risk_planes_are_mandatory(self):
        cases = (
            (("testnet",), True, "JSON boolean false"),
            (("testnet",), 0, "JSON boolean false"),
            (("testnet",), "false", "JSON boolean false"),
            (
                ("system", "market_data", "environment"),
                "testnet",
                "market_data.environment",
            ),
            (
                ("system", "market_data", "testnet"),
                True,
                "JSON boolean false",
            ),
            (
                ("system", "market_data", "testnet"),
                "false",
                "JSON boolean false",
            ),
            (("risk", "active"), False, "risk.active"),
            (
                ("system", "time_sync", "startup_required"),
                False,
                "startup_required",
            ),
            (
                ("system", "time_sync", "require_healthy_for_trading"),
                False,
                "require_healthy_for_trading",
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

    def test_market_and_margin_truth_are_mandatory(self):
        cases = (
            (
                ("risk", "market_data_freshness", "enabled"),
                False,
                "market_data_freshness.enabled",
            ),
            (
                ("risk", "market_data_freshness", "require_mark_price"),
                False,
                "require_mark_price",
            ),
            (
                ("risk", "market_data_freshness", "require_book"),
                False,
                "require_book",
            ),
            (
                ("risk", "market_data_freshness", "max_mark_age_ms"),
                0,
                "max_mark_age_ms",
            ),
            (
                ("risk", "market_data_freshness", "max_book_age_ms"),
                float("inf"),
                "max_book_age_ms",
            ),
            (
                ("risk", "margin_health", "enabled"),
                False,
                "margin_health.enabled",
            ),
            (
                ("risk", "margin_health", "require_snapshot"),
                False,
                "margin_health.require_snapshot",
            ),
            (
                ("risk", "margin_health", "max_snapshot_age_sec"),
                0,
                "margin_health.max_snapshot_age_sec",
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

    def test_single_writer_and_sidecar_durability_are_mandatory(self):
        cases = (
            (
                ("oms", "single_writer_fence", "enabled"),
                False,
                "single_writer_fence.enabled",
            ),
            (
                ("risk", "independent_supervisor", "state_required"),
                False,
                "state_required",
            ),
            (
                ("risk", "independent_supervisor", "state_fsync"),
                False,
                "state_fsync",
            ),
            (
                ("risk", "independent_supervisor", "daily_loss_enabled"),
                False,
                "daily_loss_enabled",
            ),
            (
                ("risk", "independent_supervisor", "clock_sync_enabled"),
                False,
                "clock_sync_enabled",
            ),
            (
                (
                    "risk",
                    "independent_supervisor",
                    "liquidation_proximity_enabled",
                ),
                False,
                "liquidation_proximity_enabled",
            ),
            (
                ("risk", "independent_supervisor", "require_liquidation_price"),
                False,
                "require_liquidation_price",
            ),
            (
                ("risk", "independent_supervisor", "state_path"),
                "storage/paper/risk_state.json",
                "state_path",
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

    def test_independent_supervisor_credentials_must_be_separate(self):
        cases = (
            (
                ("risk", "independent_supervisor", "api_key"),
                "primary-key",
                "api_key must differ",
            ),
            (
                ("risk", "independent_supervisor", "api_secret"),
                "primary-secret",
                "api_secret must differ",
            ),
            (
                ("risk", "independent_supervisor", "api_key_env"),
                "BINANCE_API_KEY",
                "API key environment variable must differ",
            ),
            (
                ("risk", "independent_supervisor", "api_secret_env"),
                "BINANCE_API_SECRET",
                "API secret environment variable must differ",
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

    def test_canary_scope_is_one_isolated_symbol_at_no_more_than_two_x(self):
        cases = (
            (("live_launch", "stage"), "production", "stage"),
            (("symbols",), ["BTCUSDT", "ETHUSDT"], "exactly one"),
            (
                ("account", "configuration_mode"),
                "APPLY",
                "VERIFY_ONLY",
            ),
            (("account", "margin_type"), "CROSSED", "ISOLATED"),
            (("account", "leverage"), 3, "leverage"),
            (("account", "leverage"), 1.5, "leverage"),
        )
        for path, value, expected in cases:
            with self.subTest(path=path, value=value):
                config = safe_live_config()
                target = config
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value

                with self.assertRaisesRegex(ValueError, expected):
                    validate_live_runtime_config(config)

    def test_canary_deployment_caps_are_enforced(self):
        cases = (
            (
                ("live_launch", "deployment_id"),
                "",
                "deployment_id",
            ),
            (
                ("live_launch", "deployment_id"),
                "EDIT-ME-rpi-canary-001",
                "EDIT-ME placeholder",
            ),
            (
                (
                    "live_launch",
                    "deployment_loss_reduce_only_fraction",
                ),
                1.0,
                "deployment_loss_reduce_only_fraction",
            ),
            (
                ("live_launch", "max_deployed_capital_usdt"),
                201.0,
                "2% of declared account equity",
            ),
            (
                ("live_launch", "max_deployed_capital_usdt"),
                100.01,
                "100 USDT for a canary",
            ),
            (
                ("live_launch", "max_deployment_loss_usdt"),
                5.01,
                "5% of deployed capital",
            ),
            (
                ("account", "trading_budget_total"),
                101.0,
                "trading_budget_total",
            ),
            (
                ("account", "trading_budget_by_asset"),
                {"USDT": 101.0},
                "trading_budget_by_asset",
            ),
            (
                ("risk", "limits", "max_order_notional"),
                8.01,
                "8 USDT",
            ),
            (
                ("risk", "limits", "max_pos_notional"),
                8.01,
                "8 USDT",
            ),
            (
                ("risk", "limits", "max_account_gross_notional"),
                8.01,
                "8 USDT",
            ),
            (
                ("risk", "limits", "max_daily_loss"),
                2.51,
                "50% of the deployment loss cap",
            ),
            (
                ("risk", "limits", "max_daily_loss"),
                2.01,
                "2 USDT",
            ),
            (
                ("account", "leverage"),
                2,
                "account.leverage=1",
            ),
            (
                ("oms", "max_total_active_orders"),
                3,
                "max_total_active_orders must be exactly 2",
            ),
            (
                (
                    "risk",
                    "independent_supervisor",
                    "max_open_orders",
                ),
                3,
                "max_open_orders must be exactly 2",
            ),
            (
                (
                    "oms",
                    "truth_monitor",
                    "rpi_commission_poll_interval_sec",
                ),
                61.0,
                "rpi_commission_poll_interval_sec",
            ),
            (
                (
                    "oms",
                    "truth_monitor",
                    "rpi_commission_halt_threshold",
                ),
                3,
                "rpi_commission_halt_threshold",
            ),
            (
                (
                    "oms",
                    "truth_monitor",
                    "rpi_commission_clean_polls_to_clear",
                ),
                3,
                "rpi_commission_clean_polls_to_clear",
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

    def test_rpi_only_canary_cannot_fall_back_to_gtx(self):
        config = safe_live_config()
        config["strategy"]["rpi_fallback_to_gtx"] = True

        with self.assertRaisesRegex(ValueError, "rpi_fallback_to_gtx=false"):
            validate_live_runtime_config(config)

        config = safe_live_config()
        config["strategy"]["use_rpi"] = False

        with self.assertRaisesRegex(ValueError, "strategy.use_rpi=true"):
            validate_live_runtime_config(config)

        config = safe_live_config()
        config["live_launch"]["rpi_only"] = False

        with self.assertRaisesRegex(ValueError, "rpi_only must be true"):
            validate_live_runtime_config(config)

    def test_rpi_only_canary_rejects_disabled_effective_glft_route(self):
        config = safe_live_config()
        config["strategy"]["use_rpi_for_glft"] = False

        with self.assertRaisesRegex(
            ValueError,
            "effective RPI route to be enabled",
        ):
            validate_live_runtime_config(config)

    def test_canary_requires_zero_fee_glft_without_alpha(self):
        cases = (
            (
                ("strategy", "primary_model"),
                "avellaneda_stoikov",
                "effective strategy configuration is invalid",
            ),
            (
                (
                    "strategy",
                    "rpi_live_policy",
                    "require_zero_commission",
                ),
                False,
                "require_zero_commission",
            ),
            (
                ("strategy", "glft", "alpha", "enabled"),
                True,
                "alpha.enabled=false",
            ),
            (
                (
                    "strategy",
                    "glft",
                    "target_inventory_notional_usdt",
                ),
                1.0,
                "target_inventory_notional_usdt=0",
            ),
            (
                ("strategy", "glft", "portfolio_risk"),
                {"enabled": True},
                "portfolio_risk.enabled=false",
            ),
            (
                ("strategy", "glft", "adaptive"),
                {"enabled": True},
                "adaptive.enabled=false",
            ),
            (
                ("strategy", "target_order_notional"),
                8.01,
                "8 USDT",
            ),
            (
                (
                    "strategy",
                    "glft",
                    "inventory_lot_notional_usdt",
                ),
                7.0,
                "must equal strategy.target_order_notional",
            ),
            (
                (
                    "strategy",
                    "glft",
                    "rpi_intensity",
                    "min_fill_count",
                ),
                9,
                "min_fill_count must be at least 10",
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

    def test_effective_glft_alias_overrides_cannot_bypass_live_limits(self):
        cases = (
            ({"max_pos_usdt": 8.01}, "max_pos_usdt"),
            ({"gamma": 1.01}, "gamma"),
            ({"cycle_interval": 0.249}, "cycle_interval"),
            (
                {"execution": {"min_spread_bps": 0.99}},
                "min_spread_bps",
            ),
            ({"calibrator": {"window": 49}}, "calibrator.window"),
            (
                {"calibrator": {"min_samples": 49}},
                "calibrator.min_samples",
            ),
            (
                {"calibrator": {"initial_sigma_bps": 0}},
                "initial_sigma_bps",
            ),
            (
                {"calibrator": {"sigma_max_bps": float("inf")}},
                "sigma_max_bps",
            ),
            (
                {"calibrator": {"max_tick_gap_sec": 0}},
                "max_tick_gap_sec",
            ),
        )
        for override, expected in cases:
            with self.subTest(override=override):
                config = safe_live_config()
                config["strategy"]["models"] = {
                    "GLFT_MultiScale": override
                }

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

    def test_live_requires_exactly_one_primary_strategy_risk_budget(self):
        cases = (
            (
                ("enabled",),
                False,
                "strategy_risk_budgets.enabled",
            ),
            (
                ("require_explicit_strategy",),
                False,
                "require_explicit_strategy",
            ),
        )
        for path, value, expected in cases:
            with self.subTest(path=path):
                config = safe_live_config()
                config["risk"]["strategy_risk_budgets"][path[0]] = value
                with self.assertRaisesRegex(ValueError, expected):
                    validate_live_runtime_config(config)

        config = safe_live_config()
        config["risk"]["strategy_risk_budgets"]["budgets"][
            "AvellanedaStoikov"
        ] = {
            "max_gross_notional": 8.0,
            "max_symbol_notional": 8.0,
        }
        with self.assertRaisesRegex(ValueError, "exactly the primary strategy"):
            validate_live_runtime_config(config)

        config = safe_live_config()
        config["risk"]["strategy_risk_budgets"]["budgets"][
            "GLFT_MultiScale"
        ]["max_gross_notional"] = 8.01
        with self.assertRaisesRegex(ValueError, "max_account_gross_notional"):
            validate_live_runtime_config(config)

    def test_root_loader_applies_guard_to_explicit_live_mode(self):
        payload = safe_live_config()
        payload.pop("api_key")
        payload.pop("api_secret")
        payload["risk"]["independent_supervisor"].pop("api_key")
        payload["risk"]["independent_supervisor"].pop("api_secret")
        credential_environment = {
            "BINANCE_API_KEY": "primary-key",
            "BINANCE_API_SECRET": "primary-secret",
            "BINANCE_RISK_API_KEY": "risk-key",
            "BINANCE_RISK_API_SECRET": "risk-secret",
        }

        with patch.dict(os.environ, credential_environment), patch(
            "infrastructure.config_scaling.validate_live_calibration_approval"
        ), patch(
            "infrastructure.live_config_guard."
            "validate_live_canary_local_evidence"
        ), patch("builtins.open", mock_open(read_data=json.dumps(payload))):
            loaded = load_root_config("config.json")

        self.assertEqual(loaded["symbols"], ["BTCUSDT"])

        payload["risk"]["independent_supervisor"]["enabled"] = False
        with patch.dict(os.environ, credential_environment), patch(
            "infrastructure.config_scaling.validate_live_calibration_approval"
        ), patch(
            "infrastructure.live_config_guard."
            "validate_live_canary_local_evidence"
        ), patch("builtins.open", mock_open(read_data=json.dumps(payload))):
            with self.assertRaisesRegex(ValueError, "independent_supervisor.enabled"):
                load_root_config("config.json")

    def test_root_live_loader_rejects_inline_credentials_without_echoing(self):
        payload = safe_live_config()
        secret_values = (
            payload["api_key"],
            payload["api_secret"],
            payload["risk"]["independent_supervisor"]["api_key"],
            payload["risk"]["independent_supervisor"]["api_secret"],
        )
        with patch("builtins.open", mock_open(read_data=json.dumps(payload))):
            with self.assertRaisesRegex(ValueError, "must not be stored inline") as caught:
                load_root_config("config.json")
        for value in secret_values:
            self.assertNotIn(value, str(caught.exception))

    def test_root_loader_reports_missing_and_unreadable_file_with_path(self):
        expected_path = os.path.abspath("missing-config.json")
        with patch("builtins.open", side_effect=FileNotFoundError("missing")):
            with self.assertRaisesRegex(
                FileNotFoundError,
                "root config file not found",
            ) as missing:
                load_root_config("missing-config.json")
        self.assertIn(expected_path, str(missing.exception))

        with patch("builtins.open", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(
                OSError,
                "root config file is not readable",
            ) as unreadable:
                load_root_config("missing-config.json")
        self.assertIn(expected_path, str(unreadable.exception))

    def test_root_loader_rejects_malformed_non_object_and_empty_json(self):
        cases = {
            "malformed": ("{", "JSON is malformed"),
            "array": ("[]", "must be a JSON object"),
            "empty": ("{}", "must not be an empty JSON object"),
            "duplicate": (
                '{"execution":{"mode":"paper"},"execution":{}}',
                "duplicate JSON object key",
            ),
            "non_standard_number": (
                '{"execution":{"mode":"paper"},"value":NaN}',
                "non-standard JSON number",
            ),
        }
        for name, (payload, expected) in cases.items():
            with self.subTest(name=name):
                with patch("builtins.open", mock_open(read_data=payload)):
                    with self.assertRaisesRegex(ValueError, expected) as raised:
                        load_root_config("invalid-config.json")
                self.assertIn(
                    os.path.abspath("invalid-config.json"),
                    str(raised.exception),
                )

    def test_root_live_loader_requires_local_evidence(self):
        payload = safe_live_config()
        payload.pop("api_key")
        payload.pop("api_secret")
        payload["risk"]["independent_supervisor"].pop("api_key")
        payload["risk"]["independent_supervisor"].pop("api_secret")
        credential_environment = {
            "BINANCE_API_KEY": "primary-key",
            "BINANCE_API_SECRET": "primary-secret",
            "BINANCE_RISK_API_KEY": "risk-key",
            "BINANCE_RISK_API_SECRET": "risk-secret",
        }
        with patch.dict(os.environ, credential_environment), patch(
            "infrastructure.config_scaling.validate_live_calibration_approval"
        ), patch("builtins.open", mock_open(read_data=json.dumps(payload))):
            with self.assertRaisesRegex(ValueError, "offline_evidence_path"):
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
