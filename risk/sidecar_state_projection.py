"""Durable sidecar state projection and store lifecycle."""

from __future__ import annotations

import os
from dataclasses import asdict

from risk.exchange_port import FlatProof
from risk.sidecar_state_store import (
    SidecarStateStore,
    SidecarStateStoreError,
)
from risk.sidecar_state_payload import (
    SidecarStatePayloadError,
    parse_sidecar_state_payload,
)


def open_state_store(owner, root: str, settings: dict, finite_float) -> None:
    account_scope_id = str(
        settings.get("account_scope_id", "") or ""
    ).strip()
    genesis_id = str(
        settings.get("state_genesis_id", "") or ""
    ).strip()
    deployment_id = owner.observation.account_risk.state.deployment_id
    if not account_scope_id or not genesis_id or not deployment_id:
        raise ValueError("state_store_identity_missing")
    store = SidecarStateStore(
        root,
        account_scope_id=account_scope_id,
        deployment_id=deployment_id,
        genesis_id=genesis_id,
        writer_id=str(
            settings.get("session_id", "") or f"pid-{os.getpid()}"
        ),
    )
    payload, version = store.open_recover()
    try:
        apply_state_store_payload(owner, payload, finite_float)
    except Exception:
        store.close()
        raise
    owner.state_store = store
    owner.state_path = str(store.root)
    owner.state_version = version
    owner.state_generation = version.generation
    owner.state_recovered = True
    owner.state_load_error = ""
    proof = owner.last_flat_proof
    if proof is not None and not proof.is_valid(
        owner.clock.monotonic(),
        version,
    ):
        owner.last_flat_proof = None
        owner.last_flat_proof_error = (
            "flat_proof_invalidated_by_writer_epoch"
        )
        try:
            version = store.compare_and_swap(
                version,
                build_state_store_payload(owner),
                event="flat_proof_invalidated_on_recovery",
                operation_class="SAFETY_INCREASING",
            )
        except Exception:
            close_state_store(owner)
            raise
        owner.state_version = version
        owner.state_generation = version.generation


def apply_state_store_payload(owner, payload: dict, finite_float) -> None:
    try:
        payload = parse_sidecar_state_payload(
            payload,
            account_scope_id=str(payload.get("account_scope_id", "") or ""),
            deployment_id=(
                owner.observation.account_risk.state.deployment_id
            ),
        )
    except SidecarStatePayloadError as exc:
        raise SidecarStateStoreError(str(exc)) from exc
    kill_latched = payload["kill_latched"]
    quiesced = payload["quiesced"]
    control = owner.control.state
    equity = owner.observation.account_risk.state
    control.kill_latched = kill_latched
    control.kill_reason = str(payload.get("kill_reason", "") or "")
    control.quiesced = quiesced
    control.quiesce_reason = str(payload.get("quiesce_reason", "") or "")
    control.quiesced_at = finite_float(
        payload.get("quiesced_at", 0.0) or 0.0,
        "state.quiesced_at",
    )
    control.stage = (
        "QUIESCED"
        if quiesced
        else str(
            payload.get(
                "stage",
                "FLATTENING" if kill_latched else "ARMED",
            )
            or "FAILED"
        )
    )
    for field in (
        "day_start_equity",
        "day_start_external_cash_flow_total",
        "peak_adjusted_equity",
        "last_equity",
        "deployment_start_equity",
        "deployment_start_external_cash_flow_total",
        "deployment_adjusted_equity",
        "deployment_loss",
    ):
        if field in payload:
            setattr(
                equity,
                field,
                finite_float(payload[field], f"state.{field}"),
            )
    if "risk_day" in payload:
        equity.risk_day = str(payload["risk_day"] or "")
    proof_payload = payload.get("last_flat_proof")
    if isinstance(proof_payload, dict):
        owner.last_flat_proof = FlatProof(**proof_payload)


def build_state_store_payload(owner) -> dict:
    store = owner.state_store
    control = owner.control.state
    equity = owner.observation.account_risk.state
    payload = dict(store.payload) if store is not None else {}
    payload.update({
        "schema_version": 2,
        "account_scope_id": (
            store.account_scope_id if store is not None else ""
        ),
        "deployment_id": equity.deployment_id,
        "kill_latched": bool(control.kill_latched),
        "kill_reason": str(control.kill_reason or ""),
        "stage": str(control.stage or ""),
        "quiesced": bool(control.quiesced),
        "quiesce_reason": str(control.quiesce_reason or ""),
        "quiesced_at": float(control.quiesced_at),
        "risk_day": str(equity.risk_day or ""),
        "day_start_equity": float(equity.day_start_equity),
        "day_start_external_cash_flow_total": float(
            equity.day_start_external_cash_flow_total
        ),
        "peak_adjusted_equity": float(equity.peak_adjusted_equity),
        "last_equity": float(equity.last_equity),
        "deployment_start_equity": float(equity.deployment_start_equity),
        "deployment_start_external_cash_flow_total": float(
            equity.deployment_start_external_cash_flow_total
        ),
        "deployment_adjusted_equity": float(
            equity.deployment_adjusted_equity
        ),
        "deployment_loss": float(equity.deployment_loss),
        "last_flat_proof": (
            asdict(owner.last_flat_proof)
            if owner.last_flat_proof is not None
            else None
        ),
    })
    return payload


def persist_state(owner, event: str, force: bool, finite_float) -> bool:
    del force, finite_float
    if owner.state_store is not None:
        operation_class = (
            "RISK_INCREASING"
            if "rearm" in str(event or "").lower()
            else "SAFETY_INCREASING"
            if any(
                marker in str(event or "").lower()
                for marker in ("kill", "flat", "cancel", "failed")
            )
            else "NEUTRAL"
        )
        try:
            version = owner.state_store.compare_and_swap(
                owner.state_version,
                build_state_store_payload(owner),
                event=event,
                operation_class=operation_class,
                safety_epoch=max(
                    owner.state_version.safety_epoch,
                    owner._pending_safety_epoch,
                ),
            )
        except SidecarStateStoreError as exc:
            owner.state_persist_error = (
                f"state_store_persist_failed:{type(exc).__name__}:{exc}"
            )
            owner._fail_closed_on_state_error(owner.state_persist_error)
            return False
        owner.state_version = version
        owner._pending_safety_epoch = version.safety_epoch
        owner.state_generation = version.generation
        owner.state_persist_error = ""
        return True
    return True


def close_state_store(owner) -> None:
    if owner.state_store is not None:
        owner.state_store.close()
        owner.state_store = None
