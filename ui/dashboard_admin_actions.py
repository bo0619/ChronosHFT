"""Loopback-only operator actions for the local web dashboard."""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

from infrastructure.admin_control import coordinated_rearm

ADMIN_ACTION_PATH = "/api/admin/action"
ADMIN_ACTION_HEADER = "X-Chronos-Action"
ADMIN_ACTION_HEADER_VALUE = "dashboard"
ADMIN_ACTION_MAX_BYTES = 4096

_LOGGER = logging.getLogger(__name__)
_SECRET_PATTERNS = (
    re.compile(r"(?i)(signature|listenkey|api[_-]?key|api[_-]?secret)=([^&\s]+)"),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)([^\s]+)"),
)


def _redact_text(value: Any, limit: int = 1024) -> str:
    rendered = str(value or "")
    for pattern in _SECRET_PATTERNS:
        rendered = pattern.sub(lambda match: f"{match.group(1)}***", rendered)
    if len(rendered) > limit:
        return rendered[: max(0, limit - 1)] + "…"
    return rendered


def _is_loopback_host(host: str) -> bool:
    rendered = str(host or "").strip().strip("[]").lower()
    if rendered in {"localhost", "localhost."}:
        return True
    try:
        return ipaddress.ip_address(rendered).is_loopback
    except ValueError:
        return False


def _send_json(
    request_handler: Any,
    payload: Mapping[str, Any],
    status: int,
) -> None:
    request_handler._send(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        "application/json; charset=utf-8",
        status,
    )


def serve_dashboard_admin_post(owner: Any, request_handler: Any) -> None:
    """Validate and dispatch one dashboard admin POST request."""

    if not owner._valid_host_header(request_handler.headers.get("Host", "")):
        request_handler._send(
            b'{"error":"invalid_host"}',
            "application/json",
            400,
        )
        return

    path = urlsplit(request_handler.path).path
    if path != ADMIN_ACTION_PATH:
        request_handler._send(
            b'{"error":"not_found"}',
            "application/json; charset=utf-8",
            404,
        )
        return

    if request_handler.headers.get(ADMIN_ACTION_HEADER, "") != ADMIN_ACTION_HEADER_VALUE:
        request_handler._send(
            b'{"error":"forbidden","message":"missing dashboard action header"}',
            "application/json; charset=utf-8",
            403,
        )
        return

    origin = str(request_handler.headers.get("Origin", "") or "").strip()
    if origin:
        origin_host = urlsplit(origin).hostname or ""
        if not _is_loopback_host(origin_host):
            request_handler._send(
                b'{"error":"forbidden","message":"invalid origin"}',
                "application/json; charset=utf-8",
                403,
            )
            return

    content_type = request_handler.headers.get_content_type()
    if content_type != "application/json":
        request_handler._send(
            b'{"error":"unsupported_media_type","message":"admin actions require JSON"}',
            "application/json; charset=utf-8",
            415,
        )
        return

    try:
        content_length = int(
            request_handler.headers.get("Content-Length", "0") or 0
        )
    except ValueError:
        content_length = 0
    if content_length < 0 or content_length > ADMIN_ACTION_MAX_BYTES:
        request_handler._send(
            b'{"error":"payload_too_large"}',
            "application/json; charset=utf-8",
            413,
        )
        return

    try:
        raw = request_handler.rfile.read(content_length)
    except OSError as exc:
        _send_json(
            request_handler,
            {
                "error": "read_failed",
                "message": f"{type(exc).__name__}:{_redact_text(exc)}",
            },
            400,
        )
        return

    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _send_json(
            request_handler,
            {
                "error": "invalid_json",
                "message": f"{type(exc).__name__}:{_redact_text(exc)}",
            },
            400,
        )
        return

    response, status = handle_dashboard_admin_action(owner, payload)
    _send_json(request_handler, response, status)


def _invoke_dashboard_action(
    owner: Any,
    method_name: str,
    *args: Any,
    **kwargs: Any,
) -> tuple[bool, Any, str]:
    component = owner._components.get("oms")
    if component is None:
        return False, None, "oms_unavailable"
    method = getattr(component, method_name, None)
    if not callable(method):
        return False, None, f"{method_name}_unavailable"
    try:
        result = method(*args, **kwargs)
    except Exception as exc:
        return False, None, f"{method_name}:{type(exc).__name__}:{_redact_text(exc)}"
    if result is False:
        return False, result, f"{method_name}_refused"
    return True, result, ""


