import time

from event.type import Event, EVENT_SYSTEM_HEALTH, OMSCapabilityMode
from infrastructure.logger import logger


def emit_market_data_stale_if_needed(
    event_engine,
    last_tick_time: float,
    triggered: bool,
    threshold_sec: float = 60.0,
    now: float = None,
) -> bool:
    if triggered or last_tick_time <= 0:
        return triggered

    now = time.time() if now is None else now
    silence_sec = now - last_tick_time
    if silence_sec <= threshold_sec:
        return triggered

    message = f"MARKET_DATA_STALE:{silence_sec:.1f}s>{threshold_sec:.1f}s"
    logger.critical(f"SYSTEM WATCHDOG: {message}")
    event_engine.put(Event(EVENT_SYSTEM_HEALTH, message))
    return True


def emit_event_engine_backlog_if_needed(
    event_engine,
    oms,
    venue: str,
    state: dict = None,
    config: dict = None,
):
    if event_engine is None or oms is None or not hasattr(event_engine, "get_metrics_snapshot"):
        return state or {}

    state = dict(state or {})
    config = config or {}
    snapshot = event_engine.get_metrics_snapshot()
    lanes = snapshot.get("lanes", {})
    venue = (venue or "UNKNOWN").upper()

    severity, lane, reason = _event_engine_severity(lanes, config)
    previous_severity = int(state.get("severity", 0))
    recovery_checks = max(1, int(config.get("recovery_checks", 20)))

    if severity <= 0:
        healthy_checks = int(state.get("healthy_checks", 0)) + 1
        state["healthy_checks"] = healthy_checks
        if previous_severity > 0 and healthy_checks >= recovery_checks:
            reason_prefix = ("event_engine_backlog:",)
            if hasattr(oms, "clear_trading_mode"):
                oms.clear_trading_mode(reason="event engine backlog recovered", prefixes=reason_prefix)
            venue_reason = getattr(oms, "get_venue_freeze_reason", lambda *_args, **_kwargs: "")(venue)
            if venue_reason.startswith("event_engine_backlog:") and hasattr(oms, "clear_venue_freeze"):
                oms.clear_venue_freeze(venue, reason="event engine backlog recovered")
            logger.info("[Watchdog] Event engine backlog recovered")
            return {
                "severity": 0,
                "healthy_checks": 0,
                "reason": "",
                "lane": "",
            }
        return state

    state["healthy_checks"] = 0
    if severity < previous_severity and previous_severity >= 3:
        state["severity"] = previous_severity
        return state
    if severity == previous_severity and reason == state.get("reason"):
        return state

    state.update(
        {
            "severity": severity,
            "healthy_checks": 0,
            "reason": reason,
            "lane": lane,
        }
    )

    if severity >= 3:
        logger.error(f"[Watchdog] Event engine backlog freeze: {reason}")
        oms.freeze_venue(venue, reason, cancel_active_orders=True)
        return state

    if severity == 2:
        logger.warning(f"[Watchdog] Event engine backlog passive-only: {reason}")
        oms.set_trading_mode(OMSCapabilityMode.PASSIVE_ONLY, reason)
        return state

    logger.warning(f"[Watchdog] Event engine backlog degraded: {reason}")
    oms.set_trading_mode(OMSCapabilityMode.DEGRADED, reason)
    return state


def emit_strategy_runtime_backlog_if_needed(
    strategy_runtime,
    oms,
    strategy_id: str,
    state: dict = None,
    config: dict = None,
):
    if strategy_runtime is None or oms is None or not hasattr(strategy_runtime, "get_metrics_snapshot"):
        return state or {}

    strategy_id = (strategy_id or "").strip()
    if not strategy_id:
        return state or {}

    state = dict(state or {})
    config = config or {}
    snapshot = strategy_runtime.get_metrics_snapshot()
    severity, reason = _strategy_runtime_severity(snapshot, config)
    previous_severity = int(state.get("severity", 0))
    recovery_checks = max(1, int(config.get("recovery_checks", 20)))

    if severity <= 0:
        healthy_checks = int(state.get("healthy_checks", 0)) + 1
        state["healthy_checks"] = healthy_checks
        if previous_severity > 0 and healthy_checks >= recovery_checks:
            previous_reason = getattr(oms, "get_strategy_freeze_reason", lambda *_args, **_kwargs: "")(strategy_id)
            if previous_reason.startswith("strategy_runtime_backlog:") and hasattr(oms, "clear_strategy_freeze"):
                oms.clear_strategy_freeze(strategy_id, reason="strategy runtime backlog recovered")
                logger.info(f"[Watchdog] Strategy runtime backlog recovered for {strategy_id}")
            return {
                "severity": 0,
                "healthy_checks": 0,
                "reason": "",
            }
        return state

    state["healthy_checks"] = 0
    if severity == previous_severity and reason == state.get("reason"):
        return state

    state.update(
        {
            "severity": severity,
            "healthy_checks": 0,
            "reason": reason,
        }
    )

    if severity >= 2:
        logger.error(f"[Watchdog] Strategy runtime freeze {strategy_id}: {reason}")
        oms.freeze_strategy(strategy_id, reason, cancel_active_orders=True)
        return state

    logger.warning(f"[Watchdog] Strategy runtime warning {strategy_id}: {reason}")
    return state


