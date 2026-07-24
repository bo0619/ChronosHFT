"""Offline-only readiness check for a single-symbol live canary.

This module reads JSON and local evidence files. It deliberately does not
import a gateway, construct an OMS, access Binance, or submit/cancel orders.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from infrastructure.config_scaling import (  # noqa: E402
    normalize_root_config_preapproval,
)
from infrastructure.live_config_guard import (  # noqa: E402
    CANARY_STAGE,
    LIVE_CANARY_ACCOUNT_SOURCE,
    RPI_CALIBRATION_CANARY_STAGE,
    live_launch_stage,
    validate_live_api_restrictions_evidence,
    validate_live_dual_key_account_evidence,
    validate_live_runtime_config,
    validate_live_state_path_bindings,
)
from infrastructure.rpi_calibration_permit import (  # noqa: E402
    load_and_validate_rpi_calibration_permit,
)
from infrastructure.rpi_policy import validate_live_rpi_policy  # noqa: E402
from strategy.model_readiness import (  # noqa: E402
    validate_live_calibration_approval,
)


REPORT_SCHEMA = "chronoshft.live_canary_readiness.v1"
EVIDENCE_SCHEMA = "chronoshft.live_canary_evidence.v2"
PASS = "PASS"
BLOCKED = "BLOCKED"
DEFAULT_CONFIG_PATH = "config.live.canary.example.json"
DEFAULT_EVIDENCE_MAX_AGE_SEC = 900.0
MAX_EVIDENCE_MAX_AGE_SEC = 3600.0
RPI_EXCHANGE_INFO_SOURCE = "GET /fapi/v1/exchangeInfo"
RPI_COMMISSION_SOURCE = "GET /fapi/v1/commissionRate"
ACCOUNT_TRUTH_SOURCE = LIVE_CANARY_ACCOUNT_SOURCE
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_REQUIRED_ATTESTATIONS = {
    "deployment_host_can_reach_binance_mainnet": (
        "deployment host Binance mainnet connectivity was checked"
    ),
    "operator_confirmed_exchange_access_allowed": (
        "operator confirmed the deployment environment may access Binance"
    ),
    "single_host_single_process_deployment_confirmed": (
        "this deployment_id and permit run on one host and one process only"
    ),
    "primary_api_withdrawals_disabled": (
        "primary trading key has withdrawals disabled"
    ),
    "supervisor_api_withdrawals_disabled": (
        "independent supervisor key has withdrawals disabled"
    ),
    "primary_api_ip_restricted": "primary trading key has an IP allowlist",
    "supervisor_api_ip_restricted": (
        "independent supervisor key has an IP allowlist"
    ),
    "api_keys_are_distinct": "primary and supervisor API keys are distinct",
    "api_keys_same_futures_account_confirmed": (
        "primary and supervisor keys belong to the same Futures account"
    ),
    "primary_api_futures_trading_enabled": (
        "primary key has USD-M Futures trading permission"
    ),
    "supervisor_api_futures_trading_enabled": (
        "independent supervisor key has USD-M Futures trading permission"
    ),
    "supervisor_api_emergency_permissions_confirmed": (
        "independent supervisor key can cancel and submit reduce-only closes"
    ),
    "credential_environment_populated_on_deployment_host": (
        "all four credential environment variables are populated on the host"
    ),
    "exchange_open_orders_empty": "exchange account has no open orders",
    "exchange_positions_flat": "exchange account has no position",
    "legacy_state_archived": "legacy live journal and sidecar state were archived",
    "fresh_state_generation_selected": (
        "new OMS journal and sidecar state paths were selected"
    ),
    "isolated_margin_confirmed": "symbol is configured for isolated margin",
    "leverage_confirmed": "symbol leverage was confirmed at the configured value",
    "rpi_account_permission_confirmed": (
        "the account is explicitly permitted to place RPI orders"
    ),
}

_INLINE_SECRET_PATHS = (
    ("api_key",),
    ("api_secret",),
    ("risk", "independent_supervisor", "api_key"),
    ("risk", "independent_supervisor", "api_secret"),
)

_CREDENTIAL_ENV_PATHS = (
    ("api_key_env",),
    ("api_secret_env",),
    ("risk", "independent_supervisor", "api_key_env"),
    ("risk", "independent_supervisor", "api_secret_env"),
)


def _check(check_id: str, status: str, message: str) -> dict[str, str]:
    return {"id": check_id, "status": status, "message": message}


def _finalize(config_path: Path, checks: list[dict[str, str]]) -> dict[str, Any]:
    blocked = sum(check["status"] == BLOCKED for check in checks)
    passed = sum(check["status"] == PASS for check in checks)
    return {
        "schema": REPORT_SCHEMA,
        "status": BLOCKED if blocked else PASS,
        "config_path": str(config_path),
        "offline_only": True,
        "network_requests": 0,
        "gateway_objects_constructed": 0,
        "oms_objects_constructed": 0,
        "order_paths_exercised": 0,
        "summary": {"pass": passed, "blocked": blocked},
        "checks": checks,
    }


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON number {value!r} is not allowed")


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle, parse_constant=_reject_json_constant)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"cannot read {label} at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _nested_value(config: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = config
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    return normalize_root_config_preapproval(config)


def _credential_reference_check(config: Mapping[str, Any]) -> dict[str, str]:
    inline_paths = [
        ".".join(path)
        for path in _INLINE_SECRET_PATHS
        if str(_nested_value(config, path) or "").strip()
    ]
    if inline_paths:
        return _check(
            "credentials.references",
            BLOCKED,
            "inline credential values are forbidden: " + ", ".join(inline_paths),
        )

    names = [
        str(_nested_value(config, path) or "").strip()
        for path in _CREDENTIAL_ENV_PATHS
    ]
    invalid_paths = [
        ".".join(path)
        for path, name in zip(_CREDENTIAL_ENV_PATHS, names)
        if not _ENV_NAME_RE.fullmatch(name)
    ]
    if invalid_paths:
        return _check(
            "credentials.references",
            BLOCKED,
            "valid credential environment variable names are required: "
            + ", ".join(invalid_paths),
        )
    if len(set(names)) != len(names):
        return _check(
            "credentials.references",
            BLOCKED,
            "all four credential environment variable names must be distinct",
        )
    return _check(
        "credentials.references",
        PASS,
        "credentials are referenced by four distinct environment variable names; "
        "values were not read",
    )


def _guard_projection(config: Mapping[str, Any]) -> dict[str, Any]:
    projected = deepcopy(dict(config))
    projected["api_key"] = "__OFFLINE_PRIMARY_KEY_SENTINEL__"
    projected["api_secret"] = "__OFFLINE_PRIMARY_SECRET_SENTINEL__"
    risk = projected.setdefault("risk", {})
    if not isinstance(risk, dict):
        return projected
    supervisor = risk.setdefault("independent_supervisor", {})
    if not isinstance(supervisor, dict):
        return projected
    supervisor["api_key"] = "__OFFLINE_SUPERVISOR_KEY_SENTINEL__"
    supervisor["api_secret"] = "__OFFLINE_SUPERVISOR_SECRET_SENTINEL__"
    return projected


def _configured_symbol(config: Mapping[str, Any]) -> str:
    symbols = config.get("symbols")
    if not isinstance(symbols, (list, tuple)) or len(symbols) != 1:
        return ""
    return str(symbols[0] or "").strip().upper()


def _state_binding_check(
    config: Mapping[str, Any],
    *,
    base_dir: Path,
) -> dict[str, str]:
    try:
        validate_live_state_path_bindings(config, base_dir=base_dir)
    except (TypeError, ValueError) as exc:
        return _check("state.deployment_binding", BLOCKED, str(exc))
    return _check(
        "state.deployment_binding",
        PASS,
        "all resolved durable state, journal, fence, and alert spool paths "
        "are distinct and bound to deployment_id",
    )


def _evidence_path(config: Mapping[str, Any], config_path: Path) -> Path | None:
    live_launch = config.get("live_launch", {})
    if not isinstance(live_launch, Mapping):
        return None
    raw_path = str(live_launch.get("offline_evidence_path", "") or "").strip()
    if not raw_path:
        return None
    normalized_parts = tuple(
        part
        for part in raw_path.replace("\\", "/").split("/")
        if part not in {"", "."}
    )
    if ".." in normalized_parts:
        raise ValueError(
            "live_launch.offline_evidence_path must not contain '..' components"
        )
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = config_path.parent / candidate
    return candidate.resolve()


def _operator_attestation_check(
    evidence: Mapping[str, Any],
) -> dict[str, str]:
    attestations = evidence.get("operator_attestations", {})
    if not isinstance(attestations, Mapping):
        return _check(
            "evidence.operator_attestations",
            BLOCKED,
            "operator_attestations must be a JSON object",
        )
    missing = [
        name
        for name in _REQUIRED_ATTESTATIONS
        if attestations.get(name) is not True
    ]
    if missing:
        return _check(
            "evidence.operator_attestations",
            BLOCKED,
            "required attestations are not true: " + ", ".join(missing),
        )
    return _check(
        "evidence.operator_attestations",
        PASS,
        f"all {len(_REQUIRED_ATTESTATIONS)} required operator attestations are true",
    )


def _parse_utc_timestamp(value: Any) -> datetime:
    normalized = str(value or "").strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("captured_at_utc must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("captured_at_utc must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _evidence_max_age(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if (
        not math.isfinite(parsed)
        or parsed <= 0.0
        or parsed > MAX_EVIDENCE_MAX_AGE_SEC
    ):
        return None
    return parsed


def _commission_rate(
    value: Any,
    field: str,
) -> tuple[Decimal | None, str | None]:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None, f"{field} must be a finite account-specific rate"
    if not parsed.is_finite() or abs(parsed) > Decimal("0.01"):
        return None, f"{field} must be between -0.01 and 0.01"
    return parsed, None


def _rpi_evidence_check(
    config: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    now_utc: datetime,
) -> dict[str, str]:
    symbol = _configured_symbol(config)
    truth = evidence.get("rpi_truth", {})
    if not isinstance(truth, Mapping):
        return _check(
            "evidence.rpi_truth",
            BLOCKED,
            "rpi_truth must be a JSON object",
        )

    truth_symbol = str(truth.get("symbol", "") or "").strip().upper()
    if not symbol or truth_symbol != symbol:
        return _check(
            "evidence.rpi_truth",
            BLOCKED,
            "RPI truth symbol must exactly match the configured canary symbol",
        )

    if truth.get("exchange_info_source") != RPI_EXCHANGE_INFO_SOURCE:
        return _check(
            "evidence.rpi_truth",
            BLOCKED,
            f"exchange_info_source must be {RPI_EXCHANGE_INFO_SOURCE!r}",
        )
    if truth.get("commission_source") != RPI_COMMISSION_SOURCE:
        return _check(
            "evidence.rpi_truth",
            BLOCKED,
            f"commission_source must be {RPI_COMMISSION_SOURCE!r}",
        )

    try:
        captured_at = _parse_utc_timestamp(truth.get("captured_at_utc"))
    except ValueError as exc:
        return _check("evidence.rpi_truth", BLOCKED, str(exc))

    live_launch = config.get("live_launch", {})
    if not isinstance(live_launch, Mapping):
        live_launch = {}
    max_age_sec = _evidence_max_age(
        live_launch.get(
            "offline_evidence_max_age_sec",
            DEFAULT_EVIDENCE_MAX_AGE_SEC,
        )
    )
    if max_age_sec is None:
        return _check(
            "evidence.rpi_truth",
            BLOCKED,
            "live_launch.offline_evidence_max_age_sec must be greater than 0 "
            f"and no more than {MAX_EVIDENCE_MAX_AGE_SEC:g}",
        )
    age_sec = (now_utc - captured_at).total_seconds()
    if age_sec < -300.0:
        return _check(
            "evidence.rpi_truth",
            BLOCKED,
            "RPI truth capture time is more than 300 seconds in the future",
        )
    if age_sec > max_age_sec:
        return _check(
            "evidence.rpi_truth",
            BLOCKED,
            f"RPI truth is stale ({age_sec:.0f}s > {max_age_sec:.0f}s)",
        )

    parsed_rates = {}
    for field in (
        "makerCommissionRate",
        "takerCommissionRate",
        "rpiCommissionRate",
    ):
        rate, error = _commission_rate(truth.get(field), field)
        if error:
            return _check("evidence.rpi_truth", BLOCKED, error)
        parsed_rates[field] = rate

    exchange_status = str(truth.get("exchange_status", "") or "").strip().upper()
    supports_rpi = truth.get("supports_rpi") is True and exchange_status == "TRADING"
    try:
        validate_live_rpi_policy(
            config,
            [symbol],
            {symbol: supports_rpi},
            {symbol: parsed_rates["rpiCommissionRate"]},
        )
    except (TypeError, ValueError) as exc:
        return _check("evidence.rpi_truth", BLOCKED, str(exc))
    return _check(
        "evidence.rpi_truth",
        PASS,
        "fresh local evidence contains maker/taker/RPI account rates; the symbol "
        "is TRADING, RPI-enabled, and the RPI rate satisfies policy",
    )


def _account_equity_evidence_check(
    config: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    now_utc: datetime,
) -> dict[str, str]:
    try:
        validate_live_dual_key_account_evidence(
            config,
            evidence,
            now_utc=now_utc,
            require_api_key_binding=False,
        )
    except (TypeError, ValueError) as exc:
        return _check("evidence.account_truth", BLOCKED, str(exc))
    return _check(
        "evidence.account_truth",
        PASS,
        "matching fresh USDT account truth binds declared equity and deployed "
        "capital to distinct primary and supervisor API-key fingerprints",
    )


def _api_permission_evidence_check(
    config: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    now_utc: datetime,
) -> dict[str, str]:
    try:
        validate_live_api_restrictions_evidence(
            config,
            evidence,
            now_utc=now_utc,
            require_api_key_binding=False,
        )
    except (TypeError, ValueError) as exc:
        return _check("evidence.api_permissions", BLOCKED, str(exc))
    return _check(
        "evidence.api_permissions",
        PASS,
        "fresh API-restriction truth binds distinct keys with reading and "
        "Futures enabled, withdrawals disabled, and IP restriction enabled",
    )


def _evidence_checks(
    config: Mapping[str, Any],
    config_path: Path,
    *,
    now_utc: datetime,
) -> list[dict[str, str]]:
    try:
        path = _evidence_path(config, config_path)
    except ValueError as exc:
        return [
            _check("evidence.file", BLOCKED, str(exc)),
            _check(
                "evidence.operator_attestations",
                BLOCKED,
                "not evaluated because evidence path binding failed",
            ),
            _check(
                "evidence.rpi_truth",
                BLOCKED,
                "not evaluated because evidence path binding failed",
            ),
            _check(
                "evidence.api_permissions",
                BLOCKED,
                "not evaluated because evidence path binding failed",
            ),
            _check(
                "evidence.account_truth",
                BLOCKED,
                "not evaluated because evidence path binding failed",
            ),
        ]
    if path is None:
        return [
            _check(
                "evidence.file",
                BLOCKED,
                "live_launch.offline_evidence_path is required",
            ),
            _check(
                "evidence.operator_attestations",
                BLOCKED,
                "not evaluated because no evidence file was configured",
            ),
            _check(
                "evidence.rpi_truth",
                BLOCKED,
                "not evaluated because no evidence file was configured",
            ),
            _check(
                "evidence.api_permissions",
                BLOCKED,
                "not evaluated because no evidence file was configured",
            ),
            _check(
                "evidence.account_truth",
                BLOCKED,
                "not evaluated because no evidence file was configured",
            ),
        ]

    try:
        evidence = _read_json_object(path, "live canary evidence")
    except ValueError as exc:
        return [
            _check("evidence.file", BLOCKED, str(exc)),
            _check(
                "evidence.operator_attestations",
                BLOCKED,
                "not evaluated because the evidence file is unavailable",
            ),
            _check(
                "evidence.rpi_truth",
                BLOCKED,
                "not evaluated because the evidence file is unavailable",
            ),
            _check(
                "evidence.api_permissions",
                BLOCKED,
                "not evaluated because the evidence file is unavailable",
            ),
            _check(
                "evidence.account_truth",
                BLOCKED,
                "not evaluated because the evidence file is unavailable",
            ),
        ]

    live_launch = config.get("live_launch", {})
    if not isinstance(live_launch, Mapping):
        live_launch = {}
    deployment_id = str(live_launch.get("deployment_id", "") or "").strip()
    symbol = _configured_symbol(config)
    evidence_deployment = str(evidence.get("deployment_id", "") or "").strip()
    evidence_symbol = str(evidence.get("symbol", "") or "").strip().upper()
    binding_errors = []
    if evidence.get("schema") != EVIDENCE_SCHEMA:
        binding_errors.append(f"schema must be {EVIDENCE_SCHEMA!r}")
    if not deployment_id or evidence_deployment != deployment_id:
        binding_errors.append("deployment_id must match the canary config")
    if not symbol or evidence_symbol != symbol:
        binding_errors.append("symbol must match the canary config")
    if binding_errors:
        return [
            _check("evidence.file", BLOCKED, "; ".join(binding_errors)),
            _check(
                "evidence.operator_attestations",
                BLOCKED,
                "not evaluated because evidence binding failed",
            ),
            _check(
                "evidence.rpi_truth",
                BLOCKED,
                "not evaluated because evidence binding failed",
            ),
            _check(
                "evidence.api_permissions",
                BLOCKED,
                "not evaluated because evidence binding failed",
            ),
            _check(
                "evidence.account_truth",
                BLOCKED,
                "not evaluated because evidence binding failed",
            ),
        ]

    return [
        _check(
            "evidence.file",
            PASS,
            "local evidence schema, deployment_id, and symbol match",
        ),
        _operator_attestation_check(evidence),
        _rpi_evidence_check(config, evidence, now_utc=now_utc),
        _api_permission_evidence_check(
            config,
            evidence,
            now_utc=now_utc,
        ),
        _account_equity_evidence_check(
            config,
            evidence,
            now_utc=now_utc,
        ),
    ]


def assess_live_canary_readiness(
    config_path: str | Path,
    *,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Return a redacted PASS/BLOCKED report without touching trading code."""
    path = Path(config_path).resolve()
    checks: list[dict[str, str]] = []
    try:
        raw_config = _read_json_object(path, "canary config")
    except ValueError as exc:
        checks.append(_check("config.json", BLOCKED, str(exc)))
        return _finalize(path, checks)

    checks.append(_check("config.json", PASS, "valid strict JSON object"))
    checks.append(_credential_reference_check(raw_config))

    execution = raw_config.get("execution", {})
    paper_trade = raw_config.get("paper_trade", {})
    mode = (
        str(execution.get("mode", "") or "").strip().lower()
        if isinstance(execution, Mapping)
        else ""
    )
    paper_enabled = (
        paper_trade.get("enabled") is True
        if isinstance(paper_trade, Mapping)
        else paper_trade is True
    )
    if mode == "live" and not paper_enabled:
        checks.append(
            _check(
                "execution.explicit_live_canary",
                PASS,
                "execution.mode is explicitly live and paper mode is disabled",
            )
        )
    else:
        checks.append(
            _check(
                "execution.explicit_live_canary",
                BLOCKED,
                "requires execution.mode='live' and paper_trade.enabled=false",
            )
        )

    try:
        normalized = _normalize_config(raw_config)
    except (TypeError, ValueError) as exc:
        checks.append(_check("config.normalization", BLOCKED, str(exc)))
        return _finalize(path, checks)
    checks.append(
        _check(
            "config.normalization",
            PASS,
            "production defaults, capital rules, and strategy registration normalize",
        )
    )

    effective_now = now_utc or datetime.now(timezone.utc)
    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=timezone.utc)
    else:
        effective_now = effective_now.astimezone(timezone.utc)

    stage = live_launch_stage(normalized)
    if stage == RPI_CALIBRATION_CANARY_STAGE:
        try:
            validated_permit = load_and_validate_rpi_calibration_permit(
                normalized,
                config_path=path,
                now_utc=effective_now,
            )
        except (TypeError, ValueError) as exc:
            checks.append(
                _check("calibration.signed_permit", BLOCKED, str(exc))
            )
        else:
            normalized["_validated_rpi_calibration_permit"] = (
                validated_permit
            )
            checks.append(
                _check(
                    "calibration.signed_permit",
                    PASS,
                    "independent Ed25519 permit binds this calibration config, "
                    "the target deployment config, fixed depths, TTL, cadence, "
                    "order quotas, and loss cap",
                )
            )
    elif stage == CANARY_STAGE:
        try:
            validate_live_calibration_approval(normalized, config_path=path)
        except (TypeError, ValueError) as exc:
            checks.append(_check("model.live_approval", BLOCKED, str(exc)))
        else:
            checks.append(
                _check(
                    "model.live_approval",
                    PASS,
                    "model formula, units, manifest, artifacts, and OOS "
                    "evidence are approved",
                )
            )
    else:
        checks.append(
            _check(
                "launch.approval_path",
                BLOCKED,
                "live_launch.stage does not select an approved launch path",
            )
        )

    try:
        alert = normalized.get("alert", {})
        alert = alert if isinstance(alert, Mapping) else {}
        alert_env_name = str(
            alert.get("webhook_url_env", "") or ""
        ).strip()
        offline_alert_environ = (
            {alert_env_name: "https://offline.invalid/chronoshft"}
            if alert_env_name
            else {}
        )
        validate_live_runtime_config(
            _guard_projection(normalized),
            config_path=path,
            now_utc=effective_now,
            external_alert_environ=offline_alert_environ,
            runtime_working_dir=path.parent,
        )
    except (TypeError, ValueError) as exc:
        checks.append(_check("config.live_guard", BLOCKED, str(exc)))
    else:
        checks.append(
            _check(
                "config.live_guard",
                PASS,
                "single-symbol launch and mandatory safety planes pass the "
                "stage-specific live guard",
            )
        )
    checks.append(
        _state_binding_check(
            normalized,
            base_dir=path.parent,
        )
    )

    checks.extend(
        _evidence_checks(
            normalized,
            path,
            now_utc=effective_now,
        )
    )
    return _finalize(path, checks)


def _render_text(report: Mapping[str, Any]) -> str:
    lines = [
        f"ChronosHFT offline live-canary readiness: {report['status']}",
        f"Config: {report['config_path']}",
        "",
    ]
    for check in report["checks"]:
        lines.append(f"[{check['status']}] {check['id']}: {check['message']}")
    lines.extend(
        [
            "",
            "Offline boundary: 0 network requests, 0 gateway/OMS objects, "
            "0 order paths.",
            "A PASS is only an offline prerequisite. Live startup still "
            "re-fetches exchange/account truth and may fail closed.",
        ]
    )
    return "\n".join(lines)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Check a one-symbol ChronosHFT live canary using only JSON and "
            "local evidence. No network, gateway, OMS, or order path is used."
        )
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Canary JSON path (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the redacted machine-readable report as JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    report = assess_live_canary_readiness(args.config)
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(_render_text(report))
    return 0 if report["status"] == PASS else 2


if __name__ == "__main__":
    raise SystemExit(main())
