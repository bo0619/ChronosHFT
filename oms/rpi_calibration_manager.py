"""RPI calibration permit validation and runtime-config construction."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR

from infrastructure.rpi_calibration_permit import (
    rpi_calibration_permit_signature_payload,
)
from strategy.model_readiness import verify_ed25519_signature


class RpiCalibrationManager:
    """Own immutable RPI permit verification and normalized configuration."""

    RPI_CALIBRATION_STAGE = "rpi_calibration_canary"
    RPI_CALIBRATION_VENUE = "BINANCE_USDM"
    RPI_CALIBRATION_MODEL = "glft"
    RPI_CALIBRATION_PERMIT_KEYS = frozenset(
        {
            "schema",
            "permit_id",
            "deployment_id",
            "stage",
            "venue",
            "symbol",
            "model",
            "issued_at_utc",
            "not_before_utc",
            "expires_at_utc",
            "calibration_config_sha256",
            "target_deployment_config_sha256",
            "strategy_policy_sha256",
            "implementation_sha256",
            "policy",
            "authorized_by",
            "signature",
        }
    )
    RPI_CALIBRATION_POLICY_KEYS = frozenset(
        {
            "fixed_depths_bps",
            "order_ttl_sec",
            "min_order_interval_sec",
            "max_active_orders",
            "max_order_count",
            "min_order_notional_usdt",
            "max_order_notional_usdt",
            "max_cumulative_submitted_notional_usdt",
            "max_calibration_loss_usdt",
        }
    )
    RPI_CALIBRATION_SIGNATURE_KEYS = frozenset(
        {
            "algorithm",
            "key_id",
            "signer",
            "signed_payload_sha256",
            "signature_base64",
        }
    )
    USDT_MICRO_SCALE = Decimal("1000000")

    def __init__(self, config: dict):
        self.runtime_config = self._load_rpi_calibration_config(config)

    @staticmethod
    def _canonical_json(value: dict) -> str:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _require_exact_mapping_keys(
        value,
        expected_keys,
        context: str,
    ) -> dict:
        if not isinstance(value, dict):
            raise ValueError(f"{context} must be an object")
        actual_keys = set(value)
        expected_keys = set(expected_keys)
        if actual_keys != expected_keys:
            missing = sorted(expected_keys - actual_keys)
            extra = sorted(actual_keys - expected_keys)
            raise ValueError(
                f"{context} keys are invalid: missing={missing}, extra={extra}"
            )
        return value

    @staticmethod
    def _require_sha256(value, context: str) -> str:
        digest = str(value or "").lower()
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError(f"{context} must be a lowercase SHA-256 hex digest")
        if str(value or "") != digest:
            raise ValueError(f"{context} must use lowercase hexadecimal")
        return digest

    @staticmethod
    def _finite_decimal(value, context: str) -> Decimal:
        if isinstance(value, bool):
            raise ValueError(f"{context} must be numeric")
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"{context} must be numeric") from exc
        if not parsed.is_finite():
            raise ValueError(f"{context} must be finite")
        return parsed

    @classmethod
    def _positive_decimal(cls, value, context: str) -> Decimal:
        parsed = cls._finite_decimal(value, context)
        if parsed <= 0:
            raise ValueError(f"{context} must be finite and positive")
        return parsed

    @staticmethod
    def _decimal_text(value: Decimal) -> str:
        return format(value.normalize(), "f")

    @classmethod
    def _usdt_to_microu(
        cls,
        value: Decimal,
        *,
        upper_bound: bool,
    ) -> int:
        rounding = ROUND_FLOOR if upper_bound else ROUND_CEILING
        return int(
            (value * cls.USDT_MICRO_SCALE).to_integral_value(
                rounding=rounding
            )
        )

    @staticmethod
    def _parse_utc_exchange_ns(value, context: str) -> int:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{context} is required")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{context} must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"{context} must include a UTC offset")
        parsed = parsed.astimezone(timezone.utc)
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        delta = parsed - epoch
        return (
            (delta.days * 86_400 + delta.seconds) * 1_000_000_000
            + delta.microseconds * 1_000
        )

    @classmethod
    def _verify_rpi_calibration_permit_signature(
        cls,
        permit: dict,
        trusted_signers: dict,
        *,
        context: str,
    ) -> None:
        signature = cls._require_exact_mapping_keys(
            permit.get("signature"),
            cls.RPI_CALIBRATION_SIGNATURE_KEYS,
            f"{context} signature",
        )
        if not isinstance(trusted_signers, dict) or not trusted_signers:
            raise ValueError(f"{context} trusted signer keyring is required")
        if signature.get("algorithm") != "ED25519":
            raise ValueError(f"{context} signature algorithm must be ED25519")

        key_id = signature.get("key_id")
        signer = signature.get("signer")
        authorized_by = permit.get("authorized_by")
        if (
            not isinstance(key_id, str)
            or not key_id
            or key_id != key_id.strip()
            or not isinstance(signer, str)
            or not signer
            or signer != signer.strip()
            or signer != authorized_by
        ):
            raise ValueError(f"{context} signer identity is invalid")
        trusted_signer = trusted_signers.get(key_id)
        trusted_signer = cls._require_exact_mapping_keys(
            trusted_signer,
            {"algorithm", "public_key_base64"},
            f"{context} trusted signer {key_id!r}",
        )
        if trusted_signer.get("algorithm") != "ED25519":
            raise ValueError(
                f"{context} trusted signer {key_id!r} must use ED25519"
            )

        public_key_text = trusted_signer.get("public_key_base64")
        signature_text = signature.get("signature_base64")
        if not isinstance(public_key_text, str) or not isinstance(
            signature_text,
            str,
        ):
            raise ValueError(f"{context} key and signature must be base64 text")
        try:
            public_key = base64.b64decode(public_key_text, validate=True)
            signature_bytes = base64.b64decode(
                signature_text,
                validate=True,
            )
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"{context} contains invalid base64") from exc
        if (
            len(public_key) != 32
            or len(signature_bytes) != 64
            or base64.b64encode(public_key).decode("ascii") != public_key_text
            or base64.b64encode(signature_bytes).decode("ascii")
            != signature_text
        ):
            raise ValueError(f"{context} key or signature is not canonical")

        signed_payload = rpi_calibration_permit_signature_payload(permit)
        signed_payload_sha256 = cls._require_sha256(
            signature.get("signed_payload_sha256"),
            f"{context} signed-payload SHA-256",
        )
        if (
            hashlib.sha256(signed_payload).hexdigest()
            != signed_payload_sha256
        ):
            raise ValueError(f"{context} signed-payload SHA-256 mismatch")
        try:
            verified = verify_ed25519_signature(
                public_key,
                signed_payload,
                signature_bytes,
            )
        except Exception as exc:
            raise ValueError(
                f"{context} Ed25519 verifier failed closed"
            ) from exc
        if verified is not True:
            raise ValueError(f"{context} Ed25519 signature is invalid")

    @classmethod
    def _load_rpi_calibration_config(cls, config: dict) -> dict:
        live_launch = config.get("live_launch", {}) or {}
        stage = str(live_launch.get("stage", "") or "").strip()
        wrapper = config.get("_validated_rpi_calibration_permit")
        if stage != cls.RPI_CALIBRATION_STAGE:
            if wrapper is not None:
                raise ValueError(
                    "Validated RPI calibration permit is only valid in "
                    f"{cls.RPI_CALIBRATION_STAGE!r}"
                )
            return {"enabled": False}

        wrapper = cls._require_exact_mapping_keys(
            wrapper,
            {
                "permit",
                "permit_sha256",
                "calibration_config_sha256",
                "target_deployment_config_sha256",
            },
            "_validated_rpi_calibration_permit",
        )
        permit = cls._require_exact_mapping_keys(
            wrapper["permit"],
            cls.RPI_CALIBRATION_PERMIT_KEYS,
            "RPI calibration permit",
        )
        policy = cls._require_exact_mapping_keys(
            permit["policy"],
            cls.RPI_CALIBRATION_POLICY_KEYS,
            "RPI calibration permit policy",
        )
        cls._require_exact_mapping_keys(
            permit["signature"],
            cls.RPI_CALIBRATION_SIGNATURE_KEYS,
            "RPI calibration permit signature",
        )

        permit_sha256 = cls._require_sha256(
            wrapper["permit_sha256"],
            "validated RPI calibration permit SHA-256",
        )
        calculated_permit_sha256 = hashlib.sha256(
            cls._canonical_json(permit).encode("utf-8")
        ).hexdigest()
        if calculated_permit_sha256 != permit_sha256:
            raise ValueError(
                "Validated RPI calibration permit SHA-256 does not match "
                "the exact signed permit"
            )

        calibration_config_sha256 = cls._require_sha256(
            wrapper["calibration_config_sha256"],
            "validated calibration config SHA-256",
        )
        target_config_sha256 = cls._require_sha256(
            wrapper["target_deployment_config_sha256"],
            "validated target deployment config SHA-256",
        )
        if (
            cls._require_sha256(
                permit["calibration_config_sha256"],
                "permit calibration config SHA-256",
            )
            != calibration_config_sha256
        ):
            raise ValueError("Calibration config SHA-256 wrapper mismatch")
        if (
            cls._require_sha256(
                permit["target_deployment_config_sha256"],
                "permit target deployment config SHA-256",
            )
            != target_config_sha256
        ):
            raise ValueError("Target deployment config SHA-256 wrapper mismatch")

        permit_id = str(permit["permit_id"] or "").strip()
        deployment_id = str(permit["deployment_id"] or "").strip()
        schema = str(permit["schema"] or "").strip()
        symbol = str(permit["symbol"] or "").upper().strip()
        if not schema or not permit_id or not deployment_id or not symbol:
            raise ValueError(
                "RPI calibration permit identity fields must be non-empty"
            )
        if permit["stage"] != cls.RPI_CALIBRATION_STAGE:
            raise ValueError("RPI calibration permit stage mismatch")
        if permit["venue"] != cls.RPI_CALIBRATION_VENUE:
            raise ValueError("RPI calibration permit venue mismatch")
        if permit["model"] != cls.RPI_CALIBRATION_MODEL:
            raise ValueError("RPI calibration permit model mismatch")
        configured_symbols = {
            str(item or "").upper().strip()
            for item in config.get("symbols", [])
            if str(item or "").strip()
        }
        if symbol not in configured_symbols:
            raise ValueError(
                "RPI calibration permit symbol is not configured for this OMS"
            )

        issued_at_ns = cls._parse_utc_exchange_ns(
            permit["issued_at_utc"],
            "RPI calibration permit issued_at_utc",
        )
        not_before_ns = cls._parse_utc_exchange_ns(
            permit["not_before_utc"],
            "RPI calibration permit not_before_utc",
        )
        expires_at_ns = cls._parse_utc_exchange_ns(
            permit["expires_at_utc"],
            "RPI calibration permit expires_at_utc",
        )
        if issued_at_ns > expires_at_ns or not_before_ns >= expires_at_ns:
            raise ValueError("RPI calibration permit time window is invalid")

        raw_depths = policy["fixed_depths_bps"]
        if not isinstance(raw_depths, list) or not raw_depths:
            raise ValueError(
                "RPI calibration fixed_depths_bps must be a non-empty list"
            )
        fixed_depths = tuple(
            cls._positive_decimal(value, "RPI calibration depth")
            for value in raw_depths
        )
        fixed_depth_texts = tuple(
            cls._decimal_text(value) for value in fixed_depths
        )
        if len(set(fixed_depth_texts)) != len(fixed_depth_texts):
            raise ValueError("RPI calibration depths must be unique")
        if not 3 <= len(fixed_depths) <= 16:
            raise ValueError(
                "RPI calibration requires between 3 and 16 fixed depths"
            )
        if any(
            right <= left
            for left, right in zip(fixed_depths, fixed_depths[1:])
        ):
            raise ValueError(
                "RPI calibration fixed depths must be strictly increasing"
            )
        if fixed_depths[-1] > Decimal("1000"):
            raise ValueError(
                "RPI calibration fixed depth must not exceed 1000 bps"
            )
        if fixed_depths[-1] - fixed_depths[0] < Decimal("0.5"):
            raise ValueError(
                "RPI calibration fixed-depth span must be at least 0.5 bps"
            )

        order_ttl_sec = cls._positive_decimal(
            policy["order_ttl_sec"],
            "RPI calibration order TTL",
        )
        min_order_interval_sec = cls._positive_decimal(
            policy["min_order_interval_sec"],
            "RPI calibration minimum order interval",
        )
        if order_ttl_sec > Decimal("60"):
            raise ValueError(
                "RPI calibration order TTL must not exceed 60 seconds"
            )
        if order_ttl_sec > min_order_interval_sec:
            raise ValueError(
                "RPI calibration order TTL must not exceed its order interval"
            )
        if not (
            Decimal("5")
            <= min_order_interval_sec
            <= Decimal("3600")
        ):
            raise ValueError(
                "RPI calibration order interval must be between 5 and "
                "3600 seconds"
            )
        max_active_orders = policy["max_active_orders"]
        max_order_count = policy["max_order_count"]
        for value, context in (
            (max_active_orders, "RPI calibration max active orders"),
            (max_order_count, "RPI calibration max order count"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
            ):
                raise ValueError(f"{context} must be a positive integer")
        if max_active_orders != 1:
            raise ValueError("RPI calibration max_active_orders must equal 1")
        if max_order_count > 100:
            raise ValueError(
                "RPI calibration max_order_count must not exceed 100"
            )
        permit_window_ns = expires_at_ns - not_before_ns
        required_schedule_ns = int(
            (
                (
                    Decimal(max_order_count - 1)
                    * min_order_interval_sec
                    + order_ttl_sec
                )
                * Decimal("1000000000")
            ).to_integral_value(rounding=ROUND_CEILING)
        )
        if required_schedule_ns > permit_window_ns:
            raise ValueError(
                "RPI calibration order schedule does not fit its permit window"
            )

        min_order_notional = cls._positive_decimal(
            policy["min_order_notional_usdt"],
            "RPI calibration minimum order notional",
        )
        max_order_notional = cls._positive_decimal(
            policy["max_order_notional_usdt"],
            "RPI calibration maximum order notional",
        )
        max_cumulative_notional = cls._positive_decimal(
            policy["max_cumulative_submitted_notional_usdt"],
            "RPI calibration cumulative notional cap",
        )
        max_calibration_loss = cls._positive_decimal(
            policy["max_calibration_loss_usdt"],
            "RPI calibration loss cap",
        )
        if min_order_notional > max_order_notional:
            raise ValueError(
                "RPI calibration minimum notional exceeds maximum notional"
            )
        if max_order_notional > Decimal("8"):
            raise ValueError(
                "RPI calibration maximum order notional must not exceed 8 USDT"
            )
        configured_order_cap = cls._positive_decimal(
            (
                (config.get("risk", {}) or {})
                .get("limits", {})
                or {}
            ).get("max_order_notional"),
            "configured maximum order notional",
        )
        if max_order_notional > configured_order_cap:
            raise ValueError(
                "RPI calibration order cap exceeds the configured risk cap"
            )
        if max_cumulative_notional < min_order_notional:
            raise ValueError(
                "RPI calibration cumulative cap is below one legal order"
            )
        if (
            max_cumulative_notional
            > Decimal(max_order_count) * max_order_notional
        ):
            raise ValueError(
                "RPI calibration cumulative cap exceeds count times "
                "per-order cap"
            )
        if max_calibration_loss > Decimal("2"):
            raise ValueError(
                "RPI calibration loss cap must not exceed 2 USDT"
            )
        deployment_loss_cap = cls._positive_decimal(
            live_launch.get("max_deployment_loss_usdt"),
            "live launch deployment loss cap",
        )
        if max_calibration_loss > deployment_loss_cap:
            raise ValueError(
                "RPI calibration loss cap exceeds deployment loss cap"
            )

        strategy_policy_sha256 = cls._require_sha256(
            permit["strategy_policy_sha256"],
            "RPI calibration strategy policy SHA-256",
        )
        implementation_sha256 = cls._require_sha256(
            permit["implementation_sha256"],
            "RPI calibration implementation SHA-256",
        )
        freshness = (
            (config.get("risk", {}) or {}).get(
                "market_data_freshness",
                {},
            )
            or {}
        )
        max_mark_age_ms = cls._positive_decimal(
            freshness.get("max_mark_age_ms"),
            "configured maximum mark age",
        )
        max_book_age_ms = cls._positive_decimal(
            freshness.get("max_book_age_ms"),
            "configured maximum book age",
        )
        trusted_signers = live_launch.get(
            "calibration_permit_trusted_signers"
        )
        if not isinstance(trusted_signers, dict) or not trusted_signers:
            raise ValueError(
                "RPI calibration trusted signer keyring is required"
            )
        cls._verify_rpi_calibration_permit_signature(
            permit,
            trusted_signers,
            context="RPI calibration permit",
        )
        return {
            "enabled": True,
            "signed_permit": json.loads(cls._canonical_json(permit)),
            "trusted_signers": json.loads(
                cls._canonical_json(trusted_signers)
            ),
            "schema": schema,
            "permit_id": permit_id,
            "permit_sha256": permit_sha256,
            "deployment_id": deployment_id,
            "symbol": symbol,
            "calibration_config_sha256": calibration_config_sha256,
            "target_deployment_config_sha256": target_config_sha256,
            "strategy_policy_sha256": strategy_policy_sha256,
            "implementation_sha256": implementation_sha256,
            "issued_at": str(permit["issued_at_utc"]),
            "not_before": str(permit["not_before_utc"]),
            "expires_at": str(permit["expires_at_utc"]),
            "issued_at_ns": issued_at_ns,
            "not_before_ns": not_before_ns,
            "expires_at_ns": expires_at_ns,
            "fixed_depths_bps": fixed_depth_texts,
            "order_ttl_sec": order_ttl_sec,
            "order_ttl_ns": int(
                (order_ttl_sec * Decimal("1000000000")).to_integral_value(
                    rounding=ROUND_CEILING
                )
            ),
            "min_order_interval_sec": min_order_interval_sec,
            "min_order_interval_ns": int(
                (
                    min_order_interval_sec * Decimal("1000000000")
                ).to_integral_value(rounding=ROUND_CEILING)
            ),
            "max_active_orders": max_active_orders,
            "max_order_count": max_order_count,
            "min_order_notional_microu": cls._usdt_to_microu(
                min_order_notional,
                upper_bound=False,
            ),
            "max_order_notional_microu": cls._usdt_to_microu(
                max_order_notional,
                upper_bound=True,
            ),
            "max_cumulative_notional_microu": cls._usdt_to_microu(
                max_cumulative_notional,
                upper_bound=True,
            ),
            "max_calibration_loss_microu": cls._usdt_to_microu(
                max_calibration_loss,
                upper_bound=True,
            ),
            "max_mark_age_ms": float(max_mark_age_ms),
            "max_book_age_ms": float(max_book_age_ms),
        }

