"""Durable audit enrichment and external OMS state publication."""

from __future__ import annotations

import json

from event.type import (
    Event,
    OrderIntent,
    EVENT_ORDER_UPDATE,
    EVENT_POSITION_UPDATE,
)

from .component import OMSComponent
from .order import Order


class OMSStatePublisher(OMSComponent):
    """Own audit context and event payload publication for OMS state."""

    OWNER_READS = frozenset(
        {
            "audit_logger",
            "capability_mode",
            "capability_reason",
            "event_engine",
            "exposure",
            "lock",
            "mode_override",
            "mode_override_reason",
            "state",
        }
    )

    @staticmethod
    def _serialize_intent(intent: OrderIntent) -> dict:
        return {
            "strategy_id": intent.strategy_id,
            "symbol": intent.symbol,
            "side": intent.side.value,
            "price": intent.price,
            "volume": intent.volume,
            "order_type": intent.order_type,
            "time_in_force": intent.time_in_force,
            "is_post_only": intent.is_post_only,
            "reduce_only": intent.reduce_only,
            "policy": intent.policy.value,
            "tag": intent.tag,
            "calibration_permit_id": intent.calibration_permit_id,
            "calibration_depth_bps": intent.calibration_depth_bps,
            "calibration_reference_mid": intent.calibration_reference_mid,
        }

    def _audit(self, kind: str, **payload):
        payload.setdefault("state", self.state.value)
        payload.setdefault("capability_mode", self.capability_mode.value)
        payload.setdefault("capability_reason", self.capability_reason)
        payload.setdefault(
            "mode_override",
            self.mode_override.value if self.mode_override else "",
        )
        payload.setdefault("mode_override_reason", self.mode_override_reason)
        self.audit_logger.audit(kind, payload)

    def record_rpi_commission_truth(
        self,
        rates_by_symbol: dict,
        *,
        accepted: bool,
        reason: str,
        source: str,
    ) -> bool:
        """Persist one independent runtime commission-truth observation."""
        if not isinstance(rates_by_symbol, dict):
            raise TypeError("RPI commission truth must be a mapping")
        canonical_rates = json.loads(
            json.dumps(
                rates_by_symbol,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        with self.lock:
            self._audit(
                "rpi_commission_truth",
                rates_by_symbol=canonical_rates,
                accepted=bool(accepted),
                reason=str(reason or ""),
                source=str(source or ""),
            )
        return True

    def _emit_order_update(self, order: Order):
        self.event_engine.put(Event(EVENT_ORDER_UPDATE, order.to_snapshot()))

    def _emit_position_update(self, symbol: str):
        self.event_engine.put(
            Event(
                EVENT_POSITION_UPDATE,
                self.exposure.get_position_data(symbol),
            )
        )
