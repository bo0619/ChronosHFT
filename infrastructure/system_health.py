from infrastructure.logger import logger


def handle_system_health_event(
    event,
    risk_controller,
    oms=None,
    risk_supervisor=None,
):
    message = event.data
    if not isinstance(message, str):
        message = str(message)
    if message.startswith("HALT:"):
        halt_reason = message[5:] or "unspecified"
        suspend_parent_heartbeat = getattr(
            risk_supervisor,
            "suspend_parent_heartbeat",
            None,
        )
        resume_shutdown_guard = getattr(
            risk_supervisor,
            "resume_shutdown_guard",
            None,
        )
        if callable(resume_shutdown_guard):
            try:
                supervisor_result = resume_shutdown_guard(
                    reason=f"oms_halt:{halt_reason}"
                )
            except Exception as exc:
                logger.critical(
                    "Independent risk supervisor HALT handoff raised: "
                    f"{type(exc).__name__}:{exc}"
                )
                if callable(suspend_parent_heartbeat):
                    try:
                        suspend_parent_heartbeat(
                            "oms_halt_handoff_exception"
                        )
                    except Exception as suspend_exc:
                        logger.critical(
                            "Could not suspend parent heartbeat after HALT "
                            f"handoff exception: {type(suspend_exc).__name__}:"
                            f"{suspend_exc}"
                        )
            else:
                if not (
                    isinstance(supervisor_result, dict)
                    and supervisor_result.get("accepted") is True
                    and supervisor_result.get("kill_latched") is True
                    and supervisor_result.get("persisted") is True
                ):
                    logger.critical(
                        "Independent risk supervisor did not durably accept "
                        f"OMS HALT handoff: {supervisor_result!r}"
                    )
                    if callable(suspend_parent_heartbeat):
                        try:
                            suspend_parent_heartbeat(
                                "oms_halt_handoff_unconfirmed"
                            )
                        except Exception as suspend_exc:
                            logger.critical(
                                "Could not suspend parent heartbeat after "
                                "unconfirmed HALT handoff: "
                                f"{type(suspend_exc).__name__}:{suspend_exc}"
                            )
        else:
            logger.critical(
                "Independent risk supervisor HALT handoff is unavailable"
            )
            if callable(suspend_parent_heartbeat):
                try:
                    suspend_parent_heartbeat(
                        "oms_halt_handoff_unavailable"
                    )
                except Exception as suspend_exc:
                    logger.critical(
                        "Could not suspend parent heartbeat after missing "
                        f"HALT handoff: {type(suspend_exc).__name__}:"
                        f"{suspend_exc}"
                    )
        if risk_controller is not None:
            try:
                risk_controller.trigger_kill_switch(
                    f"SystemHealth: {message}"
                )
            except Exception as exc:
                logger.critical(
                    "Parent risk kill failed after OMS HALT: "
                    f"{type(exc).__name__}:{exc}"
                )
        return

    if message.startswith("FREEZE_SYMBOL:") and oms is not None:
        _, symbol, reason = message.split(":", 2)
        oms.freeze_symbol(symbol, f"system_health:{reason}", cancel_active_orders=True)
        return

    if message.startswith("CLEAR_SYMBOL:") and oms is not None:
        _, symbol, reason = message.split(":", 2)
        if reason.startswith("ORDERBOOK_RESYNCED:"):
            _, recovery_token = reason.rsplit(":", 1)
            oms.clear_orderbook_freeze(
                symbol,
                recovery_token,
                reason=f"system_health:{reason}",
            )
        else:
            expected_reason = oms.get_symbol_freeze_reason(symbol)
            expected_epoch = oms.get_symbol_freeze_epoch(symbol)
            oms.clear_symbol_freeze(
                symbol,
                reason=f"system_health:{reason}",
                expected_epoch=expected_epoch,
                expected_reason=expected_reason,
            )
        return

    if message.startswith("FREEZE_STRATEGY:") and oms is not None:
        parts = message.split(":", 3)
        if len(parts) == 4:
            _, strategy_id, symbol, reason = parts
            oms.freeze_strategy(
                strategy_id,
                f"system_health:{reason}",
                symbol=symbol,
                cancel_active_orders=True,
            )
            return
        _, strategy_id, reason = parts
        oms.freeze_strategy(strategy_id, f"system_health:{reason}", cancel_active_orders=True)
        return

    if message.startswith("CLEAR_STRATEGY:") and oms is not None:
        parts = message.split(":", 3)
        if len(parts) == 4:
            _, strategy_id, symbol, reason = parts
            oms.clear_strategy_freeze(strategy_id, symbol=symbol, reason=f"system_health:{reason}")
            return
        _, strategy_id, reason = parts
        oms.clear_strategy_freeze(strategy_id, reason=f"system_health:{reason}")
        return

    if message.startswith("FREEZE_VENUE:") and oms is not None:
        _, venue, reason = message.split(":", 2)
        oms.freeze_venue(venue, f"system_health:{reason}", cancel_active_orders=True)
        return

    if message.startswith("CLEAR_VENUE:") and oms is not None:
        _, venue, reason = message.split(":", 2)
        oms.request_venue_recovery_verification(
            venue,
            reason=f"legacy_clear_request:{reason}",
        )
        return

    if message.startswith("VERIFY_VENUE:") and oms is not None:
        parts = message.split(":", 3)
        if len(parts) == 4:
            _, venue, raw_epoch, owner = parts
            try:
                expected_epoch = int(raw_epoch)
            except ValueError:
                expected_epoch = None
            if expected_epoch is not None and owner:
                owners = oms.get_venue_freeze_owners(venue)
                record = owners.get(owner, {})
                oms.request_venue_recovery_verification(
                    venue,
                    reason="system_health:TRANSPORT_RECOVERED",
                    expected_owner=owner,
                    expected_epoch=expected_epoch,
                    expected_reason=str(record.get("reason", "") or ""),
                )
                return
        _, venue, reason = message.split(":", 2)
        oms.request_venue_recovery_verification(
            venue,
            reason=f"system_health:{reason}",
        )
        return

    if message.startswith("KILL:"):
        risk_controller.trigger_kill_switch(message[5:])
        return

    if oms is not None and message.startswith(
        (
            "WS_TRANSPORT_DROP",
            "WS_PARSE_ERROR",
            "WS_HANDLER_FAILURE",
            "USER_STREAM_EXPIRED",
            "MARKET_DATA_STALE",
        )
    ):
        venue = getattr(getattr(oms, "gateway", None), "gateway_name", "UNKNOWN")
        oms.freeze_venue(venue, f"system_health:{message}", cancel_active_orders=True)
        return

    risk_controller.trigger_kill_switch(f"SystemHealth: {message}")
