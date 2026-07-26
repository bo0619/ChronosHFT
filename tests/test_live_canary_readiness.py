import ast
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.check_live_canary_readiness import (
    ACCOUNT_TRUTH_SOURCE,
    BLOCKED,
    EVIDENCE_SCHEMA,
    PASS,
    RPI_COMMISSION_SOURCE,
    RPI_EXCHANGE_INFO_SOURCE,
    _REQUIRED_ATTESTATIONS,
    assess_live_canary_readiness,
    main,
)
from infrastructure.live_config_guard import (
    LIVE_CANARY_EVIDENCE_CANONICALIZATION,
    LIVE_CANARY_EVIDENCE_INTEGRITY_ALGORITHM,
    LIVE_CANARY_EVIDENCE_INTEGRITY_SCHEMA,
    sign_live_canary_evidence,
    validate_live_canary_local_evidence,
)


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = ROOT / "config.live.canary.example.json"
EXAMPLE_CALIBRATION_CONFIG = (
    ROOT / "config.live.rpi-calibration.example.json"
)
FIXED_NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


def _check_by_id(report, check_id):
    return next(check for check in report["checks"] if check["id"] == check_id)


def _example_config():
    raw = EXAMPLE_CONFIG.read_text(encoding="utf-8").replace(
        "EDIT-ME-rpi-canary-001",
        "canary-2026-07-24-001",
    )
    return json.loads(raw)


def _passing_evidence(config):
    symbol = config["symbols"][0]
    deployment_id = config["live_launch"]["deployment_id"]
    evidence = {
        "schema": EVIDENCE_SCHEMA,
        "deployment_id": deployment_id,
        "symbol": symbol,
        "operator_attestations": {
            name: True for name in _REQUIRED_ATTESTATIONS
        },
        "primary_api_restrictions": {
            "source": "GET /sapi/v1/account/apiRestrictions",
            "captured_at_utc": "2026-07-24T11:55:00Z",
            "api_key_fingerprint_sha256": hashlib.sha256(
                b"primary-key"
            ).hexdigest(),
            "enableReading": True,
            "enableFutures": True,
            "enableWithdrawals": False,
            "ipRestrict": True,
        },
        "supervisor_api_restrictions": {
            "source": "GET /sapi/v1/account/apiRestrictions",
            "captured_at_utc": "2026-07-24T11:55:00Z",
            "api_key_fingerprint_sha256": hashlib.sha256(
                b"risk-key"
            ).hexdigest(),
            "enableReading": True,
            "enableFutures": True,
            "enableWithdrawals": False,
            "ipRestrict": True,
        },
        "account_truth": {
            "source": ACCOUNT_TRUTH_SOURCE,
            "captured_at_utc": "2026-07-24T11:55:00Z",
            "api_key_fingerprint_sha256": hashlib.sha256(
                b"primary-key"
            ).hexdigest(),
            "asset": "USDT",
            "canTrade": True,
            "multiAssetsMargin": False,
            "totalWalletBalance": "10000.01",
            "totalMarginBalance": "10000.01",
            "availableBalance": "10000.01",
        },
        "supervisor_account_truth": {
            "source": ACCOUNT_TRUTH_SOURCE,
            "captured_at_utc": "2026-07-24T11:55:00Z",
            "api_key_fingerprint_sha256": hashlib.sha256(
                b"risk-key"
            ).hexdigest(),
            "asset": "USDT",
            "canTrade": True,
            "multiAssetsMargin": False,
            "totalWalletBalance": "10000.01",
            "totalMarginBalance": "10000.01",
            "availableBalance": "10000.01",
        },
        "flat_start_truth": {
            "open_orders_source": "GET /fapi/v1/openOrders",
            "positions_source": "GET /fapi/v2/positionRisk",
            "captured_at_utc": "2026-07-24T11:55:00Z",
            "api_key_fingerprint_sha256": hashlib.sha256(
                b"primary-key"
            ).hexdigest(),
            "open_orders": [],
            "positions": [
                {
                    "symbol": symbol,
                    "positionAmt": "0",
                    "positionSide": "BOTH",
                    "marginType": "isolated",
                    "leverage": "1",
                }
            ],
        },
        "symbol_configuration_truth": {
            "position_mode_source": "GET /fapi/v1/positionSide/dual",
            "position_risk_source": "GET /fapi/v2/positionRisk",
            "exchange_info_source": "GET /fapi/v1/exchangeInfo",
            "captured_at_utc": "2026-07-24T11:55:00Z",
            "api_key_fingerprint_sha256": hashlib.sha256(
                b"primary-key"
            ).hexdigest(),
            "position_mode": {"dualSidePosition": False},
            "symbol_position_rows": [
                {
                    "symbol": symbol,
                    "positionAmt": "0",
                    "positionSide": "BOTH",
                    "marginType": "isolated",
                    "leverage": "1",
                }
            ],
            "exchange_symbol": {
                "symbol": symbol,
                "status": "TRADING",
                "permissionSets": [["RPI"]],
                "filters": [
                    {"filterType": "MIN_NOTIONAL", "notional": "5"}
                ],
            },
        },
        "rpi_truth": {
            "exchange_info_source": RPI_EXCHANGE_INFO_SOURCE,
            "commission_source": RPI_COMMISSION_SOURCE,
            "captured_at_utc": "2026-07-24T11:55:00Z",
            "symbol": symbol,
            "exchange_status": "TRADING",
            "supports_rpi": True,
            "makerCommissionRate": "0.0002",
            "takerCommissionRate": "0.0005",
            "rpiCommissionRate": "0",
        },
    }
    evidence["integrity"] = {
        "schema": LIVE_CANARY_EVIDENCE_INTEGRITY_SCHEMA,
        "algorithm": LIVE_CANARY_EVIDENCE_INTEGRITY_ALGORITHM,
        "canonicalization": LIVE_CANARY_EVIDENCE_CANONICALIZATION,
        "primary_hmac_sha256": "0" * 64,
        "supervisor_hmac_sha256": "1" * 64,
    }
    return evidence


