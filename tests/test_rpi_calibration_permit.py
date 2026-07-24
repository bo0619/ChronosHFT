import base64
import copy
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from infrastructure.rpi_calibration_permit import (
    MAX_PERMIT_TTL_SEC,
    RPI_CALIBRATION_PERMIT_SCHEMA,
    RPI_CALIBRATION_SIGNATURE_ALGORITHM,
    RPI_CALIBRATION_SIGNATURE_DOMAIN,
    RPI_CALIBRATION_STAGE,
    RPI_CALIBRATION_VENUE,
    RpiCalibrationPermitError,
    load_and_validate_rpi_calibration_permit,
    parse_rpi_calibration_permit_json,
    rpi_calibration_permit_sha256,
    rpi_calibration_permit_signature_payload,
    validate_rpi_calibration_permit,
)
from strategy.model_readiness import (
    deployment_config_sha256,
    implementation_sha256_for_model,
    strategy_policy_sha256,
)


NOW = datetime(2026, 7, 24, 12, 2, tzinfo=timezone.utc)
PUBLIC_KEY = b"K" * 32
KEY_ID = "rpi-permit-test-key"
AUTHORIZED_BY = "offline-test-operator"


def _strategy_config():
    return {
        "primary_model": "glft",
        "use_rpi": True,
        "use_rpi_for_glft": True,
        "rpi_fallback_to_gtx": False,
        "target_order_notional": 8.0,
        "max_pos_usdt": 18.0,
        "glft": {
            "gamma": 0.1,
            "cycle_interval": 1.0,
            "use_rpi": True,
            "rpi_fallback_to_gtx": False,
            "alpha": {"enabled": False},
        },
    }


def _configs():
    deployment_id = "rpi-calibration-20260724-001"
    trusted_signers = {
        KEY_ID: {
            "algorithm": RPI_CALIBRATION_SIGNATURE_ALGORITHM,
            "public_key_base64": base64.b64encode(PUBLIC_KEY).decode("ascii"),
        }
    }
    calibration = {
        "execution": {"mode": "live"},
        "paper_trade": {"enabled": False},
        "testnet": False,
        "record_data": True,
        "symbols": ["XAUUSDT"],
        "live_launch": {
            "stage": RPI_CALIBRATION_STAGE,
            "deployment_id": deployment_id,
            "max_deployed_capital_usdt": 100.0,
            "max_deployment_loss_usdt": 2.0,
            "calibration_permit_path": "permit.json",
            "target_deployment_config_path": "target.json",
            "calibration_permit_trusted_signers": trusted_signers,
        },
        "system": {},
        "account": {},
        "oms": {
            "journal_path": (
                f"storage/live/{deployment_id}/calibration/oms_journal.jsonl"
            ),
            "single_writer_fence": {
                "path": (
                    f"storage/live/{deployment_id}/calibration/"
                    "oms_journal.jsonl.lock"
                ),
            },
        },
        "risk": {
            "limits": {"max_order_notional": 8.0},
            "independent_supervisor": {
                "state_path": (
                    f"storage/live/{deployment_id}/calibration/"
                    "risk_supervisor_state.json"
                ),
            },
        },
        "strategy": _strategy_config(),
    }
    target = copy.deepcopy(calibration)
    target["live_launch"] = {
        "stage": "canary",
        "deployment_id": deployment_id,
        "max_deployed_capital_usdt": 100.0,
        "max_deployment_loss_usdt": 5.0,
    }
    target["oms"]["journal_path"] = (
        f"storage/live/{deployment_id}/target/oms_journal.jsonl"
    )
    target["oms"]["single_writer_fence"]["path"] = (
        f"storage/live/{deployment_id}/target/oms_journal.jsonl.lock"
    )
    target["risk"]["independent_supervisor"]["state_path"] = (
        f"storage/live/{deployment_id}/target/risk_supervisor_state.json"
    )
    return calibration, target, trusted_signers


