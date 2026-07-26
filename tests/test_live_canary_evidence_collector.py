import ast
import hashlib
import hmac
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from infrastructure.live_config_guard import (
    validate_live_canary_evidence_integrity,
)
from scripts.collect_live_canary_evidence import (
    EP_ACCOUNT,
    EP_API_RESTRICTIONS,
    EP_COMMISSION_RATE,
    EP_EXCHANGE_INFO,
    EP_OPEN_ORDERS,
    EP_POSITION_MODE,
    EP_POSITION_RISK,
    EP_SERVER_TIME,
    BinanceMainnetReadOnlyTransport,
    Credentials,
    OperatorAttestations,
    collect_evidence,
    load_credentials,
    resolve_evidence_output_path,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "collect_live_canary_evidence.py"


def _config():
    return {
        "api_key_env": "PRIMARY_KEY_ENV",
        "api_secret_env": "PRIMARY_SECRET_ENV",
        "symbols": ["XAUUSDT"],
        "live_launch": {
            "deployment_id": "live-rpi-001",
            "declared_account_equity_usdt": 10000,
            "max_deployed_capital_usdt": 50,
            "offline_evidence_path": "evidence.json",
            "offline_evidence_max_age_sec": 900,
        },
        "account": {
            "leverage": 1,
            "margin_type": "ISOLATED",
            "position_mode": "ONE_WAY",
        },
        "risk": {
            "independent_supervisor": {
                "api_key_env": "RISK_KEY_ENV",
                "api_secret_env": "RISK_SECRET_ENV",
            }
        },
        "strategy": {
            "target_order_notional": 8,
            "rpi_live_policy": {"require_zero_commission": True},
        },
    }


def _credentials():
    return Credentials(
        primary_key="primary-key",
        primary_secret="primary-secret",
        supervisor_key="risk-key",
        supervisor_secret="risk-secret",
    )


def _operator():
    return OperatorAttestations(
        legal_access=True,
        single_process=True,
        same_futures_account=True,
        supervisor_emergency_permissions=True,
        legacy_state_archived=True,
        fresh_state_generation=True,
    )


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.open_orders = []
        self.positions = [
            {
                "symbol": "XAUUSDT",
                "positionAmt": "0.000",
                "positionSide": "BOTH",
                "marginType": "isolated",
                "leverage": "1",
            },
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0",
                "positionSide": "BOTH",
                "marginType": "cross",
                "leverage": "1",
            },
        ]
        self.supervisor_balance = "10000.01"

    def get_json(self, endpoint, *, params=(), api_key=None):
        params = tuple(params)
        self.calls.append((endpoint, params, api_key))
        if endpoint == EP_SERVER_TIME:
            return {"serverTime": 1784894400000}
        if endpoint == EP_EXCHANGE_INFO:
            return {
                "symbols": [
                    {
                        "symbol": "XAUUSDT",
                        "status": "TRADING",
                        "permissionSets": [["GRID", "RPI"]],
                        "filters": [
                            {"filterType": "MIN_NOTIONAL", "notional": "5"}
                        ],
                    }
                ]
            }
        if endpoint == EP_COMMISSION_RATE:
            return {
                "symbol": "XAUUSDT",
                "makerCommissionRate": "0.0002",
                "takerCommissionRate": "0.0005",
                "rpiCommissionRate": "0",
            }
        if endpoint == EP_ACCOUNT:
            balance = (
                self.supervisor_balance if api_key == "risk-key" else "10000.01"
            )
            return {
                "canTrade": True,
                "multiAssetsMargin": False,
                "totalWalletBalance": balance,
                "totalMarginBalance": balance,
                "availableBalance": balance,
            }
        if endpoint == EP_API_RESTRICTIONS:
            return {
                "enableReading": True,
                "enableFutures": True,
                "enableWithdrawals": False,
                "ipRestrict": True,
            }
        if endpoint == EP_OPEN_ORDERS:
            return self.open_orders
        if endpoint == EP_POSITION_RISK:
            return self.positions
        if endpoint == EP_POSITION_MODE:
            return {"dualSidePosition": False}
        raise AssertionError(f"unexpected endpoint {endpoint}")


