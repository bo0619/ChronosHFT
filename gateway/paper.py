"""Select the Paper gateway for the configured execution venue."""

from infrastructure.venue import VENUE_LIGHTER, configured_venue


def paper_gateway_type(config):
    if configured_venue(config) == VENUE_LIGHTER:
        from gateway.lighter.paper_gateway import LighterPaperGateway

        return LighterPaperGateway
    from gateway.binance.paper_gateway import BinancePaperGateway

    return BinancePaperGateway