def _event_engine_severity(lanes: dict, config: dict):
    hottest_lane = ""
    hottest_reason = ""
    hottest_severity = 0

    for lane in ("market", "execution"):
        lane_stats = lanes.get(lane, {})
        if not lane_stats:
            continue
        severity, reason = _lane_severity(lane, lane_stats, config)
        if severity <= hottest_severity:
            continue
        hottest_severity = severity
        hottest_lane = lane
        hottest_reason = reason

    return hottest_severity, hottest_lane, hottest_reason


def _strategy_runtime_severity(metrics: dict, config: dict):
    control_depth = int(metrics.get("control_depth", 0) or 0)
    market_depth = int(metrics.get("market_depth", 0) or 0)
    total_depth = control_depth + market_depth
    async_worker = metrics.get("async_worker", {}) or {}
    deferred_depth = int(async_worker.get("deferred_depth", 0) or 0)
    backlog_ms = max(
        float(metrics.get("oldest_control_wait_ms", 0.0) or 0.0),
        float(metrics.get("oldest_market_wait_ms", 0.0) or 0.0),
        float(metrics.get("inflight_wait_ms", 0.0) or 0.0),
        float(metrics.get("inflight_ms", 0.0) or 0.0),
    )
    kind = metrics.get("inflight_kind") or metrics.get("last_kind") or "-"
    reason = (
        f"strategy_runtime_backlog:kind={kind}:control={control_depth}:market={market_depth}:"
        f"deferred={deferred_depth}:backlog={backlog_ms:.1f}ms"
    )

    if (
        _metric_trip(total_depth, backlog_ms, config, "freeze_queue_depth", "freeze_backlog_ms", 80, 1500.0)
        or deferred_depth >= int(config.get("alpha_process_freeze_deferred", 32))
        or (async_worker and not async_worker.get("alive", True))
    ):
        return 2, reason
    if (
        _metric_trip(total_depth, backlog_ms, config, "warn_queue_depth", "warn_backlog_ms", 20, 400.0)
        or deferred_depth >= int(config.get("alpha_process_warn_deferred", 8))
    ):
        return 1, reason
    return 0, reason


def _lane_severity(lane: str, lane_stats: dict, config: dict):
    backlog_ms = max(
        float(lane_stats.get("oldest_queued_ms", 0.0) or 0.0),
        float(lane_stats.get("inflight_ms", 0.0) or 0.0),
        float(lane_stats.get("handler_inflight_ms", 0.0) or 0.0),
    )
    depth = int(lane_stats.get("depth", 0) or 0)
    event_type = lane_stats.get("inflight_event_type") or lane_stats.get("last_event_type") or "-"
    handler_name = lane_stats.get("inflight_handler_name") or "-"
    reason = (
        f"event_engine_backlog:{lane}:event={event_type}:handler={handler_name}:"
        f"backlog={backlog_ms:.1f}ms:depth={depth}"
    )

    if _lane_trip(depth, backlog_ms, config, lane, "freeze_queue_depth", "freeze_backlog_ms", 100, 1500.0):
        return 3, reason
    if _lane_trip(
        depth,
        backlog_ms,
        config,
        lane,
        "passive_only_queue_depth",
        "passive_only_backlog_ms",
        50,
        600.0,
    ):
        return 2, reason
    if _lane_trip(
        depth,
        backlog_ms,
        config,
        lane,
        "degraded_queue_depth",
        "degraded_backlog_ms",
        20,
        250.0,
    ):
        return 1, reason
    return 0, reason


def _lane_trip(
    depth: int,
    backlog_ms: float,
    config: dict,
    lane: str,
    depth_key: str,
    backlog_key: str,
    default_depth: int,
    default_backlog_ms: float,
):
    depth_threshold = int(_lane_config_value(config.get(depth_key), lane, default_depth))
    backlog_threshold = float(_lane_config_value(config.get(backlog_key), lane, default_backlog_ms))
    return depth >= depth_threshold or backlog_ms >= backlog_threshold


def _lane_config_value(raw_value, lane: str, default):
    if isinstance(raw_value, (int, float)):
        return raw_value
    raw_value = raw_value or {}
    fallback = raw_value.get("hot", default) if lane in {"market", "execution"} else raw_value.get("cold", default)
    return raw_value.get(lane, fallback)


def _metric_trip(depth: int, backlog_ms: float, config: dict, depth_key: str, backlog_key: str, default_depth: int, default_backlog_ms: float):
    depth_threshold = int(config.get(depth_key, default_depth))
    backlog_threshold = float(config.get(backlog_key, default_backlog_ms))
    return depth >= depth_threshold or backlog_ms >= backlog_threshold