def _snapshot_response(
    owner: Any,
    *,
    accepted: bool,
    status: str,
    message: str,
    action: str,
    reason: str,
    symbol: str,
    extra: dict[str, Any] | None = None,
    code: int = 200,
) -> tuple[dict[str, Any], int]:
    try:
        owner.publish_snapshot(force=True)
    except Exception as exc:
        _LOGGER.debug(
            "Dashboard snapshot publish failed after admin action: %s:%s",
            type(exc).__name__,
            _redact_text(exc),
        )

    response = {
        "accepted": accepted,
        "status": status,
        "message": message,
        "action": action,
        "reason": reason,
        "symbol": symbol,
        "snapshot": owner.get_snapshot(),
    }
    if extra:
        response.update(extra)
    return response, code


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _clean_detail(value: Any, limit: int = 512) -> str:
    if value is None:
        return ""
    return _redact_text(value, limit=limit).strip()


def _build_rearm_refusal_diagnostic(
    snapshot: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    system = _mapping(snapshot.get("system"))
    oms = _mapping(system.get("oms"))
    capability = _mapping(oms.get("capability"))
    dms = _mapping(capability.get("venue_dead_man_switch"))
    heartbeat = _mapping(capability.get("risk_control_heartbeat"))
    risk = _mapping(snapshot.get("risk"))
    risk_status = _mapping(risk.get("status"))

    result_reason = _clean_detail(result.get("reason"), limit=160)
    kill_state = _clean_detail(risk_status.get("kill_state"), limit=80)
    kill_reason = _clean_detail(risk_status.get("kill_reason"), limit=512)
    oms_state = _clean_detail(oms.get("state"), limit=80)
    capability_mode = _clean_detail(
        oms.get("capability_mode") or capability.get("mode"),
        limit=80,
    )
    dms_reason = _clean_detail(dms.get("reason"), limit=512)
    dms_valid = dms.get("valid") if "valid" in dms else None
    heartbeat_valid = heartbeat.get("valid") if "valid" in heartbeat else None
    heartbeat_reason = _clean_detail(heartbeat.get("reason"), limit=256)

    details: dict[str, Any] = {
        "result_reason": result_reason,
    }
    for key, value in (
        ("oms_state", oms_state),
        ("capability_mode", capability_mode),
        ("manual_rearm_required", oms.get("manual_rearm_required")),
        ("kill_switch_triggered", risk_status.get("kill_switch_triggered")),
        ("kill_state", kill_state),
        ("kill_reason", kill_reason),
        ("dms_valid", dms_valid),
        ("dms_reason", dms_reason),
        ("risk_heartbeat_valid", heartbeat_valid),
        ("risk_heartbeat_reason", heartbeat_reason),
    ):
        if value not in ("", None):
            details[key] = value

    code = "rearm_refused"
    summary = "Rearm 被拒：后端没有接受本次恢复请求。"
    next_steps = [
        "确认无挂单、无持仓。",
        "等待 risk.kill_state 变为 FLAT_VERIFIED。",
        "再点击 Rearm。",
    ]

    if result_reason == "risk_manager_flat_state_not_verified":
        code = "flat_state_not_verified"
        summary = "Rearm 被拒：Kill Switch 尚未完成全平验证。"
        if kill_state:
            summary += f" 当前 kill_state={kill_state}。"
        if "PAPER_DMS_TRIGGERED" in kill_reason:
            code = "paper_dms_triggered"
            summary = (
                "Rearm 被拒：Paper DMS 到期触发 Kill Switch，"
                "需要先恢复续租/控制循环并完成全平验证。"
            )
            next_steps = [
                "确认所有标的无挂单、无持仓。",
                "让主程序继续运行，等待 kill_state 进入 FLAT_VERIFIED。",
                "如果 DMS 仍 stale，干净重启主程序以恢复 DMS 续租。",
                "确认 DMS valid=true 后再点击 Rearm。",
            ]
        elif kill_state and kill_state != "FLAT_VERIFIED":
            next_steps = [
                "确认无挂单、无持仓。",
                "等待 kill switch 验证线程把 kill_state 推进到 FLAT_VERIFIED。",
                "若长时间停在 FAILED/CANCEL_PENDING/FLATTENING，检查日志中的 KillSwitch/truth/DMS 错误。",
                "再点击 Rearm。",
            ]

    if dms_valid is False and dms_reason:
        details["blocking_hint"] = f"DMS unhealthy: {dms_reason}"

    return {
        "code": code,
        "summary": summary,
        "details": details,
        "next_steps": next_steps,
    }


def _base_error(
    *,
    action: str,
    reason: str,
    symbol: str,
    status: str,
    message: str,
    code: int,
) -> tuple[dict[str, Any], int]:
    return (
        {
            "accepted": False,
            "status": status,
            "message": message,
            "action": action,
            "reason": reason,
            "symbol": symbol,
        },
        code,
    )


def handle_dashboard_admin_action(
    owner: Any,
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], int]:
    if not isinstance(payload, Mapping):
        return (
            {
                "accepted": False,
                "status": "invalid",
                "message": "Admin action payload must be a JSON object.",
            },
            400,
        )

    action = str(payload.get("action", "") or "").strip().lower()
    reason = str(payload.get("reason", "") or "").strip() or "dashboard_action"
    symbol = str(payload.get("symbol", "") or "").strip().upper()
    if not action:
        return (
            {
                "accepted": False,
                "status": "invalid",
                "message": "Missing admin action.",
            },
            400,
        )

    oms = owner._components.get("oms")
    risk_manager = owner._components.get("risk_manager")
    risk_supervisor = owner._components.get("risk_supervisor")

    if action == "rearm":
        return _handle_rearm(
            owner,
            oms=oms,
            risk_manager=risk_manager,
            risk_supervisor=risk_supervisor,
            action=action,
            reason=reason,
            symbol=symbol,
        )

    if action == "flatten_all":
        return _handle_flatten_all(
            owner,
            oms=oms,
            action=action,
            reason=reason,
            symbol=symbol,
        )

    if action == "flatten_symbol":
        return _handle_flatten_symbol(
            owner,
            oms=oms,
            action=action,
            reason=reason,
            symbol=symbol,
        )

    return _base_error(
        action=action,
        reason=reason,
        symbol=symbol,
        status="unsupported",
        message=f"Unsupported admin action: {action or 'empty'}",
        code=400,
    )


