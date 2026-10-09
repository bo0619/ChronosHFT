"""Select the Paper gateway for the configured execution venue."""

from infrastructure.venue import VENUE_GRVT, VENUE_LIGHTER, configured_venue


def paper_gateway_type(config):
    venue = configured_venue(config)
    if venue == VENUE_LIGHTER:
        from gateway.lighter.paper_gateway import LighterPaperGateway

        return LighterPaperGateway
    if venue == VENUE_GRVT:
        from gateway.grvt.paper_gateway import GrvtPaperGateway

        return GrvtPaperGateway
    from gateway.binance.paper_gateway import BinancePaperGateway

    return BinancePaperGateway
