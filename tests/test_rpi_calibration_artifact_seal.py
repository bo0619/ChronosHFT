import hashlib
import json
from pathlib import Path

import pytest

from infrastructure.single_writer_fence import (
    SingleWriterFence,
    SingleWriterFenceError,
)
from scripts import build_rpi_calibration_artifact as artifact_builder
from scripts.build_rpi_calibration_artifact import (
    CalibrationArtifactError,
    _authorized_journal_fence,
    _CalibrationJournalReplay,
)
from strategy import model_readiness


ROOT = Path(__file__).resolve().parents[1]


def _replay_seal_state(
    *,
    open_permit_id: str = "",
    expired: bool = True,
) -> _CalibrationJournalReplay:
    replay = object.__new__(_CalibrationJournalReplay)
    replay.activations = {"permit-1": object()}
    replay.reservations = {"order-1": object()}
    replay.open_permit_id = open_permit_id
    replay.expired_permits = {"permit-1"} if expired else set()
    return replay


def _calibration_config(journal: Path) -> dict:
    return {
        "oms": {
            "journal_path": journal.name,
            "single_writer_fence": {
                "enabled": True,
                "path": f"{journal.name}.lock",
            },
        }
    }


def test_authorized_replay_requires_every_permit_to_be_expired():
    replay = _replay_seal_state(
        open_permit_id="permit-1",
        expired=False,
    )

    with pytest.raises(CalibrationArtifactError, match="no durable expiry"):
        replay.finish(
            last_record_kind="oms_stopped",
            last_record_payload={"cancel_verified": True},
        )


def test_authorized_replay_requires_final_clean_oms_stop():
    replay = _replay_seal_state()

    with pytest.raises(CalibrationArtifactError, match="final durable record"):
        replay.finish(
            last_record_kind="lifecycle",
            last_record_payload={},
        )

    with pytest.raises(CalibrationArtifactError, match="cancel_verified=true"):
        replay.finish(
            last_record_kind="oms_stopped",
            last_record_payload={"cancel_verified": False},
        )


def test_authorized_replay_accepts_only_closed_clean_journal():
    replay = _replay_seal_state()

    replay.finish(
        last_record_kind="oms_stopped",
        last_record_payload={"cancel_verified": True},
    )


def test_authorized_journal_validation_requires_existing_writer_fence(
    tmp_path,
):
    journal = tmp_path / "oms.jsonl"
    journal.write_bytes(b"")
    config_path = tmp_path / "calibration.json"

    with pytest.raises(CalibrationArtifactError, match="fence file is missing"):
        with _authorized_journal_fence(
            journal.resolve(),
            calibration_config=_calibration_config(journal),
            calibration_config_path=config_path,
        ):
            pass


def test_authorized_journal_validation_rejects_active_writer(tmp_path):
    journal = tmp_path / "oms.jsonl"
    journal.write_bytes(b"")
    lock_path = Path(f"{journal}.lock")
    writer_fence = SingleWriterFence(str(lock_path))
    writer_fence.acquire()
    try:
        with pytest.raises(
            CalibrationArtifactError,
            match="still owned by an OMS writer",
        ):
            with _authorized_journal_fence(
                journal.resolve(),
                calibration_config=_calibration_config(journal),
                calibration_config_path=tmp_path / "calibration.json",
            ):
                pass
    finally:
        writer_fence.release()


def test_artifact_publish_keeps_writer_fence_held(tmp_path, monkeypatch):
    journal = tmp_path / "oms.jsonl"
    journal.write_bytes(b"")
    lock_path = Path(f"{journal}.lock")
    seed_fence = SingleWriterFence(str(lock_path))
    seed_fence.acquire()
    seed_fence.release()

    def build_while_locked(*args, **kwargs):
        contender = SingleWriterFence(str(lock_path))
        with pytest.raises(SingleWriterFenceError, match="already held"):
            contender.acquire()
        return {"sealed": True}

    monkeypatch.setattr(
        artifact_builder,
        "_build_rpi_calibration_artifact_with_fence_held",
        build_while_locked,
    )

    result = artifact_builder.build_rpi_calibration_artifact(
        journal,
        tmp_path / "artifact.json",
        symbol="XAUUSDT",
        deployment_config_sha256="1" * 64,
        calibration_config=_calibration_config(journal),
        calibration_config_path=tmp_path / "calibration.json",
    )

    assert result == {"sealed": True}


def test_approval_graph_keeps_writer_fence_held(tmp_path, monkeypatch):
    journal = tmp_path / "oms.jsonl"
    journal.write_bytes(b"")
    lock_path = Path(f"{journal}.lock")
    seed_fence = SingleWriterFence(str(lock_path))
    seed_fence.acquire()
    seed_fence.release()

    calibration_path = tmp_path / "calibration.json"
    calibration = json.loads(
        (ROOT / "config.live.rpi-calibration.example.json").read_text(
            encoding="utf-8"
        )
    )
    calibration["oms"]["journal_path"] = journal.name
    calibration["oms"]["single_writer_fence"]["path"] = lock_path.name
    calibration_path.write_text(
        json.dumps(calibration),
        encoding="utf-8",
    )
    source_path = tmp_path / "source.json"
    source_path.write_text(
        json.dumps(
            {
                "calibration_config_path": calibration_path.name,
                "journal": {"path": journal.name},
            }
        ),
        encoding="utf-8",
    )
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(
        json.dumps({"source_data_path": source_path.name}),
        encoding="utf-8",
    )
    config = {
        "strategy": {
            "primary_model": "glft",
            "model_readiness": {
                "live_approval": {
                    "manifest_path": approval_path.name,
                }
            },
        }
    }

    def validate_while_locked(*args, **kwargs):
        assert kwargs["expected_locked_journal_path"] == journal.resolve()
        contender = SingleWriterFence(str(lock_path))
        with pytest.raises(SingleWriterFenceError, match="already held"):
            contender.acquire()
        return {"validated": True}

    monkeypatch.setattr(
        model_readiness,
        "_validate_live_calibration_approval_with_fence_held",
        validate_while_locked,
    )

    result = model_readiness.validate_live_calibration_approval(
        config,
        config_path=tmp_path / "live.json",
    )

    assert result == {"validated": True}


def test_approval_json_hash_and_parse_share_one_byte_buffer(
    tmp_path,
    monkeypatch,
):
    raw = b'{"version":"approved-a"}'
    reads = []

    def read_once(path, label):
        reads.append((path, label))
        if len(reads) > 1:
            return b'{"version":"replaced-b"}'
        return raw

    monkeypatch.setattr(model_readiness, "_read_json_bytes", read_once)

    document = model_readiness._read_hashed_json_object(
        tmp_path / "artifact.json",
        "artifact",
        expected_sha256=hashlib.sha256(raw).hexdigest(),
        mismatch_message="hash mismatch",
    )

    assert document == {"version": "approved-a"}
    assert len(reads) == 1


@pytest.mark.parametrize(
    "raw",
    [
        b'{"duplicate":1,"duplicate":2}',
        b'{"non_finite":NaN}',
    ],
)
def test_approval_json_parser_rejects_ambiguous_bytes(tmp_path, raw):
    with pytest.raises(ValueError, match="Cannot read approval evidence"):
        model_readiness._read_hashed_json_object(
            tmp_path / "evidence.json",
            "approval evidence",
            expected_sha256=hashlib.sha256(raw).hexdigest(),
            mismatch_message="hash mismatch",
        )
