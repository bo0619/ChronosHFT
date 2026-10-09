"""Execution venue selection (``execution.venue``)."""

from __future__ import annotations

from collections.abc import Mapping

VENUE_BINANCE = "binance"
VENUE_LIGHTER = "lighter"
SUPPORTED_VENUES = (VENUE_BINANCE, VENUE_LIGHTER)
# Venues whose Live gateway exists; the rest are Paper-only for now.
LIVE_VENUES = (VENUE_BINANCE,)


def configured_venue(config: Mapping | None) -> str:
    """Return ``execution.venue``; absent means Binance."""
    execution = (
        config.get("execution", {}) if isinstance(config, Mapping) else {}
    )
    if not isinstance(execution, Mapping):
        execution = {}
    venue = str(execution.get("venue", VENUE_BINANCE) or VENUE_BINANCE)
    venue = venue.strip().lower()
    if venue not in SUPPORTED_VENUES:
        raise ValueError(f"Unsupported execution.venue: {venue!r}")
    return venue


__all__ = [
    "LIVE_VENUES",
    "SUPPORTED_VENUES",
    "VENUE_BINANCE",
    "VENUE_LIGHTER",
    "configured_venue",
]