def _unsigned_permit(calibration, target):
    return {
        "schema": RPI_CALIBRATION_PERMIT_SCHEMA,
        "permit_id": "rpi-permit-20260724-001",
        "authorized_by": AUTHORIZED_BY,
        "deployment_id": calibration["live_launch"]["deployment_id"],
        "stage": RPI_CALIBRATION_STAGE,
        "venue": RPI_CALIBRATION_VENUE,
        "symbol": "XAUUSDT",
        "model": "glft",
        "issued_at_utc": "2026-07-24T11:59:00Z",
        "not_before_utc": "2026-07-24T12:00:00Z",
        "expires_at_utc": "2026-07-24T12:10:00Z",
        "calibration_config_sha256": deployment_config_sha256(calibration),
        "target_deployment_config_sha256": deployment_config_sha256(target),
        "strategy_policy_sha256": strategy_policy_sha256(
            calibration,
            "glft",
        ),
        "implementation_sha256": implementation_sha256_for_model("glft"),
        "policy": {
            "fixed_depths_bps": [1.0, 1.25, 1.5],
            "order_ttl_sec": 30,
            "min_order_interval_sec": 30,
            "max_active_orders": 1,
            "max_order_count": 10,
            "min_order_notional_usdt": 5.0,
            "max_order_notional_usdt": 8.0,
            "max_cumulative_submitted_notional_usdt": 80.0,
            "max_calibration_loss_usdt": 2.0,
        },
    }


def _signed_permit(calibration, target):
    permit = _unsigned_permit(calibration, target)
    payload = rpi_calibration_permit_signature_payload(permit)
    signature = hashlib.sha512(payload).digest()
    permit["signature"] = {
        "algorithm": RPI_CALIBRATION_SIGNATURE_ALGORITHM,
        "key_id": KEY_ID,
        "signer": AUTHORIZED_BY,
        "signed_payload_sha256": hashlib.sha256(payload).hexdigest(),
        "signature_base64": base64.b64encode(signature).decode("ascii"),
    }
    return permit


def _test_signature_verifier(algorithm, key_id, payload, signature):
    return (
        algorithm == RPI_CALIBRATION_SIGNATURE_ALGORITHM
        and key_id == KEY_ID
        and signature == hashlib.sha512(payload).digest()
    )


def _validate(
    permit,
    calibration,
    target,
    trusted_signers,
    *,
    now_utc=NOW,
    verifier=_test_signature_verifier,
):
    return validate_rpi_calibration_permit(
        permit,
        calibration_config=calibration,
        target_deployment_config=target,
        trusted_signers=trusted_signers,
        now_utc=now_utc,
        signature_verifier=verifier,
    )


def test_valid_permit_returns_signed_payload_and_bound_digests():
    calibration, target, trusted_signers = _configs()
    permit = _signed_permit(calibration, target)

    result = _validate(permit, calibration, target, trusted_signers)

    assert result == {
        "permit": permit,
        "permit_sha256": rpi_calibration_permit_sha256(permit),
        "calibration_config_sha256": deployment_config_sha256(calibration),
        "target_deployment_config_sha256": deployment_config_sha256(target),
    }
    assert result["permit"] is not permit
    assert result["permit"]["signature"] == permit["signature"]
    json.dumps(result, allow_nan=False)


def test_signature_payload_uses_independent_domain_and_is_order_stable():
    calibration, target, _ = _configs()
    unsigned = _unsigned_permit(calibration, target)
    reordered = dict(reversed(tuple(unsigned.items())))

    first = rpi_calibration_permit_signature_payload(unsigned)
    second = rpi_calibration_permit_signature_payload(reordered)

    assert first == second
    assert first.startswith(RPI_CALIBRATION_SIGNATURE_DOMAIN)
    assert b"chronoshft.calibration_approval" not in first[:80]


def test_complete_permit_hash_is_stable_across_mapping_order():
    calibration, target, _ = _configs()
    permit = _signed_permit(calibration, target)
    reordered = dict(reversed(tuple(permit.items())))
    reordered["signature"] = dict(
        reversed(tuple(permit["signature"].items()))
    )

    assert rpi_calibration_permit_sha256(reordered) == (
        rpi_calibration_permit_sha256(permit)
    )


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (
            lambda permit: permit.update({"unexpected": True}),
            "permit keys",
        ),
        (
            lambda permit: permit.pop("venue"),
            "permit keys",
        ),
        (
            lambda permit: permit["policy"].update({"quote_depth_bps": 1}),
            "policy keys",
        ),
        (
            lambda permit: permit["policy"].pop("fixed_depths_bps"),
            "policy keys",
        ),
        (
            lambda permit: permit["signature"].update({"extra": "field"}),
            "signature keys",
        ),
    ],
)
def test_exact_key_contract_rejects_missing_extra_and_alias_keys(
    mutation,
    match,
):
    calibration, target, trusted_signers = _configs()
    permit = _signed_permit(calibration, target)
    mutation(permit)

    with pytest.raises(RpiCalibrationPermitError, match=match):
        _validate(permit, calibration, target, trusted_signers)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("schema", "chronoshft.rpi_calibration_permit.v0", "schema"),
        ("deployment_id", "other-deployment", "deployment_id"),
        ("stage", "canary", "stage"),
        ("venue", "OTHER", "venue"),
        ("symbol", "BTCUSDT", "symbol"),
        ("model", "avellaneda_stoikov", "model"),
        ("calibration_config_sha256", "0" * 64, "binding mismatch"),
        (
            "target_deployment_config_sha256",
            "0" * 64,
            "binding mismatch",
        ),
        ("strategy_policy_sha256", "0" * 64, "binding mismatch"),
        ("implementation_sha256", "0" * 64, "binding mismatch"),
    ],
)
def test_permit_is_bound_to_runtime_identity_and_digests(field, value, match):
    calibration, target, trusted_signers = _configs()
    permit = _signed_permit(calibration, target)
    permit[field] = value

    with pytest.raises(RpiCalibrationPermitError, match=match):
        _validate(permit, calibration, target, trusted_signers)