class LiveCanaryEvidenceCollectorTests(unittest.TestCase):
    def test_fake_transport_collects_complete_v4_evidence(self):
        transport = FakeTransport()
        with patch(
            "scripts.collect_live_canary_evidence.http.client.HTTPSConnection"
        ) as connection:
            evidence = collect_evidence(
                _config(),
                _credentials(),
                _operator(),
                transport=transport,
            )

        connection.assert_not_called()
        self.assertEqual(evidence["schema"], "chronoshft.live_canary_evidence.v4")
        self.assertEqual(
            validate_live_canary_evidence_integrity(
                evidence,
                primary_api_secret="primary-secret",
                supervisor_api_secret="risk-secret",
            ),
            evidence["integrity"],
        )
        self.assertEqual(evidence["flat_start_truth"]["open_orders"], [])
        self.assertFalse(
            evidence["symbol_configuration_truth"]["position_mode"][
                "dualSidePosition"
            ]
        )
        expected_endpoints = {
            EP_SERVER_TIME,
            EP_EXCHANGE_INFO,
            EP_COMMISSION_RATE,
            EP_ACCOUNT,
            EP_API_RESTRICTIONS,
            EP_OPEN_ORDERS,
            EP_POSITION_RISK,
            EP_POSITION_MODE,
        }
        self.assertEqual({call[0] for call in transport.calls}, expected_endpoints)
        self.assertEqual(len(transport.calls), 10)
        serialized = json.dumps(evidence)
        for secret in (
            "primary-key",
            "primary-secret",
            "risk-key",
            "risk-secret",
        ):
            self.assertNotIn(secret, serialized)

    def test_collected_evidence_integrity_rejects_tampering(self):
        evidence = collect_evidence(
            _config(),
            _credentials(),
            _operator(),
            transport=FakeTransport(),
        )
        evidence["primary_api_restrictions"]["ipRestrict"] = False
        with self.assertRaisesRegex(ValueError, "primary integrity"):
            validate_live_canary_evidence_integrity(
                evidence,
                primary_api_secret="primary-secret",
                supervisor_api_secret="risk-secret",
            )

    def test_collected_evidence_requires_supervisor_hmac(self):
        evidence = collect_evidence(
            _config(),
            _credentials(),
            _operator(),
            transport=FakeTransport(),
        )
        evidence["integrity"]["supervisor_hmac_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "supervisor integrity"):
            validate_live_canary_evidence_integrity(
                evidence,
                primary_api_secret="primary-secret",
                supervisor_api_secret="risk-secret",
            )

    def test_all_signed_calls_use_server_time_and_valid_hmac(self):
        transport = FakeTransport()
        credentials = _credentials()
        collect_evidence(
            _config(),
            credentials,
            _operator(),
            transport=transport,
        )
        secrets = {
            credentials.primary_key: credentials.primary_secret,
            credentials.supervisor_key: credentials.supervisor_secret,
        }
        signed_calls = [call for call in transport.calls if call[2] is not None]
        self.assertEqual(len(signed_calls), 8)
        for endpoint, params, api_key in signed_calls:
            with self.subTest(endpoint=endpoint, api_key=api_key):
                parameter_map = dict(params)
                self.assertGreaterEqual(int(parameter_map["timestamp"]), 1784894400000)
                self.assertEqual(parameter_map["recvWindow"], "5000")
                unsigned = tuple(pair for pair in params if pair[0] != "signature")
                expected = hmac.new(
                    secrets[api_key].encode(),
                    urlencode(unsigned).encode("ascii"),
                    hashlib.sha256,
                ).hexdigest()
                self.assertEqual(parameter_map["signature"], expected)

    def test_production_transport_rejects_endpoint_before_network(self):
        transport = BinanceMainnetReadOnlyTransport()
        with patch(
            "scripts.collect_live_canary_evidence.http.client.HTTPSConnection"
        ) as connection:
            with self.assertRaisesRegex(ValueError, "allowlist"):
                transport.get_json("/fapi/v1/unsafe")
        connection.assert_not_called()

    def test_source_has_one_fixed_http_method_and_no_mutating_api_path(self):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        request_methods = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "request"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                request_methods.append(node.args[0].value)
        self.assertEqual(request_methods, ["GET"])
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn('"/fapi/v1/order"', source)
        self.assertNotIn('"/fapi/v1/allOpenOrders"', source)
        self.assertNotIn('"/fapi/v1/countdownCancelAll"', source)

    def test_nonflat_or_misconfigured_truth_fails_closed(self):
        cases = (
            ("open order", lambda fake: fake.open_orders.append({"orderId": 1})),
            (
                "position",
                lambda fake: fake.positions[0].update({"positionAmt": "0.001"}),
            ),
            (
                "leverage",
                lambda fake: fake.positions[0].update({"leverage": "2"}),
            ),
            (
                "same account",
                lambda fake: setattr(fake, "supervisor_balance", "9999.99"),
            ),
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                transport = FakeTransport()
                mutate(transport)
                with self.assertRaises(ValueError):
                    collect_evidence(
                        _config(),
                        _credentials(),
                        _operator(),
                        transport=transport,
                    )

    def test_target_notional_below_exchange_minimum_fails_closed(self):
        config = _config()
        config["strategy"]["target_order_notional"] = Decimal("4.99")
        with self.assertRaisesRegex(ValueError, "below the exchange minimum"):
            collect_evidence(
                config,
                _credentials(),
                _operator(),
                transport=FakeTransport(),
            )

    def test_six_operator_attestations_are_never_defaulted(self):
        missing = OperatorAttestations(
            legal_access=True,
            single_process=True,
            same_futures_account=True,
            supervisor_emergency_permissions=False,
            legacy_state_archived=True,
            fresh_state_generation=True,
        )
        with self.assertRaisesRegex(ValueError, "six explicit"):
            collect_evidence(
                _config(),
                _credentials(),
                missing,
                transport=FakeTransport(),
            )

    def test_environment_loader_keeps_four_values_out_of_errors(self):
        environment = {
            "PRIMARY_KEY_ENV": "primary-key-secret-value",
            "PRIMARY_SECRET_ENV": "primary-secret-value",
            "RISK_KEY_ENV": "risk-key-secret-value",
        }
        with self.assertRaises(ValueError) as caught:
            load_credentials(_config(), environ=environment)
        message = str(caught.exception)
        for value in environment.values():
            self.assertNotIn(value, message)

    def test_output_is_config_bound_and_parent_traversal_is_rejected(self):
        config = _config()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "canary.json"
            config_path.write_text("{}", encoding="utf-8")
            self.assertEqual(
                resolve_evidence_output_path(config, config_path=config_path),
                Path(temp_dir) / "evidence.json",
            )
            config["live_launch"]["offline_evidence_path"] = "../evidence.json"
            with self.assertRaisesRegex(ValueError, r"\.\."):
                resolve_evidence_output_path(config, config_path=config_path)

            outside_path = Path(temp_dir).parent / "outside-evidence.json"
            config["live_launch"]["offline_evidence_path"] = str(outside_path)
            with self.assertRaisesRegex(ValueError, "config directory"):
                resolve_evidence_output_path(config, config_path=config_path)

    def test_output_rejects_unc_device_and_drive_relative_paths(self):
        for value in (
            r"\\server\share\evidence.json",
            r"\\?\C:\evidence.json",
            r"C:evidence.json",
            r"evidence.json:payload",
            r"NUL.json",
        ):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp_dir:
                config = _config()
                config["live_launch"]["offline_evidence_path"] = value
                config_path = Path(temp_dir) / "canary.json"
                config_path.write_text("{}", encoding="utf-8")
                with self.assertRaises(ValueError):
                    resolve_evidence_output_path(
                        config,
                        config_path=config_path,
                    )

    def test_output_rejects_symlinked_parent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            linked = root / "linked"
            linked.mkdir()
            config_path = root / "canary.json"
            config_path.write_text("{}", encoding="utf-8")
            config = _config()
            config["live_launch"]["offline_evidence_path"] = (
                "linked/evidence.json"
            )
            linked_identity = linked.resolve()

            def reports_reparse(path):
                return Path(path).resolve(strict=False) == linked_identity

            with patch(
                "infrastructure.live_config_guard._path_is_reparse_or_symlink",
                side_effect=reports_reparse,
            ), self.assertRaisesRegex(ValueError, "symlink or reparse"):
                resolve_evidence_output_path(
                    config,
                    config_path=config_path,
                )

    def test_output_rejects_mapped_network_drive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "canary.json"
            config_path.write_text("{}", encoding="utf-8")
            with patch(
                "infrastructure.live_config_guard._path_uses_remote_drive",
                return_value=True,
            ), self.assertRaisesRegex(ValueError, "mapped network drive"):
                resolve_evidence_output_path(
                    _config(),
                    config_path=config_path,
                )


if __name__ == "__main__":
    unittest.main()
