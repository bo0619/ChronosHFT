"""Canonical JSON and configuration digest helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from governance.contracts import (
    CANONICAL_CONFIG_DIGEST_DOMAIN,
    CANONICAL_CONFIG_SCHEMA,
)


class CanonicalizationError(ValueError):
    """Raised when a value cannot be represented unambiguously as JSON."""


_RUNTIME_VALIDATION_PRODUCTS = frozenset(
    {
        "_runtime_calibration",
        "_validated_calibration",
        "_validated_live_canary_evidence",
        "_validated_rpi_calibration_permit",
    }
)


def _plain_json_value(value: Any, *, path: str) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(f"{path} contains a non-string key")
            if key in _RUNTIME_VALIDATION_PRODUCTS:
                continue
            normalized[key] = _plain_json_value(item, path=f"{path}.{key}")
        return normalized
    if isinstance(value, list):
        return [
            _plain_json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise CanonicalizationError(
        f"{path} contains non-JSON value {type(value).__name__}"
    )


def canonical_json_bytes(value: Any, *, label: str = "document") -> bytes:
    """Encode JSON deterministically and reject NaN or runtime-only objects."""
    normalized = _plain_json_value(value, path=label)
    try:
        encoded = json.dumps(
            normalized,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise CanonicalizationError(
            f"{label} is not canonical JSON: {exc}"
        ) from exc
    return encoded.encode("ascii")


def canonical_config_document(config: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        raise CanonicalizationError("runtime config must be an object")
    return {
        "schema": CANONICAL_CONFIG_SCHEMA,
        "config": _plain_json_value(config, path="config"),
    }


def canonical_config_digest(config: Mapping[str, Any]) -> str:
    document = canonical_config_document(config)
    return hashlib.sha256(
        CANONICAL_CONFIG_DIGEST_DOMAIN
        + canonical_json_bytes(document, label="canonical config")
    ).hexdigest()


__all__ = [
    "CanonicalizationError",
    "canonical_config_digest",
    "canonical_config_document",
    "canonical_json_bytes",
]