def test_target_config_mutation_invalidates_signed_digest():
    calibration, target, trusted_signers = _configs()
    permit = _signed_permit(calibration, target)
    target["risk"]["limits"]["max_order_notional"] = 7.0

    with pytest.raises(RpiCalibrationPermitError, match="binding mismatch"):
        _validate(permit, calibration, target, trusted_signers)


def test_calibration_and_target_must_share_deployment_id():
    calibration, target, trusted_signers = _configs()
    target["live_launch"]["deployment_id"] = "different-deployment-001"
    permit = _signed_permit(calibration, target)

    with pytest.raises(RpiCalibrationPermitError, match="must match"):
        _validate(permit, calibration, target, trusted_signers)


def test_calibration_and_target_strategy_policy_must_match():
    calibration, target, trusted_signers = _configs()
    target["strategy"]["target_order_notional"] = 7.0
    permit = _signed_permit(calibration, target)

    with pytest.raises(RpiCalibrationPermitError, match="policy digests"):
        _validate(permit, calibration, target, trusted_signers)


def test_calibration_and_target_durable_state_paths_must_not_overlap():
    calibration, target, trusted_signers = _configs()
    target["oms"] = copy.deepcopy(calibration["oms"])
    target["risk"]["independent_supervisor"] = copy.deepcopy(
        calibration["risk"]["independent_supervisor"]
    )
    permit = _signed_permit(calibration, target)

    with pytest.raises(RpiCalibrationPermitError, match="fully isolated"):
        _validate(permit, calibration, target, trusted_signers)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("fixed_depths_bps", [1.0, 1.5], "between 3 and"),
        (
            "fixed_depths_bps",
            [1.0, 1.5, 1.25],
            "strictly increasing",
        ),
        ("fixed_depths_bps", [1.0, 1.1, 1.2], "span"),
        ("fixed_depths_bps", [1.0, 2.0, 1001.0], "maximum"),
        ("order_ttl_sec", 61, "must not exceed"),
        ("min_order_interval_sec", 4, "outside"),
        ("max_active_orders", 2, "exactly 1"),
        ("max_order_count", 101, "must not exceed"),
        ("min_order_notional_usdt", 0, "positive"),
        ("max_order_notional_usdt", 8.01, "must not exceed"),
        ("max_cumulative_submitted_notional_usdt", 81, "exceeds"),
        ("max_calibration_loss_usdt", 2.01, "must not exceed"),
    ],
)
def test_signed_policy_hard_caps_are_fail_closed(field, value, match):
    calibration, target, trusted_signers = _configs()
    permit = _signed_permit(calibration, target)
    permit["policy"][field] = value

    with pytest.raises(RpiCalibrationPermitError, match=match):
        _validate(permit, calibration, target, trusted_signers)


def test_order_ttl_cannot_exceed_submission_interval():
    calibration, target, trusted_signers = _configs()
    permit = _signed_permit(calibration, target)
    permit["policy"]["order_ttl_sec"] = 31

    with pytest.raises(RpiCalibrationPermitError, match="must not exceed policy"):
        _validate(permit, calibration, target, trusted_signers)


def test_order_schedule_must_fit_inside_active_permit_window():
    calibration, target, trusted_signers = _configs()
    permit = _signed_permit(calibration, target)
    permit["policy"]["min_order_interval_sec"] = 61
    permit["policy"]["order_ttl_sec"] = 60

    with pytest.raises(RpiCalibrationPermitError, match="validity window"):
        _validate(permit, calibration, target, trusted_signers)


