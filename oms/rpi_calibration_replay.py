"""Durable replay and validation of RPI calibration journal records."""

from __future__ import annotations

import base64
import binascii
import hashlib
from decimal import Decimal, ROUND_CEILING

from infrastructure.rpi_calibration_permit import (
    rpi_calibration_permit_sha256,
    rpi_calibration_permit_signature_payload,
)
from strategy.model_readiness import verify_ed25519_signature

from event.type import Side, TIF_RPI

from .component import OMSComponent
from .journal import JournalCorruptionError


class RpiCalibrationReplay(OMSComponent):
    """Rebuild RPI calibration quota state from the durable OMS journal.

    Its declared context permits replay to read OMS policy and calibration
    state; live permit enforcement remains with the runtime component.
    """

    OWNER_READS = frozenset(
        {
            "RPI_CALIBRATION_ACTIVATION_PAYLOAD_KEYS",
            "RPI_CALIBRATION_BYPASS_PAYLOAD_KEYS",
            "RPI_CALIBRATION_EXPIRY_PAYLOAD_KEYS",
            "RPI_CALIBRATION_JOURNAL_SCHEMA",
            "RPI_CALIBRATION_MODEL",
            "RPI_CALIBRATION_PERMIT_KEYS",
            "RPI_CALIBRATION_POLICY_KEYS",
            "RPI_CALIBRATION_RESERVATION_PAYLOAD_KEYS",
            "RPI_CALIBRATION_SIGNATURE_KEYS",
            "RPI_CALIBRATION_STAGE",
            "RPI_CALIBRATION_STRATEGY_ID",
            "RPI_CALIBRATION_VENUE",
            "USDT_MICRO_SCALE",
            "_decimal_text",
            "_parse_utc_exchange_ns",
            "_positive_decimal",
            "_require_exact_mapping_keys",
            "_require_sha256",
            "_rpi_calibration",
            "_usdt_to_microu",
        }
    )

    @staticmethod
    def _journal_int(
        payload: dict,
        field: str,
        *,
        minimum: int | None = 0,
    ) -> int:
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise JournalCorruptionError(
                f"RPI calibration journal field {field} must be an integer"
            )
        if minimum is not None and value < minimum:
            raise JournalCorruptionError(
                f"RPI calibration journal field {field} must be >= {minimum}"
            )
        return value

    
    def _journal_decimal(
        self,
        payload: dict,
        field: str,
    ) -> Decimal:
        try:
            value = self._positive_decimal(
                payload.get(field),
                f"RPI calibration journal {field}",
            )
        except ValueError as exc:
            raise JournalCorruptionError(str(exc)) from exc
        return value

    @staticmethod
    def _new_rpi_calibration_replay_state() -> dict:
        return {
            "deployments": {},
            "activations": {},
            "reservation_ids": set(),
            "reservation_exchange_ns": {},
            "expired_permits": {},
            "bypass_ids": set(),
            "send_ids": set(),
            "permit_identities": {},
            "last_bypass_exchange_ns": {},
        }

    def _verify_replayed_rpi_calibration_permit(
        self,
        signed_permit,
        expected_sha256: str,
        record_index: int,
    ) -> dict:
        try:
            permit = self._require_exact_mapping_keys(
                signed_permit,
                self.RPI_CALIBRATION_PERMIT_KEYS,
                "replayed RPI calibration signed permit",
            )
            self._require_exact_mapping_keys(
                permit.get("policy"),
                self.RPI_CALIBRATION_POLICY_KEYS,
                "replayed RPI calibration signed policy",
            )
            signature = self._require_exact_mapping_keys(
                permit.get("signature"),
                self.RPI_CALIBRATION_SIGNATURE_KEYS,
                "replayed RPI calibration signature",
            )
            calculated_sha256 = rpi_calibration_permit_sha256(permit)
            if calculated_sha256 != expected_sha256:
                raise ValueError(
                    "complete signed permit SHA-256 mismatch"
                )
            if signature.get("algorithm") != "ED25519":
                raise ValueError(
                    "replayed permit signature algorithm is not ED25519"
                )
            key_id = str(signature.get("key_id", "") or "")
            signer = str(signature.get("signer", "") or "")
            if not key_id or not signer or signer != permit.get("authorized_by"):
                raise ValueError(
                    "replayed permit signer identity is invalid"
                )
            trusted_signers = self._rpi_calibration.get(
                "trusted_signers",
                {},
            )
            trusted_signer = (
                trusted_signers.get(key_id)
                if isinstance(trusted_signers, dict)
                else None
            )
            if (
                not isinstance(trusted_signer, dict)
                or set(trusted_signer)
                != {"algorithm", "public_key_base64"}
                or trusted_signer.get("algorithm") != "ED25519"
            ):
                raise ValueError(
                    f"replayed permit signer key {key_id!r} is not trusted"
                )
            public_key_text = str(
                trusted_signer.get("public_key_base64", "") or ""
            )
            signature_text = str(
                signature.get("signature_base64", "") or ""
            )
            try:
                public_key = base64.b64decode(
                    public_key_text,
                    validate=True,
                )
                signature_bytes = base64.b64decode(
                    signature_text,
                    validate=True,
                )
            except (binascii.Error, ValueError) as exc:
                raise ValueError(
                    "replayed permit contains invalid base64"
                ) from exc
            if (
                len(public_key) != 32
                or len(signature_bytes) != 64
                or base64.b64encode(public_key).decode("ascii")
                != public_key_text
                or base64.b64encode(signature_bytes).decode("ascii")
                != signature_text
            ):
                raise ValueError(
                    "replayed permit key or signature is not canonical"
                )
            signed_payload = rpi_calibration_permit_signature_payload(
                permit
            )
            signed_payload_sha256 = self._require_sha256(
                signature.get("signed_payload_sha256"),
                "replayed permit signed-payload SHA-256",
            )
            if (
                hashlib.sha256(signed_payload).hexdigest()
                != signed_payload_sha256
            ):
                raise ValueError(
                    "replayed permit signed-payload digest mismatch"
                )
            if (
                verify_ed25519_signature(
                    public_key,
                    signed_payload,
                    signature_bytes,
                )
                is not True
            ):
                raise ValueError(
                    "replayed permit Ed25519 signature is invalid"
                )
        except Exception as exc:
            raise JournalCorruptionError(
                "Cannot independently verify signed RPI calibration permit "
                f"at journal record {record_index}: {exc}"
            ) from exc
        return permit

    def _replay_rpi_calibration_activation(
        self,
        payload: dict,
        state: dict,
        record_index: int,
    ) -> None:
        if payload.get("schema") != self.RPI_CALIBRATION_JOURNAL_SCHEMA:
            raise JournalCorruptionError(
                "Invalid RPI calibration activation schema at journal "
                f"record {record_index}"
            )
        permit_id = str(payload.get("permit_id", "") or "")
        permit_sha256 = self._require_sha256(
            payload.get("permit_sha256"),
            "RPI calibration activation permit SHA-256",
        )
        signed_permit = self._verify_replayed_rpi_calibration_permit(
            payload.get("signed_permit"),
            permit_sha256,
            record_index,
        )
        deployment_id = str(payload.get("deployment_id", "") or "")
        symbol = str(payload.get("symbol", "") or "").upper()
        if not permit_id or not deployment_id or not symbol:
            raise JournalCorruptionError(
                "Incomplete RPI calibration activation identity at journal "
                f"record {record_index}"
            )
        if permit_id in state["activations"]:
            raise JournalCorruptionError(
                "Duplicate RPI calibration permit activation at journal "
                f"record {record_index}: {permit_id}"
            )
        permit_identity = (permit_sha256, deployment_id)
        existing_permit_identity = state["permit_identities"].get(permit_id)
        if (
            existing_permit_identity is not None
            and existing_permit_identity != permit_identity
        ):
            raise JournalCorruptionError(
                "RPI calibration permit identity changed at journal "
                f"record {record_index}"
            )
        state["permit_identities"][permit_id] = permit_identity
        if (
            payload.get("stage") != self.RPI_CALIBRATION_STAGE
            or payload.get("venue") != self.RPI_CALIBRATION_VENUE
            or payload.get("model") != self.RPI_CALIBRATION_MODEL
        ):
            raise JournalCorruptionError(
                "RPI calibration activation environment mismatch at journal "
                f"record {record_index}"
            )
        signed_identity = {
            "permit_id": permit_id,
            "deployment_id": deployment_id,
            "stage": self.RPI_CALIBRATION_STAGE,
            "venue": self.RPI_CALIBRATION_VENUE,
            "symbol": symbol,
            "model": self.RPI_CALIBRATION_MODEL,
            "calibration_config_sha256": str(
                payload.get("calibration_config_sha256", "") or ""
            ),
            "target_deployment_config_sha256": str(
                payload.get(
                    "target_deployment_config_sha256",
                    "",
                )
                or ""
            ),
            "strategy_policy_sha256": str(
                payload.get("strategy_policy_sha256", "") or ""
            ),
            "implementation_sha256": str(
                payload.get("implementation_sha256", "") or ""
            ),
        }
        if any(
            signed_permit.get(field) != value
            for field, value in signed_identity.items()
        ):
            raise JournalCorruptionError(
                "RPI calibration activation derived identity differs from "
                f"its signed permit at journal record {record_index}"
            )

        calibration_config_sha256 = self._require_sha256(
            payload.get("calibration_config_sha256"),
            "RPI calibration activation config SHA-256",
        )
        target_config_sha256 = self._require_sha256(
            payload.get("target_deployment_config_sha256"),
            "RPI calibration activation target config SHA-256",
        )
        strategy_policy_sha256 = self._require_sha256(
            payload.get("strategy_policy_sha256"),
            "RPI calibration activation strategy policy SHA-256",
        )
        implementation_sha256 = self._require_sha256(
            payload.get("implementation_sha256"),
            "RPI calibration activation implementation SHA-256",
        )
        activated_at_ns = self._journal_int(
            payload,
            "activated_at_exchange_ns",
            minimum=1,
        )
        not_before_ns = self._journal_int(
            payload,
            "not_before_exchange_ns",
            minimum=1,
        )
        expires_at_ns = self._journal_int(
            payload,
            "expires_at_exchange_ns",
            minimum=1,
        )
        if not (
            not_before_ns <= activated_at_ns < expires_at_ns
        ):
            raise JournalCorruptionError(
                "RPI calibration activation is outside its permit window at "
                f"journal record {record_index}"
            )
        if (
            self._parse_utc_exchange_ns(
                signed_permit.get("not_before_utc"),
                "signed permit not_before_utc",
            )
            != not_before_ns
            or self._parse_utc_exchange_ns(
                signed_permit.get("expires_at_utc"),
                "signed permit expires_at_utc",
            )
            != expires_at_ns
        ):
            raise JournalCorruptionError(
                "RPI calibration activation timestamps differ from its "
                f"signed permit at journal record {record_index}"
            )

        raw_depths = payload.get("fixed_depths_bps")
        if not isinstance(raw_depths, list) or not raw_depths:
            raise JournalCorruptionError(
                "RPI calibration activation has no fixed depths at journal "
                f"record {record_index}"
            )
        try:
            depth_texts = tuple(
                self._decimal_text(
                    self._positive_decimal(
                        value,
                        "RPI calibration activation depth",
                    )
                )
                for value in raw_depths
            )
        except ValueError as exc:
            raise JournalCorruptionError(str(exc)) from exc
        if len(set(depth_texts)) != len(depth_texts):
            raise JournalCorruptionError(
                "RPI calibration activation contains duplicate depths at "
                f"journal record {record_index}"
            )
        parsed_depths = tuple(Decimal(value) for value in depth_texts)
        if (
            not 3 <= len(parsed_depths) <= 16
            or any(
                right <= left
                for left, right in zip(
                    parsed_depths,
                    parsed_depths[1:],
                )
            )
            or parsed_depths[-1] > Decimal("1000")
            or parsed_depths[-1] - parsed_depths[0] < Decimal("0.5")
        ):
            raise JournalCorruptionError(
                "RPI calibration activation depth schedule is unsafe at "
                f"journal record {record_index}"
            )

        limits = {
            "order_ttl_ns": self._journal_int(
                payload,
                "order_ttl_ns",
                minimum=1,
            ),
            "min_order_interval_ns": self._journal_int(
                payload,
                "min_order_interval_ns",
                minimum=1,
            ),
            "max_active_orders": self._journal_int(
                payload,
                "max_active_orders",
                minimum=1,
            ),
            "max_order_count": self._journal_int(
                payload,
                "max_order_count",
                minimum=1,
            ),
            "min_order_notional_microu": self._journal_int(
                payload,
                "min_order_notional_microu",
                minimum=1,
            ),
            "max_order_notional_microu": self._journal_int(
                payload,
                "max_order_notional_microu",
                minimum=1,
            ),
            "max_cumulative_submitted_notional_microu": (
                self._journal_int(
                    payload,
                    "max_cumulative_submitted_notional_microu",
                    minimum=1,
                )
            ),
            "max_calibration_loss_microu": self._journal_int(
                payload,
                "max_calibration_loss_microu",
                minimum=1,
            ),
        }
        if limits["max_active_orders"] != 1:
            raise JournalCorruptionError(
                "RPI calibration activation max_active_orders must equal 1"
            )
        if (
            limits["order_ttl_ns"] > 60_000_000_000
            or limits["order_ttl_ns"]
            > limits["min_order_interval_ns"]
            or not (
                5_000_000_000
                <= limits["min_order_interval_ns"]
                <= 3_600_000_000_000
            )
            or limits["max_order_count"] > 100
            or limits["min_order_notional_microu"]
            > limits["max_order_notional_microu"]
            or limits["max_order_notional_microu"] > 8_000_000
            or limits[
                "max_cumulative_submitted_notional_microu"
            ]
            < limits["min_order_notional_microu"]
            or limits[
                "max_cumulative_submitted_notional_microu"
            ]
            > (
                limits["max_order_count"]
                * limits["max_order_notional_microu"]
            )
            or limits["max_calibration_loss_microu"] > 2_000_000
        ):
            raise JournalCorruptionError(
                "RPI calibration activation limits are unsafe at journal "
                f"record {record_index}"
            )
        required_duration_ns = (
            (limits["max_order_count"] - 1)
            * limits["min_order_interval_ns"]
            + limits["order_ttl_ns"]
        )
        if required_duration_ns > expires_at_ns - not_before_ns:
            raise JournalCorruptionError(
                "RPI calibration activation schedule exceeds its window at "
                f"journal record {record_index}"
            )
        signed_policy = signed_permit["policy"]
        signed_depths = tuple(
            self._decimal_text(
                self._positive_decimal(
                    value,
                    "signed RPI calibration depth",
                )
            )
            for value in signed_policy["fixed_depths_bps"]
        )
        signed_policy_fields = {
            "fixed_depths_bps": signed_depths,
            "order_ttl_ns": int(
                (
                    self._positive_decimal(
                        signed_policy["order_ttl_sec"],
                        "signed RPI calibration order TTL",
                    )
                    * Decimal("1000000000")
                ).to_integral_value(rounding=ROUND_CEILING)
            ),
            "min_order_interval_ns": int(
                (
                    self._positive_decimal(
                        signed_policy["min_order_interval_sec"],
                        "signed RPI calibration interval",
                    )
                    * Decimal("1000000000")
                ).to_integral_value(rounding=ROUND_CEILING)
            ),
            "max_active_orders": signed_policy["max_active_orders"],
            "max_order_count": signed_policy["max_order_count"],
            "min_order_notional_microu": self._usdt_to_microu(
                self._positive_decimal(
                    signed_policy["min_order_notional_usdt"],
                    "signed minimum order notional",
                ),
                upper_bound=False,
            ),
            "max_order_notional_microu": self._usdt_to_microu(
                self._positive_decimal(
                    signed_policy["max_order_notional_usdt"],
                    "signed maximum order notional",
                ),
                upper_bound=True,
            ),
            "max_cumulative_submitted_notional_microu": (
                self._usdt_to_microu(
                    self._positive_decimal(
                        signed_policy[
                            "max_cumulative_submitted_notional_usdt"
                        ],
                        "signed cumulative notional cap",
                    ),
                    upper_bound=True,
                )
            ),
            "max_calibration_loss_microu": self._usdt_to_microu(
                self._positive_decimal(
                    signed_policy["max_calibration_loss_usdt"],
                    "signed calibration loss cap",
                ),
                upper_bound=True,
            ),
        }
        if signed_policy_fields["fixed_depths_bps"] != depth_texts or any(
            signed_policy_fields[field] != limits[field]
            for field in limits
        ):
            raise JournalCorruptionError(
                "RPI calibration activation derived policy differs from its "
                f"signed policy at journal record {record_index}"
            )

        starting_count = self._journal_int(
            payload,
            "starting_reserved_order_count",
        )
        starting_cumulative = self._journal_int(
            payload,
            "starting_cumulative_submitted_notional_microu",
        )
        deployment = state["deployments"].setdefault(
            deployment_id,
            {
                "reserved_order_count": 0,
                "cumulative_notional_microu": 0,
                "last_reserved_exchange_ns": 0,
                "identity": None,
                "start_equity_microu": 0,
                "start_external_cash_flow_microu": 0,
                "peak_observed_loss_microu": 0,
                "effective_loss_cap_microu": 0,
                "open_permit_id": "",
                "last_expired_at_ns": 0,
                "latest_permit_id": "",
                "last_signed_permit_expires_at_ns": 0,
            },
        )
        if (
            starting_count != deployment["reserved_order_count"]
            or starting_cumulative
            != deployment["cumulative_notional_microu"]
        ):
            raise JournalCorruptionError(
                "RPI calibration permit activation attempted to reset "
                f"deployment history at journal record {record_index}"
            )
        previous_permit_id = str(deployment["latest_permit_id"] or "")
        previous_permit_expires_at_ns = int(
            deployment["last_signed_permit_expires_at_ns"] or 0
        )
        if (
            previous_permit_id
            and previous_permit_id != permit_id
            and not_before_ns < previous_permit_expires_at_ns
        ):
            raise JournalCorruptionError(
                "RPI calibration permit validity window overlaps the "
                "previous signed permit at journal "
                f"record {record_index}"
            )
        if deployment["open_permit_id"]:
            raise JournalCorruptionError(
                "RPI calibration permit activation overlaps an unexpired "
                f"permit at journal record {record_index}"
            )
        if activated_at_ns < deployment["last_expired_at_ns"]:
            raise JournalCorruptionError(
                "RPI calibration permit activation predates the previous "
                "permit expiry at "
                f"journal record {record_index}"
            )
        start_equity_microu = self._journal_int(
            payload,
            "deployment_start_equity_microu",
            minimum=1,
        )
        start_external_cash_flow_microu = self._journal_int(
            payload,
            "deployment_start_external_cash_flow_microu",
            minimum=None,
        )
        peak_observed_loss_microu = self._journal_int(
            payload,
            "peak_observed_loss_microu",
        )
        effective_loss_cap_microu = self._journal_int(
            payload,
            "effective_deployment_loss_cap_microu",
            minimum=1,
        )
        expected_effective_loss_cap = min(
            (
                deployment["effective_loss_cap_microu"]
                or limits["max_calibration_loss_microu"]
            ),
            limits["max_calibration_loss_microu"],
        )
        if effective_loss_cap_microu != expected_effective_loss_cap:
            raise JournalCorruptionError(
                "RPI calibration activation widened or changed the effective "
                f"loss cap at journal record {record_index}"
            )
        if deployment["start_equity_microu"] > 0 and (
            deployment["start_equity_microu"] != start_equity_microu
            or deployment["start_external_cash_flow_microu"]
            != start_external_cash_flow_microu
        ):
            raise JournalCorruptionError(
                "RPI calibration activation reset the deployment loss "
                f"baseline at journal record {record_index}"
            )
        if (
            peak_observed_loss_microu
            < deployment["peak_observed_loss_microu"]
            or peak_observed_loss_microu >= effective_loss_cap_microu
        ):
            raise JournalCorruptionError(
                "RPI calibration activation loss evidence is non-monotonic "
                f"or already breached at journal record {record_index}"
            )
        deployment["start_equity_microu"] = start_equity_microu
        deployment["start_external_cash_flow_microu"] = (
            start_external_cash_flow_microu
        )
        deployment["peak_observed_loss_microu"] = (
            peak_observed_loss_microu
        )
        deployment["effective_loss_cap_microu"] = (
            effective_loss_cap_microu
        )
        deployment["open_permit_id"] = permit_id
        deployment["latest_permit_id"] = permit_id
        deployment["last_signed_permit_expires_at_ns"] = expires_at_ns
        deployment_identity = {
            "symbol": symbol,
            "target_deployment_config_sha256": target_config_sha256,
            "strategy_policy_sha256": strategy_policy_sha256,
            "implementation_sha256": implementation_sha256,
        }
        if (
            deployment["identity"] is not None
            and deployment["identity"] != deployment_identity
        ):
            raise JournalCorruptionError(
                "RPI calibration deployment identity changed at journal "
                f"record {record_index}"
            )
        deployment["identity"] = deployment_identity
        state["activations"][permit_id] = {
            "permit_id": permit_id,
            "permit_sha256": permit_sha256,
            "deployment_id": deployment_id,
            "symbol": symbol,
            "calibration_config_sha256": calibration_config_sha256,
            "target_deployment_config_sha256": target_config_sha256,
            "strategy_policy_sha256": strategy_policy_sha256,
            "implementation_sha256": implementation_sha256,
            "not_before_exchange_ns": not_before_ns,
            "expires_at_exchange_ns": expires_at_ns,
            "fixed_depths_bps": depth_texts,
            "starting_reserved_order_count": starting_count,
            "starting_cumulative_submitted_notional_microu": (
                starting_cumulative
            ),
            **limits,
        }

    def _replay_rpi_calibration_reservation(
        self,
        payload: dict,
        state: dict,
        record_index: int,
    ) -> None:
        if payload.get("schema") != self.RPI_CALIBRATION_JOURNAL_SCHEMA:
            raise JournalCorruptionError(
                "Invalid RPI calibration reservation schema at journal "
                f"record {record_index}"
            )
        permit_id = str(payload.get("permit_id", "") or "")
        activation = state["activations"].get(permit_id)
        if activation is None:
            raise JournalCorruptionError(
                "RPI calibration reservation precedes permit activation at "
                f"journal record {record_index}"
            )
        if permit_id in state["expired_permits"]:
            raise JournalCorruptionError(
                "RPI calibration reservation follows permit expiry at "
                f"journal record {record_index}"
            )
        reservation_id = str(payload.get("reservation_id", "") or "")
        client_oid = str(payload.get("client_oid", "") or "")
        if not reservation_id or reservation_id != client_oid:
            raise JournalCorruptionError(
                "Invalid RPI calibration reservation identity at journal "
                f"record {record_index}"
            )
        if reservation_id in state["send_ids"]:
            raise JournalCorruptionError(
                "Duplicate RPI calibration reservation at journal "
                f"record {record_index}: {reservation_id}"
            )
        identity_fields = (
            "permit_sha256",
            "deployment_id",
            "calibration_config_sha256",
            "target_deployment_config_sha256",
            "strategy_policy_sha256",
            "implementation_sha256",
        )
        if any(
            str(payload.get(field, "") or "") != activation[field]
            for field in identity_fields
        ):
            raise JournalCorruptionError(
                "RPI calibration reservation identity mismatch at journal "
                f"record {record_index}"
            )
        if (
            str(payload.get("symbol", "") or "").upper()
            != activation["symbol"]
            or payload.get("strategy_id")
            != self.RPI_CALIBRATION_STRATEGY_ID
            or payload.get("order_type") != "LIMIT"
            or payload.get("time_in_force") != TIF_RPI
            or payload.get("post_only") is not True
            or not isinstance(payload.get("reduce_only"), bool)
        ):
            raise JournalCorruptionError(
                "RPI calibration reservation order contract mismatch at "
                f"journal record {record_index}"
            )

        deployment = state["deployments"][activation["deployment_id"]]
        reservation_seq = self._journal_int(
            payload,
            "reservation_seq",
            minimum=1,
        )
        expected_seq = deployment["reserved_order_count"] + 1
        if reservation_seq != expected_seq:
            raise JournalCorruptionError(
                "Non-monotonic RPI calibration reservation sequence at "
                f"journal record {record_index}: expected {expected_seq}, "
                f"got {reservation_seq}"
            )
        permit_reservation_seq = self._journal_int(
            payload,
            "permit_reservation_seq",
            minimum=1,
        )
        expected_permit_seq = (
            reservation_seq
            - activation["starting_reserved_order_count"]
        )
        if permit_reservation_seq != expected_permit_seq:
            raise JournalCorruptionError(
                "RPI calibration per-permit reservation sequence is "
                f"inconsistent at journal record {record_index}"
            )
        reserved_at_ns = self._journal_int(
            payload,
            "reserved_at_exchange_ns",
            minimum=1,
        )
        if not (
            activation["not_before_exchange_ns"]
            <= reserved_at_ns
            < activation["expires_at_exchange_ns"]
        ):
            raise JournalCorruptionError(
                "RPI calibration reservation is outside its permit window at "
                f"journal record {record_index}"
            )
        last_reserved_ns = deployment["last_reserved_exchange_ns"]
        if reserved_at_ns < last_reserved_ns:
            raise JournalCorruptionError(
                "Non-monotonic RPI calibration reservation timestamp at "
                f"journal record {record_index}"
            )
        if (
            last_reserved_ns > 0
            and reserved_at_ns - last_reserved_ns
            < activation["min_order_interval_ns"]
        ):
            raise JournalCorruptionError(
                "RPI calibration reservation violated its minimum interval "
                f"at journal record {record_index}"
            )

        depth = self._journal_decimal(payload, "declared_depth_bps")
        depth_text = self._decimal_text(depth)
        if depth_text not in activation["fixed_depths_bps"]:
            raise JournalCorruptionError(
                "RPI calibration reservation depth is not permitted at "
                f"journal record {record_index}"
            )
        price = self._journal_decimal(payload, "price")
        quantity = self._journal_decimal(payload, "quantity")
        reference_mid = self._journal_decimal(
            payload,
            "calibration_reference_mid",
        )
        side = str(payload.get("side", "") or "")
        if (
            side not in {Side.BUY.value, Side.SELL.value}
            or (side == Side.BUY.value and price >= reference_mid)
            or (side == Side.SELL.value and price <= reference_mid)
        ):
            raise JournalCorruptionError(
                "RPI calibration reservation reference direction is invalid "
                f"at journal record {record_index}"
            )
        calculated_notional_microu = int(
            (
                price * quantity * self.USDT_MICRO_SCALE
            ).to_integral_value(rounding=ROUND_CEILING)
        )
        submitted_notional_microu = self._journal_int(
            payload,
            "submitted_notional_microu",
            minimum=1,
        )
        if calculated_notional_microu != submitted_notional_microu:
            raise JournalCorruptionError(
                "RPI calibration reservation notional mismatch at journal "
                f"record {record_index}"
            )
        if not (
            activation["min_order_notional_microu"]
            <= submitted_notional_microu
            <= activation["max_order_notional_microu"]
        ):
            raise JournalCorruptionError(
                "RPI calibration reservation notional is outside permit "
                f"bounds at journal record {record_index}"
            )
        cumulative = self._journal_int(
            payload,
            "cumulative_submitted_notional_microu",
            minimum=1,
        )
        expected_cumulative = (
            deployment["cumulative_notional_microu"]
            + submitted_notional_microu
        )
        if cumulative != expected_cumulative:
            raise JournalCorruptionError(
                "Non-monotonic RPI calibration cumulative notional at "
                f"journal record {record_index}"
            )
        permit_cumulative = self._journal_int(
            payload,
            "permit_cumulative_submitted_notional_microu",
            minimum=1,
        )
        expected_permit_cumulative = (
            cumulative
            - activation[
                "starting_cumulative_submitted_notional_microu"
            ]
        )
        if permit_cumulative != expected_permit_cumulative:
            raise JournalCorruptionError(
                "RPI calibration per-permit cumulative notional is "
                f"inconsistent at journal record {record_index}"
            )
        if (
            permit_reservation_seq > activation["max_order_count"]
            or permit_cumulative
            > activation[
                "max_cumulative_submitted_notional_microu"
            ]
        ):
            raise JournalCorruptionError(
                "RPI calibration reservation exceeded signed quota at "
                f"journal record {record_index}"
            )
        loss_before_send_microu = self._journal_int(
            payload,
            "loss_before_send_microu",
        )
        effective_loss_cap_microu = self._journal_int(
            payload,
            "effective_deployment_loss_cap_microu",
            minimum=1,
        )
        if (
            effective_loss_cap_microu
            != deployment["effective_loss_cap_microu"]
            or loss_before_send_microu >= effective_loss_cap_microu
        ):
            raise JournalCorruptionError(
                "RPI calibration reservation loss envelope is invalid at "
                f"journal record {record_index}"
            )

        deployment["reserved_order_count"] = reservation_seq
        deployment["cumulative_notional_microu"] = cumulative
        deployment["last_reserved_exchange_ns"] = reserved_at_ns
        deployment["peak_observed_loss_microu"] = max(
            deployment["peak_observed_loss_microu"],
            loss_before_send_microu,
        )
        state["reservation_ids"].add(reservation_id)
        state["reservation_exchange_ns"][reservation_id] = reserved_at_ns
        state["send_ids"].add(reservation_id)

    def _replay_rpi_calibration_expiry(
        self,
        payload: dict,
        state: dict,
        record_index: int,
    ) -> None:
        if payload.get("schema") != self.RPI_CALIBRATION_JOURNAL_SCHEMA:
            raise JournalCorruptionError(
                "Invalid RPI calibration expiry schema at journal "
                f"record {record_index}"
            )
        permit_id = str(payload.get("permit_id", "") or "")
        if not permit_id or permit_id in state["expired_permits"]:
            raise JournalCorruptionError(
                "Duplicate or missing RPI calibration expiry permit at "
                f"journal record {record_index}"
            )
        permit_sha256 = self._require_sha256(
            payload.get("permit_sha256"),
            "RPI calibration expiry permit SHA-256",
        )
        signed_permit = self._verify_replayed_rpi_calibration_permit(
            payload.get("signed_permit"),
            permit_sha256,
            record_index,
        )
        deployment_id = str(payload.get("deployment_id", "") or "")
        if not deployment_id:
            raise JournalCorruptionError(
                "RPI calibration expiry deployment is missing at journal "
                f"record {record_index}"
            )
        permit_identity = (permit_sha256, deployment_id)
        existing_permit_identity = state["permit_identities"].get(permit_id)
        if (
            existing_permit_identity is not None
            and existing_permit_identity != permit_identity
        ):
            raise JournalCorruptionError(
                "RPI calibration expiry permit identity changed at journal "
                f"record {record_index}"
            )
        state["permit_identities"][permit_id] = permit_identity
        signed_identity = {
            "permit_id": permit_id,
            "deployment_id": deployment_id,
            "symbol": str(payload.get("symbol", "") or "").upper(),
            "calibration_config_sha256": str(
                payload.get("calibration_config_sha256", "") or ""
            ),
            "target_deployment_config_sha256": str(
                payload.get(
                    "target_deployment_config_sha256",
                    "",
                )
                or ""
            ),
            "strategy_policy_sha256": str(
                payload.get("strategy_policy_sha256", "") or ""
            ),
            "implementation_sha256": str(
                payload.get("implementation_sha256", "") or ""
            ),
        }
        if any(
            signed_permit.get(field) != value
            for field, value in signed_identity.items()
        ):
            raise JournalCorruptionError(
                "RPI calibration expiry derived identity differs from its "
                f"signed permit at journal record {record_index}"
            )
        if (
            signed_permit.get("stage") != self.RPI_CALIBRATION_STAGE
            or signed_permit.get("venue") != self.RPI_CALIBRATION_VENUE
            or signed_permit.get("model") != self.RPI_CALIBRATION_MODEL
        ):
            raise JournalCorruptionError(
                "RPI calibration expiry signed environment is invalid at "
                f"journal record {record_index}"
            )
        signed_not_before_ns = self._parse_utc_exchange_ns(
            signed_permit.get("not_before_utc"),
            "expired signed permit not_before_utc",
        )
        signed_expires_at_ns = self._parse_utc_exchange_ns(
            signed_permit.get("expires_at_utc"),
            "expired signed permit expires_at_utc",
        )
        if signed_not_before_ns >= signed_expires_at_ns:
            raise JournalCorruptionError(
                "RPI calibration expiry signed time window is invalid at "
                f"journal record {record_index}"
            )
        activation = state["activations"].get(permit_id)
        if activation is not None and (
            activation["permit_sha256"] != permit_sha256
            or activation["deployment_id"] != deployment_id
            or activation["not_before_exchange_ns"]
            != signed_not_before_ns
            or activation["expires_at_exchange_ns"]
            != signed_expires_at_ns
        ):
            raise JournalCorruptionError(
                "RPI calibration expiry identity mismatch at journal "
                f"record {record_index}"
            )
        deployment = state["deployments"].setdefault(
            deployment_id,
            {
                "reserved_order_count": 0,
                "cumulative_notional_microu": 0,
                "last_reserved_exchange_ns": 0,
                "identity": None,
                "start_equity_microu": 0,
                "start_external_cash_flow_microu": 0,
                "peak_observed_loss_microu": 0,
                "effective_loss_cap_microu": 0,
                "open_permit_id": "",
                "last_expired_at_ns": 0,
                "latest_permit_id": "",
                "last_signed_permit_expires_at_ns": 0,
            },
        )
        previous_permit_id = str(deployment["latest_permit_id"] or "")
        previous_permit_expires_at_ns = int(
            deployment["last_signed_permit_expires_at_ns"] or 0
        )
        if (
            activation is None
            and previous_permit_id
            and previous_permit_id != permit_id
            and signed_not_before_ns < previous_permit_expires_at_ns
        ):
            raise JournalCorruptionError(
                "RPI calibration expired permit validity window overlaps the "
                "previous signed permit at journal "
                f"record {record_index}"
            )
        deployment_identity = {
            "symbol": signed_identity["symbol"],
            "target_deployment_config_sha256": (
                signed_identity["target_deployment_config_sha256"]
            ),
            "strategy_policy_sha256": (
                signed_identity["strategy_policy_sha256"]
            ),
            "implementation_sha256": (
                signed_identity["implementation_sha256"]
            ),
        }
        if (
            deployment["identity"] is not None
            and deployment["identity"] != deployment_identity
        ):
            raise JournalCorruptionError(
                "RPI calibration expiry deployment identity changed at "
                f"journal record {record_index}"
            )
        deployment["identity"] = deployment_identity
        recorded_count = self._journal_int(
            payload,
            "reserved_order_count",
        )
        recorded_cumulative = self._journal_int(
            payload,
            "cumulative_submitted_notional_microu",
        )
        if (
            recorded_count != deployment["reserved_order_count"]
            or recorded_cumulative
            != deployment["cumulative_notional_microu"]
        ):
            raise JournalCorruptionError(
                "RPI calibration expiry counters are inconsistent at journal "
                f"record {record_index}"
            )
        expired_at_ns = self._journal_int(
            payload,
            "expired_at_exchange_ns",
            minimum=1,
        )
        if expired_at_ns < deployment["last_reserved_exchange_ns"]:
            raise JournalCorruptionError(
                "RPI calibration expiry timestamp moved backwards at journal "
                f"record {record_index}"
            )
        if (
            deployment["open_permit_id"]
            and deployment["open_permit_id"] != permit_id
        ):
            raise JournalCorruptionError(
                "RPI calibration expiry does not match the deployment's "
                f"open permit at journal record {record_index}"
            )
        budget_exhausted = payload.get("budget_exhausted")
        if not isinstance(budget_exhausted, bool):
            raise JournalCorruptionError(
                "RPI calibration expiry budget flag is invalid at journal "
                f"record {record_index}"
            )
        start_equity_microu = self._journal_int(
            payload,
            "deployment_start_equity_microu",
        )
        start_external_cash_flow_microu = self._journal_int(
            payload,
            "deployment_start_external_cash_flow_microu",
            minimum=None,
        )
        peak_observed_loss_microu = self._journal_int(
            payload,
            "peak_observed_loss_microu",
        )
        effective_loss_cap_microu = self._journal_int(
            payload,
            "effective_deployment_loss_cap_microu",
            minimum=1,
        )
        signed_loss_cap_microu = self._usdt_to_microu(
            self._positive_decimal(
                signed_permit["policy"]["max_calibration_loss_usdt"],
                "signed calibration loss cap",
            ),
            upper_bound=True,
        )
        expected_effective_loss_cap = min(
            (
                deployment["effective_loss_cap_microu"]
                or signed_loss_cap_microu
            ),
            signed_loss_cap_microu,
        )
        if effective_loss_cap_microu != expected_effective_loss_cap:
            raise JournalCorruptionError(
                "RPI calibration expiry widened the deployment loss cap at "
                f"journal record {record_index}"
            )
        if deployment["start_equity_microu"] > 0 and (
            deployment["start_equity_microu"] != start_equity_microu
            or deployment["start_external_cash_flow_microu"]
            != start_external_cash_flow_microu
        ):
            raise JournalCorruptionError(
                "RPI calibration expiry reset the deployment loss baseline "
                f"at journal record {record_index}"
            )
        if (
            peak_observed_loss_microu
            < deployment["peak_observed_loss_microu"]
        ):
            raise JournalCorruptionError(
                "RPI calibration expiry loss evidence moved backwards at "
                f"journal record {record_index}"
            )
        if start_equity_microu > 0:
            deployment["start_equity_microu"] = start_equity_microu
            deployment["start_external_cash_flow_microu"] = (
                start_external_cash_flow_microu
            )
        deployment["peak_observed_loss_microu"] = (
            peak_observed_loss_microu
        )
        deployment["effective_loss_cap_microu"] = (
            effective_loss_cap_microu
        )
        if deployment["open_permit_id"] == permit_id:
            deployment["open_permit_id"] = ""
        deployment["last_expired_at_ns"] = max(
            deployment["last_expired_at_ns"],
            expired_at_ns,
        )
        deployment["latest_permit_id"] = permit_id
        deployment["last_signed_permit_expires_at_ns"] = max(
            deployment["last_signed_permit_expires_at_ns"],
            signed_expires_at_ns,
        )
        state["expired_permits"][permit_id] = {
            "permit_sha256": permit_sha256,
            "deployment_id": deployment_id,
            "reason": str(payload.get("reason", "") or ""),
            "budget_exhausted": budget_exhausted,
            "expired_at_exchange_ns": expired_at_ns,
        }

    def _replay_rpi_calibration_bypass(
        self,
        payload: dict,
        state: dict,
        record_index: int,
    ) -> None:
        if payload.get("schema") != self.RPI_CALIBRATION_JOURNAL_SCHEMA:
            raise JournalCorruptionError(
                "Invalid RPI calibration bypass schema at journal "
                f"record {record_index}"
            )
        bypass_id = str(payload.get("bypass_id", "") or "")
        client_oid = str(payload.get("client_oid", "") or "")
        deployment_id = str(payload.get("deployment_id", "") or "")
        if (
            not bypass_id
            or bypass_id != client_oid
            or bypass_id in state["send_ids"]
            or not deployment_id
            or payload.get("reduce_only") is not True
        ):
            raise JournalCorruptionError(
                "Invalid or duplicate RPI calibration emergency bypass at "
                f"journal record {record_index}"
            )
        recorded_at_ns = self._journal_int(
            payload,
            "recorded_at_exchange_ns",
            minimum=1,
        )
        previous_ns = state["last_bypass_exchange_ns"].get(
            deployment_id,
            0,
        )
        if recorded_at_ns <= previous_ns:
            raise JournalCorruptionError(
                "Non-monotonic RPI calibration bypass timestamp at journal "
                f"record {record_index}"
            )
        permit_sha256 = self._require_sha256(
            payload.get("permit_sha256"),
            "RPI calibration bypass permit SHA-256",
        )
        permit_id = str(payload.get("permit_id", "") or "")
        if not permit_id:
            raise JournalCorruptionError(
                "RPI calibration bypass permit is missing at journal "
                f"record {record_index}"
            )
        permit_identity = (permit_sha256, deployment_id)
        existing_permit_identity = state["permit_identities"].get(permit_id)
        if (
            existing_permit_identity is not None
            and existing_permit_identity != permit_identity
        ):
            raise JournalCorruptionError(
                "RPI calibration bypass permit identity changed at journal "
                f"record {record_index}"
            )
        state["permit_identities"][permit_id] = permit_identity
        activation = state["activations"].get(permit_id)
        if activation is not None and (
            activation["permit_sha256"] != permit_sha256
            or activation["deployment_id"] != deployment_id
        ):
            raise JournalCorruptionError(
                "RPI calibration bypass identity mismatch at journal "
                f"record {record_index}"
            )
        price = self._journal_decimal(payload, "price")
        quantity = self._journal_decimal(payload, "quantity")
        estimated_notional_microu = self._journal_int(
            payload,
            "estimated_notional_microu",
            minimum=1,
        )
        calculated_notional_microu = int(
            (
                price * quantity * self.USDT_MICRO_SCALE
            ).to_integral_value(rounding=ROUND_CEILING)
        )
        if calculated_notional_microu != estimated_notional_microu:
            raise JournalCorruptionError(
                "RPI calibration bypass notional mismatch at journal "
                f"record {record_index}"
            )
        state["bypass_ids"].add(bypass_id)
        state["send_ids"].add(bypass_id)
        state["last_bypass_exchange_ns"][deployment_id] = recorded_at_ns

    def _replay_rpi_calibration_record(
        self,
        kind: str,
        payload: dict,
        state: dict,
        record_index: int,
    ) -> bool:
        calibration_kinds = {
            "rpi_calibration_permit_activated",
            "rpi_calibration_send_reserved",
            "rpi_calibration_permit_expired",
            "rpi_calibration_emergency_reduce_bypass",
        }
        if kind not in calibration_kinds:
            return False
        if not isinstance(payload, dict):
            raise JournalCorruptionError(
                "RPI calibration journal payload must be an object at "
                f"record {record_index}"
            )
        try:
            payload_keys = {
                "rpi_calibration_permit_activated": (
                    self.RPI_CALIBRATION_ACTIVATION_PAYLOAD_KEYS
                ),
                "rpi_calibration_send_reserved": (
                    self.RPI_CALIBRATION_RESERVATION_PAYLOAD_KEYS
                ),
                "rpi_calibration_permit_expired": (
                    self.RPI_CALIBRATION_EXPIRY_PAYLOAD_KEYS
                ),
                "rpi_calibration_emergency_reduce_bypass": (
                    self.RPI_CALIBRATION_BYPASS_PAYLOAD_KEYS
                ),
            }
            self._require_exact_mapping_keys(
                payload,
                payload_keys[kind],
                f"{kind} journal payload",
            )
            if kind == "rpi_calibration_permit_activated":
                self._replay_rpi_calibration_activation(
                    payload,
                    state,
                    record_index,
                )
            elif kind == "rpi_calibration_send_reserved":
                self._replay_rpi_calibration_reservation(
                    payload,
                    state,
                    record_index,
                )
            elif kind == "rpi_calibration_permit_expired":
                self._replay_rpi_calibration_expiry(
                    payload,
                    state,
                    record_index,
                )
            elif kind == "rpi_calibration_emergency_reduce_bypass":
                self._replay_rpi_calibration_bypass(
                    payload,
                    state,
                    record_index,
                )
        except ValueError as exc:
            raise JournalCorruptionError(
                "Malformed RPI calibration record at journal "
                f"record {record_index}: {exc}"
            ) from exc
        return True

    def _finalize_rpi_calibration_replay(
        self,
        state: dict,
        *,
        dirty_shutdown: bool = False,
    ) -> dict:
        config = self._rpi_calibration
        if not config["enabled"]:
            return {
                "permit_activated": False,
                "expired": False,
                "expiry_reason": "",
                "budget_exhausted": False,
                "reserved_order_count": 0,
                "cumulative_submitted_notional_microu": 0,
                "last_reserved_exchange_ns": 0,
                "reservation_ids": [],
                "reservation_exchange_ns": {},
                "permit_start_order_count": 0,
                "permit_start_notional_microu": 0,
                "deployment_start_equity_microu": 0,
                "deployment_start_external_cash_flow_microu": 0,
                "peak_observed_loss_microu": 0,
                "effective_loss_cap_microu": 0,
                "restart_rearm_blocked": False,
            }
        deployment = state["deployments"].get(
            config["deployment_id"],
            {
                "reserved_order_count": 0,
                "cumulative_notional_microu": 0,
                "last_reserved_exchange_ns": 0,
                "identity": None,
                "start_equity_microu": 0,
                "start_external_cash_flow_microu": 0,
                "peak_observed_loss_microu": 0,
                "effective_loss_cap_microu": 0,
                "open_permit_id": "",
                "last_expired_at_ns": 0,
                "latest_permit_id": "",
                "last_signed_permit_expires_at_ns": 0,
            },
        )
        expected_deployment_identity = {
            "symbol": config["symbol"],
            "target_deployment_config_sha256": (
                config["target_deployment_config_sha256"]
            ),
            "strategy_policy_sha256": config["strategy_policy_sha256"],
            "implementation_sha256": config["implementation_sha256"],
        }
        if (
            deployment["identity"] is not None
            and deployment["identity"] != expected_deployment_identity
        ):
            raise JournalCorruptionError(
                "Configured RPI calibration permit does not match the "
                "persisted deployment identity"
            )

        activation = state["activations"].get(config["permit_id"])
        latest_permit_id = str(deployment["latest_permit_id"] or "")
        if (
            activation is None
            and latest_permit_id
            and latest_permit_id != config["permit_id"]
            and config["not_before_ns"]
            < deployment["last_signed_permit_expires_at_ns"]
        ):
            raise JournalCorruptionError(
                "Configured RPI calibration permit validity window overlaps "
                "the previous signed permit"
            )
        persisted_permit_identity = state["permit_identities"].get(
            config["permit_id"]
        )
        if (
            persisted_permit_identity is not None
            and persisted_permit_identity
            != (
                config["permit_sha256"],
                config["deployment_id"],
            )
        ):
            raise JournalCorruptionError(
                "Configured RPI calibration permit identity differs from "
                "persisted audit records"
            )
        if activation is not None:
            expected_activation = {
                "permit_sha256": config["permit_sha256"],
                "deployment_id": config["deployment_id"],
                "symbol": config["symbol"],
                "calibration_config_sha256": (
                    config["calibration_config_sha256"]
                ),
                "target_deployment_config_sha256": (
                    config["target_deployment_config_sha256"]
                ),
                "strategy_policy_sha256": (
                    config["strategy_policy_sha256"]
                ),
                "implementation_sha256": (
                    config["implementation_sha256"]
                ),
                "not_before_exchange_ns": config["not_before_ns"],
                "expires_at_exchange_ns": config["expires_at_ns"],
                "fixed_depths_bps": config["fixed_depths_bps"],
                "order_ttl_ns": config["order_ttl_ns"],
                "min_order_interval_ns": config["min_order_interval_ns"],
                "max_active_orders": config["max_active_orders"],
                "max_order_count": config["max_order_count"],
                "min_order_notional_microu": (
                    config["min_order_notional_microu"]
                ),
                "max_order_notional_microu": (
                    config["max_order_notional_microu"]
                ),
                "max_cumulative_submitted_notional_microu": (
                    config["max_cumulative_notional_microu"]
                ),
                "max_calibration_loss_microu": (
                    config["max_calibration_loss_microu"]
                ),
            }
            if any(
                activation.get(field) != value
                for field, value in expected_activation.items()
            ):
                raise JournalCorruptionError(
                    "Configured RPI calibration permit differs from its "
                    "persisted activation"
                )

        expiry = state["expired_permits"].get(config["permit_id"], {})
        open_permit_id = str(deployment["open_permit_id"] or "")
        if (
            open_permit_id
            and open_permit_id != config["permit_id"]
        ):
            raise JournalCorruptionError(
                "A renewed RPI calibration permit cannot activate before "
                "the previous permit has a durable expiry record"
            )
        if activation is not None and not expiry and (
            open_permit_id != config["permit_id"]
        ):
            raise JournalCorruptionError(
                "Persisted RPI calibration activation is not the deployment's "
                "open permit"
            )
        permit_start_order_count = (
            activation["starting_reserved_order_count"]
            if activation is not None
            else deployment["reserved_order_count"]
        )
        permit_start_notional_microu = (
            activation[
                "starting_cumulative_submitted_notional_microu"
            ]
            if activation is not None
            else deployment["cumulative_notional_microu"]
        )
        restart_rearm_blocked = bool(
            dirty_shutdown
            and activation is not None
            and not expiry
        )
        return {
            "permit_activated": activation is not None,
            "expired": bool(expiry),
            "expiry_reason": str(expiry.get("reason", "") or ""),
            "budget_exhausted": bool(
                expiry.get("budget_exhausted", False)
            ),
            "reserved_order_count": deployment["reserved_order_count"],
            "cumulative_submitted_notional_microu": (
                deployment["cumulative_notional_microu"]
            ),
            "last_reserved_exchange_ns": (
                deployment["last_reserved_exchange_ns"]
            ),
            "reservation_ids": sorted(state["reservation_ids"]),
            "reservation_exchange_ns": dict(
                state["reservation_exchange_ns"]
            ),
            "permit_start_order_count": permit_start_order_count,
            "permit_start_notional_microu": (
                permit_start_notional_microu
            ),
            "deployment_start_equity_microu": (
                deployment["start_equity_microu"]
            ),
            "deployment_start_external_cash_flow_microu": (
                deployment["start_external_cash_flow_microu"]
            ),
            "peak_observed_loss_microu": (
                deployment["peak_observed_loss_microu"]
            ),
            "effective_loss_cap_microu": (
                deployment["effective_loss_cap_microu"]
                or config["max_calibration_loss_microu"]
            ),
            "restart_rearm_blocked": restart_rearm_blocked,
        }
