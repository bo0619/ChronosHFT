import base64
import builtins
import hashlib
import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from alpha.factors import GLFTCalibrator
from alpha.signal import MultiHorizonPredictor
from data.oos_reconstruction import (
    OOS_RECONSTRUCTION_SCHEMA,
    RAW_OOS_EVIDENCE_SCHEMA,
)
from event.type import OrderBook
from governance.canonical import canonical_config_digest
from governance.release_manifest import write_release_manifest
from infrastructure.config_scaling import (
    load_root_config,
    normalize_root_config_preapproval,
)
from infrastructure.rpi_calibration_permit import (
    RPI_CALIBRATION_PERMIT_SCHEMA,
    RPI_CALIBRATION_SIGNATURE_ALGORITHM,
    RPI_CALIBRATION_STAGE,
    RPI_CALIBRATION_VENUE,
    rpi_calibration_permit_sha256,
    rpi_calibration_permit_signature_payload,
)
from scripts.build_rpi_calibration_artifact import (
    CALIBRATION_ACTIVATION_KIND,
    CALIBRATION_EXPIRY_KIND,
    CALIBRATION_JOURNAL_SCHEMA,
    CALIBRATION_RESERVATION_KIND,
    EXPECTED_DATA_SOURCE,
    EXPECTED_STRATEGY,
    SAMPLE_KIND,
    SAMPLE_SCHEMA,
    build_rpi_calibration_artifact,
    validate_rpi_calibration_journal,
)
from strategy.model_readiness import (
    APPROVAL_SIGNATURE_ALGORITHM,
    CALIBRATION_APPROVAL_SCHEMA,
    GLFT_CALIBRATION_DATA_SOURCE,
    GLFT_CALIBRATION_SOURCE_EVIDENCE_SCHEMA,
    GLFT_CALIBRATION_VENUE,
    IMPLEMENTED_UNITS_VERSION,
    apply_model_readiness_defaults,
    approval_signature_payload,
    calibration_evidence_bundle_sha256,
    deployment_config_sha256,
    evaluate_symbol_readiness,
    formula_version_for_model,
    implementation_sha256_for_model,
    oos_evidence_sha256,
    readiness_requirements,
    sha256_file,
    strategy_policy_sha256,
    validate_live_calibration_approval,
    verify_ed25519_signature,
)
from tests.test_live_config_guard import safe_live_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _attach_test_signature(manifest):
    payload = approval_signature_payload(manifest)
    signature = hashlib.sha512(payload).digest()
    manifest["signature"] = {
        "algorithm": APPROVAL_SIGNATURE_ALGORITHM,
        "key_id": "readiness-test-key",
        "signer": manifest["approved_by"],
        "signed_payload_sha256": hashlib.sha256(payload).hexdigest(),
        "signature_base64": base64.b64encode(signature).decode("ascii"),
    }


def _test_signature_verifier(algorithm, key_id, payload, signature):
    return (
        algorithm == APPROVAL_SIGNATURE_ALGORITHM
        and key_id == "readiness-test-key"
        and signature == hashlib.sha512(payload).digest()
    )


def _fixture_intent(
    symbol,
    side,
    price,
    quantity,
    *,
    permit_id="",
    depth_bps=None,
    reference_mid=None,
):
    intent = {
        "strategy_id": EXPECTED_STRATEGY,
        "symbol": symbol,
        "side": side,
        "price": price,
        "volume": quantity,
        "order_type": "LIMIT",
        "time_in_force": "RPI",
        "is_post_only": True,
        "reduce_only": False,
        "policy": "PASSIVE",
        "tag": "rpi_calibration_canary" if permit_id else "glft_quote",
    }
    if permit_id:
        intent.update(
            {
                "calibration_permit_id": permit_id,
                "calibration_depth_bps": depth_bps,
                "calibration_reference_mid": reference_mid,
            }
        )
    return intent


def _utc_text(value):
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _set_test_state_paths(config, lane):
    deployment_id = config["live_launch"]["deployment_id"]
    base = f"{deployment_id}/{lane}"
    config.setdefault("oms", {}).update(
        {
            "journal_path": f"{base}/oms-journal.jsonl",
            "single_writer_fence": {
                "enabled": True,
                "path": f"{base}/oms-journal.jsonl.lock",
            },
        }
    )
    config.setdefault("risk", {}).setdefault(
        "independent_supervisor",
        {},
    )["state_path"] = f"{base}/risk-supervisor-state.json"
    system = config.setdefault("system", {})
    system.setdefault("admin_control", {})["path"] = f"{base}/admin"
    system.setdefault("evidence_recorder", {}).update(
        {
            "path": f"{base}/market-evidence.jsonl",
            "single_writer_fence": {
                "enabled": True,
                "path": f"{base}/market-evidence.jsonl.lock",
            },
        }
    )
    config.setdefault("alert", {})["failure_spool_path"] = (
        f"{base}/alert-failures.jsonl"
    )


