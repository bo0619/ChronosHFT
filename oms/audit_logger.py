"""Durable OMS audit and command-record construction."""

from __future__ import annotations

from event.type import OrderRequest

from .journal import JournalError, OMSJournal
from .order import Order


class OMSAuditLogger:
    """Own durable audit payloads without owning OMS lifecycle state."""

    def __init__(self, journal: OMSJournal):
        self.journal = journal

    @staticmethod
    def command_prepared_payload(
        command_id: str,
        command_type: str,
        order: Order,
        request,
    ) -> dict:
        if isinstance(request, OrderRequest):
            request_payload = {
                "symbol": request.symbol,
                "price": request.price,
                "volume": request.volume,
                "side": request.side,
                "order_type": request.order_type,
                "time_in_force": request.time_in_force,
                "post_only": request.post_only,
                "reduce_only": request.reduce_only,
                "self_trade_prevention_mode": (
                    request.self_trade_prevention_mode
                ),
            }
        else:
            request_payload = {
                "symbol": request.symbol,
                "order_id": request.order_id,
            }
        return {
            "command_id": command_id,
            "command_type": command_type,
            "idempotency_key": order.client_oid,
            "client_oid": order.client_oid,
            "exchange_oid": order.exchange_oid,
            "order": order.to_record(),
            "request": request_payload,
        }

    def record_command_prepared(
        self,
        command_id: str,
        command_type: str,
        order: Order,
        request,
    ) -> int:
        return self.journal.append(
            "command_prepared",
            self.command_prepared_payload(
                command_id,
                command_type,
                order,
                request,
            ),
        )

    def build_submit_prepared_records(
        self,
        command_id: str,
        order: Order,
        request: OrderRequest,
        snapshot_source: str,
        **snapshot_extra,
    ) -> tuple[tuple[str, dict], tuple[str, dict]]:
        order_payload = order.to_record()
        order_payload["source"] = snapshot_source
        if snapshot_extra:
            order_payload["extra"] = dict(snapshot_extra)
        return (
            ("order_snapshot", order_payload),
            (
                "command_prepared",
                self.command_prepared_payload(
                    command_id,
                    "SUBMIT",
                    order,
                    request,
                ),
            ),
        )

    def record_submit_prepared_batch(
        self,
        records: tuple[tuple[str, dict], tuple[str, dict]],
    ) -> list[int]:
        append_batch = getattr(self.journal, "append_batch", None)
        if callable(append_batch):
            committed = list(append_batch(records))
        else:
            committed = [
                self.journal.append(kind, payload)
                for kind, payload in records
            ]
        if (
            bool(getattr(self.journal, "enabled", True))
            and (len(committed) != 2 or not all(committed))
        ):
            raise JournalError(
                "Submit preparation WAL batch was not committed"
            )
        return committed

    def record_command_result(
        self,
        command_id: str,
        command_type: str,
        order: Order,
        outcome: str,
        *,
        exchange_oid: str = "",
        error_code: str = "",
        error_message: str = "",
    ) -> int:
        return self.journal.append(
            "command_result",
            {
                "command_id": command_id,
                "command_type": command_type,
                "idempotency_key": order.client_oid,
                "client_oid": order.client_oid,
                "exchange_oid": exchange_oid or order.exchange_oid,
                "outcome": str(outcome),
                "error_code": error_code,
                "error_message": error_message,
            },
        )

    def audit(self, kind: str, payload: dict) -> int:
        return self.journal.append(kind, payload)

    def record_order_snapshot(
        self,
        order: Order,
        source: str,
        **extra,
    ) -> int:
        payload = order.to_record()
        payload["source"] = source
        if extra:
            payload["extra"] = extra
        return self.journal.append("order_snapshot", payload)