def test_policy_cannot_exceed_calibration_config_caps():
    calibration, target, trusted_signers = _configs()
    calibration["live_launch"]["max_deployment_loss_usdt"] = 1.0
    permit = _signed_permit(calibration, target)
    with pytest.raises(RpiCalibrationPermitError, match="deployment loss"):
        _validate(permit, calibration, target, trusted_signers)

    calibration, target, trusted_signers = _configs()
    calibration["risk"]["limits"]["max_order_notional"] = 7.0
    permit = _signed_permit(calibration, target)
    with pytest.raises(RpiCalibrationPermitError, match="risk cap"):
        _validate(permit, calibration, target, trusted_signers)


@pytest.mark.parametrize(
    ("issued_at", "not_before", "expires_at", "now_utc", "match"),
    [
        (
            "2026-07-24T12:01:00Z",
            "2026-07-24T12:00:00Z",
            "2026-07-24T12:10:00Z",
            NOW,
            "timestamps",
        ),
        (
            "2026-07-24T11:59:00Z",
            "2026-07-24T12:03:00Z",
            "2026-07-24T12:10:00Z",
            NOW,
            "not active",
        ),
        (
            "2026-07-24T11:59:00Z",
            "2026-07-24T12:00:00Z",
            "2026-07-24T12:02:00Z",
            NOW,
            "expired",
        ),
        (
            "2026-07-24T11:59:00+08:00",
            "2026-07-24T12:00:00Z",
            "2026-07-24T12:10:00Z",
            NOW,
            "explicit UTC",
        ),
    ],
)
def test_permit_time_window_is_strict(
    issued_at,
    not_before,
    expires_at,
    now_utc,
    match,
):
    calibration, target, trusted_signers = _configs()
    permit = _signed_permit(calibration, target)
    permit["issued_at_utc"] = issued_at
    permit["not_before_utc"] = not_before
    permit["expires_at_utc"] = expires_at

    with pytest.raises(RpiCalibrationPermitError, match=match):
        _validate(
            permit,
            calibration,
            target,
            trusted_signers,
            now_utc=now_utc,
        )


def test_permit_ttl_is_capped_at_24_hours():
    calibration, target, trusted_signers = _configs()
    permit = _signed_permit(calibration, target)
    issued_at = datetime(2026, 7, 24, 0, 0, tzinfo=timezone.utc)
    permit["issued_at_utc"] = issued_at.isoformat()
    permit["not_before_utc"] = issued_at.isoformat()
    permit["expires_at_utc"] = (
        issued_at + timedelta(seconds=MAX_PERMIT_TTL_SEC + 1)
    ).isoformat()

    with pytest.raises(RpiCalibrationPermitError, match="TTL"):
        _validate(permit, calibration, target, trusted_signers)


def test_naive_validation_clock_is_rejected():
    calibration, target, trusted_signers = _configs()
    permit = _signed_permit(calibration, target)

    with pytest.raises(RpiCalibrationPermitError, match="timezone-aware"):
        _validate(
            permit,
            calibration,
            target,
            trusted_signers,
            now_utc=datetime(2026, 7, 24, 12, 2),
        )


def test_duplicate_keys_and_nonfinite_json_are_rejected():
    with pytest.raises(RpiCalibrationPermitError, match="duplicate"):
        parse_rpi_calibration_permit_json(
            '{"schema":"first","schema":"second"}'
        )
    with pytest.raises(RpiCalibrationPermitError, match="duplicate"):
        parse_rpi_calibration_permit_json(
            '{"policy":{"max_order_count":1,"max_order_count":2}}'
        )
    for constant in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(RpiCalibrationPermitError, match="not allowed"):
            parse_rpi_calibration_permit_json(
                f'{{"policy":{{"order_ttl_sec":{constant}}}}}'
            )


def test_nonfinite_direct_mapping_is_rejected_before_signature_verification():
    calibration, target, trusted_signers = _configs()
    permit = _signed_permit(calibration, target)
    permit["policy"]["fixed_depths_bps"][0] = math.nan

    with pytest.raises(RpiCalibrationPermitError, match="canonical JSON"):
        _validate(permit, calibration, target, trusted_signers)


def test_signature_verification_and_backend_fail_closed():
    calibration, target, trusted_signers = _configs()
    permit = _signed_permit(calibration, target)

    with pytest.raises(RpiCalibrationPermitError, match="verification failed"):
        _validate(
            permit,
            calibration,
            target,
            trusted_signers,
            verifier=lambda *_: False,
        )
    with pytest.raises(RpiCalibrationPermitError, match="failed closed"):
        _validate(
            permit,
            calibration,
            target,
            trusted_signers,
            verifier=lambda *_: (_ for _ in ()).throw(RuntimeError("boom")),
        )
    with pytest.raises(RpiCalibrationPermitError, match="verification failed"):
        _validate(
            permit,
            calibration,
            target,
            trusted_signers,
            verifier=lambda *_: 1,
        )
    with patch(
        "infrastructure.rpi_calibration_permit.verify_ed25519_signature",
        side_effect=ValueError("backend unavailable"),
    ), pytest.raises(RpiCalibrationPermitError, match="failed closed"):
        _validate(
            permit,
            calibration,
            target,
            trusted_signers,
            verifier=None,
        )