def _signed_test_calibration_permit(
    *,
    index,
    ack_time,
    terminal_time,
    calibration_config,
    target_config,
):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    permit = {
        "schema": RPI_CALIBRATION_PERMIT_SCHEMA,
        "permit_id": f"readiness-permit-{index:04d}",
        "authorized_by": "offline-test-operator",
        "deployment_id": calibration_config["live_launch"]["deployment_id"],
        "stage": RPI_CALIBRATION_STAGE,
        "venue": RPI_CALIBRATION_VENUE,
        "symbol": "XAUUSDT",
        "model": "glft",
        "issued_at_utc": _utc_text(
            datetime.fromtimestamp(ack_time - 180.0, tz=timezone.utc)
        ),
        "not_before_utc": _utc_text(
            datetime.fromtimestamp(ack_time - 120.0, tz=timezone.utc)
        ),
        "expires_at_utc": _utc_text(
            datetime.fromtimestamp(terminal_time + 60.0, tz=timezone.utc)
        ),
        "calibration_config_sha256": deployment_config_sha256(
            calibration_config
        ),
        "target_deployment_config_sha256": deployment_config_sha256(
            target_config
        ),
        "strategy_policy_sha256": strategy_policy_sha256(
            calibration_config,
            "glft",
        ),
        "implementation_sha256": implementation_sha256_for_model("glft"),
        "policy": {
            "fixed_depths_bps": [0.5, 1.0, 1.5],
            "order_ttl_sec": 30,
            "min_order_interval_sec": 30,
            "max_active_orders": 1,
            "max_order_count": 1,
            "min_order_notional_usdt": 5.0,
            "max_order_notional_usdt": 8.0,
            "max_cumulative_submitted_notional_usdt": 8.0,
            "max_calibration_loss_usdt": 2.0,
        },
    }
    payload = rpi_calibration_permit_signature_payload(permit)
    private_key = Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(
            "9d61b19deffd5a60ba844af492ec2cc4"
            "4449c5697b326919703bac031cae7f60"
        )
    )
    signature = private_key.sign(payload)
    permit["signature"] = {
        "algorithm": RPI_CALIBRATION_SIGNATURE_ALGORITHM,
        "key_id": "readiness-test-key",
        "signer": permit["authorized_by"],
        "signed_payload_sha256": hashlib.sha256(payload).hexdigest(),
        "signature_base64": base64.b64encode(signature).decode("ascii"),
    }
    return permit


def _fixture_snapshot(
    *,
    client_oid,
    exchange_oid,
    status,
    source,
    intent,
    created_at,
    updated_at,
    created_monotonic,
    updated_monotonic,
    filled_volume=0.0,
    exchange_status="",
):
    fill_price = intent["price"] if filled_volume else 0.0
    payload = {
        "client_oid": client_oid,
        "exchange_oid": exchange_oid,
        "status": status,
        "filled_volume": filled_volume,
        "avg_price": fill_price,
        "cumulative_cost": filled_volume * fill_price,
        "created_at": created_at,
        "updated_at": updated_at,
        "created_monotonic": created_monotonic,
        "updated_monotonic": updated_monotonic,
        "recovered_from_journal": False,
        "error_msg": "",
        "last_update_seq": 0,
        "last_exchange_status": exchange_status,
        "last_exchange_update_time": (
            updated_at if exchange_status else 0.0
        ),
        "intent": intent,
        "source": source,
    }
    if source == "exchange_update":
        payload["extra"] = {
            "exchange_status": exchange_status,
            "seq": 0,
            "cum_filled_qty": filled_volume,
        }
    return payload


