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
from event.type import OrderBook
from infrastructure.config_scaling import load_root_config
from scripts.build_rpi_calibration_artifact import (
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


def _fixture_intent(symbol, side, price, quantity):
    return {
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
        "tag": "glft_quote",
    }


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
):
    symbol = "XAUUSDT"
    entries = [("runtime_state", {"state": "READY"})]
    started_at = datetime(2026, 7, 15, tzinfo=timezone.utc)
    index = 0
    for depth_bps, filled_orders in ((0.0, 10), (1.0, 6), (2.0, 3)):
        for group_index in range(10):
            filled = group_index < filled_orders
            client_oid = f"readiness-rpi-{index:04d}"
            exchange_oid = f"exchange-{index:04d}"
            price = 2000.0
            quantity = 1.0
            side = "BUY"
            ack_time = started_at.timestamp() + index * 24_000.0
            terminal_time = ack_time + 10.0
            ack_monotonic = 10_000.0 + index * 20.0
            terminal_monotonic = ack_monotonic + 10.0
            intent = _fixture_intent(
                symbol,
                side,
                price,
                quantity,
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
                            "fill_qty": 1.0,
                            "fill_price": price,
                            "cum_filled_qty": 1.0,
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
                        filled_volume=1.0 if filled else 0.0,
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
            index += 1

    records = []
    previous_hash = ""
    for offset, (kind, payload) in enumerate(entries):
        if kind == SAMPLE_KIND:
            record_epoch = float(payload["terminal_time"]) + 0.001
        elif kind == "order_snapshot":
            record_epoch = float(payload["updated_at"])
        elif kind == "execution_record":
            record_epoch = float(payload["exchange_time"])
        else:
            record_epoch = started_at.timestamp() - 1.0
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
    return validate_rpi_calibration_journal(path, symbol=symbol)


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
        root = Path(__file__).resolve().parents[1]
        config = json.loads(
            (root / "config.live.canary.example.json").read_text(
                encoding="utf-8"
            )
        )
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
            config = self._live_config(manifest_path.name)
            policy_sha256 = strategy_policy_sha256(config, model)
            deployment_sha256 = deployment_config_sha256(config)
            implementation_sha256 = implementation_sha256_for_model(model)
            deployment_id = config["live_launch"]["deployment_id"]
            journal = root / "oms-journal.jsonl"
            summary = _write_v2_calibration_journal(
                journal,
                policy_sha256=policy_sha256,
                implementation_sha256=implementation_sha256,
                deployment_id=deployment_id,
            )
            artifact = root / "glft-calibration.json"
            build_rpi_calibration_artifact(
                journal,
                artifact,
                symbol="XAUUSDT",
                deployment_config_sha256=deployment_sha256,
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

            def utc_text(value):
                return (
                    value.astimezone(timezone.utc)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z")
                )

            source_data = root / "source-data-manifest.json"
            oos = {
                "method": "WALK_FORWARD",
                "training_ended_at_utc": utc_text(training_ended),
                "started_at_utc": utc_text(training_ended),
                "ended_at_utc": summary.last_terminal_at_utc,
                "sample_count": 10_000,
                "fill_count": 100,
                "maker_fill_fraction": 1.0,
                "rpi_commission_rate": "0",
                "net_pnl_usdt": 1.0,
                "max_drawdown_usdt": 4.0,
                "markout": {
                    "1000": {
                        "sample_count": 100,
                        "net_edge_bps_lcb95": 0.1,
                    },
                    "5000": {
                        "sample_count": 100,
                        "net_edge_bps_lcb95": 0.05,
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
                "symbols": ["XAUUSDT"],
                "capture_started_at_utc": summary.first_ack_at_utc,
                "capture_ended_at_utc": summary.last_terminal_at_utc,
                "order_sample_count": 30,
                "unique_order_count": 30,
                "journal": {
                    "path": journal.name,
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
                "approved_at": utc_text(
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
            altered_source["oos"]["net_pnl_usdt"] = 2.0
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
                "strategy configuration SHA-256 mismatch",
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
                "deployment configuration SHA-256 mismatch",
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
                    "cryptography Ed25519 verification backend is unavailable",
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

    def test_paper_root_load_does_not_require_live_manifest(self):
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

            loaded = load_root_config(str(config_path))

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
                        "symbols": ["XAUUSDT"],
                        "strategy": {
                            "primary_model": "glft",
                            "registered_models": ["glft"],
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "manifest_path"):
                load_root_config(str(config_path))

    @staticmethod
    def _live_config(manifest_path):
        return {
            "symbols": ["XAUUSDT"],
            "live_launch": {
                "deployment_id": "readiness-test-deployment",
                "max_deployment_loss_usdt": 5.0,
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


if __name__ == "__main__":
    unittest.main()