def test_signature_metadata_and_dedicated_keyring_are_strict():
    calibration, target, trusted_signers = _configs()
    permit = _signed_permit(calibration, target)
    permit["signature"]["signer"] = "different-operator"
    with pytest.raises(RpiCalibrationPermitError, match="authorized_by"):
        _validate(permit, calibration, target, trusted_signers)

    permit = _signed_permit(calibration, target)
    permit["signature"]["key_id"] = "untrusted-key"
    with pytest.raises(RpiCalibrationPermitError, match="not trusted"):
        _validate(permit, calibration, target, trusted_signers)

    permit = _signed_permit(calibration, target)
    permit["signature"]["signed_payload_sha256"] = "0" * 64
    with pytest.raises(RpiCalibrationPermitError, match="payload"):
        _validate(permit, calibration, target, trusted_signers)

    malformed_keyring = copy.deepcopy(trusted_signers)
    malformed_keyring[KEY_ID]["public_key_base64"] = "not-base64"
    permit = _signed_permit(calibration, target)
    with pytest.raises(RpiCalibrationPermitError, match="base64"):
        _validate(permit, calibration, target, malformed_keyring)


def _write_loader_fixture(root: Path):
    from infrastructure.config_scaling import (
        normalize_root_config_preapproval,
    )

    raw_calibration, raw_target, _ = _configs()
    calibration = normalize_root_config_preapproval(raw_calibration)
    target = normalize_root_config_preapproval(raw_target)
    config_path = root / "calibration.json"
    target_path = root / "target.json"
    permit_path = root / "permit.json"
    permit = _signed_permit(calibration, target)
    config_path.write_text(json.dumps(raw_calibration), encoding="utf-8")
    target_path.write_text(json.dumps(raw_target), encoding="utf-8")
    permit_path.write_text(json.dumps(permit), encoding="utf-8")
    return calibration, config_path, target_path, permit_path, permit


def test_loader_reads_relative_bound_files_without_network_or_trading(tmp_path):
    calibration, config_path, _, _, permit = _write_loader_fixture(tmp_path)

    with patch(
        "infrastructure.rpi_calibration_permit.verify_ed25519_signature",
        side_effect=lambda public_key, payload, signature: (
            public_key == PUBLIC_KEY
            and signature == hashlib.sha512(payload).digest()
        ),
    ):
        result = load_and_validate_rpi_calibration_permit(
            calibration,
            config_path=config_path,
            now_utc=NOW,
        )

    assert result["permit"] == permit
    assert result["permit_sha256"] == rpi_calibration_permit_sha256(permit)


def test_loader_rejects_duplicate_key_in_target_config(tmp_path):
    calibration, config_path, target_path, _, _ = _write_loader_fixture(
        tmp_path
    )
    target_path.write_text(
        '{"symbols":["XAUUSDT"],"symbols":["BTCUSDT"]}',
        encoding="utf-8",
    )

    with pytest.raises(RpiCalibrationPermitError, match="duplicate"):
        load_and_validate_rpi_calibration_permit(
            calibration,
            config_path=config_path,
            now_utc=NOW,
        )


def test_loader_rejects_parent_traversal_and_path_aliases(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    calibration, config_path, _, _, _ = _write_loader_fixture(nested)
    outside_target = tmp_path / "target.json"
    outside_target.write_text("{}", encoding="utf-8")
    calibration["live_launch"]["target_deployment_config_path"] = (
        "../target.json"
    )

    with pytest.raises(RpiCalibrationPermitError, match=r"\.\."):
        load_and_validate_rpi_calibration_permit(
            calibration,
            config_path=config_path,
            now_utc=NOW,
        )

    calibration, config_path, _, _, _ = _write_loader_fixture(nested)
    calibration["live_launch"]["target_deployment_config_path"] = (
        "calibration.json"
    )
    with pytest.raises(RpiCalibrationPermitError, match="paths must differ"):
        load_and_validate_rpi_calibration_permit(
            calibration,
            config_path=config_path,
            now_utc=NOW,
        )