def _handle_rearm(
    owner: Any,
    *,
    oms: Any,
    risk_manager: Any,
    risk_supervisor: Any,
    action: str,
    reason: str,
    symbol: str,
) -> tuple[dict[str, Any], int]:
    if oms is None or risk_manager is None:
        return _base_error(
            action=action,
            reason=reason,
            symbol=symbol,
            status="unavailable",
            message="OMS or risk manager is unavailable.",
            code=503,
        )

    try:
        result = coordinated_rearm(
            oms,
            reason,
            risk_manager=risk_manager,
            risk_supervisor=risk_supervisor,
        )
    except Exception as exc:
        return _base_error(
            action=action,
            reason=reason,
            symbol=symbol,
            status="error",
            message=f"Rearm failed: {type(exc).__name__}:{_redact_text(exc)}",
            code=500,
        )

    accepted = bool(result.get("accepted", False))
    message = (
        "Rearm completed."
        if accepted
        else f"Rearm refused: {result.get('reason', 'unknown')}"
    )
    response, code = _snapshot_response(
        owner,
        accepted=accepted,
        status="ok" if accepted else "rejected",
        message=message,
        action=action,
        reason=reason,
        symbol=symbol,
        extra={"result": deepcopy(dict(result))},
        code=200 if accepted else 409,
    )
    if not accepted:
        response["diagnostic"] = _build_rearm_refusal_diagnostic(
            _mapping(response.get("snapshot")),
            _mapping(result),
        )
    return response, code


