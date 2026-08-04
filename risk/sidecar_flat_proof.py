"""Account-wide flatness proof generation for safety transitions."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import time

from risk.exchange_port import (
    AccountTruthSnapshot,
    FlatProof,
    SnapshotPurpose,
    StateVersion,
    TruthResult,
)


class FlatProofError(RuntimeError):
    """Raised when fresh account-wide flatness cannot be proved."""


class FlatProofEngine:
    """Require multiple stable, strictly newer full-account zero snapshots."""

    def __init__(
        self,
        exchange,
        *,
        required_samples: int = 2,
        settle_interval_sec: float = 0.0,
        proof_ttl_sec: float = 2.0,
        monotonic=time.perf_counter,
        sleep=time.sleep,
        proof_id_factory=lambda: secrets.token_hex(16),
    ) -> None:
        self.exchange = exchange
        self.required_samples = max(2, int(required_samples))
        self.settle_interval_sec = max(0.0, float(settle_interval_sec))
        self.proof_ttl_sec = max(0.05, float(proof_ttl_sec))
        self._monotonic = monotonic
        self._sleep = sleep
        self._proof_id_factory = proof_id_factory

    @staticmethod
    def _nonzero_position_count(snapshot: AccountTruthSnapshot) -> int:
        count = 0
        for position in snapshot.positions:
            try:
                amount = float(position.get("positionAmt", 0.0) or 0.0)
            except (AttributeError, TypeError, ValueError) as exc:
                raise FlatProofError("flat_proof_position_invalid") from exc
            if not math.isfinite(amount):
                raise FlatProofError("flat_proof_position_non_finite")
            if abs(amount) > 1e-9:
                count += 1
        return count

    def _read(self) -> AccountTruthSnapshot:
        read = getattr(self.exchange, "read_account_truth", None)
        if not callable(read):
            raise FlatProofError("flat_proof_port_unavailable")
        result = read(SnapshotPurpose.FLAT_PROOF)
        if not isinstance(result, TruthResult):
            raise FlatProofError("flat_proof_result_invalid")
        if not result.ok or result.snapshot is None:
            raise FlatProofError(
                str(result.reason or "flat_proof_exchange_failed")
            )
        snapshot = result.snapshot
        if not snapshot.complete:
            raise FlatProofError("flat_proof_snapshot_incomplete")
        if not snapshot.account_wide:
            raise FlatProofError("flat_proof_scope_not_account_wide")
        return snapshot

    def capture(
        self,
        *,
        purpose: str,
        deployment_id: str,
        version: StateVersion,
        barrier_monotonic: float,
    ) -> FlatProof:
        snapshots = []
        previous_sequence = 0
        for index in range(self.required_samples):
            snapshot = self._read()
            if snapshot.captured_monotonic < float(barrier_monotonic):
                raise FlatProofError("flat_proof_predates_barrier")
            if snapshot.truth_sequence <= previous_sequence:
                raise FlatProofError("flat_proof_sequence_not_increasing")
            if snapshot.open_orders:
                raise FlatProofError("flat_proof_open_orders_remain")
            if self._nonzero_position_count(snapshot):
                raise FlatProofError("flat_proof_positions_remain")
            previous_sequence = snapshot.truth_sequence
            snapshots.append(snapshot)
            if index + 1 < self.required_samples and self.settle_interval_sec:
                self._sleep(self.settle_interval_sec)

        now = float(self._monotonic())
        digest_payload = {
            "sequences": [item.truth_sequence for item in snapshots],
            "digests": [item.consistency_digest for item in snapshots],
            "version": {
                "writer_epoch": version.writer_epoch,
                "owner_epoch": version.owner_epoch,
                "safety_epoch": version.safety_epoch,
            },
        }
        digest = hashlib.sha256(
            json.dumps(
                digest_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return FlatProof(
            proof_id=str(self._proof_id_factory()),
            purpose=str(purpose or "FLAT_PROOF"),
            account_scope_id=snapshots[-1].account_scope_id,
            deployment_id=str(deployment_id or ""),
            writer_epoch=version.writer_epoch,
            owner_epoch=version.owner_epoch,
            safety_epoch=version.safety_epoch,
            first_truth_sequence=snapshots[0].truth_sequence,
            last_truth_sequence=snapshots[-1].truth_sequence,
            sample_count=len(snapshots),
            open_order_count=0,
            nonzero_position_count=0,
            snapshot_digest=digest,
            verified_monotonic=now,
            valid_until_monotonic=now + self.proof_ttl_sec,
        )
