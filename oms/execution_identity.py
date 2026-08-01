"""Parsing and compaction helpers for durable execution identities."""

from __future__ import annotations


def canonical_execution_trade_key(
    execution_id: object,
) -> tuple[str, int] | None:
    """Return ``(symbol, trade_id)`` for ``VENUE:SYMBOL:TRADE_ID`` IDs."""

    normalized = str(execution_id or "")
    parts = normalized.split(":")
    if len(parts) != 3:
        return None
    venue, symbol, raw_trade_id = parts
    venue = venue.strip().upper()
    symbol = symbol.strip().upper()
    if not venue or not symbol:
        return None
    try:
        trade_id = int(raw_trade_id)
    except (TypeError, ValueError):
        return None
    if trade_id < 0 or str(trade_id) != raw_trade_id:
        return None
    return symbol, trade_id


def discard_cursor_covered_execution_ids(
    execution_ids: set[str],
    *,
    symbol: str,
    trade_id: int,
) -> int:
    """Discard canonical IDs proven covered by a persisted REST cursor."""

    normalized_symbol = str(symbol or "").strip().upper()
    cursor = int(trade_id)
    if not normalized_symbol or cursor < 0:
        return 0
    covered = {
        execution_id
        for execution_id in execution_ids
        if (
            (trade_key := canonical_execution_trade_key(execution_id))
            is not None
            and trade_key[0] == normalized_symbol
            and trade_key[1] <= cursor
        )
    }
    execution_ids.difference_update(covered)
    return len(covered)


def retain_cursor_uncovered_execution_ids(
    execution_ids: set[str],
    trade_cursors: dict[str, int],
) -> set[str]:
    """Return IDs which cannot be replaced by a recovered REST cursor."""

    retained = set()
    for execution_id in execution_ids:
        trade_key = canonical_execution_trade_key(execution_id)
        if trade_key is None:
            retained.add(execution_id)
            continue
        symbol, trade_id = trade_key
        if trade_id > int(trade_cursors.get(symbol, -1)):
            retained.add(execution_id)
    return retained


__all__ = [
    "canonical_execution_trade_key",
    "discard_cursor_covered_execution_ids",
    "retain_cursor_uncovered_execution_ids",
]