def _handle_flatten_all(
    owner: Any,
    *,
    oms: Any,
    action: str,
    reason: str,
    symbol: str,
) -> tuple[dict[str, Any], int]:
    if oms is None:
        return _base_error(
            action=action,
            reason=reason,
            symbol=symbol,
            status="unavailable",
            message="OMS is unavailable.",
            code=503,
        )

    warnings: list[str] = []
    close_gate = getattr(oms, "close_outbound_gate", None)
    if callable(close_gate):
        try:
            gate_result = close_gate(reason, wait=True)
            if gate_result is False:
                warnings.append("close_outbound_gate_refused")
        except Exception as exc:
            warnings.append(f"close_outbound_gate:{type(exc).__name__}")

    flatten_ok, submitted, flatten_error = _invoke_dashboard_action(
        owner,
        "emergency_reduce_only_flatten",
        reason,
    )
    if not flatten_ok:
        return _base_error(
            action=action,
            reason=reason,
            symbol=symbol,
            status="rejected",
            message=f"Flatten all refused: {flatten_error}",
            code=409,
        )

    halt = getattr(oms, "halt_system", None)
    if not callable(halt):
        return (
            {
                "accepted": False,
                "status": "unavailable",
                "message": "OMS halt_system is unavailable.",
                "action": action,
                "reason": reason,
                "symbol": symbol,
                "submitted": int(submitted or 0),
            },
            503,
        )
    try:
        halt_result = halt(reason)
        halt_ok = halt_result is not False
    except Exception as exc:
        return (
            {
                "accepted": False,
                "status": "error",
                "message": (
                    "Global flatten halt failed: "
                    f"{type(exc).__name__}:{_redact_text(exc)}"
                ),
                "action": action,
                "reason": reason,
                "symbol": symbol,
                "submitted": int(submitted or 0),
            },
            500,
        )

    if not halt_ok:
        return (
            {
                "accepted": False,
                "status": "rejected",
                "message": "Global flatten submitted, but OMS halt was refused.",
                "action": action,
                "reason": reason,
                "symbol": symbol,
                "submitted": int(submitted or 0),
            },
            409,
        )

    return _snapshot_response(
        owner,
        accepted=True,
        status="ok",
        message=f"一键平仓已提交，已发送 {int(submitted or 0)} 笔 reduce-only 平仓单。",
        action=action,
        reason=reason,
        symbol=symbol,
        extra={
            "submitted": int(submitted or 0),
            "warnings": warnings,
            "halted": True,
        },
    )


def _handle_flatten_symbol(
    owner: Any,
    *,
    oms: Any,
    action: str,
    reason: str,
    symbol: str,
) -> tuple[dict[str, Any], int]:
    if oms is None:
        return _base_error(
            action=action,
            reason=reason,
            symbol=symbol,
            status="unavailable",
            message="OMS is unavailable.",
            code=503,
        )
    if not symbol:
        return _base_error(
            action=action,
            reason=reason,
            symbol=symbol,
            status="invalid",
            message="Missing symbol for symbol flatten.",
            code=400,
        )

    flatten_ok, submitted, flatten_error = _invoke_dashboard_action(
        owner,
        "emergency_reduce_only_flatten",
        reason,
        symbol=symbol,
    )
    if not flatten_ok:
        return _base_error(
            action=action,
            reason=reason,
            symbol=symbol,
            status="rejected",
            message=f"{symbol} flatten refused: {flatten_error}",
            code=409,
        )

    freeze = getattr(oms, "freeze_symbol", None)
    if not callable(freeze):
        return (
            {
                "accepted": False,
                "status": "unavailable",
                "message": "OMS freeze_symbol is unavailable.",
                "action": action,
                "reason": reason,
                "symbol": symbol,
                "submitted": int(submitted or 0),
            },
            503,
        )
    try:
        freeze_result = freeze(symbol, reason, cancel_active_orders=True)
        freeze_ok = freeze_result is not False
    except Exception as exc:
        return (
            {
                "accepted": False,
                "status": "error",
                "message": f"{symbol} freeze failed: {type(exc).__name__}:{_redact_text(exc)}",
                "action": action,
                "reason": reason,
                "symbol": symbol,
                "submitted": int(submitted or 0),
            },
            500,
        )

    if not freeze_ok:
        return (
            {
                "accepted": False,
                "status": "rejected",
                "message": f"{symbol} flatten submitted, but freeze was refused.",
                "action": action,
                "reason": reason,
                "symbol": symbol,
                "submitted": int(submitted or 0),
            },
            409,
        )

    return _snapshot_response(
        owner,
        accepted=True,
        status="ok",
        message=f"{symbol} 平仓已提交，已发送 {int(submitted or 0)} 笔 reduce-only 平仓单。",
        action=action,
        reason=reason,
        symbol=symbol,
        extra={
            "submitted": int(submitted or 0),
            "frozen": True,
        },
    )
