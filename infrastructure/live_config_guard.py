import hashlib
import hmac
import json
import math
import ntpath
import os
import re
import stat
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from infrastructure.external_alerts import validate_https_webhook_url
from infrastructure.paper_trade import is_paper_trade
from infrastructure.rpi_policy import effective_rpi_route_enabled
from strategy.registry import (
    effective_primary_strategy_config,
    strategy_id_for_model,
)


INDEPENDENT_SUPERVISOR_SOURCE = "independent_supervisor"
CANARY_STAGE = "canary"
RPI_CALIBRATION_CANARY_STAGE = "rpi_calibration_canary"
LIVE_CANARY_STAGES = frozenset(
    {CANARY_STAGE, RPI_CALIBRATION_CANARY_STAGE}
)
MAINNET_ENVIRONMENTS = frozenset({"mainnet", "production"})
MAX_CANARY_DEPLOYED_EQUITY_FRACTION = 0.02
MAX_CANARY_DEPLOYMENT_LOSS_FRACTION = 0.05
MAX_CANARY_ORDER_FRACTION = 0.12
MAX_CANARY_POSITION_FRACTION = 0.20
MAX_CANARY_GROSS_FRACTION = 0.40
MAX_CANARY_DAILY_LOSS_FRACTION = 0.50
MAX_CANARY_ACTIVE_ORDERS = 2
MAX_CANARY_DEPLOYED_CAPITAL_USDT = 100.0
MAX_CANARY_DEPLOYMENT_LOSS_USDT = 5.0
MAX_CANARY_ORDER_NOTIONAL_USDT = 8.0
MAX_CANARY_POSITION_NOTIONAL_USDT = 8.0
MAX_CANARY_GROSS_NOTIONAL_USDT = 8.0
MAX_CANARY_DAILY_LOSS_USDT = 2.0
MIN_RPI_COMMISSION_POLL_INTERVAL_SEC = 5.0
MAX_RPI_COMMISSION_POLL_INTERVAL_SEC = 60.0
MAX_RPI_COMMISSION_HALT_THRESHOLD = 2
RPI_COMMISSION_CLEAN_POLLS_TO_CLEAR = 2
MAX_CANARY_FUNDING_SNAPSHOT_AGE_MS = 3000.0
MIN_CANARY_PRE_FUNDING_REDUCE_ONLY_SEC = 300.0
MIN_CANARY_POST_FUNDING_HOLD_SEC = 60.0
MAX_CANARY_ABS_FUNDING_RATE = 0.0005
MAX_CANARY_NEXT_FUNDING_HORIZON_SEC = 32_400.0
MIN_CANARY_FUNDING_RECOVERY_UPDATES = 3
MIN_CANARY_RPI_INTENSITY_SAMPLES = 30
MIN_CANARY_RPI_DEPTH_LEVELS = 3
MIN_CANARY_RPI_EXPOSURE_SEC = 60.0
MIN_CANARY_RPI_FILLS = 10
MIN_CANARY_RPI_DEPTH_SPAN_BPS = 0.5
MAX_CALIBRATION_DEPLOYED_EQUITY_FRACTION = 0.005
MAX_CALIBRATION_ORDER_NOTIONAL_USDT = 8.0
MAX_CALIBRATION_POSITION_NOTIONAL_USDT = 8.0
MAX_CALIBRATION_GROSS_NOTIONAL_USDT = 8.0
MAX_CALIBRATION_DAILY_LOSS_USDT = 1.0
MAX_CALIBRATION_DEPLOYMENT_LOSS_USDT = 2.0
MIN_CALIBRATION_DEPTH_LEVELS = 3
MAX_CALIBRATION_DEPTH_LEVELS = 16
MIN_CALIBRATION_DEPTH_SPAN_BPS = 0.5
MAX_CALIBRATION_DEPTH_BPS = 1000.0
MIN_CALIBRATION_ORDER_INTERVAL_SEC = 5.0
MAX_CALIBRATION_ORDER_INTERVAL_SEC = 3600.0
MAX_CALIBRATION_ORDER_TTL_SEC = 60.0
MAX_CALIBRATION_ORDER_COUNT = 100
MAX_CALIBRATION_PERMIT_TTL_SEC = 86_400.0
CALIBRATION_ACTIVE_ORDER_CAP_FIELDS = (
    "max_total_active_orders",
    "max_symbol_active_orders",
    "max_strategy_active_orders",
    "max_strategy_symbol_active_orders",
)
CALIBRATION_PERMIT_WRAPPER_FIELDS = frozenset(
    {
        "permit",
        "permit_sha256",
        "calibration_config_sha256",
        "target_deployment_config_sha256",
    }
)
CALIBRATION_PERMIT_POLICY_FIELDS = frozenset(
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
LIVE_CANARY_EVIDENCE_SCHEMA = "chronoshft.live_canary_evidence.v4"
LIVE_CANARY_EVIDENCE_INTEGRITY_SCHEMA = (
    "chronoshft.live_canary_evidence_integrity.v1"
)
LIVE_CANARY_EVIDENCE_INTEGRITY_ALGORITHM = "HMAC-SHA256"
LIVE_CANARY_EVIDENCE_CANONICALIZATION = "SORTED-COMPACT-ASCII-JSON-V1"
LIVE_CANARY_EVIDENCE_INTEGRITY_FIELDS = frozenset(
    {
        "schema",
        "algorithm",
        "canonicalization",
        "primary_hmac_sha256",
        "supervisor_hmac_sha256",
    }
)
_LIVE_CANARY_PRIMARY_HMAC_DOMAIN = (
    b"ChronosHFT/live-canary-evidence/v4/primary\x00"
)
_LIVE_CANARY_SUPERVISOR_HMAC_DOMAIN = (
    b"ChronosHFT/live-canary-evidence/v4/supervisor\x00"
)
LIVE_CANARY_ACCOUNT_SOURCE = "GET /fapi/v3/account"
LIVE_CANARY_API_RESTRICTIONS_SOURCE = (
    "GET /sapi/v1/account/apiRestrictions"
)
LIVE_CANARY_RPI_EXCHANGE_INFO_SOURCE = "GET /fapi/v1/exchangeInfo"
LIVE_CANARY_RPI_COMMISSION_SOURCE = "GET /fapi/v1/commissionRate"
LIVE_CANARY_OPEN_ORDERS_SOURCE = "GET /fapi/v1/openOrders"
LIVE_CANARY_POSITION_RISK_SOURCE = "GET /fapi/v2/positionRisk"
LIVE_CANARY_POSITION_MODE_SOURCE = "GET /fapi/v1/positionSide/dual"
DEFAULT_LIVE_EVIDENCE_MAX_AGE_SEC = 900.0
MAX_LIVE_EVIDENCE_MAX_AGE_SEC = 3600.0
_DEPLOYMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$")
_DEPLOYMENT_PLACEHOLDER_TOKENS = (
    "EDIT-ME",
    "EDIT_ME",
    "EDIT.ME",
    "CHANGE-ME",
    "CHANGE_ME",
    "CHANGEME",
    "PLACEHOLDER",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{1,127}$")
MIN_EXTERNAL_ALERT_QUEUE_CAPACITY = 32
MAX_EXTERNAL_ALERT_QUEUE_CAPACITY = 1024
MAX_EXTERNAL_ALERT_CONNECT_TIMEOUT_SEC = 2.0
MAX_EXTERNAL_ALERT_READ_TIMEOUT_SEC = 5.0
MAX_EXTERNAL_ALERT_ATTEMPTS = 3
MAX_EXTERNAL_ALERT_RETRY_BACKOFF_SEC = 2.0
MAX_EXTERNAL_ALERT_STARTUP_PROBE_TIMEOUT_SEC = 30.0
MIN_EXTERNAL_ALERT_RECOVERY_PROBE_INTERVAL_SEC = 10.0
MAX_EXTERNAL_ALERT_RECOVERY_PROBE_INTERVAL_SEC = 300.0
MAX_EXTERNAL_ALERT_SHUTDOWN_FLUSH_TIMEOUT_SEC = 5.0
MIN_LIVE_EVIDENCE_QUEUE_CAPACITY = 1024
MAX_LIVE_EVIDENCE_QUEUE_CAPACITY = 65_536
MAX_LIVE_EVIDENCE_BATCH_RECORDS = 1024
MAX_LIVE_EVIDENCE_FSYNC_INTERVAL_SEC = 5.0
MAX_LIVE_EVIDENCE_CLOSE_TIMEOUT_SEC = 30.0
_REQUIRED_LIVE_ATTESTATIONS = (
    "deployment_host_can_reach_binance_mainnet",
    "operator_confirmed_exchange_access_allowed",
    "single_host_single_process_deployment_confirmed",
    "primary_api_withdrawals_disabled",
    "supervisor_api_withdrawals_disabled",
    "primary_api_ip_restricted",
    "supervisor_api_ip_restricted",
    "api_keys_are_distinct",
    "api_keys_same_futures_account_confirmed",
    "primary_api_futures_trading_enabled",
    "supervisor_api_futures_trading_enabled",
    "supervisor_api_emergency_permissions_confirmed",
    "credential_environment_populated_on_deployment_host",
    "exchange_open_orders_empty",
    "exchange_positions_flat",
    "legacy_state_archived",
    "fresh_state_generation_selected",
    "isolated_margin_confirmed",
    "leverage_confirmed",
    "rpi_account_permission_confirmed",
)


def _enabled(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    if isinstance(value, (int, float)):
        return value == 1
    return False


def _positive_finite(value) -> bool:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(parsed) and parsed > 0.0


def _positive_finite_value(value):
    if not _positive_finite(value):
        return None
    return float(value)


def _section(config: Mapping, key: str) -> Mapping:
    value = config.get(key, {})
    return value if isinstance(value, Mapping) else {}


def live_launch_stage(config: Mapping) -> str:
    return str(
        _section(config, "live_launch").get("stage", "") or ""
    ).strip().lower()


def _max_deployed_equity_fraction(config: Mapping) -> float:
    if live_launch_stage(config) == RPI_CALIBRATION_CANARY_STAGE:
        return MAX_CALIBRATION_DEPLOYED_EQUITY_FRACTION
    return MAX_CANARY_DEPLOYED_EQUITY_FRACTION


def _deployed_equity_fraction_label(config: Mapping) -> str:
    fraction = _max_deployed_equity_fraction(config)
    if live_launch_stage(config) == RPI_CALIBRATION_CANARY_STAGE:
        return f"{fraction:.1%}"
    return f"{fraction:.0%}"


def _model_value(
    strategy: Mapping,
    model_config: Mapping,
    field: str,
    default=None,
):
    if field in strategy:
        return strategy.get(field)
    return model_config.get(field, default)


def _uses_paper_state_path(value) -> bool:
    normalized = str(value or "").strip().replace("\\", "/").lower()
    parts = tuple(part for part in normalized.split("/") if part not in {"", "."})
    return "paper" in parts


def _raw_path_parts(value) -> tuple[str, ...]:
    normalized = str(value or "").strip().replace("\\", "/")
    return tuple(part for part in normalized.split("/") if part not in {"", "."})


def _resolved_path_identity(value, *, base_dir: str | Path | None = None):
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("path must be configured")
    if ".." in _raw_path_parts(raw):
        raise ValueError("path must not contain '..' components")

    normalized = raw.replace("\\", os.sep).replace("/", os.sep)
    candidate = Path(normalized)
    if not candidate.is_absolute():
        candidate = Path(base_dir or Path.cwd()) / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"path cannot be resolved: {exc}") from exc
    identity = os.path.normcase(os.path.normpath(str(resolved)))
    parts = tuple(os.path.normcase(part) for part in resolved.parts)
    return identity, parts


def validate_live_external_alert_config(
    config: Mapping,
    *,
    environ: Mapping[str, str] | None = None,
    base_dir: str | Path | None = None,
) -> dict[str, str]:
    """Validate the fail-closed Live webhook without returning its URL."""
    alert = _section(config, "alert")
    live_launch = _section(config, "live_launch")
    oms = _section(config, "oms")
    risk = _section(config, "risk")
    supervisor = _section(risk, "independent_supervisor")
    writer_fence = _section(oms, "single_writer_fence")
    system = _section(config, "system")
    evidence = _section(system, "evidence_recorder")
    evidence_fence = _section(evidence, "single_writer_fence")
    violations = []

    if not _enabled(alert.get("active")):
        violations.append("alert.active must be true")
    if str(alert.get("transport", "") or "").strip().lower() != (
        "https_webhook"
    ):
        violations.append("alert.transport must be 'https_webhook'")
    for field in (
        "url",
        "webhook_url",
        "telegram_token",
        "telegram_chat_id",
    ):
        if field in alert:
            violations.append(
                f"alert.{field} must not be present; configure only an "
                "environment-variable reference"
            )

    env_name = str(alert.get("webhook_url_env", "") or "").strip()
    if not _ENV_NAME_RE.fullmatch(env_name):
        violations.append(
            "alert.webhook_url_env must name a valid environment variable"
        )
    environment = os.environ if environ is None else environ
    endpoint = str(environment.get(env_name, "") or "").strip()
    if not endpoint:
        violations.append(
            "the external alert webhook environment variable must be populated"
        )
    elif not validate_https_webhook_url(endpoint):
        violations.append(
            "the external alert webhook environment value must be a valid "
            "HTTPS endpoint without userinfo or fragment"
        )

    if str(alert.get("minimum_level", "") or "").strip().upper() != "WARNING":
        violations.append(
            "alert.minimum_level must be 'WARNING' for Live"
        )
    if not _enabled(alert.get("startup_probe_required")):
        violations.append("alert.startup_probe_required must be true")
    if not _enabled(alert.get("runtime_fail_closed")):
        violations.append("alert.runtime_fail_closed must be true")
    if not _enabled(alert.get("failure_spool_fsync")):
        violations.append("alert.failure_spool_fsync must be true")

    queue_capacity = alert.get("queue_capacity")
    if (
        isinstance(queue_capacity, bool)
        or not isinstance(queue_capacity, int)
        or not (
            MIN_EXTERNAL_ALERT_QUEUE_CAPACITY
            <= queue_capacity
            <= MAX_EXTERNAL_ALERT_QUEUE_CAPACITY
        )
    ):
        violations.append(
            "alert.queue_capacity must be an integer between "
            f"{MIN_EXTERNAL_ALERT_QUEUE_CAPACITY} and "
            f"{MAX_EXTERNAL_ALERT_QUEUE_CAPACITY}"
        )

    connect_timeout = _positive_finite_value(
        alert.get("connect_timeout_sec")
    )
    if (
        connect_timeout is None
        or connect_timeout > MAX_EXTERNAL_ALERT_CONNECT_TIMEOUT_SEC
    ):
        violations.append(
            "alert.connect_timeout_sec must be positive and no more than "
            f"{MAX_EXTERNAL_ALERT_CONNECT_TIMEOUT_SEC:g}"
        )
    read_timeout = _positive_finite_value(
        alert.get("read_timeout_sec")
    )
    if (
        read_timeout is None
        or read_timeout > MAX_EXTERNAL_ALERT_READ_TIMEOUT_SEC
    ):
        violations.append(
            "alert.read_timeout_sec must be positive and no more than "
            f"{MAX_EXTERNAL_ALERT_READ_TIMEOUT_SEC:g}"
        )

    max_attempts = alert.get("max_attempts")
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= MAX_EXTERNAL_ALERT_ATTEMPTS
    ):
        violations.append(
            "alert.max_attempts must be an integer between 1 and "
            f"{MAX_EXTERNAL_ALERT_ATTEMPTS}"
        )
        parsed_attempts = None
    else:
        parsed_attempts = max_attempts

    retry_backoff = alert.get("retry_backoff_sec")
    try:
        parsed_backoff = float(retry_backoff)
    except (TypeError, ValueError):
        parsed_backoff = None
    if (
        parsed_backoff is None
        or not math.isfinite(parsed_backoff)
        or parsed_backoff < 0.0
        or parsed_backoff > MAX_EXTERNAL_ALERT_RETRY_BACKOFF_SEC
    ):
        violations.append(
            "alert.retry_backoff_sec must be finite, non-negative, and no "
            f"more than {MAX_EXTERNAL_ALERT_RETRY_BACKOFF_SEC:g}"
        )

    startup_probe_timeout = _positive_finite_value(
        alert.get("startup_probe_timeout_sec")
    )
    if (
        startup_probe_timeout is None
        or startup_probe_timeout
        > MAX_EXTERNAL_ALERT_STARTUP_PROBE_TIMEOUT_SEC
    ):
        violations.append(
            "alert.startup_probe_timeout_sec must be positive and no more "
            f"than {MAX_EXTERNAL_ALERT_STARTUP_PROBE_TIMEOUT_SEC:g}"
        )
    elif (
        parsed_attempts is not None
        and connect_timeout is not None
        and read_timeout is not None
        and parsed_backoff is not None
    ):
        worst_case_delivery_sec = parsed_attempts * (
            connect_timeout + read_timeout
        ) + parsed_backoff * sum(
            2**index for index in range(parsed_attempts - 1)
        )
        if startup_probe_timeout < worst_case_delivery_sec:
            violations.append(
                "alert.startup_probe_timeout_sec must cover the configured "
                "bounded request and retry budget"
            )

    recovery_probe_interval = _positive_finite_value(
        alert.get("recovery_probe_interval_sec")
    )
    if (
        recovery_probe_interval is None
        or recovery_probe_interval
        < MIN_EXTERNAL_ALERT_RECOVERY_PROBE_INTERVAL_SEC
        or recovery_probe_interval
        > MAX_EXTERNAL_ALERT_RECOVERY_PROBE_INTERVAL_SEC
    ):
        violations.append(
            "alert.recovery_probe_interval_sec must be between "
            f"{MIN_EXTERNAL_ALERT_RECOVERY_PROBE_INTERVAL_SEC:g} and "
            f"{MAX_EXTERNAL_ALERT_RECOVERY_PROBE_INTERVAL_SEC:g}"
        )

    shutdown_flush_timeout = _positive_finite_value(
        alert.get("shutdown_flush_timeout_sec")
    )
    if (
        shutdown_flush_timeout is None
        or shutdown_flush_timeout
        > MAX_EXTERNAL_ALERT_SHUTDOWN_FLUSH_TIMEOUT_SEC
    ):
        violations.append(
            "alert.shutdown_flush_timeout_sec must be positive and no more "
            f"than {MAX_EXTERNAL_ALERT_SHUTDOWN_FLUSH_TIMEOUT_SEC:g}"
        )

    spool_path = str(
        alert.get("failure_spool_path", "") or ""
    ).strip()
    spool_identity = ""
    spool_parts: tuple[str, ...] = ()
    if not spool_path:
        violations.append("alert.failure_spool_path must be configured")
    elif _uses_paper_state_path(spool_path):
        violations.append(
            "alert.failure_spool_path must not use Paper state"
        )
    elif not spool_path.lower().endswith(".jsonl"):
        violations.append(
            "alert.failure_spool_path must use a .jsonl file"
        )
    else:
        try:
            spool_identity, spool_parts = _resolved_path_identity(
                spool_path,
                base_dir=base_dir,
            )
        except ValueError as exc:
            violations.append(f"alert.failure_spool_path {exc}")

    deployment_id = str(
        live_launch.get("deployment_id", "") or ""
    ).strip()
    deployment_component = os.path.normcase(deployment_id)
    if (
        spool_parts
        and deployment_component
        and deployment_component not in spool_parts
    ):
        violations.append(
            "alert.failure_spool_path must contain deployment_id as a "
            "resolved path component"
        )

    if spool_identity:
        for field, raw_path in (
            ("oms.journal_path", oms.get("journal_path")),
            (
                "oms.single_writer_fence.path",
                writer_fence.get("path"),
            ),
            (
                "risk.independent_supervisor.state_path",
                supervisor.get("state_path"),
            ),
            (
                "system.evidence_recorder.path",
                evidence.get("path"),
            ),
            (
                "system.evidence_recorder.single_writer_fence.path",
                evidence_fence.get("path"),
            ),
        ):
            try:
                identity, _parts = _resolved_path_identity(
                    raw_path,
                    base_dir=base_dir,
                )
            except ValueError:
                continue
            if identity == spool_identity:
                violations.append(
                    "alert.failure_spool_path must differ from "
                    f"{field}"
                )

    if violations:
        raise ValueError(
            "unsafe external alert configuration: "
            + "; ".join(violations)
        )
    return {
        "webhook_url_env": env_name,
        "failure_spool_path": spool_identity,
    }


def validate_live_evidence_recorder_config(
    config: Mapping,
    *,
    base_dir: str | Path | None = None,
) -> dict[str, str]:
    """Validate the bounded, deployment-bound raw Live evidence writer."""
    system = _section(config, "system")
    evidence = _section(system, "evidence_recorder")
    fence = _section(evidence, "single_writer_fence")
    live_launch = _section(config, "live_launch")
    violations = []

    if not _enabled(config.get("record_data")):
        violations.append("record_data must be true for Live")
    if not _enabled(evidence.get("enabled")):
        violations.append("system.evidence_recorder.enabled must be true")

    queue_capacity = evidence.get("queue_capacity")
    if (
        isinstance(queue_capacity, bool)
        or not isinstance(queue_capacity, int)
        or not (
            MIN_LIVE_EVIDENCE_QUEUE_CAPACITY
            <= queue_capacity
            <= MAX_LIVE_EVIDENCE_QUEUE_CAPACITY
        )
    ):
        violations.append(
            "system.evidence_recorder.queue_capacity must be an integer "
            f"between {MIN_LIVE_EVIDENCE_QUEUE_CAPACITY} and "
            f"{MAX_LIVE_EVIDENCE_QUEUE_CAPACITY}"
        )
        parsed_queue_capacity = None
    else:
        parsed_queue_capacity = queue_capacity

    max_batch_records = evidence.get("max_batch_records")
    if (
        isinstance(max_batch_records, bool)
        or not isinstance(max_batch_records, int)
        or max_batch_records < 1
        or max_batch_records > MAX_LIVE_EVIDENCE_BATCH_RECORDS
        or (
            parsed_queue_capacity is not None
            and max_batch_records > parsed_queue_capacity
        )
    ):
        violations.append(
            "system.evidence_recorder.max_batch_records must be a positive "
            f"integer no more than {MAX_LIVE_EVIDENCE_BATCH_RECORDS} and "
            "no more than queue_capacity"
        )

    fsync_interval = _positive_finite_value(
        evidence.get("fsync_interval_sec")
    )
    if (
        fsync_interval is None
        or fsync_interval > MAX_LIVE_EVIDENCE_FSYNC_INTERVAL_SEC
    ):
        violations.append(
            "system.evidence_recorder.fsync_interval_sec must be positive "
            f"and no more than {MAX_LIVE_EVIDENCE_FSYNC_INTERVAL_SEC:g}"
        )
    close_timeout = _positive_finite_value(
        evidence.get("close_timeout_sec")
    )
    if (
        close_timeout is None
        or close_timeout > MAX_LIVE_EVIDENCE_CLOSE_TIMEOUT_SEC
    ):
        violations.append(
            "system.evidence_recorder.close_timeout_sec must be positive "
            f"and no more than {MAX_LIVE_EVIDENCE_CLOSE_TIMEOUT_SEC:g}"
        )

    evidence_path = str(evidence.get("path", "") or "").strip()
    evidence_identity = ""
    evidence_parts: tuple[str, ...] = ()
    if not evidence_path:
        violations.append("system.evidence_recorder.path must be configured")
    elif _uses_paper_state_path(evidence_path):
        violations.append(
            "system.evidence_recorder.path must not use Paper state"
        )
    elif not evidence_path.lower().endswith(".jsonl"):
        violations.append(
            "system.evidence_recorder.path must use a .jsonl file"
        )
    else:
        try:
            evidence_identity, evidence_parts = _resolved_path_identity(
                evidence_path,
                base_dir=base_dir,
            )
        except ValueError as exc:
            violations.append(f"system.evidence_recorder.path {exc}")

    if not _enabled(fence.get("enabled")):
        violations.append(
            "system.evidence_recorder.single_writer_fence.enabled must be true"
        )
    fence_path = str(fence.get("path", "") or "").strip()
    fence_identity = ""
    if not fence_path:
        violations.append(
            "system.evidence_recorder.single_writer_fence.path must be "
            "configured"
        )
    elif _uses_paper_state_path(fence_path):
        violations.append(
            "system.evidence_recorder.single_writer_fence.path must not use "
            "Paper state"
        )
    else:
        try:
            fence_identity, _ = _resolved_path_identity(
                fence_path,
                base_dir=base_dir,
            )
        except ValueError as exc:
            violations.append(
                "system.evidence_recorder.single_writer_fence.path "
                f"{exc}"
            )

    if evidence_identity and fence_identity:
        expected_fence, _ = _resolved_path_identity(
            f"{evidence_path}.lock",
            base_dir=base_dir,
        )
        if fence_identity != expected_fence:
            violations.append(
                "system.evidence_recorder.single_writer_fence.path must "
                "resolve to system.evidence_recorder.path + '.lock'"
            )

    deployment_id = str(
        live_launch.get("deployment_id", "") or ""
    ).strip()
    if (
        evidence_parts
        and deployment_id
        and os.path.normcase(deployment_id) not in evidence_parts
    ):
        violations.append(
            "system.evidence_recorder.path must contain deployment_id as a "
            "resolved path component"
        )

    if violations:
        raise ValueError(
            "unsafe Live evidence recorder configuration: "
            + "; ".join(violations)
        )
    return {
        "path": evidence_identity,
        "single_writer_fence_path": fence_identity,
    }


def validate_live_state_path_bindings(
    config: Mapping,
    *,
    base_dir: str | Path | None = None,
) -> dict[str, str]:
    """Resolve and bind all durable Live state files to one deployment."""
    live_launch = _section(config, "live_launch")
    oms = _section(config, "oms")
    risk = _section(config, "risk")
    supervisor = _section(risk, "independent_supervisor")
    writer_fence = _section(oms, "single_writer_fence")
    system = _section(config, "system")
    evidence = _section(system, "evidence_recorder")
    evidence_fence = _section(evidence, "single_writer_fence")
    admin_control = _section(system, "admin_control")
    alert = _section(config, "alert")
    deployment_id = str(
        live_launch.get("deployment_id", "") or ""
    ).strip()
    if not _DEPLOYMENT_ID_RE.fullmatch(deployment_id):
        raise ValueError(
            "live_launch.deployment_id must be 6-128 path-safe characters"
        )

    raw_paths = {
        "oms.journal_path": oms.get("journal_path"),
        "oms.single_writer_fence.path": writer_fence.get("path"),
        "risk.independent_supervisor.state_path": supervisor.get("state_path"),
        "system.evidence_recorder.path": evidence.get("path"),
        "system.evidence_recorder.single_writer_fence.path": (
            evidence_fence.get("path")
        ),
        "system.admin_control.path": admin_control.get("path"),
        "alert.failure_spool_path": alert.get("failure_spool_path"),
    }
    identities: dict[str, str] = {}
    resolved_parts: dict[str, tuple[str, ...]] = {}
    for field, raw_path in raw_paths.items():
        try:
            identity, parts = _resolved_path_identity(
                raw_path,
                base_dir=base_dir,
            )
        except ValueError as exc:
            raise ValueError(f"{field} {exc}") from exc
        identities[field] = identity
        resolved_parts[field] = parts

    deployment_component = os.path.normcase(deployment_id)
    unbound = [
        field
        for field, parts in resolved_parts.items()
        if deployment_component not in parts
    ]
    if unbound:
        raise ValueError(
            "state paths must contain deployment_id as a resolved path "
            "component: " + ", ".join(unbound)
        )

    journal_raw = str(raw_paths["oms.journal_path"] or "").strip()
    expected_fence, _ = _resolved_path_identity(
        f"{journal_raw}.lock",
        base_dir=base_dir,
    )
    if identities["oms.single_writer_fence.path"] != expected_fence:
        raise ValueError(
            "oms.single_writer_fence.path must resolve to "
            "oms.journal_path + '.lock'"
        )

    evidence_raw = str(
        raw_paths["system.evidence_recorder.path"] or ""
    ).strip()
    expected_evidence_fence, _ = _resolved_path_identity(
        f"{evidence_raw}.lock",
        base_dir=base_dir,
    )
    if (
        identities[
            "system.evidence_recorder.single_writer_fence.path"
        ]
        != expected_evidence_fence
    ):
        raise ValueError(
            "system.evidence_recorder.single_writer_fence.path must resolve "
            "to system.evidence_recorder.path + '.lock'"
        )

    if len(set(identities.values())) != len(identities):
        raise ValueError(
            "all durable Live state, journal, fence, and alert spool paths "
            "must resolve to different files"
        )
    return identities


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON number {value!r} is not allowed")


def _reject_duplicate_json_keys(pairs):
    payload = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate JSON object keys are not allowed")
        payload[key] = value
    return payload


def _read_json_object(path: Path, label: str) -> dict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(
                handle,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_json_keys,
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label} at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _parse_utc_timestamp(value, field: str) -> datetime:
    normalized = str(value or "").strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _effective_now_utc(now_utc: datetime | None) -> datetime:
    effective = now_utc or datetime.now(timezone.utc)
    if effective.tzinfo is None:
        return effective.replace(tzinfo=timezone.utc)
    return effective.astimezone(timezone.utc)


def _live_evidence_max_age(live_launch: Mapping) -> float:
    value = live_launch.get(
        "offline_evidence_max_age_sec",
        DEFAULT_LIVE_EVIDENCE_MAX_AGE_SEC,
    )
    parsed = _positive_finite_value(value)
    if parsed is None or parsed > MAX_LIVE_EVIDENCE_MAX_AGE_SEC:
        raise ValueError(
            "live_launch.offline_evidence_max_age_sec must be greater than "
            f"0 and no more than {MAX_LIVE_EVIDENCE_MAX_AGE_SEC:g}"
        )
    return parsed


def _validate_fresh_capture(
    payload: Mapping,
    *,
    field_prefix: str,
    now_utc: datetime,
    max_age_sec: float,
) -> None:
    captured_at = _parse_utc_timestamp(
        payload.get("captured_at_utc"),
        f"{field_prefix}.captured_at_utc",
    )
    age_sec = (now_utc - captured_at).total_seconds()
    if age_sec < -300.0:
        raise ValueError(
            f"{field_prefix}.captured_at_utc is more than 300 seconds "
            "in the future"
        )
    if age_sec > max_age_sec:
        raise ValueError(
            f"{field_prefix} is stale ({age_sec:.0f}s > {max_age_sec:.0f}s)"
        )


def _finite_decimal(value, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    return parsed


def _configured_symbol(config: Mapping) -> str:
    symbols = config.get("symbols")
    if not isinstance(symbols, (list, tuple)) or len(symbols) != 1:
        return ""
    return str(symbols[0] or "").strip().upper()


def _path_is_reparse_or_symlink(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError(f"cannot inspect local evidence path {path}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(file_attributes & reparse_flag)


def _path_uses_remote_drive(path: Path) -> bool:
    if os.name != "nt":
        return False
    anchor = path.anchor
    if not anchor:
        return False
    try:
        import ctypes

        get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
        get_drive_type.argtypes = [ctypes.c_wchar_p]
        get_drive_type.restype = ctypes.c_uint
        return get_drive_type(anchor) == 4  # DRIVE_REMOTE
    except (AttributeError, OSError):
        return True


def _require_local_path_chain(path: Path, *, label: str) -> None:
    absolute = Path(os.path.abspath(path))
    if _path_uses_remote_drive(absolute):
        raise ValueError(f"{label} must not use a mapped network drive")
    existing: list[Path] = []
    current = absolute
    while True:
        if os.path.lexists(current):
            existing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    for component in reversed(existing):
        if _path_is_reparse_or_symlink(component):
            raise ValueError(
                f"{label} must not traverse a symlink or reparse point"
            )


def _reject_nonlocal_path_syntax(raw: str, *, label: str) -> None:
    if os.name != "nt" and raw.startswith("/") and not raw.startswith("//"):
        return
    windows_path = raw.replace("/", "\\")
    if windows_path.startswith(("\\\\", "\\??\\")):
        raise ValueError(f"{label} must not use UNC or device storage")
    drive, tail = ntpath.splitdrive(windows_path)
    if drive and not ntpath.isabs(windows_path):
        raise ValueError(f"{label} must not be drive-relative")
    if not drive and ntpath.isabs(windows_path):
        raise ValueError(f"{label} must not be root-relative")
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
    for component in (part for part in tail.split("\\") if part):
        if ":" in component:
            raise ValueError(f"{label} must not use alternate data streams")
        if component != component.rstrip(" ."):
            raise ValueError(f"{label} must not use trailing dots or spaces")
        device_name = component.split(".", 1)[0].rstrip(" ").upper()
        if device_name in reserved:
            raise ValueError(f"{label} must not use Windows device names")


def validate_live_canary_evidence_destination(path: Path) -> None:
    """Reject non-local or link-backed evidence output destinations."""
    absolute = Path(os.path.abspath(path))
    _require_local_path_chain(
        absolute.parent,
        label="live canary evidence parent",
    )
    if not absolute.parent.is_dir():
        raise ValueError("live canary evidence parent must be a directory")
    if os.path.lexists(absolute):
        if _path_is_reparse_or_symlink(absolute):
            raise ValueError(
                "live canary evidence file must not be a symlink or reparse point"
            )
        if not absolute.is_file():
            raise ValueError("live canary evidence path must be a regular file")


def resolve_live_canary_evidence_path(
    config: Mapping,
    *,
    config_path: str | Path,
    require_existing_file: bool = True,
) -> Path:
    """Resolve evidence to a local regular file inside the config directory."""
    live_launch = _section(config, "live_launch")
    raw = str(live_launch.get("offline_evidence_path", "") or "").strip()
    if not raw:
        raise ValueError("live_launch.offline_evidence_path must be configured")
    if ".." in _raw_path_parts(raw):
        raise ValueError(
            "live_launch.offline_evidence_path must not contain '..' components"
        )
    _reject_nonlocal_path_syntax(
        raw,
        label="live_launch.offline_evidence_path",
    )

    raw_config_path = os.fspath(config_path)
    _reject_nonlocal_path_syntax(
        raw_config_path,
        label="Live canary config path",
    )
    config_file = Path(os.path.abspath(raw_config_path))
    _require_local_path_chain(config_file, label="Live canary config path")
    if not config_file.is_file():
        raise ValueError("Live canary config path must be an existing regular file")
    try:
        config_directory = config_file.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("Live canary config directory cannot be resolved") from exc

    normalized = raw.replace("\\", os.sep).replace("/", os.sep)
    candidate = Path(normalized)
    if not candidate.is_absolute():
        candidate = config_directory / candidate
    _require_local_path_chain(
        candidate.parent,
        label="live canary evidence parent",
    )
    try:
        parent = candidate.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("live canary evidence parent cannot be resolved") from exc
    if not parent.is_dir():
        raise ValueError("live canary evidence parent must be a directory")
    output_path = parent / candidate.name
    try:
        within_config_tree = (
            os.path.commonpath(
                (
                    os.path.normcase(str(config_directory)),
                    os.path.normcase(str(output_path)),
                )
            )
            == os.path.normcase(str(config_directory))
        )
    except (OSError, ValueError):
        within_config_tree = False
    if not within_config_tree:
        raise ValueError(
            "live_launch.offline_evidence_path must stay within the config directory"
        )
    validate_live_canary_evidence_destination(output_path)
    if require_existing_file and not output_path.is_file():
        raise ValueError("live canary evidence path must be an existing regular file")
    return output_path


def _canonical_live_canary_evidence_bytes(evidence: Mapping) -> bytes:
    if not isinstance(evidence, Mapping):
        raise ValueError("live canary evidence must be a JSON object")
    payload = dict(evidence)
    payload.pop("integrity", None)
    try:
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "live canary evidence is not canonical strict JSON"
        ) from exc
    return canonical.encode("ascii")


def build_live_canary_evidence_integrity(
    evidence: Mapping,
    *,
    primary_api_secret: str,
    supervisor_api_secret: str,
) -> dict[str, str]:
    """Build role-separated HMAC tags without retaining either API secret."""
    primary_secret = str(primary_api_secret or "")
    supervisor_secret = str(supervisor_api_secret or "")
    if not primary_secret or not supervisor_secret:
        raise ValueError("both Live evidence API secrets are required")
    canonical = _canonical_live_canary_evidence_bytes(evidence)
    return {
        "schema": LIVE_CANARY_EVIDENCE_INTEGRITY_SCHEMA,
        "algorithm": LIVE_CANARY_EVIDENCE_INTEGRITY_ALGORITHM,
        "canonicalization": LIVE_CANARY_EVIDENCE_CANONICALIZATION,
        "primary_hmac_sha256": hmac.new(
            primary_secret.encode("utf-8"),
            _LIVE_CANARY_PRIMARY_HMAC_DOMAIN + canonical,
            hashlib.sha256,
        ).hexdigest(),
        "supervisor_hmac_sha256": hmac.new(
            supervisor_secret.encode("utf-8"),
            _LIVE_CANARY_SUPERVISOR_HMAC_DOMAIN + canonical,
            hashlib.sha256,
        ).hexdigest(),
    }


def sign_live_canary_evidence(
    evidence: Mapping,
    *,
    primary_api_secret: str,
    supervisor_api_secret: str,
) -> dict:
    signed = dict(evidence)
    signed.pop("integrity", None)
    signed["integrity"] = build_live_canary_evidence_integrity(
        signed,
        primary_api_secret=primary_api_secret,
        supervisor_api_secret=supervisor_api_secret,
    )
    return signed


def validate_live_canary_evidence_integrity(
    evidence: Mapping,
    *,
    primary_api_secret: str | None = None,
    supervisor_api_secret: str | None = None,
    require_secret_binding: bool = True,
) -> Mapping:
    """Validate the v4 integrity envelope, optionally without offline secrets."""
    integrity = evidence.get("integrity") if isinstance(evidence, Mapping) else None
    if not isinstance(integrity, Mapping):
        raise ValueError("live canary evidence integrity must be an object")
    if frozenset(integrity) != LIVE_CANARY_EVIDENCE_INTEGRITY_FIELDS:
        raise ValueError("live canary evidence integrity fields are invalid")
    expected_metadata = {
        "schema": LIVE_CANARY_EVIDENCE_INTEGRITY_SCHEMA,
        "algorithm": LIVE_CANARY_EVIDENCE_INTEGRITY_ALGORITHM,
        "canonicalization": LIVE_CANARY_EVIDENCE_CANONICALIZATION,
    }
    for field, expected in expected_metadata.items():
        if integrity.get(field) != expected:
            raise ValueError(f"live canary evidence integrity {field} is invalid")
    for field in ("primary_hmac_sha256", "supervisor_hmac_sha256"):
        tag = str(integrity.get(field, "") or "")
        if not _SHA256_RE.fullmatch(tag):
            raise ValueError(f"live canary evidence integrity {field} is invalid")

    primary_secret = str(primary_api_secret or "")
    supervisor_secret = str(supervisor_api_secret or "")
    if require_secret_binding and (not primary_secret or not supervisor_secret):
        raise ValueError(
            "both resolved API secrets are required to verify Live evidence"
        )
    if primary_secret or supervisor_secret:
        if not primary_secret or not supervisor_secret:
            raise ValueError(
                "both resolved API secrets are required to verify Live evidence"
            )
        expected = build_live_canary_evidence_integrity(
            evidence,
            primary_api_secret=primary_secret,
            supervisor_api_secret=supervisor_secret,
        )
        for field, role in (
            ("primary_hmac_sha256", "primary"),
            ("supervisor_hmac_sha256", "supervisor"),
        ):
            if not hmac.compare_digest(
                str(integrity.get(field, "") or ""),
                expected[field],
            ):
                raise ValueError(
                    f"live canary evidence {role} integrity verification failed"
                )
    return integrity


def _validate_live_evidence_binding(
    config: Mapping,
    evidence: Mapping,
) -> None:
    live_launch = _section(config, "live_launch")
    deployment_id = str(
        live_launch.get("deployment_id", "") or ""
    ).strip()
    symbol = _configured_symbol(config)
    if evidence.get("schema") != LIVE_CANARY_EVIDENCE_SCHEMA:
        raise ValueError(
            "live canary evidence schema must be "
            f"{LIVE_CANARY_EVIDENCE_SCHEMA!r}"
        )
    if str(evidence.get("deployment_id", "") or "").strip() != deployment_id:
        raise ValueError(
            "live canary evidence deployment_id must match the config"
        )
    if str(evidence.get("symbol", "") or "").strip().upper() != symbol:
        raise ValueError("live canary evidence symbol must match the config")


def _validate_live_attestations(evidence: Mapping) -> None:
    attestations = evidence.get("operator_attestations")
    if not isinstance(attestations, Mapping):
        raise ValueError(
            "live canary evidence operator_attestations must be an object"
        )
    missing = [
        field
        for field in _REQUIRED_LIVE_ATTESTATIONS
        if attestations.get(field) is not True
    ]
    if missing:
        raise ValueError(
            "live canary evidence attestations are not true: "
            + ", ".join(missing)
        )


def validate_live_account_equity_truth(
    config: Mapping,
    account_snapshot: Mapping,
) -> Mapping:
    """Bind canary caps to a fresh account snapshot supplied by the caller."""
    if not isinstance(account_snapshot, Mapping):
        raise ValueError("account equity truth must be an object")
    if account_snapshot.get("canTrade") is not True:
        raise ValueError("account_truth.canTrade must be true")
    if account_snapshot.get("multiAssetsMargin") is not False:
        raise ValueError("account_truth.multiAssetsMargin must be false")

    live_launch = _section(config, "live_launch")
    wallet = _finite_decimal(
        account_snapshot.get("totalWalletBalance"),
        "account_truth.totalWalletBalance",
    )
    margin = _finite_decimal(
        account_snapshot.get("totalMarginBalance"),
        "account_truth.totalMarginBalance",
    )
    available = _finite_decimal(
        account_snapshot.get("availableBalance"),
        "account_truth.availableBalance",
    )
    if wallet <= 0 or margin <= 0 or available < 0:
        raise ValueError(
            "account truth balances must be positive, with nonnegative "
            "availableBalance"
        )

    declared = _finite_decimal(
        live_launch.get("declared_account_equity_usdt"),
        "live_launch.declared_account_equity_usdt",
    )
    deployed_cap = _finite_decimal(
        live_launch.get("max_deployed_capital_usdt"),
        "live_launch.max_deployed_capital_usdt",
    )
    conservative_equity = min(wallet, margin)
    if declared > conservative_equity:
        raise ValueError(
            "live_launch.declared_account_equity_usdt must not exceed fresh "
            "account equity truth"
        )
    deployed_equity_fraction = _max_deployed_equity_fraction(config)
    if deployed_cap > (
        conservative_equity * Decimal(str(deployed_equity_fraction))
    ):
        raise ValueError(
            "live_launch.max_deployed_capital_usdt must not exceed "
            f"{_deployed_equity_fraction_label(config)} of fresh account "
            "equity truth"
        )
    if deployed_cap > available:
        raise ValueError(
            "live_launch.max_deployed_capital_usdt must not exceed fresh "
            "account availableBalance"
        )
    return account_snapshot


def validate_live_flat_start_truth(
    positions: object,
    open_orders: object,
) -> dict[str, int]:
    """Require an exact account-wide flat/no-orders snapshot before canary."""
    if not isinstance(positions, (list, tuple)):
        raise ValueError("flat-start positions truth must be a list")
    if not isinstance(open_orders, (list, tuple)):
        raise ValueError("flat-start open-orders truth must be a list")
    if open_orders:
        raise ValueError(
            "live canary flat-start requires zero exchange open orders"
        )

    nonzero_symbols = []
    for index, position in enumerate(positions):
        if not isinstance(position, Mapping):
            raise ValueError(
                f"flat-start position row {index} must be an object"
            )
        symbol = str(position.get("symbol", "") or "").strip().upper()
        if not symbol:
            raise ValueError(
                f"flat-start position row {index} requires symbol"
            )
        amount = _finite_decimal(
            position.get("positionAmt"),
            f"flat_start.positions[{index}].positionAmt",
        )
        if amount != Decimal(0):
            nonzero_symbols.append(symbol)
    if nonzero_symbols:
        raise ValueError(
            "live canary flat-start requires exact zero positions: "
            + ", ".join(sorted(set(nonzero_symbols)))
        )
    return {
        "position_row_count": len(positions),
        "open_order_count": 0,
    }


def _validate_evidence_key_fingerprint(
    payload: Mapping,
    *,
    field_prefix: str,
    api_key: str | None,
    require_api_key_binding: bool,
) -> str:
    fingerprint = str(
        payload.get("api_key_fingerprint_sha256", "") or ""
    ).strip()
    if not _SHA256_RE.fullmatch(fingerprint):
        raise ValueError(
            f"{field_prefix}.api_key_fingerprint_sha256 must be a lowercase "
            "SHA-256 digest"
        )
    key = str(api_key or "")
    if require_api_key_binding and not key:
        raise ValueError(
            f"{field_prefix} API key is required to bind exchange truth"
        )
    if key:
        expected = hashlib.sha256(key.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(fingerprint, expected):
            raise ValueError(
                f"{field_prefix} was captured with a different primary API key"
            )
    return fingerprint


def validate_live_flat_start_evidence(
    config: Mapping,
    evidence: Mapping,
    *,
    now_utc: datetime | None = None,
    primary_api_key: str | None = None,
    require_api_key_binding: bool = True,
) -> dict[str, int]:
    """Validate fresh, primary-key-bound raw account-wide flat-start truth."""
    truth = evidence.get("flat_start_truth")
    if not isinstance(truth, Mapping):
        raise ValueError(
            "live canary evidence flat_start_truth must be an object"
        )
    if truth.get("open_orders_source") != LIVE_CANARY_OPEN_ORDERS_SOURCE:
        raise ValueError(
            "flat_start_truth.open_orders_source must be "
            f"{LIVE_CANARY_OPEN_ORDERS_SOURCE!r}"
        )
    if truth.get("positions_source") != LIVE_CANARY_POSITION_RISK_SOURCE:
        raise ValueError(
            "flat_start_truth.positions_source must be "
            f"{LIVE_CANARY_POSITION_RISK_SOURCE!r}"
        )
    effective_now = _effective_now_utc(now_utc)
    _validate_fresh_capture(
        truth,
        field_prefix="flat_start_truth",
        now_utc=effective_now,
        max_age_sec=_live_evidence_max_age(_section(config, "live_launch")),
    )
    _validate_evidence_key_fingerprint(
        truth,
        field_prefix="flat_start_truth",
        api_key=primary_api_key,
        require_api_key_binding=require_api_key_binding,
    )
    return validate_live_flat_start_truth(
        truth.get("positions"),
        truth.get("open_orders"),
    )


def _flatten_exchange_permissions(value: object) -> frozenset[str]:
    pending = [value]
    permissions: set[str] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            permissions.add(current.strip().upper())
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
    return frozenset(item for item in permissions if item)


def _exchange_min_notional(exchange_symbol: Mapping) -> Decimal:
    filters = exchange_symbol.get("filters")
    if not isinstance(filters, (list, tuple)):
        raise ValueError(
            "symbol_configuration_truth.exchange_symbol.filters must be a list"
        )
    candidates: list[Decimal] = []
    for index, row in enumerate(filters):
        if not isinstance(row, Mapping):
            raise ValueError(
                "symbol_configuration_truth.exchange_symbol.filters"
                f"[{index}] must be an object"
            )
        if str(row.get("filterType", "") or "").strip().upper() not in {
            "MIN_NOTIONAL",
            "NOTIONAL",
        }:
            continue
        raw = row.get("notional", row.get("minNotional"))
        value = _finite_decimal(
            raw,
            "symbol_configuration_truth.exchange_symbol minimum notional",
        )
        if value < 0:
            raise ValueError(
                "symbol_configuration_truth exchange minimum notional must "
                "not be negative"
            )
        candidates.append(value)
    if not candidates:
        raise ValueError(
            "symbol_configuration_truth exchange_symbol has no minimum "
            "notional filter"
        )
    return max(candidates)


def validate_live_symbol_configuration_evidence(
    config: Mapping,
    evidence: Mapping,
    *,
    now_utc: datetime | None = None,
    primary_api_key: str | None = None,
    require_api_key_binding: bool = True,
) -> dict[str, object]:
    """Validate raw one-way, isolated, 1x and minimum-notional truth."""
    truth = evidence.get("symbol_configuration_truth")
    if not isinstance(truth, Mapping):
        raise ValueError(
            "live canary evidence symbol_configuration_truth must be an object"
        )
    expected_sources = {
        "position_mode_source": LIVE_CANARY_POSITION_MODE_SOURCE,
        "position_risk_source": LIVE_CANARY_POSITION_RISK_SOURCE,
        "exchange_info_source": LIVE_CANARY_RPI_EXCHANGE_INFO_SOURCE,
    }
    for field, expected in expected_sources.items():
        if truth.get(field) != expected:
            raise ValueError(
                f"symbol_configuration_truth.{field} must be {expected!r}"
            )
    effective_now = _effective_now_utc(now_utc)
    _validate_fresh_capture(
        truth,
        field_prefix="symbol_configuration_truth",
        now_utc=effective_now,
        max_age_sec=_live_evidence_max_age(_section(config, "live_launch")),
    )
    _validate_evidence_key_fingerprint(
        truth,
        field_prefix="symbol_configuration_truth",
        api_key=primary_api_key,
        require_api_key_binding=require_api_key_binding,
    )

    mode = truth.get("position_mode")
    if not isinstance(mode, Mapping) or mode.get("dualSidePosition") is not False:
        raise ValueError(
            "symbol_configuration_truth.position_mode must show ONE_WAY "
            "(dualSidePosition=false)"
        )

    symbol = _configured_symbol(config)
    rows = truth.get("symbol_position_rows")
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ValueError(
            "symbol_configuration_truth.symbol_position_rows must contain "
            "the configured symbol"
        )
    matching_rows = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get("symbol", "") or "").strip().upper() == symbol
    ]
    if len(matching_rows) != 1:
        raise ValueError(
            "symbol_configuration_truth must contain exactly one ONE_WAY "
            "position row for the configured symbol"
        )
    row = matching_rows[0]
    if str(row.get("positionSide", "") or "").strip().upper() != "BOTH":
        raise ValueError(
            "symbol_configuration_truth configured symbol must use "
            "positionSide=BOTH"
        )
    expected_margin = str(
        _section(config, "account").get("margin_type", "") or ""
    ).strip().upper()
    actual_margin = str(row.get("marginType", "") or "").strip().upper()
    if actual_margin != expected_margin or actual_margin != "ISOLATED":
        raise ValueError(
            "symbol_configuration_truth configured symbol must use ISOLATED "
            "margin matching account.margin_type"
        )
    expected_leverage = _finite_decimal(
        _section(config, "account").get("leverage"),
        "account.leverage",
    )
    actual_leverage = _finite_decimal(
        row.get("leverage"),
        "symbol_configuration_truth.symbol_position_rows[0].leverage",
    )
    if actual_leverage != expected_leverage or actual_leverage != Decimal(1):
        raise ValueError(
            "symbol_configuration_truth configured symbol leverage must "
            "match account.leverage=1"
        )
    if _finite_decimal(
        row.get("positionAmt"),
        "symbol_configuration_truth.symbol_position_rows[0].positionAmt",
    ) != Decimal(0):
        raise ValueError(
            "symbol_configuration_truth configured symbol position must be flat"
        )

    exchange_symbol = truth.get("exchange_symbol")
    if not isinstance(exchange_symbol, Mapping):
        raise ValueError(
            "symbol_configuration_truth.exchange_symbol must be an object"
        )
    if (
        str(exchange_symbol.get("symbol", "") or "").strip().upper()
        != symbol
    ):
        raise ValueError(
            "symbol_configuration_truth.exchange_symbol must match the config"
        )
    if str(exchange_symbol.get("status", "") or "").strip().upper() != "TRADING":
        raise ValueError(
            "symbol_configuration_truth.exchange_symbol must be TRADING"
        )
    permissions = _flatten_exchange_permissions(
        exchange_symbol.get("permissionSets", [])
    )
    if "RPI" not in permissions:
        raise ValueError(
            "symbol_configuration_truth.exchange_symbol must explicitly "
            "include RPI permission"
        )
    min_notional = _exchange_min_notional(exchange_symbol)
    target_notional = _finite_decimal(
        _section(config, "strategy").get("target_order_notional"),
        "strategy.target_order_notional",
    )
    if target_notional < min_notional:
        raise ValueError(
            "strategy.target_order_notional is below the exchange minimum "
            f"notional ({target_notional} < {min_notional})"
        )
    return {
        "position_mode": "ONE_WAY",
        "margin_type": actual_margin,
        "leverage": actual_leverage,
        "minimum_notional": min_notional,
    }


def _validate_account_equity_evidence_payload(
    config: Mapping,
    account_truth: Mapping,
    *,
    field_prefix: str,
    now_utc: datetime | None = None,
    api_key: str | None = None,
    require_api_key_binding: bool = True,
) -> Mapping:
    if not isinstance(account_truth, Mapping):
        raise ValueError(
            f"live canary evidence {field_prefix} must be an object"
        )
    if account_truth.get("source") != LIVE_CANARY_ACCOUNT_SOURCE:
        raise ValueError(
            f"{field_prefix}.source must be "
            f"{LIVE_CANARY_ACCOUNT_SOURCE!r}"
        )
    if str(account_truth.get("asset", "") or "").strip().upper() != "USDT":
        raise ValueError(f"{field_prefix}.asset must be 'USDT'")

    live_launch = _section(config, "live_launch")
    effective_now = _effective_now_utc(now_utc)
    max_age_sec = _live_evidence_max_age(live_launch)
    _validate_fresh_capture(
        account_truth,
        field_prefix=field_prefix,
        now_utc=effective_now,
        max_age_sec=max_age_sec,
    )

    fingerprint = str(
        account_truth.get("api_key_fingerprint_sha256", "") or ""
    ).strip()
    if not _SHA256_RE.fullmatch(fingerprint):
        raise ValueError(
            f"{field_prefix}.api_key_fingerprint_sha256 must be a lowercase "
            "SHA-256 digest"
        )
    key = str(api_key or "")
    if require_api_key_binding and not key:
        raise ValueError(
            f"{field_prefix} API key is required to bind local account truth"
        )
    if key:
        expected = hashlib.sha256(key.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(fingerprint, expected):
            key_owner = (
                "primary"
                if field_prefix == "account_truth"
                else "supervisor"
            )
            raise ValueError(
                "account truth was captured with a different "
                f"{key_owner} API key"
            )
    return validate_live_account_equity_truth(config, account_truth)


def validate_live_account_equity_evidence(
    config: Mapping,
    evidence: Mapping,
    *,
    now_utc: datetime | None = None,
    primary_api_key: str | None = None,
    require_api_key_binding: bool = True,
) -> Mapping:
    """Validate provenance/freshness, then apply the primary equity guard."""
    return _validate_account_equity_evidence_payload(
        config,
        evidence.get("account_truth"),
        field_prefix="account_truth",
        now_utc=now_utc,
        api_key=primary_api_key,
        require_api_key_binding=require_api_key_binding,
    )


def validate_live_dual_key_account_evidence(
    config: Mapping,
    evidence: Mapping,
    *,
    now_utc: datetime | None = None,
    primary_api_key: str | None = None,
    supervisor_api_key: str | None = None,
    require_api_key_binding: bool = True,
) -> dict[str, Mapping]:
    """Bind fresh, flat-start account truth to both independent API keys."""
    primary = validate_live_account_equity_evidence(
        config,
        evidence,
        now_utc=now_utc,
        primary_api_key=primary_api_key,
        require_api_key_binding=require_api_key_binding,
    )
    supervisor = _validate_account_equity_evidence_payload(
        config,
        evidence.get("supervisor_account_truth"),
        field_prefix="supervisor_account_truth",
        now_utc=now_utc,
        api_key=supervisor_api_key,
        require_api_key_binding=require_api_key_binding,
    )
    if (
        str(primary.get("api_key_fingerprint_sha256", "") or "")
        == str(supervisor.get("api_key_fingerprint_sha256", "") or "")
    ):
        raise ValueError(
            "primary and supervisor account truth must use different API keys"
        )

    balance_fields = (
        "totalWalletBalance",
        "totalMarginBalance",
        "availableBalance",
    )
    mismatched = [
        field
        for field in balance_fields
        if _finite_decimal(primary.get(field), f"account_truth.{field}")
        != _finite_decimal(
            supervisor.get(field),
            f"supervisor_account_truth.{field}",
        )
    ]
    if mismatched:
        raise ValueError(
            "primary and supervisor account truth do not describe the same "
            "stable flat-start account snapshot: " + ", ".join(mismatched)
        )
    return {"primary": primary, "supervisor": supervisor}


def _validate_api_restriction_payload(
    payload: Mapping,
    *,
    field_prefix: str,
    now_utc: datetime,
    max_age_sec: float,
    api_key: str | None,
    require_api_key_binding: bool,
) -> Mapping:
    if not isinstance(payload, Mapping):
        raise ValueError(
            f"live canary evidence {field_prefix} must be an object"
        )
    if payload.get("source") != LIVE_CANARY_API_RESTRICTIONS_SOURCE:
        raise ValueError(
            f"{field_prefix}.source must be "
            f"{LIVE_CANARY_API_RESTRICTIONS_SOURCE!r}"
        )
    _validate_fresh_capture(
        payload,
        field_prefix=field_prefix,
        now_utc=now_utc,
        max_age_sec=max_age_sec,
    )
    fingerprint = str(
        payload.get("api_key_fingerprint_sha256", "") or ""
    ).strip()
    if not _SHA256_RE.fullmatch(fingerprint):
        raise ValueError(
            f"{field_prefix}.api_key_fingerprint_sha256 must be a lowercase "
            "SHA-256 digest"
        )
    key = str(api_key or "")
    if require_api_key_binding and not key:
        raise ValueError(
            f"{field_prefix} API key is required to bind permission truth"
        )
    if key:
        expected = hashlib.sha256(key.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(fingerprint, expected):
            key_owner = (
                "primary"
                if field_prefix == "primary_api_restrictions"
                else "supervisor"
            )
            raise ValueError(
                f"{field_prefix} was captured with a different "
                f"{key_owner} API key"
            )

    expected_flags = {
        "enableReading": True,
        "enableFutures": True,
        "enableWithdrawals": False,
        "ipRestrict": True,
    }
    mismatched = [
        f"{field}={payload.get(field)!r}"
        for field, expected_value in expected_flags.items()
        if payload.get(field) is not expected_value
    ]
    if mismatched:
        raise ValueError(
            f"{field_prefix} has unsafe API permissions: "
            + ", ".join(mismatched)
        )
    return payload


def validate_live_api_restrictions_evidence(
    config: Mapping,
    evidence: Mapping,
    *,
    now_utc: datetime | None = None,
    primary_api_key: str | None = None,
    supervisor_api_key: str | None = None,
    require_api_key_binding: bool = True,
) -> dict[str, Mapping]:
    """Validate read-only API-key permission evidence for both safety planes."""
    effective_now = _effective_now_utc(now_utc)
    max_age_sec = _live_evidence_max_age(_section(config, "live_launch"))
    primary = _validate_api_restriction_payload(
        evidence.get("primary_api_restrictions"),
        field_prefix="primary_api_restrictions",
        now_utc=effective_now,
        max_age_sec=max_age_sec,
        api_key=primary_api_key,
        require_api_key_binding=require_api_key_binding,
    )
    supervisor = _validate_api_restriction_payload(
        evidence.get("supervisor_api_restrictions"),
        field_prefix="supervisor_api_restrictions",
        now_utc=effective_now,
        max_age_sec=max_age_sec,
        api_key=supervisor_api_key,
        require_api_key_binding=require_api_key_binding,
    )
    if (
        str(primary.get("api_key_fingerprint_sha256", "") or "")
        == str(supervisor.get("api_key_fingerprint_sha256", "") or "")
    ):
        raise ValueError(
            "primary and supervisor API restriction truth must use "
            "different API keys"
        )
    return {"primary": primary, "supervisor": supervisor}


def _validate_live_rpi_truth(
    config: Mapping,
    evidence: Mapping,
    *,
    now_utc: datetime,
) -> None:
    truth = evidence.get("rpi_truth")
    if not isinstance(truth, Mapping):
        raise ValueError("live canary evidence rpi_truth must be an object")
    if truth.get("exchange_info_source") != (
        LIVE_CANARY_RPI_EXCHANGE_INFO_SOURCE
    ):
        raise ValueError(
            "rpi_truth.exchange_info_source must be "
            f"{LIVE_CANARY_RPI_EXCHANGE_INFO_SOURCE!r}"
        )
    if truth.get("commission_source") != LIVE_CANARY_RPI_COMMISSION_SOURCE:
        raise ValueError(
            "rpi_truth.commission_source must be "
            f"{LIVE_CANARY_RPI_COMMISSION_SOURCE!r}"
        )
    symbol = _configured_symbol(config)
    if str(truth.get("symbol", "") or "").strip().upper() != symbol:
        raise ValueError("rpi_truth.symbol must match the configured symbol")
    if (
        str(truth.get("exchange_status", "") or "").strip().upper()
        != "TRADING"
        or truth.get("supports_rpi") is not True
    ):
        raise ValueError(
            "rpi_truth must show a TRADING symbol with explicit RPI support"
        )

    max_age_sec = _live_evidence_max_age(_section(config, "live_launch"))
    _validate_fresh_capture(
        truth,
        field_prefix="rpi_truth",
        now_utc=now_utc,
        max_age_sec=max_age_sec,
    )
    rates = {}
    for field in (
        "makerCommissionRate",
        "takerCommissionRate",
        "rpiCommissionRate",
    ):
        rate = _finite_decimal(truth.get(field), f"rpi_truth.{field}")
        if abs(rate) > Decimal("0.01"):
            raise ValueError(
                f"rpi_truth.{field} must be between -0.01 and 0.01"
            )
        rates[field] = rate
    if rates["rpiCommissionRate"] != Decimal(0):
        raise ValueError(
            "account-specific rpi_truth.rpiCommissionRate must be exactly zero"
        )


def validate_live_canary_evidence_payload(
    config: Mapping,
    evidence: Mapping,
    *,
    now_utc: datetime | None = None,
    primary_api_key: str | None = None,
    supervisor_api_key: str | None = None,
    primary_api_secret: str | None = None,
    supervisor_api_secret: str | None = None,
    require_api_key_binding: bool = True,
    require_integrity_binding: bool = True,
) -> dict:
    """Validate a complete in-memory v4 Live canary evidence payload."""
    if not isinstance(evidence, dict):
        raise ValueError("live canary evidence must be a JSON object")
    _validate_live_evidence_binding(config, evidence)
    validate_live_canary_evidence_integrity(
        evidence,
        primary_api_secret=primary_api_secret,
        supervisor_api_secret=supervisor_api_secret,
        require_secret_binding=require_integrity_binding,
    )
    _validate_live_attestations(evidence)
    effective_now = _effective_now_utc(now_utc)
    validate_live_api_restrictions_evidence(
        config,
        evidence,
        now_utc=effective_now,
        primary_api_key=primary_api_key,
        supervisor_api_key=supervisor_api_key,
        require_api_key_binding=require_api_key_binding,
    )
    validate_live_dual_key_account_evidence(
        config,
        evidence,
        now_utc=effective_now,
        primary_api_key=primary_api_key,
        supervisor_api_key=supervisor_api_key,
        require_api_key_binding=require_api_key_binding,
    )
    validate_live_flat_start_evidence(
        config,
        evidence,
        now_utc=effective_now,
        primary_api_key=primary_api_key,
        require_api_key_binding=require_api_key_binding,
    )
    validate_live_symbol_configuration_evidence(
        config,
        evidence,
        now_utc=effective_now,
        primary_api_key=primary_api_key,
        require_api_key_binding=require_api_key_binding,
    )
    _validate_live_rpi_truth(config, evidence, now_utc=effective_now)
    return evidence


def validate_live_canary_local_evidence(
    config: Mapping,
    *,
    config_path: str | Path,
    now_utc: datetime | None = None,
    require_api_key_binding: bool = True,
) -> dict:
    """Fail closed on stale, unbound, or nonzero-fee local Live evidence."""
    evidence_path = resolve_live_canary_evidence_path(
        config,
        config_path=config_path,
    )
    evidence = _read_json_object(evidence_path, "live canary evidence")
    supervisor = _section(_section(config, "risk"), "independent_supervisor")
    primary_api_key = str(config.get("api_key", "") or "")
    primary_api_secret = str(config.get("api_secret", "") or "")
    supervisor_api_key = str(supervisor.get("api_key", "") or "")
    supervisor_api_secret = str(supervisor.get("api_secret", "") or "")
    return validate_live_canary_evidence_payload(
        config,
        evidence,
        now_utc=now_utc,
        primary_api_key=primary_api_key,
        supervisor_api_key=supervisor_api_key,
        primary_api_secret=primary_api_secret,
        supervisor_api_secret=supervisor_api_secret,
        require_api_key_binding=require_api_key_binding,
    )


def _append_cap_violation(
    violations: list[str],
    *,
    field: str,
    value,
    cap_field: str,
    cap_value,
) -> None:
    parsed = _positive_finite_value(value)
    if parsed is None:
        violations.append(f"{field} must be positive and finite")
        return
    if cap_value is not None and parsed > cap_value:
        violations.append(
            f"{field}={parsed:g} exceeds {cap_field}={cap_value:g}"
        )


def _validated_rpi_calibration_policy(
    config: Mapping,
    violations: list[str],
    *,
    deployment_id: str,
    symbol: str,
    deployment_loss_cap: float | None,
    risk_order_cap: float | None,
    target_order_notional: float | None,
) -> Mapping:
    wrapper = config.get("_validated_rpi_calibration_permit")
    if not isinstance(wrapper, Mapping):
        violations.append(
            "rpi_calibration_canary requires a validated signed calibration "
            "permit"
        )
        return {}
    if set(wrapper) != CALIBRATION_PERMIT_WRAPPER_FIELDS:
        violations.append(
            "_validated_rpi_calibration_permit must contain exactly: "
            + ", ".join(sorted(CALIBRATION_PERMIT_WRAPPER_FIELDS))
        )

    for field in (
        "permit_sha256",
        "calibration_config_sha256",
        "target_deployment_config_sha256",
    ):
        digest = str(wrapper.get(field, "") or "").strip().lower()
        if not _SHA256_RE.fullmatch(digest):
            violations.append(
                f"_validated_rpi_calibration_permit.{field} must be a "
                "lowercase SHA-256 digest"
            )

    permit = wrapper.get("permit")
    if not isinstance(permit, Mapping):
        violations.append(
            "_validated_rpi_calibration_permit.permit must be an object"
        )
        return {}
    expected_identity = {
        "stage": RPI_CALIBRATION_CANARY_STAGE,
        "venue": "BINANCE_USDM",
        "symbol": symbol,
        "model": "glft",
        "deployment_id": deployment_id,
    }
    for field, expected in expected_identity.items():
        actual = str(permit.get(field, "") or "").strip()
        if field in {"stage", "model"}:
            actual = actual.lower()
        elif field in {"venue", "symbol"}:
            actual = actual.upper()
        if actual != expected:
            violations.append(
                f"calibration permit {field} must be {expected!r}"
            )
    for field in (
        "calibration_config_sha256",
        "target_deployment_config_sha256",
    ):
        if not hmac.compare_digest(
            str(permit.get(field, "") or "").strip().lower(),
            str(wrapper.get(field, "") or "").strip().lower(),
        ):
            violations.append(
                f"calibration permit {field} must match the validated wrapper"
            )

    policy = permit.get("policy")
    if not isinstance(policy, Mapping):
        violations.append("calibration permit policy must be an object")
        return {}
    if set(policy) != CALIBRATION_PERMIT_POLICY_FIELDS:
        violations.append(
            "calibration permit policy must contain exactly: "
            + ", ".join(sorted(CALIBRATION_PERMIT_POLICY_FIELDS))
        )

    raw_depths = policy.get("fixed_depths_bps")
    depths = []
    if not isinstance(raw_depths, (list, tuple)):
        violations.append(
            "calibration permit policy.fixed_depths_bps must be a list"
        )
    else:
        for index, value in enumerate(raw_depths):
            if isinstance(value, bool):
                parsed = math.nan
            else:
                try:
                    parsed = float(value)
                except (TypeError, ValueError):
                    parsed = math.nan
            if (
                not math.isfinite(parsed)
                or parsed <= 0.0
                or parsed > MAX_CALIBRATION_DEPTH_BPS
            ):
                violations.append(
                    "calibration permit policy.fixed_depths_bps"
                    f"[{index}] must be positive, finite, and no more than "
                    f"{MAX_CALIBRATION_DEPTH_BPS:g}"
                )
            depths.append(parsed)
        if not (
            MIN_CALIBRATION_DEPTH_LEVELS
            <= len(depths)
            <= MAX_CALIBRATION_DEPTH_LEVELS
        ):
            violations.append(
                "calibration permit policy.fixed_depths_bps must contain "
                f"between {MIN_CALIBRATION_DEPTH_LEVELS} and "
                f"{MAX_CALIBRATION_DEPTH_LEVELS} depths"
            )
        if depths and any(
            right <= left
            for left, right in zip(depths, depths[1:])
        ):
            violations.append(
                "calibration permit policy.fixed_depths_bps must be strictly "
                "increasing"
            )
        if (
            len(depths) >= 2
            and all(math.isfinite(value) for value in depths)
            and depths[-1] - depths[0] < MIN_CALIBRATION_DEPTH_SPAN_BPS
        ):
            violations.append(
                "calibration permit policy.fixed_depths_bps must span at "
                f"least {MIN_CALIBRATION_DEPTH_SPAN_BPS:g} bps"
            )

    ttl_sec = _positive_finite_value(policy.get("order_ttl_sec"))
    interval_sec = _positive_finite_value(
        policy.get("min_order_interval_sec")
    )
    if ttl_sec is None or ttl_sec > MAX_CALIBRATION_ORDER_TTL_SEC:
        violations.append(
            "calibration permit policy.order_ttl_sec must be greater than 0 "
            f"and no more than {MAX_CALIBRATION_ORDER_TTL_SEC:g}"
        )
    if (
        interval_sec is None
        or interval_sec < MIN_CALIBRATION_ORDER_INTERVAL_SEC
        or interval_sec > MAX_CALIBRATION_ORDER_INTERVAL_SEC
    ):
        violations.append(
            "calibration permit policy.min_order_interval_sec must be between "
            f"{MIN_CALIBRATION_ORDER_INTERVAL_SEC:g} and "
            f"{MAX_CALIBRATION_ORDER_INTERVAL_SEC:g}"
        )
    if (
        ttl_sec is not None
        and interval_sec is not None
        and ttl_sec > interval_sec
    ):
        violations.append(
            "calibration permit policy.order_ttl_sec must not exceed "
            "min_order_interval_sec"
        )

    max_active_orders = policy.get("max_active_orders")
    if (
        isinstance(max_active_orders, bool)
        or not isinstance(max_active_orders, int)
        or max_active_orders != 1
    ):
        violations.append(
            "calibration permit policy.max_active_orders must be exactly 1"
        )
    max_order_count = policy.get("max_order_count")
    if (
        isinstance(max_order_count, bool)
        or not isinstance(max_order_count, int)
        or max_order_count <= 0
        or max_order_count > MAX_CALIBRATION_ORDER_COUNT
    ):
        violations.append(
            "calibration permit policy.max_order_count must be a positive "
            f"integer no greater than {MAX_CALIBRATION_ORDER_COUNT}"
        )
        max_order_count = None
    try:
        permit_issued = _parse_utc_timestamp(
            permit.get("issued_at_utc"),
            "calibration permit issued_at_utc",
        )
        permit_not_before = _parse_utc_timestamp(
            permit.get("not_before_utc"),
            "calibration permit not_before_utc",
        )
        permit_expires = _parse_utc_timestamp(
            permit.get("expires_at_utc"),
            "calibration permit expires_at_utc",
        )
    except ValueError as exc:
        violations.append(str(exc))
    else:
        if not permit_issued <= permit_not_before < permit_expires:
            violations.append(
                "calibration permit timestamps must satisfy issued_at_utc <= "
                "not_before_utc < expires_at_utc"
            )
        elif (
            permit_expires - permit_issued
        ).total_seconds() > MAX_CALIBRATION_PERMIT_TTL_SEC:
            violations.append(
                "calibration permit TTL must not exceed "
                f"{MAX_CALIBRATION_PERMIT_TTL_SEC:g} seconds"
            )
        elif (
            max_order_count is not None
            and ttl_sec is not None
            and interval_sec is not None
            and (
                (max_order_count - 1) * interval_sec + ttl_sec
                > (permit_expires - permit_not_before).total_seconds()
            )
        ):
            violations.append(
                "calibration permit order count, interval, and TTL do not fit "
                "within the permit validity window"
            )

    min_order_notional = _positive_finite_value(
        policy.get("min_order_notional_usdt")
    )
    max_order_notional = _positive_finite_value(
        policy.get("max_order_notional_usdt")
    )
    if min_order_notional is None:
        violations.append(
            "calibration permit policy.min_order_notional_usdt must be "
            "positive and finite"
        )
    if (
        max_order_notional is None
        or max_order_notional > MAX_CALIBRATION_ORDER_NOTIONAL_USDT
    ):
        violations.append(
            "calibration permit policy.max_order_notional_usdt must be "
            f"positive and no more than {MAX_CALIBRATION_ORDER_NOTIONAL_USDT:g}"
        )
    if (
        min_order_notional is not None
        and max_order_notional is not None
        and min_order_notional > max_order_notional
    ):
        violations.append(
            "calibration permit policy.min_order_notional_usdt must not "
            "exceed max_order_notional_usdt"
        )
    if (
        max_order_notional is not None
        and risk_order_cap is not None
        and max_order_notional > risk_order_cap
    ):
        violations.append(
            "calibration permit policy.max_order_notional_usdt must not "
            "exceed risk.limits.max_order_notional"
        )
    if (
        target_order_notional is not None
        and min_order_notional is not None
        and target_order_notional < min_order_notional
    ):
        violations.append(
            "strategy.target_order_notional must not be below calibration "
            "permit policy.min_order_notional_usdt"
        )
    if (
        target_order_notional is not None
        and max_order_notional is not None
        and target_order_notional > max_order_notional
    ):
        violations.append(
            "strategy.target_order_notional must not exceed calibration "
            "permit policy.max_order_notional_usdt"
        )

    cumulative_notional = _positive_finite_value(
        policy.get("max_cumulative_submitted_notional_usdt")
    )
    if cumulative_notional is None:
        violations.append(
            "calibration permit policy.max_cumulative_submitted_notional_usdt "
            "must be positive and finite"
        )
    elif (
        min_order_notional is not None
        and cumulative_notional < min_order_notional
    ):
        violations.append(
            "calibration permit policy.max_cumulative_submitted_notional_usdt "
            "must allow at least one minimum-notional order"
        )
    elif (
        max_order_count is not None
        and max_order_notional is not None
        and cumulative_notional > max_order_count * max_order_notional
    ):
        violations.append(
            "calibration permit policy.max_cumulative_submitted_notional_usdt "
            "must not exceed max_order_count * max_order_notional_usdt"
        )
    calibration_loss = _positive_finite_value(
        policy.get("max_calibration_loss_usdt")
    )
    if (
        calibration_loss is None
        or calibration_loss > MAX_CALIBRATION_DEPLOYMENT_LOSS_USDT
    ):
        violations.append(
            "calibration permit policy.max_calibration_loss_usdt must be "
            f"positive and no more than {MAX_CALIBRATION_DEPLOYMENT_LOSS_USDT:g}"
        )
    if (
        calibration_loss is not None
        and deployment_loss_cap is not None
        and calibration_loss > deployment_loss_cap
    ):
        violations.append(
            "calibration permit policy.max_calibration_loss_usdt must not "
            "exceed live_launch.max_deployment_loss_usdt"
        )
    return policy


def validate_live_runtime_config(
    config: dict,
    *,
    config_path: str | Path | None = None,
    require_local_evidence: bool = False,
    now_utc: datetime | None = None,
    external_alert_environ: Mapping[str, str] | None = None,
    runtime_working_dir: str | Path | None = None,
) -> dict:
    """Reject a live runtime that disables mandatory independent safety planes."""
    if not isinstance(config, dict):
        raise ValueError("Live runtime configuration must be a JSON object")
    if is_paper_trade(config):
        return config

    violations = []
    config_base_dir = (
        Path(config_path).resolve().parent
        if config_path is not None
        else None
    )
    effective_working_dir = Path(
        runtime_working_dir
        if runtime_working_dir is not None
        else Path.cwd()
    ).resolve()
    if (
        config_base_dir is not None
        and config_base_dir != effective_working_dir
    ):
        violations.append(
            "Live config must reside in the process working directory so "
            "all durable relative paths have one runtime identity"
        )
    symbols = config.get("symbols", [])
    execution = _section(config, "execution")
    system = _section(config, "system")
    market_data = _section(system, "market_data")
    time_sync = _section(system, "time_sync")
    web_dashboard = _section(system, "web_dashboard")
    admin_control = _section(system, "admin_control")
    account = _section(config, "account")
    root_strategy = _section(config, "strategy")
    order_sizing = _section(root_strategy, "order_sizing")
    if str(order_sizing.get("mode", "notional") or "notional").strip().lower() == (
        "fixed_quantity"
    ):
        violations.append("strategy.order_sizing.fixed_quantity is Paper-only")
    try:
        strategy = effective_primary_strategy_config(config)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Unsafe live trading configuration: effective strategy "
            f"configuration is invalid: {exc}"
        ) from exc
    live_launch = _section(config, "live_launch")
    oms = _section(config, "oms")
    risk = _section(config, "risk")
    limits = _section(risk, "limits")
    tech_health = _section(risk, "tech_health")
    market_freshness = _section(risk, "market_data_freshness")
    margin_health = _section(risk, "margin_health")
    funding_guard = _section(risk, "funding_guard")
    supervisor = _section(risk, "independent_supervisor")
    cash_flow = _section(risk, "cash_flow_truth")
    heartbeat = _section(risk, "risk_control_heartbeat")
    strategy_budget = _section(risk, "strategy_risk_budgets")
    dead_man_switch = _section(oms, "venue_dead_man_switch")
    writer_fence = _section(oms, "single_writer_fence")
    truth_monitor = _section(oms, "truth_monitor")

    execution_mode = str(execution.get("mode", "") or "").strip().lower()
    if execution_mode != "live":
        violations.append("execution.mode must be explicitly set to 'live'")
    if config.get("testnet") is not False:
        violations.append(
            "testnet must be the JSON boolean false for a live mainnet runtime"
        )
    market_environment = str(
        market_data.get("environment", "") or ""
    ).strip().lower()
    if market_environment not in MAINNET_ENVIRONMENTS:
        violations.append(
            "system.market_data.environment must be 'production' or 'mainnet'"
        )
    if market_data.get("testnet") is not False:
        violations.append(
            "system.market_data.testnet must be the JSON boolean false"
        )
    risk_latency_ms = _positive_finite_value(
        tech_health.get("max_latency_ms")
    )
    ingress_age_ms = _positive_finite_value(
        market_data.get(
            "max_market_event_ingress_age_ms",
            risk_latency_ms or 1000.0,
        )
    )
    if ingress_age_ms is None or ingress_age_ms < 100.0:
        violations.append(
            "system.market_data.max_market_event_ingress_age_ms must be "
            "finite and at least 100ms"
        )
    elif risk_latency_ms is not None and ingress_age_ms > risk_latency_ms:
        violations.append(
            "system.market_data.max_market_event_ingress_age_ms must be no "
            "greater than risk.tech_health.max_latency_ms so stale batches "
            "are rejected before symbol circuit breakers"
        )
    if web_dashboard.get("enabled") is not True:
        violations.append("system.web_dashboard.enabled must be JSON true")
    dashboard_host = str(
        web_dashboard.get("host", "") or ""
    ).strip().lower()
    if dashboard_host not in {"127.0.0.1", "::1", "localhost"}:
        violations.append(
            "system.web_dashboard.host must be an explicit loopback address"
        )
    dashboard_port = web_dashboard.get("port")
    if (
        isinstance(dashboard_port, bool)
        or not isinstance(dashboard_port, int)
        or not 1 <= dashboard_port <= 65535
    ):
        violations.append(
            "system.web_dashboard.port must be an integer from 1 to 65535"
        )
    admin_command_ttl = _positive_finite_value(
        admin_control.get("command_ttl_sec")
    )
    if admin_command_ttl is None or admin_command_ttl > 30.0:
        violations.append(
            "system.admin_control.command_ttl_sec must be positive and no "
            "more than 30 seconds"
        )
    admin_session_max_age = _positive_finite_value(
        admin_control.get("session_max_age_sec")
    )
    if admin_session_max_age is None or admin_session_max_age > 5.0:
        violations.append(
            "system.admin_control.session_max_age_sec must be positive and "
            "no more than 5 seconds"
        )
    elif (
        admin_command_ttl is not None
        and admin_session_max_age >= admin_command_ttl
    ):
        violations.append(
            "system.admin_control.session_max_age_sec must be less than "
            "command_ttl_sec"
        )

    try:
        validate_live_external_alert_config(
            config,
            environ=external_alert_environ,
            base_dir=config_base_dir,
        )
    except (TypeError, ValueError) as exc:
        violations.append(str(exc))
    try:
        validate_live_evidence_recorder_config(
            config,
            base_dir=config_base_dir,
        )
    except (TypeError, ValueError) as exc:
        violations.append(str(exc))

    if not _enabled(risk.get("active")):
        violations.append("risk.active must be true")
    if not _enabled(time_sync.get("startup_required")):
        violations.append("system.time_sync.startup_required must be true")
    if not _enabled(time_sync.get("require_healthy_for_trading")):
        violations.append(
            "system.time_sync.require_healthy_for_trading must be true"
        )
    if not _enabled(market_freshness.get("enabled")):
        violations.append("risk.market_data_freshness.enabled must be true")
    if not _enabled(market_freshness.get("require_mark_price")):
        violations.append(
            "risk.market_data_freshness.require_mark_price must be true"
        )
    if not _enabled(market_freshness.get("require_book")):
        violations.append(
            "risk.market_data_freshness.require_book must be true"
        )
    for field in ("max_mark_age_ms", "max_book_age_ms"):
        if not _positive_finite(market_freshness.get(field)):
            violations.append(
                f"risk.market_data_freshness.{field} must be positive"
            )
    if not _enabled(margin_health.get("enabled")):
        violations.append("risk.margin_health.enabled must be true")
    if not _enabled(margin_health.get("require_snapshot")):
        violations.append("risk.margin_health.require_snapshot must be true")
    if not _positive_finite(margin_health.get("max_snapshot_age_sec")):
        violations.append(
            "risk.margin_health.max_snapshot_age_sec must be positive"
        )

    if not _enabled(funding_guard.get("enabled")):
        violations.append("risk.funding_guard.enabled must be true")
    if not _enabled(funding_guard.get("require_snapshot")):
        violations.append(
            "risk.funding_guard.require_snapshot must be true"
        )
    funding_snapshot_age_ms = _positive_finite_value(
        funding_guard.get("max_snapshot_age_ms")
    )
    market_mark_age_ms = _positive_finite_value(
        market_freshness.get("max_mark_age_ms")
    )
    if (
        funding_snapshot_age_ms is None
        or funding_snapshot_age_ms > MAX_CANARY_FUNDING_SNAPSHOT_AGE_MS
    ):
        violations.append(
            "risk.funding_guard.max_snapshot_age_ms must be positive and "
            f"no more than {MAX_CANARY_FUNDING_SNAPSHOT_AGE_MS:g}"
        )
    elif (
        market_mark_age_ms is not None
        and funding_snapshot_age_ms > market_mark_age_ms
    ):
        violations.append(
            "risk.funding_guard.max_snapshot_age_ms must not exceed "
            "risk.market_data_freshness.max_mark_age_ms"
        )
    pre_funding_sec = _positive_finite_value(
        funding_guard.get("pre_funding_reduce_only_sec")
    )
    if (
        pre_funding_sec is None
        or pre_funding_sec < MIN_CANARY_PRE_FUNDING_REDUCE_ONLY_SEC
    ):
        violations.append(
            "risk.funding_guard.pre_funding_reduce_only_sec must be at "
            f"least {MIN_CANARY_PRE_FUNDING_REDUCE_ONLY_SEC:g}"
        )
    post_funding_sec = _positive_finite_value(
        funding_guard.get("post_funding_hold_sec")
    )
    if (
        post_funding_sec is None
        or post_funding_sec < MIN_CANARY_POST_FUNDING_HOLD_SEC
    ):
        violations.append(
            "risk.funding_guard.post_funding_hold_sec must be at least "
            f"{MIN_CANARY_POST_FUNDING_HOLD_SEC:g}"
        )
    max_abs_funding_rate = _positive_finite_value(
        funding_guard.get("max_abs_funding_rate")
    )
    if (
        max_abs_funding_rate is None
        or max_abs_funding_rate > MAX_CANARY_ABS_FUNDING_RATE
    ):
        violations.append(
            "risk.funding_guard.max_abs_funding_rate must be positive and "
            f"no more than {MAX_CANARY_ABS_FUNDING_RATE:g}"
        )
    max_funding_horizon_sec = _positive_finite_value(
        funding_guard.get("max_next_funding_horizon_sec")
    )
    if (
        max_funding_horizon_sec is None
        or max_funding_horizon_sec
        > MAX_CANARY_NEXT_FUNDING_HORIZON_SEC
    ):
        violations.append(
            "risk.funding_guard.max_next_funding_horizon_sec must be "
            "positive and no more than "
            f"{MAX_CANARY_NEXT_FUNDING_HORIZON_SEC:g}"
        )
    elif (
        pre_funding_sec is not None
        and max_funding_horizon_sec <= pre_funding_sec
    ):
        violations.append(
            "risk.funding_guard.max_next_funding_horizon_sec must exceed "
            "risk.funding_guard.pre_funding_reduce_only_sec"
        )
    funding_recovery_updates = funding_guard.get("recovery_updates")
    if (
        isinstance(funding_recovery_updates, bool)
        or not isinstance(funding_recovery_updates, int)
        or funding_recovery_updates < MIN_CANARY_FUNDING_RECOVERY_UPDATES
    ):
        violations.append(
            "risk.funding_guard.recovery_updates must be an integer of at "
            f"least {MIN_CANARY_FUNDING_RECOVERY_UPDATES}"
        )

    if not _enabled(supervisor.get("enabled")):
        violations.append("risk.independent_supervisor.enabled must be true")
    if not _enabled(supervisor.get("flatten_enabled")):
        violations.append(
            "risk.independent_supervisor.flatten_enabled must be true"
        )
    for field in (
        "state_required",
        "state_fsync",
        "daily_loss_enabled",
        "clock_sync_enabled",
        "liquidation_proximity_enabled",
        "require_liquidation_price",
    ):
        if not _enabled(supervisor.get(field)):
            violations.append(
                f"risk.independent_supervisor.{field} must be true"
            )
    supervisor_state_path = str(
        supervisor.get("state_path", "") or ""
    ).strip()
    if not supervisor_state_path:
        violations.append(
            "risk.independent_supervisor.state_path must be configured"
        )
    elif _uses_paper_state_path(supervisor_state_path):
        violations.append(
            "risk.independent_supervisor.state_path must not use Paper state"
        )

    primary_api_key = str(config.get("api_key", "") or "").strip()
    primary_api_secret = str(config.get("api_secret", "") or "").strip()
    supervisor_api_key = str(supervisor.get("api_key", "") or "").strip()
    supervisor_api_secret = str(
        supervisor.get("api_secret", "") or ""
    ).strip()
    if not primary_api_key or not primary_api_secret:
        violations.append("primary Binance API credentials must be configured")
    if not supervisor_api_key or not supervisor_api_secret:
        violations.append(
            "risk.independent_supervisor must use dedicated API credentials"
        )
    if (
        primary_api_key
        and supervisor_api_key
        and primary_api_key == supervisor_api_key
    ):
        violations.append(
            "risk.independent_supervisor.api_key must differ from the "
            "primary API key"
        )
    if (
        primary_api_secret
        and supervisor_api_secret
        and primary_api_secret == supervisor_api_secret
    ):
        violations.append(
            "risk.independent_supervisor.api_secret must differ from the "
            "primary API secret"
        )

    primary_key_env = str(config.get("api_key_env", "") or "").strip()
    primary_secret_env = str(config.get("api_secret_env", "") or "").strip()
    supervisor_key_env = str(
        supervisor.get("api_key_env", "") or ""
    ).strip()
    supervisor_secret_env = str(
        supervisor.get("api_secret_env", "") or ""
    ).strip()
    if not primary_key_env or not primary_secret_env:
        violations.append(
            "primary API credential environment variable names must be configured"
        )
    if not supervisor_key_env or not supervisor_secret_env:
        violations.append(
            "independent supervisor credential environment variable names "
            "must be configured"
        )
    if (
        primary_key_env
        and supervisor_key_env
        and primary_key_env == supervisor_key_env
    ):
        violations.append(
            "independent supervisor API key environment variable must differ "
            "from the primary"
        )
    if (
        primary_secret_env
        and supervisor_secret_env
        and primary_secret_env == supervisor_secret_env
    ):
        violations.append(
            "independent supervisor API secret environment variable must "
            "differ from the primary"
        )
    if not _enabled(dead_man_switch.get("enabled")):
        violations.append("oms.venue_dead_man_switch.enabled must be true")

    if not _enabled(oms.get("journal_enabled")):
        violations.append("oms.journal_enabled must be true")
    if not _enabled(oms.get("replay_journal_on_startup")):
        violations.append("oms.replay_journal_on_startup must be true")
    if not _enabled(oms.get("journal_fsync")):
        violations.append("oms.journal_fsync must be true")
    if not _enabled(oms.get("journal_integrity_check")):
        violations.append("oms.journal_integrity_check must be true")
    if not _enabled(writer_fence.get("enabled")):
        violations.append("oms.single_writer_fence.enabled must be true")

    journal_path = str(oms.get("journal_path", "") or "").strip()
    if not journal_path:
        violations.append("oms.journal_path must be configured")
    elif _uses_paper_state_path(journal_path):
        violations.append("oms.journal_path must not use Paper state")

    fence_path = writer_fence.get("path", "")
    if fence_path and _uses_paper_state_path(fence_path):
        violations.append(
            "oms.single_writer_fence.path must not use Paper state"
        )
    try:
        validate_live_state_path_bindings(
            config,
            base_dir=config_base_dir,
        )
    except (TypeError, ValueError) as exc:
        violations.append(str(exc))

    if not _enabled(cash_flow.get("enabled")):
        violations.append("risk.cash_flow_truth.enabled must be true")
    if not _enabled(cash_flow.get("require_snapshot")):
        violations.append(
            "risk.cash_flow_truth.require_snapshot must be true"
        )
    if not _positive_finite(cash_flow.get("max_snapshot_age_sec")):
        violations.append(
            "risk.cash_flow_truth.max_snapshot_age_sec must be positive"
        )

    if not _enabled(heartbeat.get("enabled")):
        violations.append("risk.risk_control_heartbeat.enabled must be true")
    heartbeat_source = str(
        heartbeat.get("required_source", "") or ""
    ).strip()
    if heartbeat_source != INDEPENDENT_SUPERVISOR_SOURCE:
        violations.append(
            "risk.risk_control_heartbeat.required_source must be "
            f"{INDEPENDENT_SUPERVISOR_SOURCE!r}"
        )
    if not _positive_finite(heartbeat.get("max_age_sec")):
        violations.append(
            "risk.risk_control_heartbeat.max_age_sec must be positive"
        )

    if not _enabled(strategy_budget.get("enabled")):
        violations.append("risk.strategy_risk_budgets.enabled must be true")
    if not _enabled(strategy_budget.get("require_explicit_strategy")):
        violations.append(
            "risk.strategy_risk_budgets.require_explicit_strategy must be true"
        )
    primary_strategy_id = strategy_id_for_model(
        root_strategy.get("primary_model", root_strategy.get("name"))
    )
    configured_budgets = strategy_budget.get("budgets")
    if not isinstance(configured_budgets, Mapping):
        violations.append("risk.strategy_risk_budgets.budgets must be an object")
    else:
        configured_budget_ids = {
            str(strategy_id or "").strip()
            for strategy_id in configured_budgets
            if str(strategy_id or "").strip()
        }
        if configured_budget_ids != {primary_strategy_id}:
            violations.append(
                "risk.strategy_risk_budgets.budgets must contain exactly the "
                f"primary strategy ID {primary_strategy_id!r}"
            )
        primary_budget = configured_budgets.get(primary_strategy_id)
        if not isinstance(primary_budget, Mapping):
            violations.append(
                "risk.strategy_risk_budgets.budgets must configure the "
                f"primary strategy ID {primary_strategy_id!r}"
            )
        else:
            budget_symbol_cap = _positive_finite_value(
                primary_budget.get("max_symbol_notional")
            )
            budget_gross_cap = _positive_finite_value(
                primary_budget.get("max_gross_notional")
            )
            risk_symbol_cap = _positive_finite_value(
                limits.get("max_pos_notional")
            )
            risk_gross_cap = _positive_finite_value(
                limits.get("max_account_gross_notional")
            )
            if budget_symbol_cap is None or budget_gross_cap is None:
                violations.append(
                    "primary strategy risk budget caps must be positive and finite"
                )
            elif budget_symbol_cap > budget_gross_cap:
                violations.append(
                    "primary strategy max_symbol_notional must not exceed "
                    "max_gross_notional"
                )
            if (
                budget_symbol_cap is not None
                and risk_symbol_cap is not None
                and budget_symbol_cap > risk_symbol_cap
            ):
                violations.append(
                    "primary strategy max_symbol_notional must not exceed "
                    "risk.limits.max_pos_notional"
                )
            if (
                budget_gross_cap is not None
                and risk_gross_cap is not None
                and budget_gross_cap > risk_gross_cap
            ):
                violations.append(
                    "primary strategy max_gross_notional must not exceed "
                    "risk.limits.max_account_gross_notional"
                )

    stage = live_launch_stage(config)
    is_calibration_canary = stage == RPI_CALIBRATION_CANARY_STAGE
    if stage not in LIVE_CANARY_STAGES:
        violations.append(
            "live_launch.stage must be 'canary' or "
            f"{RPI_CALIBRATION_CANARY_STAGE!r}"
        )
    deployment_id = str(
        live_launch.get("deployment_id", "") or ""
    ).strip()
    if not deployment_id:
        violations.append("live_launch.deployment_id must be configured")
    elif not _DEPLOYMENT_ID_RE.fullmatch(deployment_id):
        violations.append(
            "live_launch.deployment_id must be 6-128 path-safe characters"
        )
    elif any(
        token in deployment_id.upper()
        for token in _DEPLOYMENT_PLACEHOLDER_TOKENS
    ):
        violations.append(
            "live_launch.deployment_id must replace the EDIT-ME placeholder"
        )
    if is_calibration_canary:
        if config_path is None:
            violations.append(
                "rpi_calibration_canary runtime validation requires the "
                "exact config_path for independent permit revalidation"
            )
        else:
            try:
                from infrastructure.rpi_calibration_permit import (
                    load_and_validate_rpi_calibration_permit,
                )

                revalidated_permit = (
                    load_and_validate_rpi_calibration_permit(
                        config,
                        config_path=config_path,
                        now_utc=now_utc,
                    )
                )
            except Exception as exc:
                violations.append(
                    "rpi_calibration_canary independent permit "
                    f"revalidation failed: {exc}"
                )
            else:
                if (
                    config.get("_validated_rpi_calibration_permit")
                    != revalidated_permit
                ):
                    violations.append(
                        "rpi_calibration_canary caller permit wrapper does "
                        "not match the independently revalidated permit"
                    )
        for field in (
            "calibration_permit_path",
            "target_deployment_config_path",
        ):
            if not str(live_launch.get(field, "") or "").strip():
                violations.append(
                    f"rpi_calibration_canary live_launch.{field} must be "
                    "configured"
                )
        trusted_signers = live_launch.get(
            "calibration_permit_trusted_signers"
        )
        if not isinstance(trusted_signers, Mapping) or not trusted_signers:
            violations.append(
                "rpi_calibration_canary requires dedicated "
                "live_launch.calibration_permit_trusted_signers"
            )
        for field in CALIBRATION_ACTIVE_ORDER_CAP_FIELDS:
            value = oms.get(field)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value != 1
            ):
                violations.append(
                    f"rpi_calibration_canary oms.{field} must be exactly 1"
                )
        supervisor_open_orders = supervisor.get("max_open_orders")
        if (
            isinstance(supervisor_open_orders, bool)
            or not isinstance(supervisor_open_orders, int)
            or supervisor_open_orders != 1
        ):
            violations.append(
                "rpi_calibration_canary "
                "risk.independent_supervisor.max_open_orders must be exactly 1"
            )
    else:
        if "_validated_rpi_calibration_permit" in config:
            violations.append(
                "_validated_rpi_calibration_permit is forbidden outside "
                "rpi_calibration_canary"
            )
        for field in CALIBRATION_ACTIVE_ORDER_CAP_FIELDS:
            value = oms.get(field)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value != MAX_CANARY_ACTIVE_ORDERS
            ):
                violations.append(
                    f"live canary oms.{field} must be exactly "
                    f"{MAX_CANARY_ACTIVE_ORDERS}"
                )
        supervisor_open_orders = supervisor.get("max_open_orders")
        if (
            isinstance(supervisor_open_orders, bool)
            or not isinstance(supervisor_open_orders, int)
            or supervisor_open_orders != MAX_CANARY_ACTIVE_ORDERS
        ):
            violations.append(
                "live canary risk.independent_supervisor.max_open_orders "
                f"must be exactly {MAX_CANARY_ACTIVE_ORDERS}"
            )

    commission_poll_interval = _positive_finite_value(
        truth_monitor.get("rpi_commission_poll_interval_sec")
    )
    if (
        commission_poll_interval is None
        or commission_poll_interval
        < MIN_RPI_COMMISSION_POLL_INTERVAL_SEC
        or commission_poll_interval
        > MAX_RPI_COMMISSION_POLL_INTERVAL_SEC
    ):
        violations.append(
            "oms.truth_monitor.rpi_commission_poll_interval_sec must be "
            f"between {MIN_RPI_COMMISSION_POLL_INTERVAL_SEC:g} and "
            f"{MAX_RPI_COMMISSION_POLL_INTERVAL_SEC:g}"
        )
    commission_halt_threshold = truth_monitor.get(
        "rpi_commission_halt_threshold"
    )
    if (
        isinstance(commission_halt_threshold, bool)
        or not isinstance(commission_halt_threshold, int)
        or not 1
        <= commission_halt_threshold
        <= MAX_RPI_COMMISSION_HALT_THRESHOLD
    ):
        violations.append(
            "oms.truth_monitor.rpi_commission_halt_threshold must be "
            f"an integer between 1 and {MAX_RPI_COMMISSION_HALT_THRESHOLD}"
        )
    commission_clean_polls = truth_monitor.get(
        "rpi_commission_clean_polls_to_clear"
    )
    if (
        isinstance(commission_clean_polls, bool)
        or not isinstance(commission_clean_polls, int)
        or commission_clean_polls != RPI_COMMISSION_CLEAN_POLLS_TO_CLEAR
    ):
        violations.append(
            "oms.truth_monitor.rpi_commission_clean_polls_to_clear must "
            f"be exactly {RPI_COMMISSION_CLEAN_POLLS_TO_CLEAR}"
        )
    reduce_only_fraction = _positive_finite_value(
        live_launch.get("deployment_loss_reduce_only_fraction")
    )
    if reduce_only_fraction is None or reduce_only_fraction >= 1.0:
        violations.append(
            "live_launch.deployment_loss_reduce_only_fraction must be "
            "greater than 0 and less than 1"
        )

    if (
        not isinstance(symbols, (list, tuple))
        or len(symbols) != 1
        or not str(symbols[0] or "").strip()
    ):
        violations.append(
            "live_launch canary requires exactly one configured symbol"
        )

    margin_type = str(account.get("margin_type", "") or "").strip().upper()
    if margin_type != "ISOLATED":
        violations.append(
            "live_launch canary requires account.margin_type='ISOLATED'"
        )
    leverage = _positive_finite_value(account.get("leverage"))
    if leverage != 1.0:
        violations.append(
            "live_launch canary requires account.leverage=1"
        )
    configuration_mode = str(
        account.get("configuration_mode", "") or ""
    ).strip().upper()
    if configuration_mode != "VERIFY_ONLY":
        violations.append(
            "live_launch canary requires "
            "account.configuration_mode='VERIFY_ONLY'"
        )

    declared_equity = _positive_finite_value(
        live_launch.get("declared_account_equity_usdt")
    )
    deployed_cap = _positive_finite_value(
        live_launch.get("max_deployed_capital_usdt")
    )
    deployment_loss_cap = _positive_finite_value(
        live_launch.get("max_deployment_loss_usdt")
    )
    max_deployed_equity_fraction = _max_deployed_equity_fraction(config)
    if declared_equity is None:
        violations.append(
            "live_launch.declared_account_equity_usdt must be positive and finite"
        )
    if deployed_cap is None:
        violations.append(
            "live_launch.max_deployed_capital_usdt must be positive and finite"
        )
    elif declared_equity is not None and deployed_cap > declared_equity:
        violations.append(
            "live_launch.max_deployed_capital_usdt must not exceed "
            "live_launch.declared_account_equity_usdt"
        )
    elif (
        declared_equity is not None
        and deployed_cap
        > declared_equity * max_deployed_equity_fraction
    ):
        violations.append(
            "live_launch.max_deployed_capital_usdt must not exceed "
            f"{_deployed_equity_fraction_label(config)} of declared account "
            "equity"
        )
    elif (
        not is_calibration_canary
        and deployed_cap > MAX_CANARY_DEPLOYED_CAPITAL_USDT
    ):
        violations.append(
            "live_launch.max_deployed_capital_usdt must not exceed "
            f"{MAX_CANARY_DEPLOYED_CAPITAL_USDT:g} USDT for a canary"
        )
    if deployment_loss_cap is None:
        violations.append(
            "live_launch.max_deployment_loss_usdt must be positive and finite"
        )
    elif deployed_cap is not None and deployment_loss_cap > deployed_cap:
        violations.append(
            "live_launch.max_deployment_loss_usdt must not exceed "
            "live_launch.max_deployed_capital_usdt"
        )
    elif (
        deployed_cap is not None
        and deployment_loss_cap
        > deployed_cap * MAX_CANARY_DEPLOYMENT_LOSS_FRACTION
    ):
        violations.append(
            "live_launch.max_deployment_loss_usdt must not exceed "
            f"{MAX_CANARY_DEPLOYMENT_LOSS_FRACTION:.0%} of deployed capital"
        )
    elif (
        not is_calibration_canary
        and deployment_loss_cap > MAX_CANARY_DEPLOYMENT_LOSS_USDT
    ):
        violations.append(
            "live_launch.max_deployment_loss_usdt must not exceed "
            f"{MAX_CANARY_DEPLOYMENT_LOSS_USDT:g} USDT for a canary"
        )
    if (
        is_calibration_canary
        and deployment_loss_cap is not None
        and deployment_loss_cap > MAX_CALIBRATION_DEPLOYMENT_LOSS_USDT
    ):
        violations.append(
            "rpi_calibration_canary "
            "live_launch.max_deployment_loss_usdt must not exceed "
            f"{MAX_CALIBRATION_DEPLOYMENT_LOSS_USDT:g}"
        )

    _append_cap_violation(
        violations,
        field="account.trading_budget_total",
        value=account.get("trading_budget_total"),
        cap_field="live_launch.max_deployed_capital_usdt",
        cap_value=deployed_cap,
    )
    budget_by_asset = account.get("trading_budget_by_asset")
    if not isinstance(budget_by_asset, Mapping) or not budget_by_asset:
        violations.append(
            "account.trading_budget_by_asset must declare the canary budget"
        )
    else:
        parsed_asset_budgets = [
            _positive_finite_value(value)
            for value in budget_by_asset.values()
        ]
        if any(value is None for value in parsed_asset_budgets):
            violations.append(
                "account.trading_budget_by_asset values must be positive and finite"
            )
        else:
            asset_budget_total = sum(parsed_asset_budgets)
            if deployed_cap is not None and asset_budget_total > deployed_cap:
                violations.append(
                    "account.trading_budget_by_asset total exceeds "
                    "live_launch.max_deployed_capital_usdt"
                )

    canary_limit_fractions = {
        "max_order_notional": MAX_CANARY_ORDER_FRACTION,
        "max_pos_notional": MAX_CANARY_POSITION_FRACTION,
        "max_account_gross_notional": MAX_CANARY_GROSS_FRACTION,
    }
    absolute_canary_caps = {
        "max_order_notional": (
            MAX_CALIBRATION_ORDER_NOTIONAL_USDT
            if is_calibration_canary
            else MAX_CANARY_ORDER_NOTIONAL_USDT
        ),
        "max_pos_notional": (
            MAX_CALIBRATION_POSITION_NOTIONAL_USDT
            if is_calibration_canary
            else MAX_CANARY_POSITION_NOTIONAL_USDT
        ),
        "max_account_gross_notional": (
            MAX_CALIBRATION_GROSS_NOTIONAL_USDT
            if is_calibration_canary
            else MAX_CANARY_GROSS_NOTIONAL_USDT
        ),
    }
    for field, canary_fraction in canary_limit_fractions.items():
        _append_cap_violation(
            violations,
            field=f"risk.limits.{field}",
            value=limits.get(field),
            cap_field="live_launch.max_deployed_capital_usdt",
            cap_value=deployed_cap,
        )
        parsed_limit = _positive_finite_value(limits.get(field))
        if (
            parsed_limit is not None
            and parsed_limit > absolute_canary_caps[field]
        ):
            stage_label = (
                "rpi_calibration_canary"
                if is_calibration_canary
                else "live canary"
            )
            violations.append(
                f"{stage_label} risk.limits.{field} must not exceed "
                f"{absolute_canary_caps[field]:g} USDT"
            )
        elif (
            not is_calibration_canary
            and parsed_limit is not None
            and deployed_cap is not None
            and parsed_limit > deployed_cap * canary_fraction
        ):
            violations.append(
                f"risk.limits.{field} must not exceed "
                f"{canary_fraction:.0%} of deployed capital"
            )
    _append_cap_violation(
        violations,
        field="risk.limits.max_daily_loss",
        value=limits.get("max_daily_loss"),
        cap_field="live_launch.max_deployment_loss_usdt",
        cap_value=deployment_loss_cap,
    )
    max_daily_loss = _positive_finite_value(limits.get("max_daily_loss"))
    if (
        max_daily_loss is not None
        and deployment_loss_cap is not None
        and max_daily_loss
        > deployment_loss_cap * MAX_CANARY_DAILY_LOSS_FRACTION
    ):
        violations.append(
            "risk.limits.max_daily_loss must not exceed "
            f"{MAX_CANARY_DAILY_LOSS_FRACTION:.0%} of the deployment loss cap"
        )
    if (
        is_calibration_canary
        and max_daily_loss is not None
        and max_daily_loss > MAX_CALIBRATION_DAILY_LOSS_USDT
    ):
        violations.append(
            "rpi_calibration_canary risk.limits.max_daily_loss must not "
            f"exceed {MAX_CALIBRATION_DAILY_LOSS_USDT:g} USDT"
        )
    if (
        not is_calibration_canary
        and max_daily_loss is not None
        and max_daily_loss > MAX_CANARY_DAILY_LOSS_USDT
    ):
        violations.append(
            "live canary risk.limits.max_daily_loss must not exceed "
            f"{MAX_CANARY_DAILY_LOSS_USDT:g} USDT"
        )

    if not _enabled(live_launch.get("rpi_only")):
        violations.append("live_launch.rpi_only must be true")
    if not _enabled(strategy.get("use_rpi")):
        violations.append(
            "live_launch.rpi_only requires strategy.use_rpi=true"
        )
    elif not effective_rpi_route_enabled(config):
        violations.append(
            "live_launch.rpi_only requires the primary strategy's "
            "effective RPI route to be enabled"
        )
    if _enabled(strategy.get("rpi_fallback_to_gtx")):
        violations.append(
            "live_launch.rpi_only requires "
            "strategy.rpi_fallback_to_gtx=false"
        )
    rpi_live_policy = _section(strategy, "rpi_live_policy")
    if not _enabled(rpi_live_policy.get("require_zero_commission")):
        violations.append(
            "strategy.rpi_live_policy.require_zero_commission must be true"
        )

    primary_model = str(
        root_strategy.get("primary_model", "") or ""
    ).strip().lower()
    if primary_model != "glft":
        violations.append(
            "live_launch canary requires strategy.primary_model='glft'"
        )
    if is_calibration_canary and root_strategy.get("registered_models") != [
        "glft"
    ]:
        violations.append(
            "rpi_calibration_canary requires "
            "strategy.registered_models=['glft']"
        )
    glft = _section(strategy, "glft")
    alpha_value = _model_value(strategy, glft, "alpha", {})
    alpha = alpha_value if isinstance(alpha_value, Mapping) else {}
    if _enabled(alpha.get("enabled")):
        violations.append(
            "live_launch canary requires strategy.glft.alpha.enabled=false"
        )
    portfolio_risk_value = _model_value(
        strategy,
        glft,
        "portfolio_risk",
        {},
    )
    portfolio_risk = (
        portfolio_risk_value
        if isinstance(portfolio_risk_value, Mapping)
        else {}
    )
    if _enabled(portfolio_risk.get("enabled")):
        violations.append(
            "live_launch canary requires "
            "strategy.glft.portfolio_risk.enabled=false until separately "
            "approved"
        )
    adaptive_value = _model_value(strategy, glft, "adaptive", {})
    adaptive = adaptive_value if isinstance(adaptive_value, Mapping) else {}
    if _enabled(adaptive.get("enabled")):
        violations.append(
            "live_launch canary requires "
            "strategy.glft.adaptive.enabled=false until separately approved"
        )
    target_inventory = _model_value(
        strategy,
        glft,
        "target_inventory_notional_usdt",
    )
    try:
        target_inventory = float(target_inventory)
    except (TypeError, ValueError):
        target_inventory = math.nan
    if not math.isfinite(target_inventory) or target_inventory != 0.0:
        violations.append(
            "live_launch canary requires "
            "strategy.glft.target_inventory_notional_usdt=0"
        )
    target_order_notional = _positive_finite_value(
        strategy.get("target_order_notional")
    )
    if target_order_notional is None:
        violations.append(
            "strategy.target_order_notional must be positive and finite"
        )
    elif is_calibration_canary and (
        target_order_notional > MAX_CALIBRATION_ORDER_NOTIONAL_USDT
    ):
        violations.append(
            "rpi_calibration_canary strategy.target_order_notional must not "
            f"exceed {MAX_CALIBRATION_ORDER_NOTIONAL_USDT:g} USDT"
        )
    elif (
        not is_calibration_canary
        and target_order_notional > MAX_CANARY_ORDER_NOTIONAL_USDT
    ):
        violations.append(
            "strategy.target_order_notional must not exceed "
            f"{MAX_CANARY_ORDER_NOTIONAL_USDT:g} USDT for a live canary"
        )
    elif (
        not is_calibration_canary
        and deployed_cap is not None
        and target_order_notional
        > deployed_cap * (MAX_CANARY_ORDER_FRACTION / 1.5)
    ):
        violations.append(
            "strategy.target_order_notional must not exceed "
            "8% of deployed capital"
        )

    max_pos_usdt = _positive_finite_value(
        _model_value(strategy, glft, "max_pos_usdt")
    )
    risk_max_pos = _positive_finite_value(limits.get("max_pos_notional"))
    if max_pos_usdt is None:
        violations.append(
            "effective strategy.max_pos_usdt must be positive and finite"
        )
    elif risk_max_pos is not None and max_pos_usdt > risk_max_pos:
        violations.append(
            "effective strategy.max_pos_usdt must not exceed "
            "risk.limits.max_pos_notional"
        )

    gamma = _positive_finite_value(
        _model_value(strategy, glft, "gamma", 0.1)
    )
    if gamma is None or gamma > 1.0:
        violations.append(
            "effective GLFT gamma must be positive, finite, and no more than 1"
        )
    cycle_interval = _positive_finite_value(
        _model_value(strategy, glft, "cycle_interval", 1.0)
    )
    if cycle_interval is None or cycle_interval < 0.25:
        violations.append(
            "effective GLFT cycle_interval must be at least 0.25 seconds"
        )
    if is_calibration_canary:
        calibration_policy = _validated_rpi_calibration_policy(
            config,
            violations,
            deployment_id=deployment_id,
            symbol=_configured_symbol(config),
            deployment_loss_cap=deployment_loss_cap,
            risk_order_cap=_positive_finite_value(
                limits.get("max_order_notional")
            ),
            target_order_notional=target_order_notional,
        )
        permit_interval = _positive_finite_value(
            calibration_policy.get("min_order_interval_sec")
        )
        if (
            cycle_interval is not None
            and permit_interval is not None
            and cycle_interval < permit_interval
        ):
            violations.append(
                "rpi_calibration_canary effective GLFT cycle_interval must "
                "be at least the signed permit min_order_interval_sec"
            )

    execution_value = _model_value(strategy, glft, "execution", {})
    execution_config = (
        execution_value if isinstance(execution_value, Mapping) else {}
    )
    min_spread_bps = _positive_finite_value(
        execution_config.get("min_spread_bps", 5.0)
    )
    if min_spread_bps is None or min_spread_bps < 1.0:
        violations.append(
            "effective GLFT execution.min_spread_bps must be at least 1"
        )

    readiness = _section(strategy, "model_readiness")
    readiness_models = _section(readiness, "models")
    glft_readiness = _section(readiness_models, "glft")
    readiness_volatility_samples = _positive_finite_value(
        glft_readiness.get(
            "min_volatility_samples",
            readiness.get("min_volatility_samples"),
        )
    )
    readiness_model_samples = _positive_finite_value(
        glft_readiness.get(
            "min_model_samples",
            readiness.get("min_model_samples"),
        )
    )
    readiness_values = (
        readiness_volatility_samples,
        readiness_model_samples,
    )
    if any(
        value is None or not value.is_integer()
        for value in readiness_values
    ):
        violations.append(
            "effective GLFT model readiness sample requirements must be "
            "positive integers"
        )
        required_calibrator_samples = None
    else:
        required_calibrator_samples = max(readiness_values)

    calibrator_value = _model_value(strategy, glft, "calibrator", {})
    calibrator = (
        calibrator_value if isinstance(calibrator_value, Mapping) else {}
    )
    for field in ("window", "min_samples"):
        parsed = _positive_finite_value(calibrator.get(field))
        if (
            parsed is None
            or not parsed.is_integer()
            or (
                required_calibrator_samples is not None
                and parsed < required_calibrator_samples
            )
        ):
            required_text = (
                f" and at least {required_calibrator_samples:g}"
                if required_calibrator_samples is not None
                else ""
            )
            violations.append(
                f"effective GLFT calibrator.{field} must be a positive "
                f"integer{required_text}"
            )
    calibrator_bounds = {}
    for field in (
        "initial_sigma_bps",
        "sigma_max_bps",
        "max_tick_gap_sec",
    ):
        parsed = _positive_finite_value(calibrator.get(field))
        if parsed is None:
            violations.append(
                f"effective GLFT calibrator.{field} must be positive and finite"
            )
        calibrator_bounds[field] = parsed
    if (
        calibrator_bounds["initial_sigma_bps"] is not None
        and calibrator_bounds["sigma_max_bps"] is not None
        and calibrator_bounds["initial_sigma_bps"]
        > calibrator_bounds["sigma_max_bps"]
    ):
        violations.append(
            "effective GLFT calibrator.initial_sigma_bps must not exceed "
            "calibrator.sigma_max_bps"
        )

    inventory_lot_notional = _positive_finite_value(
        _model_value(
            strategy,
            glft,
            "inventory_lot_notional_usdt",
        )
    )
    if inventory_lot_notional is None:
        violations.append(
            "strategy.glft.inventory_lot_notional_usdt must be positive "
            "and finite"
        )
    elif (
        target_order_notional is not None
        and not math.isclose(
            inventory_lot_notional,
            target_order_notional,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    ):
        violations.append(
            "strategy.glft.inventory_lot_notional_usdt must equal "
            "strategy.target_order_notional"
        )

    rpi_intensity_value = _model_value(strategy, glft, "rpi_intensity", {})
    rpi_intensity = (
        rpi_intensity_value
        if isinstance(rpi_intensity_value, Mapping)
        else {}
    )
    intensity_minimums = {
        "min_sample_count": MIN_CANARY_RPI_INTENSITY_SAMPLES,
        "min_depth_level_count": MIN_CANARY_RPI_DEPTH_LEVELS,
        "min_total_exposure_seconds": MIN_CANARY_RPI_EXPOSURE_SEC,
        "min_fill_count": MIN_CANARY_RPI_FILLS,
        "min_depth_span_bps": MIN_CANARY_RPI_DEPTH_SPAN_BPS,
    }
    integer_fields = {
        "min_sample_count",
        "min_depth_level_count",
        "min_fill_count",
    }
    for field, minimum in intensity_minimums.items():
        parsed = _positive_finite_value(rpi_intensity.get(field))
        if (
            parsed is None
            or parsed < minimum
            or (field in integer_fields and not parsed.is_integer())
        ):
            violations.append(
                f"strategy.glft.rpi_intensity.{field} must be "
                f"at least {minimum:g}"
            )

    if require_local_evidence and not violations:
        if config_path is None:
            violations.append(
                "config_path is required for the Live local evidence gate"
            )
        else:
            try:
                validate_live_canary_local_evidence(
                    config,
                    config_path=config_path,
                    now_utc=now_utc,
                )
            except (TypeError, ValueError) as exc:
                violations.append(f"local Live evidence rejected: {exc}")

    if violations:
        raise ValueError(
            "Unsafe live trading configuration: " + "; ".join(violations)
        )
    return config