def _sign_for_runtime(evidence):
    return sign_live_canary_evidence(
        evidence,
        primary_api_secret="primary-secret",
        supervisor_api_secret="risk-secret",
    )


def _write_case(directory, config, evidence):
    root = Path(directory)
    config_path = root / "canary.json"
    evidence_path = root / "evidence.json"
    config["live_launch"]["offline_evidence_path"] = evidence_path.name
    config_path.write_text(json.dumps(config), encoding="utf-8")
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    return config_path


class LiveCanaryReadinessTests(unittest.TestCase):
    def test_checker_has_no_network_gateway_or_oms_imports(self):
        script_path = ROOT / "scripts" / "check_live_canary_readiness.py"
        tree = ast.parse(script_path.read_text(encoding="utf-8"))
        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)

        forbidden_prefixes = (
            "aiohttp",
            "gateway",
            "httpx",
            "oms",
            "requests",
            "socket",
            "urllib",
        )
        self.assertFalse(
            {
                module
                for module in imported_modules
                if module.startswith(forbidden_prefixes)
            }
        )

    def test_blocked_example_rejects_placeholder_deployment_id(self):
        report = assess_live_canary_readiness(
            EXAMPLE_CONFIG,
            now_utc=FIXED_NOW,
        )

        self.assertEqual(report["status"], BLOCKED)
        guard = _check_by_id(report, "config.live_guard")
        self.assertEqual(guard["status"], BLOCKED)
        self.assertIn("EDIT-ME placeholder", guard["message"])
        self.assertEqual(
            _check_by_id(report, "state.deployment_binding")["status"],
            PASS,
        )
        self.assertEqual(
            _check_by_id(report, "model.live_approval")["status"],
            BLOCKED,
        )
        self.assertEqual(report["network_requests"], 0)
        self.assertEqual(report["order_paths_exercised"], 0)

    def test_calibration_example_uses_independent_permit_path_offline(self):
        report = assess_live_canary_readiness(
            EXAMPLE_CALIBRATION_CONFIG,
            now_utc=FIXED_NOW,
        )

        self.assertEqual(report["status"], BLOCKED)
        self.assertEqual(
            _check_by_id(report, "calibration.signed_permit")["status"],
            BLOCKED,
        )
        self.assertNotIn(
            "model.live_approval",
            {check["id"] for check in report["checks"]},
        )
        self.assertEqual(report["network_requests"], 0)
        self.assertEqual(report["gateway_objects_constructed"], 0)
        self.assertEqual(report["oms_objects_constructed"], 0)
        self.assertEqual(report["order_paths_exercised"], 0)

    def test_complete_local_evidence_can_pass_offline_prerequisites(self):
        config = _example_config()
        evidence = _passing_evidence(config)
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = _write_case(temp_dir, config, evidence)
            with patch(
                "scripts.check_live_canary_readiness."
                "validate_live_calibration_approval",
                return_value={"approved": True},
            ):
                report = assess_live_canary_readiness(
                    config_path,
                    now_utc=FIXED_NOW,
                )

        self.assertEqual(report["status"], PASS)
        self.assertTrue(
            all(check["status"] == PASS for check in report["checks"])
        )

    def test_sidecar_credential_attestations_fail_closed(self):
        critical_attestations = (
            "api_keys_same_futures_account_confirmed",
            "primary_api_futures_trading_enabled",
            "supervisor_api_futures_trading_enabled",
            "supervisor_api_emergency_permissions_confirmed",
        )
        for field in critical_attestations:
            with self.subTest(field=field):
                config = _example_config()
                evidence = _passing_evidence(config)
                evidence["operator_attestations"][field] = False
                with tempfile.TemporaryDirectory() as temp_dir:
                    config_path = _write_case(temp_dir, config, evidence)
                    with patch(
                        "scripts.check_live_canary_readiness."
                        "validate_live_calibration_approval",
                        return_value={"approved": True},
                    ):
                        report = assess_live_canary_readiness(
                            config_path,
                            now_utc=FIXED_NOW,
                        )

                check = _check_by_id(
                    report,
                    "evidence.operator_attestations",
                )
                self.assertEqual(check["status"], BLOCKED)
                self.assertIn(field, check["message"])

    def test_runtime_local_gate_binds_evidence_to_primary_api_key(self):
        config = _example_config()
        config["api_key"] = "primary-key"
        config["api_secret"] = "primary-secret"
        config["risk"]["independent_supervisor"]["api_key"] = "risk-key"
        config["risk"]["independent_supervisor"]["api_secret"] = "risk-secret"
        evidence = _sign_for_runtime(_passing_evidence(config))
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = _write_case(temp_dir, config, evidence)
            self.assertEqual(
                validate_live_canary_local_evidence(
                    config,
                    config_path=config_path,
                    now_utc=FIXED_NOW,
                ),
                evidence,
            )

            config["api_key"] = "different-key"
            with self.assertRaisesRegex(ValueError, "different primary API key"):
                validate_live_canary_local_evidence(
                    config,
                    config_path=config_path,
                    now_utc=FIXED_NOW,
                )

            config["api_key"] = "primary-key"
            config["risk"]["independent_supervisor"]["api_key"] = (
                "different-risk-key"
            )
            with self.assertRaisesRegex(
                ValueError,
                "different supervisor API key",
            ):
                validate_live_canary_local_evidence(
                    config,
                    config_path=config_path,
                    now_utc=FIXED_NOW,
                )

    def test_dual_key_account_truth_must_match_flat_start_balances(self):
        config = _example_config()
        config["api_key"] = "primary-key"
        config["api_secret"] = "primary-secret"
        config["risk"]["independent_supervisor"]["api_key"] = "risk-key"
        config["risk"]["independent_supervisor"]["api_secret"] = "risk-secret"
        evidence = _passing_evidence(config)
        evidence["supervisor_account_truth"]["availableBalance"] = "9999.99"
        evidence = _sign_for_runtime(evidence)
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = _write_case(temp_dir, config, evidence)
            with self.assertRaisesRegex(
                ValueError,
                "same stable flat-start account snapshot",
            ):
                validate_live_canary_local_evidence(
                    config,
                    config_path=config_path,
                    now_utc=FIXED_NOW,
                )

    def test_runtime_local_gate_rejects_evidence_tampering(self):
        config = _example_config()
        config["api_key"] = "primary-key"
        config["api_secret"] = "primary-secret"
        supervisor = config["risk"]["independent_supervisor"]
        supervisor["api_key"] = "risk-key"
        supervisor["api_secret"] = "risk-secret"
        evidence = _sign_for_runtime(_passing_evidence(config))
        evidence["primary_api_restrictions"]["ipRestrict"] = False
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = _write_case(temp_dir, config, evidence)
            with self.assertRaisesRegex(ValueError, "primary integrity"):
                validate_live_canary_local_evidence(
                    config,
                    config_path=config_path,
                    now_utc=FIXED_NOW,
                )

    def test_runtime_local_gate_rejects_outside_evidence_path(self):
        config = _example_config()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_directory = root / "config"
            config_directory.mkdir()
            config_path = config_directory / "canary.json"
            evidence_path = root / "evidence.json"
            config["live_launch"]["offline_evidence_path"] = str(evidence_path)
            config_path.write_text(json.dumps(config), encoding="utf-8")
            evidence_path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "config directory"):
                validate_live_canary_local_evidence(
                    config,
                    config_path=config_path,
                    now_utc=FIXED_NOW,
                )

    def test_api_permission_truth_fails_closed(self):
        config = _example_config()
        evidence = _passing_evidence(config)
        cases = (
            ("enableFutures", False),
            ("enableWithdrawals", True),
            ("ipRestrict", False),
            ("enableReading", False),
        )
        for field, value in cases:
            with self.subTest(field=field):
                case_evidence = json.loads(json.dumps(evidence))
                case_evidence["supervisor_api_restrictions"][field] = value
                with tempfile.TemporaryDirectory() as temp_dir:
                    config_path = _write_case(
                        temp_dir,
                        config,
                        case_evidence,
                    )
                    with patch(
                        "scripts.check_live_canary_readiness."
                        "validate_live_calibration_approval",
                        return_value={"approved": True},
                    ):
                        report = assess_live_canary_readiness(
                            config_path,
                            now_utc=FIXED_NOW,
                        )

                check = _check_by_id(report, "evidence.api_permissions")
                self.assertEqual(check["status"], BLOCKED)
                self.assertIn(field, check["message"])

    def test_nonzero_account_rpi_rate_blocks_zero_fee_policy(self):
        for nonzero_rate in ("0.00001", "1e-400"):
            with self.subTest(nonzero_rate=nonzero_rate):
                config = _example_config()
                evidence = _passing_evidence(config)
                evidence["rpi_truth"]["rpiCommissionRate"] = nonzero_rate
                with tempfile.TemporaryDirectory() as temp_dir:
                    config_path = _write_case(temp_dir, config, evidence)
                    with patch(
                        "scripts.check_live_canary_readiness."
                        "validate_live_calibration_approval",
                        return_value={"approved": True},
                    ):
                        report = assess_live_canary_readiness(
                            config_path,
                            now_utc=FIXED_NOW,
                        )

                check = _check_by_id(report, "evidence.rpi_truth")
                self.assertEqual(check["status"], BLOCKED)
                self.assertIn("must be zero", check["message"])

    def test_rpi_evidence_must_be_fresh_and_include_all_account_rates(self):
        cases = (
            ("captured_at_utc", "2026-07-24T10:00:00Z", "stale"),
            ("makerCommissionRate", None, "makerCommissionRate"),
            ("takerCommissionRate", None, "takerCommissionRate"),
        )
        for field, value, expected in cases:
            with self.subTest(field=field):
                config = _example_config()
                evidence = _passing_evidence(config)
                evidence["rpi_truth"][field] = value
                with tempfile.TemporaryDirectory() as temp_dir:
                    config_path = _write_case(temp_dir, config, evidence)
                    with patch(
                        "scripts.check_live_canary_readiness."
                        "validate_live_calibration_approval",
                        return_value={"approved": True},
                    ):
                        report = assess_live_canary_readiness(
                            config_path,
                            now_utc=FIXED_NOW,
                        )

                check = _check_by_id(report, "evidence.rpi_truth")
                self.assertEqual(check["status"], BLOCKED)
                self.assertIn(expected, check["message"])

    def test_state_paths_must_be_bound_to_deployment(self):
        config = _example_config()
        config["oms"]["journal_path"] = "storage/live/shared/oms_journal.jsonl"
        evidence = _passing_evidence(config)
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = _write_case(temp_dir, config, evidence)
            with patch(
                "scripts.check_live_canary_readiness."
                "validate_live_calibration_approval",
                return_value={"approved": True},
            ):
                report = assess_live_canary_readiness(
                    config_path,
                    now_utc=FIXED_NOW,
                )

        check = _check_by_id(report, "state.deployment_binding")
        self.assertEqual(check["status"], BLOCKED)
        self.assertIn("deployment_id", check["message"])

    def test_state_path_parent_traversal_is_rejected_before_resolution(self):
        config = _example_config()
        deployment_id = config["live_launch"]["deployment_id"]
        journal = (
            f"storage/live/{deployment_id}/../shared/oms_journal.jsonl"
        )
        config["oms"]["journal_path"] = journal
        config["oms"]["single_writer_fence"]["path"] = f"{journal}.lock"
        evidence = _passing_evidence(config)
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = _write_case(temp_dir, config, evidence)
            with patch(
                "scripts.check_live_canary_readiness."
                "validate_live_calibration_approval",
                return_value={"approved": True},
            ):
                report = assess_live_canary_readiness(
                    config_path,
                    now_utc=FIXED_NOW,
                )

        check = _check_by_id(report, "state.deployment_binding")
        self.assertEqual(check["status"], BLOCKED)
        self.assertIn("'..'", check["message"])

    @unittest.skipUnless(os.name == "nt", "Windows path identity requirement")
    def test_windows_case_alias_between_state_files_is_rejected(self):
        config = _example_config()
        journal = config["oms"]["journal_path"]
        config["risk"]["independent_supervisor"]["state_path"] = (
            journal.upper()
        )
        evidence = _passing_evidence(config)
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = _write_case(temp_dir, config, evidence)
            with patch(
                "scripts.check_live_canary_readiness."
                "validate_live_calibration_approval",
                return_value={"approved": True},
            ):
                report = assess_live_canary_readiness(
                    config_path,
                    now_utc=FIXED_NOW,
                )

        check = _check_by_id(report, "state.deployment_binding")
        self.assertEqual(check["status"], BLOCKED)
        self.assertIn("different files", check["message"])

    def test_declared_equity_cannot_exceed_fresh_account_truth(self):
        config = _example_config()
        evidence = _passing_evidence(config)
        evidence["account_truth"]["totalWalletBalance"] = "9999.99"
        evidence["account_truth"]["totalMarginBalance"] = "9999.99"
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = _write_case(temp_dir, config, evidence)
            with patch(
                "scripts.check_live_canary_readiness."
                "validate_live_calibration_approval",
                return_value={"approved": True},
            ):
                report = assess_live_canary_readiness(
                    config_path,
                    now_utc=FIXED_NOW,
                )

        check = _check_by_id(report, "evidence.account_truth")
        self.assertEqual(check["status"], BLOCKED)
        self.assertIn("declared_account_equity_usdt", check["message"])

    def test_evidence_path_parent_traversal_is_rejected(self):
        config = _example_config()
        evidence = _passing_evidence(config)
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = _write_case(temp_dir, config, evidence)
            config["live_launch"]["offline_evidence_path"] = "../evidence.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with patch(
                "scripts.check_live_canary_readiness."
                "validate_live_calibration_approval",
                return_value={"approved": True},
            ):
                report = assess_live_canary_readiness(
                    config_path,
                    now_utc=FIXED_NOW,
                )

        check = _check_by_id(report, "evidence.file")
        self.assertEqual(check["status"], BLOCKED)

    def test_inline_secret_is_blocked_and_never_echoed(self):
        config = _example_config()
        secret = "DO-NOT-ECHO-THIS-SECRET"
        config["api_key"] = secret
        evidence = _passing_evidence(config)
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = _write_case(temp_dir, config, evidence)
            with patch(
                "scripts.check_live_canary_readiness."
                "validate_live_calibration_approval",
                return_value={"approved": True},
            ):
                report = assess_live_canary_readiness(
                    config_path,
                    now_utc=FIXED_NOW,
                )

        check = _check_by_id(report, "credentials.references")
        self.assertEqual(check["status"], BLOCKED)
        self.assertNotIn(secret, json.dumps(report))

    def test_cli_json_is_machine_readable_and_blocked_exit_is_two(self):
        with patch("builtins.print") as print_mock:
            exit_code = main(["--config", str(EXAMPLE_CONFIG), "--json"])

        self.assertEqual(exit_code, 2)
        output = print_mock.call_args.args[0]
        self.assertEqual(json.loads(output)["status"], BLOCKED)


if __name__ == "__main__":
    unittest.main()
