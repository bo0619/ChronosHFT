import math
from collections.abc import Mapping

from infrastructure.paper_trade import is_paper_trade


INDEPENDENT_SUPERVISOR_SOURCE = "independent_supervisor"


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


def _section(config: Mapping, key: str) -> Mapping:
    value = config.get(key, {})
    return value if isinstance(value, Mapping) else {}


def _uses_paper_state_path(value) -> bool:
    normalized = str(value or "").strip().replace("\\", "/").lower()
    parts = tuple(part for part in normalized.split("/") if part not in {"", "."})
    return "paper" in parts


def validate_live_runtime_config(config: dict) -> dict:
    """Reject a live runtime that disables mandatory independent safety planes."""
    if not isinstance(config, dict):
        raise ValueError("Live runtime configuration must be a JSON object")
    if is_paper_trade(config):
        return config

    violations = []
    oms = _section(config, "oms")
    risk = _section(config, "risk")
    supervisor = _section(risk, "independent_supervisor")
    cash_flow = _section(risk, "cash_flow_truth")
    heartbeat = _section(risk, "risk_control_heartbeat")
    dead_man_switch = _section(oms, "venue_dead_man_switch")
    writer_fence = _section(oms, "single_writer_fence")

    if not _enabled(supervisor.get("enabled")):
        violations.append("risk.independent_supervisor.enabled must be true")
    if not _enabled(supervisor.get("flatten_enabled")):
        violations.append(
            "risk.independent_supervisor.flatten_enabled must be true"
        )
    if not _enabled(dead_man_switch.get("enabled")):
        violations.append("oms.venue_dead_man_switch.enabled must be true")

    if not _enabled(oms.get("journal_enabled", True)):
        violations.append("oms.journal_enabled must be true")
    if not _enabled(oms.get("replay_journal_on_startup", True)):
        violations.append("oms.replay_journal_on_startup must be true")

    journal_path = oms.get(
        "journal_path",
        "storage/oms/oms_journal.jsonl",
    )
    if _uses_paper_state_path(journal_path):
        violations.append("oms.journal_path must not use Paper state")

    fence_path = writer_fence.get("path", "")
    if fence_path and _uses_paper_state_path(fence_path):
        violations.append(
            "oms.single_writer_fence.path must not use Paper state"
        )

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

    if violations:
        raise ValueError(
            "Unsafe live trading configuration: " + "; ".join(violations)
        )
    return config
