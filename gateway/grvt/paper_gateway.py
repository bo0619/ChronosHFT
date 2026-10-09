"""Paper gateway on GRVT public market data.

GRVT holds every order that is not post-only in a 25ms speed bump before it
reaches matching; post-only orders go straight to the book.
"""

from __future__ import annotations

from data.ref_data import ref_data_manager
from event.type import TIF_GTX, TIF_RPI, OrderRequest
from gateway.venue_paper import VenuePaperGateway

from .public_ws import GrvtPublicWs

DEFAULT_MAKER_ORDER_DELAY_MS = 0.0
DEFAULT_TAKER_ORDER_DELAY_MS = 25.0


class GrvtPaperGateway(VenuePaperGateway):
    """GRVT production-public-data gateway with a local paper venue."""

    venue_gateway_name = "GRVT_PAPER"
    default_maker_order_delay_ms = DEFAULT_MAKER_ORDER_DELAY_MS
    default_taker_order_delay_ms = DEFAULT_TAKER_ORDER_DELAY_MS

    def _order_delay_sec(self, request: OrderRequest) -> float:
        # GRVT's speed bump keys on post-only, not on whether the order
        # crosses: a resting non-post-only limit is delayed too.
        if request.post_only or request.time_in_force in {TIF_GTX, TIF_RPI}:
            return self.maker_order_delay_sec
        return self.taker_order_delay_sec

    def _instruments(self) -> dict[str, str]:
        instruments = {}
        for symbol in self.symbols:
            info = ref_data_manager.get_info(symbol)
            instrument = getattr(info, "venue_symbol", None)
            if not instrument:
                raise ValueError(f"No GRVT instrument for {symbol}")
            instruments[symbol] = str(instrument)
        return instruments

    def _new_public_ws(self, generation: int):
        return GrvtPublicWs(
            self._record_callback(generation),
            self._error_callback(generation),
            instruments=self._instruments(),
        )


__all__ = ["GrvtPaperGateway"]
