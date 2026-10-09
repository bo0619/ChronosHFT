"""Paper gateway on Lighter public market data.

Lighter Standard accounts hold orders in a speed bump before matching:
200ms for maker orders and 300ms for taker orders.
"""

from __future__ import annotations

from data.ref_data import ref_data_manager
from gateway.venue_paper import VenuePaperGateway

from .public_ws import LighterPublicWs

DEFAULT_MAKER_ORDER_DELAY_MS = 200.0
DEFAULT_TAKER_ORDER_DELAY_MS = 300.0


class LighterPaperGateway(VenuePaperGateway):
    """Lighter production-public-data gateway with a local paper venue."""

    venue_gateway_name = "LIGHTER_PAPER"
    default_maker_order_delay_ms = DEFAULT_MAKER_ORDER_DELAY_MS
    default_taker_order_delay_ms = DEFAULT_TAKER_ORDER_DELAY_MS

    def _market_ids(self) -> dict[str, int]:
        market_ids = {}
        for symbol in self.symbols:
            info = ref_data_manager.get_info(symbol)
            market_id = getattr(info, "market_id", None)
            if market_id is None:
                raise ValueError(f"No Lighter market id for {symbol}")
            market_ids[symbol] = int(market_id)
        return market_ids

    def _new_public_ws(self, generation: int):
        return LighterPublicWs(
            self._record_callback(generation),
            self._error_callback(generation),
            market_ids=self._market_ids(),
        )


__all__ = ["LighterPaperGateway"]