def _write_v2_calibration_journal(
    path,
    *,
    policy_sha256,
    implementation_sha256,
    deployment_id,
    calibration_config,
    target_config,
    calibration_config_path,
    target_config_path,
):
    symbol = "XAUUSDT"
    entries = [("runtime_state", {"state": "READY"})]
    started_at = datetime(2026, 7, 15, tzinfo=timezone.utc)
    calibration_sha256 = deployment_config_sha256(calibration_config)
    target_sha256 = deployment_config_sha256(target_config)
    submitted_notional_microu = 6_000_000
    effective_loss_cap_microu = 2_000_000
    deployment_start_equity_microu = 1_000_000_000
    index = 0
    for depth_bps, filled_orders in ((0.5, 10), (1.0, 6), (1.5, 3)):
        for group_index in range(10):
            filled = group_index < filled_orders
            client_oid = f"readiness-rpi-{index:04d}"
            exchange_oid = f"exchange-{index:04d}"
            price = 2000.0
            quantity = 0.003
            side = "BUY"
            ack_time = started_at.timestamp() + index * 24_000.0
            terminal_time = ack_time + 10.0
            ack_monotonic = 10_000.0 + index * 20.0
            terminal_monotonic = ack_monotonic + 10.0
            reference_mid = price * math.exp(
                (depth_bps + 0.1) / 10_000.0
            )
            permit = _signed_test_calibration_permit(
                index=index,
                ack_time=ack_time,
                terminal_time=terminal_time,
                calibration_config=calibration_config,
                target_config=target_config,
            )
            permit_id = permit["permit_id"]
            permit_sha256 = rpi_calibration_permit_sha256(permit)
            not_before_ns = int((ack_time - 120.0) * 1_000_000_000)
            expires_at_ns = int((terminal_time + 60.0) * 1_000_000_000)
            cumulative_notional_microu = (
                submitted_notional_microu * (index + 1)
            )
            entries.extend(
                [
                    (
                        CALIBRATION_ACTIVATION_KIND,
                        {
                            "schema": CALIBRATION_JOURNAL_SCHEMA,
                            "signed_permit": permit,
                            "permit_id": permit_id,
                            "permit_sha256": permit_sha256,
                            "deployment_id": deployment_id,
                            "stage": RPI_CALIBRATION_STAGE,
                            "venue": RPI_CALIBRATION_VENUE,
                            "symbol": symbol,
                            "model": "glft",
                            "calibration_config_sha256": calibration_sha256,
                            "target_deployment_config_sha256": target_sha256,
                            "strategy_policy_sha256": policy_sha256,
                            "implementation_sha256": implementation_sha256,
                            "activated_at_exchange_ns": int(
                                (ack_time - 60.0) * 1_000_000_000
                            ),
                            "not_before_exchange_ns": not_before_ns,
                            "expires_at_exchange_ns": expires_at_ns,
                            "fixed_depths_bps": [0.5, 1.0, 1.5],
                            "order_ttl_ns": 30_000_000_000,
                            "min_order_interval_ns": 30_000_000_000,
                            "max_active_orders": 1,
                            "max_order_count": 1,
                            "min_order_notional_microu": 5_000_000,
                            "max_order_notional_microu": 8_000_000,
                            "max_cumulative_submitted_notional_microu": (
                                8_000_000
                            ),
                            "max_calibration_loss_microu": 2_000_000,
                            "effective_deployment_loss_cap_microu": (
                                effective_loss_cap_microu
                            ),
                            "deployment_start_equity_microu": (
                                deployment_start_equity_microu
                            ),
                            "deployment_start_external_cash_flow_microu": 0,
                            "peak_observed_loss_microu": 0,
                            "starting_reserved_order_count": index,
                            "starting_cumulative_submitted_notional_microu": (
                                submitted_notional_microu * index
                            ),
                        },
                    ),
                    (
                        CALIBRATION_RESERVATION_KIND,
                        {
                            "schema": CALIBRATION_JOURNAL_SCHEMA,
                            "reservation_seq": index + 1,
                            "permit_reservation_seq": 1,
                            "reservation_id": client_oid,
                            "client_oid": client_oid,
                            "permit_id": permit_id,
                            "permit_sha256": permit_sha256,
                            "deployment_id": deployment_id,
                            "calibration_config_sha256": calibration_sha256,
                            "target_deployment_config_sha256": target_sha256,
                            "strategy_policy_sha256": policy_sha256,
                            "implementation_sha256": implementation_sha256,
                            "reserved_at_exchange_ns": int(
                                (ack_time - 30.0) * 1_000_000_000
                            ),
                            "symbol": symbol,
                            "strategy_id": EXPECTED_STRATEGY,
                            "side": side,
                            "price": "2000",
                            "quantity": "0.003",
                            "declared_depth_bps": str(depth_bps),
                            "calibration_reference_mid": str(reference_mid),
                            "order_type": "LIMIT",
                            "time_in_force": "RPI",
                            "post_only": True,
                            "reduce_only": False,
                            "submitted_notional_microu": (
                                submitted_notional_microu
                            ),
                            "cumulative_submitted_notional_microu": (
                                cumulative_notional_microu
                            ),
                            "permit_cumulative_submitted_notional_microu": (
                                submitted_notional_microu
                            ),
                            "loss_before_send_microu": 0,
                            "effective_deployment_loss_cap_microu": (
                                effective_loss_cap_microu
                            ),
                        },
                    ),
                ]
            )
            intent = _fixture_intent(
                symbol,
                side,
                price,
                quantity,
                permit_id=permit_id,
                depth_bps=depth_bps,
                reference_mid=reference_mid,
            )
            common = {
                "client_oid": client_oid,
                "intent": intent,
                "created_at": ack_time - 1.0,
                "created_monotonic": ack_monotonic - 1.0,
            }
            entries.extend(
                [
                    (
                        "order_snapshot",
                        _fixture_snapshot(
                            **common,
                            exchange_oid="",
                            status="SUBMITTING",
                            source="accepted",
                            updated_at=ack_time - 1.0,
                            updated_monotonic=ack_monotonic - 1.0,
                        ),
                    ),
                    (
                        "order_snapshot",
                        _fixture_snapshot(
                            **common,
                            exchange_oid=exchange_oid,
                            status="PENDING_ACK",
                            source="rest_ack",
                            updated_at=ack_time,
                            updated_monotonic=ack_monotonic,
                            exchange_status="NEW",
                        ),
                    ),
                    (
                        "order_snapshot",
                        _fixture_snapshot(
                            **common,
                            exchange_oid=exchange_oid,
                            status="NEW",
                            source="exchange_update",
                            updated_at=ack_time,
                            updated_monotonic=ack_monotonic,
                            exchange_status="NEW",
                        ),
                    ),
                ]
            )
            if filled:
                trade_id = index + 1
                entries.append(
                    (
                        "execution_record",
                        {
                            "execution_id": (
                                f"BINANCE:{symbol}:{trade_id}"
                            ),
                            "venue": "BINANCE",
                            "client_oid": client_oid,
                            "exchange_oid": exchange_oid,
                            "strategy_id": EXPECTED_STRATEGY,
                            "symbol": symbol,
                            "side": side,
                            "fill_qty": quantity,
                            "fill_price": price,
                            "cum_filled_qty": quantity,
                            "exchange_status": "FILLED",
                            "exchange_time": ack_time + 5.0,
                            "trade_id": trade_id,
                            "commission": 0.0,
                            "commission_asset": "USDT",
                            "booked_fee": 0.0,
                            "realized_pnl": 0.0,
                            "is_maker": True,
                            "pre_status": "NEW",
                        },
                    )
                )
            terminal_status = "FILLED" if filled else "CANCELLED"
            terminal_exchange_status = "FILLED" if filled else "CANCELED"
            entries.append(
                (
                    "order_snapshot",
                    _fixture_snapshot(
                        **common,
                        exchange_oid=exchange_oid,
                        status=terminal_status,
                        source="exchange_update",
                        updated_at=terminal_time,
                        updated_monotonic=terminal_monotonic,
                        filled_volume=quantity if filled else 0.0,
                        exchange_status=terminal_exchange_status,
                    ),
                )
            )
            entries.append(
                (
                    SAMPLE_KIND,
                    {
                        "schema": SAMPLE_SCHEMA,
                        "strategy": EXPECTED_STRATEGY,
                        "symbol": symbol,
                        "client_oid": client_oid,
                        "exchange_oid": exchange_oid,
                        "terminal_status": terminal_status,
                        "side": side,
                        "price": price,
                        "quantity": quantity,
                        "ack_time": ack_time,
                        "ack_monotonic": ack_monotonic,
                        "terminal_time": terminal_time,
                        "terminal_monotonic": terminal_monotonic,
                        "deployment_id": deployment_id,
                        "strategy_policy_sha256": policy_sha256,
                        "implementation_sha256": implementation_sha256,
                        "exposure_bins": [
                            {
                                "depth_bps": depth_bps,
                                "exposure_seconds": 10.0,
                                "fill_count": 1 if filled else 0,
                                "sample_count": 1,
                            }
                        ],
                        "fill_count": 1 if filled else 0,
                        "censored": False,
                        "censor_reason": "",
                        "units_version": IMPLEMENTED_UNITS_VERSION,
                        "formula_version": formula_version_for_model("glft"),
                        "data_source": EXPECTED_DATA_SOURCE,
                    },
                )
            )
            entries.append(
                (
                    CALIBRATION_EXPIRY_KIND,
                    {
                        "schema": CALIBRATION_JOURNAL_SCHEMA,
                        "signed_permit": permit,
                        "permit_id": permit_id,
                        "permit_sha256": permit_sha256,
                        "deployment_id": deployment_id,
                        "symbol": symbol,
                        "calibration_config_sha256": calibration_sha256,
                        "target_deployment_config_sha256": target_sha256,
                        "strategy_policy_sha256": policy_sha256,
                        "implementation_sha256": implementation_sha256,
                        "reason": "test_permit_complete",
                        "budget_exhausted": True,
                        "expired_at_exchange_ns": int(
                            (terminal_time + 30.0) * 1_000_000_000
                        ),
                        "reserved_order_count": index + 1,
                        "cumulative_submitted_notional_microu": (
                            cumulative_notional_microu
                        ),
                        "deployment_start_equity_microu": (
                            deployment_start_equity_microu
                        ),
                        "deployment_start_external_cash_flow_microu": 0,
                        "peak_observed_loss_microu": 0,
                        "effective_deployment_loss_cap_microu": (
                            effective_loss_cap_microu
                        ),
                    },
                )
            )
            index += 1

    entries.append(("oms_stopped", {"cancel_verified": True}))

    records = []
    previous_hash = ""
    for offset, (kind, payload) in enumerate(entries):
        if kind == CALIBRATION_ACTIVATION_KIND:
            record_epoch = payload["activated_at_exchange_ns"] / 1_000_000_000
        elif kind == CALIBRATION_RESERVATION_KIND:
            record_epoch = payload["reserved_at_exchange_ns"] / 1_000_000_000
        elif kind == CALIBRATION_EXPIRY_KIND:
            record_epoch = payload["expired_at_exchange_ns"] / 1_000_000_000
        elif kind == SAMPLE_KIND:
            record_epoch = float(payload["terminal_time"]) + 0.001
        elif kind == "order_snapshot":
            record_epoch = float(payload["updated_at"])
        elif kind == "execution_record":
            record_epoch = float(payload["exchange_time"])
        elif kind == "oms_stopped":
            record_epoch = terminal_time + 31.0
        else:
            record_epoch = started_at.timestamp() - 181.0
        record_at = datetime.fromtimestamp(record_epoch, tz=timezone.utc)
        unsigned = {
            "version": 2,
            "seq": offset + 1,
            "ts": (
                record_at.isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            ),
            "kind": kind,
            "payload": payload,
            "prev_hash": previous_hash,
        }
        record_hash = hashlib.sha256(
            _canonical_json(unsigned).encode("utf-8")
        ).hexdigest()
        record = dict(unsigned)
        record["hash"] = record_hash
        records.append(record)
        previous_hash = record_hash
    path.write_text(
        "\n".join(_canonical_json(record) for record in records) + "\n",
        encoding="utf-8",
    )
    return validate_rpi_calibration_journal(
        path,
        symbol=symbol,
        calibration_config=calibration_config,
        target_deployment_config=target_config,
        calibration_config_path=calibration_config_path,
        target_deployment_config_path=target_config_path,
    )


class ModelReadinessTests(unittest.TestCase):
    def test_ed25519_verifier_matches_rfc8032_vector(self):
        try:
            import cryptography  # noqa: F401
        except ImportError:
            self.skipTest("cryptography Ed25519 backend is not installed")
        public_key = bytes.fromhex(
            "d75a980182b10ab7d54bfed3c964073a"
            "0ee172f3daa62325af021a68f707511a"
        )
        signature = bytes.fromhex(
            "e5564300c360ac729086e2cc806e828a"
            "84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46b"
            "d25bf5f0595bbe24655141438e7a100b"
        )
        self.assertTrue(
            verify_ed25519_signature(public_key, b"", signature)
        )
        corrupted = bytearray(signature)
        corrupted[0] ^= 1
        self.assertFalse(
            verify_ed25519_signature(public_key, b"", bytes(corrupted))
        )

    def test_deployment_digest_redacts_secrets_and_binds_safety_policy(self):
        config = safe_live_config()
        config["system"]["event_engine"] = {"queue_size": 10_000}
        config["system"]["strategy_runtime"] = {"cycle_timeout_ms": 250}
        config["system"]["watchdog"] = {"stall_timeout_ms": 1_000}
        baseline = deployment_config_sha256(config)
        redacted = json.loads(json.dumps(config))
        redacted["api_key"] = "must-not-affect-digest"
        redacted["api_secret"] = "must-not-affect-digest"
        supervisor = redacted["risk"]["independent_supervisor"]
        supervisor["api_key"] = "risk-key-secret"
        supervisor["api_secret"] = "risk-secret"
        redacted["strategy"]["model_readiness"]["live_approval"][
            "manifest_path"
        ] = "different-approval.json"
        redacted["system"]["web_dashboard"]["port"] = 9999
        self.assertEqual(deployment_config_sha256(redacted), baseline)

        mutations = (
            ("live cap", ("live_launch", "max_deployment_loss_usdt"), 4.0),
            ("leverage", ("account", "leverage"), 2),
            (
                "DMS",
                ("oms", "venue_dead_man_switch", "countdown_time_ms"),
                60_000,
            ),
            (
                "state path",
                ("risk", "independent_supervisor", "state_path"),
                "storage/live/other/risk_state.json",
            ),
            ("strategy", ("strategy", "glft", "gamma"), 0.2),
            (
                "event engine",
                ("system", "event_engine", "queue_size"),
                20_000,
            ),
            (
                "strategy runtime",
                ("system", "strategy_runtime", "cycle_timeout_ms"),
                500,
            ),
            (
                "watchdog",
                ("system", "watchdog", "stall_timeout_ms"),
                2_000,
            ),
            ("record data", ("record_data",), False),
            (
                "readiness",
                (
                    "strategy",
                    "model_readiness",
                    "live_approval",
                    "min_oos_samples",
                ),
                20_000,
            ),
        )
        for label, path, value in mutations:
            with self.subTest(label=label):
                changed = json.loads(json.dumps(config))
                cursor = changed
                for key in path[:-1]:
                    cursor = cursor[key]
                cursor[path[-1]] = value
                self.assertNotEqual(
                    deployment_config_sha256(changed),
                    baseline,
                )

    def test_defaults_and_model_overrides_are_explicit(self):
        strategy = {}
        apply_model_readiness_defaults(strategy)

        glft = readiness_requirements(strategy, "glft")
        avellaneda_stoikov = readiness_requirements(
            strategy,
            "avellaneda_stoikov",
        )

        self.assertTrue(glft.enabled)
        self.assertEqual(glft.min_volatility_samples, 50)
        self.assertEqual(glft.min_model_samples, 30)
        self.assertEqual(avellaneda_stoikov.min_volatility_samples, 50)
        self.assertEqual(avellaneda_stoikov.min_model_samples, 60)

    def test_symbol_readiness_fails_closed_until_both_counts_pass(self):
        requirements = readiness_requirements(
            {
                "model_readiness": {
                    "enabled": True,
                    "min_volatility_samples": 5,
                    "min_model_samples": 3,
                }
            },
            "glft",
        )

        warming = evaluate_symbol_readiness(
            "glft",
            requirements,
            volatility_samples=4,
            model_samples=3,
        )
        self.assertFalse(warming.ready)
        self.assertEqual(warming.state, "WARMING_UP")
        self.assertEqual(warming.reasons, ("volatility_samples:4<5",))

        ready = evaluate_symbol_readiness(
            "glft",
            requirements,
            volatility_samples=5,
            model_samples=3,
        )
        self.assertTrue(ready.ready)
        self.assertEqual(ready.state, "READY")

    def test_glft_calibrator_accepts_direct_model_configuration(self):
        calibrator = GLFTCalibrator(
            window=1000,
            config={
                "window": 17,
                "min_samples": 12,
                "initial_sigma_bps": 3.5,
                "initial_A": 2.0,
                "initial_k": 0.4,
                "max_tick_gap_sec": 0.75,
            },
        )

        self.assertEqual(calibrator.window, 17)
        self.assertEqual(calibrator.min_samples, 12)
        self.assertEqual(calibrator.sigma_bps, 3.5)
        self.assertEqual(calibrator.A, 2.0)
        self.assertEqual(calibrator.k, 0.4)
        self.assertEqual(calibrator.max_tick_gap, 0.75)
        self.assertEqual(calibrator.volatility_sample_count, 0)
        self.assertEqual(calibrator.intensity_sample_count, 0)

    def test_multi_horizon_model_exposes_minimum_trained_sample_count(self):
        predictor = MultiHorizonPredictor(num_features=9)
        for index in range(95):
            predictor.update_and_predict(
                [0.0] * 9,
                100.0 + index * 0.01,
                float(index),
            )

        self.assertEqual(predictor.observation_count, 95)
        self.assertEqual(predictor.sample_count, 35)

    def test_glft_volatility_uses_log_bps_and_rejects_crossed_books(self):
        calibrator = GLFTCalibrator(
            window=20,
            config={"min_samples": 2, "max_tick_gap_sec": 1.0},
        )

        def book(mid, timestamp):
            return OrderBook(
                symbol="XAUUSDT",
                exchange="BINANCE",
                datetime=datetime(2026, 7, 24),
                bids={mid - 0.5: 1.0},
                asks={mid + 0.5: 1.0},
                exchange_timestamp=timestamp,
            )

        with patch(
            "alpha.factors.time.perf_counter",
            side_effect=[1.0, 1.1, 1.2],
        ):
            calibrator.on_orderbook(book(100.0, 100.0))
            calibrator.on_orderbook(
                OrderBook(
                    symbol="XAUUSDT",
                    exchange="BINANCE",
                    datetime=datetime(2026, 7, 24),
                    bids={102.0: 1.0},
                    asks={101.0: 1.0},
                    exchange_timestamp=100.25,
                )
            )
            calibrator.on_orderbook(book(101.0, 100.5))

        self.assertEqual(calibrator.volatility_sample_count, 1)
        self.assertAlmostEqual(
            calibrator.norm_returns[-1],
            math.log(101.0 / 100.0) * 10_000.0 / math.sqrt(0.5),
        )

    def test_live_manifest_is_structurally_and_cryptographically_verified(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model = "glft"
            formula_version = formula_version_for_model(model)
            manifest_path = root / "approval.json"
            target_config_path = root / "config.json"
            raw_target_config = self._live_config(manifest_path.name)
            config = normalize_root_config_preapproval(raw_target_config)
            raw_calibration_config = json.loads(
                json.dumps(raw_target_config)
            )
            raw_calibration_config["live_launch"].update(
                {
                    "stage": RPI_CALIBRATION_STAGE,
                    "max_deployment_loss_usdt": 2.0,
                    "calibration_permit_path": "test-permit.json",
                    "target_deployment_config_path": (
                        target_config_path.name
                    ),
                    "calibration_permit_trusted_signers": {
                        "readiness-test-key": {
                            "algorithm": RPI_CALIBRATION_SIGNATURE_ALGORITHM,
                            "public_key_base64": (
                                "11qYAYKxCrfVS/7TyWQHOg7hcvPapiMl"
                                "rwIaaPcHURo="
                            ),
                        }
                    },
                }
            )
            _set_test_state_paths(raw_calibration_config, "calibration")
            calibration_config = normalize_root_config_preapproval(
                raw_calibration_config
            )
            calibration_config_path = root / "calibration-config.json"
            target_config_path.write_text(
                json.dumps(config),
                encoding="utf-8",
            )
            calibration_config_path.write_text(
                json.dumps(calibration_config),
                encoding="utf-8",
            )
            policy_sha256 = strategy_policy_sha256(config, model)
            self.assertEqual(
                strategy_policy_sha256(calibration_config, model),
                policy_sha256,
            )
            deployment_sha256 = deployment_config_sha256(config)
            implementation_sha256 = implementation_sha256_for_model(model)
            deployment_id = config["live_launch"]["deployment_id"]
            journal = root / calibration_config["oms"]["journal_path"]
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal_fence = Path(f"{journal}.lock")
            journal_fence.write_bytes(b"\0")
            summary = _write_v2_calibration_journal(
                journal,
                policy_sha256=policy_sha256,
                implementation_sha256=implementation_sha256,
                deployment_id=deployment_id,
                calibration_config=calibration_config,
                target_config=config,
                calibration_config_path=calibration_config_path,
                target_config_path=target_config_path,
            )
            artifact = root / "glft-calibration.json"
            build_rpi_calibration_artifact(
                journal,
                artifact,
                symbol="XAUUSDT",
                deployment_config_sha256=deployment_sha256,
                calibration_config=calibration_config,
                target_deployment_config=config,
                calibration_config_path=calibration_config_path,
                target_deployment_config_path=target_config_path,
            )
            capture_started = datetime.fromisoformat(
                summary.first_ack_at_utc.replace("Z", "+00:00")
            )
            capture_ended = datetime.fromisoformat(
                summary.last_terminal_at_utc.replace("Z", "+00:00")
            )
            data_duration_sec = (
                capture_ended - capture_started
            ).total_seconds()
            training_ended = capture_started + timedelta(
                seconds=data_duration_sec * 0.6
            )

            source_data = root / "source-data-manifest.json"
            oos = {
                "method": "WALK_FORWARD",
                "training_ended_at_utc": _utc_text(training_ended),
                "started_at_utc": _utc_text(training_ended),
                "ended_at_utc": summary.last_terminal_at_utc,
                "sample_count": 10_000,
                "fill_count": 100,
                "maker_fill_fraction": 1.0,
                "rpi_fill_fraction": 1.0,
                "rpi_commission_rate": "0",
                "total_commission_usdt": 0.0,
                "total_booked_fee_usdt": 0.0,
                "funding_pnl_usdt": 0.0,
                "net_pnl_usdt": 1.0,
                "exchange_net_pnl_usdt": 1.0,
                "max_drawdown_usdt": 4.0,
                "markout": {
                    "1000": {
                        "sample_count": 100,
                        "mean_net_edge_bps": 0.2,
                        "net_edge_bps_lcb95": 0.1,
                        "cluster_count": 5,
                        "cluster_unit": "UTC_DAY",
                        "estimator": "T_DISTRIBUTION_CLUSTER_MEAN",
                        "max_mark_lag_ms": 2000,
                    },
                    "5000": {
                        "sample_count": 100,
                        "mean_net_edge_bps": 0.1,
                        "net_edge_bps_lcb95": 0.05,
                        "cluster_count": 5,
                        "cluster_unit": "UTC_DAY",
                        "estimator": "T_DISTRIBUTION_CLUSTER_MEAN",
                        "max_mark_lag_ms": 2000,
                    },
                },
                "raw_evidence": {
                    "schema": RAW_OOS_EVIDENCE_SCHEMA,
                    "deployment_id": deployment_id,
                    "deployment_config_sha256": deployment_sha256,
                    "oms_journal": {
                        "sha256": summary.journal_sha256,
                        "record_count": summary.record_count,
                        "first_seq": summary.first_seq,
                        "last_seq": summary.last_seq,
                        "final_hash": summary.final_hash,
                        "last_kind": "oms_stopped",
                    },
                    "market_evidence_journal": {
                        "sha256": "a" * 64,
                        "record_count": 10_102,
                        "final_hash": "b" * 64,
                        "mark_price_count": 10_000,
                        "account_update_count": 100,
                        "last_kind": "clean_stop",
                    },
                    "reconstruction": {
                        "schema": OOS_RECONSTRUCTION_SCHEMA,
                        "flat_tolerance": 0.000001,
                        "pnl_crosscheck_tolerance_usdt": 0.000001,
                        "max_markout_lag_ms": 2000,
                        "min_utc_day_clusters": 5,
                    },
                },
            }
            source_manifest = {
                "schema": GLFT_CALIBRATION_SOURCE_EVIDENCE_SCHEMA,
                "model": model,
                "venue": GLFT_CALIBRATION_VENUE,
                "data_source": GLFT_CALIBRATION_DATA_SOURCE,
                "units_version": IMPLEMENTED_UNITS_VERSION,
                "validated_formula_version": formula_version,
                "strategy_config_sha256": policy_sha256,
                "deployment_config_sha256": deployment_sha256,
                "implementation_sha256": implementation_sha256,
                "deployment_id": deployment_id,
                "calibration_artifact_sha256": sha256_file(artifact),
                "calibration_config_path": calibration_config_path.name,
                "calibration_config_sha256": (
                    summary.calibration_config_sha256
                ),
                "symbols": ["XAUUSDT"],
                "capture_started_at_utc": summary.first_ack_at_utc,
                "capture_ended_at_utc": summary.last_terminal_at_utc,
                "order_sample_count": 30,
                "unique_order_count": 30,
                "journal": {
                    "path": journal.relative_to(root).as_posix(),
                    "sha256": summary.journal_sha256,
                    "deployment_id": deployment_id,
                    "first_seq": summary.first_seq,
                    "last_seq": summary.last_seq,
                    "final_hash": summary.final_hash,
                    "record_count": summary.record_count,
                    "first_record_at_utc": summary.first_record_at_utc,
                    "last_record_at_utc": summary.last_record_at_utc,
                    "first_sample_at_utc": summary.first_sample_at_utc,
                    "last_sample_at_utc": summary.last_sample_at_utc,
                    "first_ack_at_utc": summary.first_ack_at_utc,
                    "last_terminal_at_utc": summary.last_terminal_at_utc,
                    "sample_count": summary.sample_count,
                    "unique_order_count": summary.unique_order_count,
                    "censored_sample_count": (
                        summary.censored_sample_count
                    ),
                    "strategy_policy_sha256": policy_sha256,
                    "implementation_sha256": implementation_sha256,
                    "calibration_config_sha256": (
                        summary.calibration_config_sha256
                    ),
                    "target_deployment_config_sha256": (
                        summary.target_deployment_config_sha256
                    ),
                    "permit_activation_count": (
                        summary.permit_activation_count
                    ),
                    "permit_sha256s": list(summary.permit_sha256s),
                    "reservation_count": summary.reservation_count,
                    "cumulative_submitted_notional_microu": (
                        summary.cumulative_submitted_notional_microu
                    ),
                },
                "oos_evidence_sha256": oos_evidence_sha256(oos),
                "oos": oos,
            }
            source_data.write_text(
                json.dumps(source_manifest),
                encoding="utf-8",
            )
            artifact_sha256 = sha256_file(artifact)
            source_data_sha256 = sha256_file(source_data)
            oos_sha256 = oos_evidence_sha256(oos)
            release_manifest_path = root / "release-manifest.json"
            release_manifest = write_release_manifest(
                PROJECT_ROOT,
                release_manifest_path,
            )
            manifest = {
                "schema": CALIBRATION_APPROVAL_SCHEMA,
                "model": model,
                "symbols": ["XAUUSDT"],
                "units_version": IMPLEMENTED_UNITS_VERSION,
                "validated_formula_version": formula_version,
                "deployment_config_sha256": deployment_sha256,
                "data_duration_sec": data_duration_sec,
                "oos_samples": 10_000,
                "approved": True,
                "approved_by": "offline-review",
                "approved_at": _utc_text(
                    capture_ended + timedelta(hours=12)
                ),
                "artifact_path": artifact.name,
                "artifact_sha256": artifact_sha256,
                "source_data_path": source_data.name,
                "source_data_sha256": source_data_sha256,
                "oos_evidence_sha256": oos_sha256,
                "evidence_bundle_sha256": (
                    calibration_evidence_bundle_sha256(
                        artifact_sha256=artifact_sha256,
                        source_data_sha256=source_data_sha256,
                        oos_sha256=oos_sha256,
                        deployment_config_digest=deployment_sha256,
                        strategy_policy_digest=policy_sha256,
                        implementation_digest=implementation_sha256,
                    )
                ),
                "formula_sha256": implementation_sha256,
                "strategy_config_sha256": policy_sha256,
                "canonical_config_sha256": canonical_config_digest(config),
                "release_manifest_path": release_manifest_path.name,
                "release_digest": release_manifest["release_digest"],
            }
            _attach_test_signature(manifest)
            manifest_path.write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            approved = validate_live_calibration_approval(
                config,
                config_path=root / "config.json",
                approved_formula_versions={formula_version},
                now_utc=datetime(2026, 7, 24, 1, 0, tzinfo=timezone.utc),
                signature_verifier=_test_signature_verifier,
            )
            self.assertEqual(approved["artifact_sha256"], sha256_file(artifact))
            runtime = approved["_runtime_calibration"]
            self.assertEqual(runtime["data_source"], GLFT_CALIBRATION_DATA_SOURCE)
            self.assertGreater(
                runtime["symbols"]["XAUUSDT"]["estimate"]["A_per_s"],
                0.0,
            )

            artifact_bytes = artifact.read_bytes()
            artifact_text = artifact_bytes.decode("utf-8")
            source_text = source_data.read_text(encoding="utf-8")
            valid_manifest = json.loads(json.dumps(manifest))
            altered_artifact = json.loads(artifact_text)
            altered_artifact["symbols"]["XAUUSDT"]["rpi_exposure_bins"][0][
                "exposure_seconds"
            ] -= 1.0
            artifact.write_text(
                json.dumps(altered_artifact),
                encoding="utf-8",
            )
            manifest["artifact_sha256"] = sha256_file(artifact)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                "calibration_artifact_sha256",
            ):
                validate_live_calibration_approval(
                    config,
                    config_path=root / "config.json",
                    approved_formula_versions={formula_version},
                    now_utc=datetime(
                        2026,
                        7,
                        24,
                        1,
                        0,
                        tzinfo=timezone.utc,
                    ),
                )
            artifact.write_bytes(artifact_bytes)
            manifest = json.loads(json.dumps(valid_manifest))

            altered_source = json.loads(source_text)
            altered_source["oos"]["markout"]["1000"][
                "mean_net_edge_bps"
            ] = 0.3
            altered_source["oos_evidence_sha256"] = oos_evidence_sha256(
                altered_source["oos"]
            )
            source_data.write_text(
                json.dumps(altered_source),
                encoding="utf-8",
            )
            manifest["source_data_sha256"] = sha256_file(source_data)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "OOS evidence SHA-256"):
                validate_live_calibration_approval(
                    config,
                    config_path=root / "config.json",
                    approved_formula_versions={formula_version},
                    now_utc=datetime(
                        2026,
                        7,
                        24,
                        1,
                        0,
                        tzinfo=timezone.utc,
                    ),
                )

            source_data.write_text(source_text, encoding="utf-8")
            manifest = json.loads(json.dumps(valid_manifest))
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            changed_policy = json.loads(json.dumps(config))
            changed_policy["strategy"]["target_order_notional"] = 9.0
            with self.assertRaisesRegex(
                ValueError,
                "canonical config digest mismatch",
            ):
                validate_live_calibration_approval(
                    changed_policy,
                    config_path=root / "config.json",
                    approved_formula_versions={formula_version},
                    now_utc=datetime(
                        2026,
                        7,
                        24,
                        1,
                        0,
                        tzinfo=timezone.utc,
                    ),
                )

            changed_deployment = json.loads(json.dumps(config))
            changed_deployment["live_launch"][
                "max_deployment_loss_usdt"
            ] = 4.0
            with self.assertRaisesRegex(
                ValueError,
                "canonical config digest mismatch",
            ):
                validate_live_calibration_approval(
                    changed_deployment,
                    config_path=root / "config.json",
                    approved_formula_versions={formula_version},
                    now_utc=datetime(
                        2026,
                        7,
                        24,
                        1,
                        0,
                        tzinfo=timezone.utc,
                    ),
                )

            real_import = builtins.__import__

            def import_without_crypto(name, *args, **kwargs):
                if name.startswith("cryptography"):
                    raise ImportError("test backend unavailable")
                return real_import(name, *args, **kwargs)

            with patch(
                "builtins.__import__",
                side_effect=import_without_crypto,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "RPI calibration permit Ed25519 verifier failed closed",
                ):
                    validate_live_calibration_approval(
                        config,
                        config_path=root / "config.json",
                        approved_formula_versions={formula_version},
                        now_utc=datetime(
                            2026,
                            7,
                            24,
                            1,
                            0,
                            tzinfo=timezone.utc,
                        ),
                    )

            with self.assertRaisesRegex(ValueError, "not live-approved"):
                validate_live_calibration_approval(
                    config,
                    config_path=root / "config.json",
                )

    def test_unversioned_paper_config_requires_explicit_offline_opt_in(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "paper.json"
            config_path.write_text(
                json.dumps(
                    {
                        "execution": {"mode": "paper"},
                        "paper_trade": {"enabled": True},
                        "symbols": ["XAUUSDT"],
                        "strategy": {
                            "primary_model": "glft",
                            "registered_models": ["glft"],
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "strict"):
                load_root_config(str(config_path))
            loaded = load_root_config(
                str(config_path),
                allow_unversioned_offline=True,
            )

        self.assertEqual(loaded["execution"]["mode"], "paper")
        requirements = readiness_requirements(loaded["strategy"], "glft")
        self.assertTrue(requirements.enabled)
        self.assertEqual(requirements.min_volatility_samples, 50)

    def test_live_root_load_requires_manifest_before_runtime_startup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "live.json"
            config_path.write_text(
                json.dumps(
                    {
                        "execution": {"mode": "live"},
                        "live_launch": {"stage": "canary"},
                        "symbols": ["XAUUSDT"],
                        "strategy": {
                            "primary_model": "glft",
                            "registered_models": ["glft"],
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "strict"):
                load_root_config(str(config_path))

    @staticmethod
    def _live_config(manifest_path):
        config = {
            "execution": {"mode": "live"},
            "paper_trade": {"enabled": False},
            "testnet": False,
            "symbols": ["XAUUSDT"],
            "live_launch": {
                "stage": "canary",
                "deployment_id": "readiness-test-deployment",
                "max_deployed_capital_usdt": 100.0,
                "max_deployment_loss_usdt": 5.0,
            },
            "risk": {
                "limits": {
                    "max_order_notional": 8.0,
                },
            },
            "strategy": {
                "primary_model": "glft",
                "model_readiness": {
                    "enabled": True,
                    "min_volatility_samples": 50,
                    "min_model_samples": 30,
                    "live_approval": {
                        "manifest_path": manifest_path,
                        "min_data_duration_sec": 604_800,
                        "min_oos_samples": 10_000,
                        "trusted_signers": {
                            "readiness-test-key": {
                                "algorithm": "ED25519",
                                "public_key_base64": (
                                    "11qYAYKxCrfVS/7TyWQHOg7hcvPapiMl"
                                    "rwIaaPcHURo="
                                ),
                            }
                        },
                    },
                },
            },
        }
        _set_test_state_paths(config, "target")
        return config


if __name__ == "__main__":
    unittest.main()
