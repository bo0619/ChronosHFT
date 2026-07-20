"""List Binance USD-M contracts that currently support RPI orders.

The source of truth is ``GET /fapi/v1/exchangeInfo``. A symbol is considered
eligible when its ``permissionSets`` contains ``RPI``. By default, contracts
whose status is not ``TRADING`` are excluded.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import requests


MAINNET_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
TESTNET_EXCHANGE_INFO_URL = "https://testnet.binancefuture.com/fapi/v1/exchangeInfo"

OUTPUT_FIELDS = (
    "symbol",
    "contractType",
    "status",
    "baseAsset",
    "quoteAsset",
    "marginAsset",
    "onboardDate",
    "permissionSets",
)


def flatten_permissions(value: Any) -> frozenset[str]:
    """Normalize Binance's occasionally nested ``permissionSets`` value."""
    pending = [value]
    permissions: set[str] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            permissions.add(current.upper())
        elif isinstance(current, Mapping):
            pending.extend(current.values())
        elif isinstance(current, (list, tuple, set, frozenset)):
            pending.extend(current)
    return frozenset(permissions)


def fetch_exchange_info(
    url: str = MAINNET_EXCHANGE_INFO_URL,
    *,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Fetch and validate the public USD-M exchange information payload."""
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise ValueError("Binance exchangeInfo response has no symbols list")
    return payload


def select_rpi_contracts(
    symbols: Iterable[Mapping[str, Any]],
    *,
    trading_only: bool = True,
    quote_assets: Sequence[str] = (),
    contract_types: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Return sorted RPI-enabled contracts matching the optional filters."""
    quote_filter = {str(value).upper() for value in quote_assets if value}
    contract_filter = {str(value).upper() for value in contract_types if value}
    selected: list[dict[str, Any]] = []

    for raw_symbol in symbols:
        permissions = flatten_permissions(raw_symbol.get("permissionSets", []))
        if "RPI" not in permissions:
            continue

        status = str(raw_symbol.get("status", "") or "").upper()
        quote_asset = str(raw_symbol.get("quoteAsset", "") or "").upper()
        contract_type = str(raw_symbol.get("contractType", "") or "").upper()
        if trading_only and status != "TRADING":
            continue
        if quote_filter and quote_asset not in quote_filter:
            continue
        if contract_filter and contract_type not in contract_filter:
            continue

        selected.append(
            {
                "symbol": str(raw_symbol.get("symbol", "") or "").upper(),
                "contractType": contract_type,
                "status": status,
                "baseAsset": str(raw_symbol.get("baseAsset", "") or "").upper(),
                "quoteAsset": quote_asset,
                "marginAsset": str(raw_symbol.get("marginAsset", "") or "").upper(),
                "onboardDate": raw_symbol.get("onboardDate"),
                "permissionSets": sorted(permissions),
            }
        )

    return sorted(
        selected,
        key=lambda item: (
            item["quoteAsset"],
            item["contractType"],
            item["symbol"],
        ),
    )


def _iso_timestamp(timestamp_ms: Any) -> str:
    try:
        timestamp = float(timestamp_ms) / 1000.0
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def render_table(contracts: Sequence[Mapping[str, Any]]) -> str:
    """Render a dependency-free fixed-width table."""
    columns = (
        ("SYMBOL", "symbol"),
        ("CONTRACT_TYPE", "contractType"),
        ("STATUS", "status"),
        ("BASE", "baseAsset"),
        ("QUOTE", "quoteAsset"),
        ("MARGIN", "marginAsset"),
    )
    rows = [
        [str(contract.get(field, "") or "") for _heading, field in columns]
        for contract in contracts
    ]
    widths = [
        max(len(heading), *(len(row[index]) for row in rows))
        if rows
        else len(heading)
        for index, (heading, _field) in enumerate(columns)
    ]

    header = "  ".join(
        heading.ljust(widths[index])
        for index, (heading, _field) in enumerate(columns)
    )
    separator = "  ".join("-" * width for width in widths)
    body = [
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in rows
    ]
    return "\n".join([header, separator, *body])


def render_csv(contracts: Sequence[Mapping[str, Any]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=OUTPUT_FIELDS, lineterminator="\n")
    writer.writeheader()
    for contract in contracts:
        row = dict(contract)
        row["permissionSets"] = "|".join(contract.get("permissionSets", []))
        writer.writerow({field: row.get(field, "") for field in OUTPUT_FIELDS})
    return buffer.getvalue()


def render_json(
    contracts: Sequence[Mapping[str, Any]],
    *,
    endpoint: str,
    server_time: Any,
) -> str:
    payload = {
        "endpoint": endpoint,
        "serverTime": server_time,
        "serverTimeUtc": _iso_timestamp(server_time),
        "count": len(contracts),
        "contracts": list(contracts),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def configure_console_streams() -> None:
    """Prevent legacy Windows console encodings from crashing on symbol names."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="backslashreplace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "List Binance USD-M symbols whose exchangeInfo permissionSets "
            "currently contains RPI."
        )
    )
    parser.add_argument(
        "--testnet",
        action="store_true",
        help="Query Binance Futures testnet instead of mainnet.",
    )
    parser.add_argument(
        "--quote-asset",
        action="append",
        default=[],
        metavar="ASSET",
        help="Filter by quote asset; repeat for multiple assets, e.g. USDT.",
    )
    parser.add_argument(
        "--contract-type",
        action="append",
        default=[],
        metavar="TYPE",
        help="Filter by contract type, e.g. PERPETUAL or TRADIFI_PERPETUAL.",
    )
    parser.add_argument(
        "--include-non-trading",
        action="store_true",
        help="Include RPI-permitted symbols whose status is not TRADING.",
    )
    parser.add_argument(
        "--format",
        choices=("table", "symbols", "json", "csv"),
        default="table",
        help="Output format (default: table).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write output to this UTF-8 file instead of stdout.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="HTTP timeout in seconds (default: 15).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_console_streams()
    args = build_parser().parse_args(argv)
    endpoint = (
        TESTNET_EXCHANGE_INFO_URL if args.testnet else MAINNET_EXCHANGE_INFO_URL
    )

    try:
        payload = fetch_exchange_info(endpoint, timeout=max(0.1, args.timeout))
        contracts = select_rpi_contracts(
            payload["symbols"],
            trading_only=not args.include_non_trading,
            quote_assets=args.quote_asset,
            contract_types=args.contract_type,
        )
    except (requests.RequestException, ValueError, TypeError) as exc:
        print(f"Failed to query Binance RPI contracts: {exc}", file=sys.stderr)
        return 1

    if args.format == "symbols":
        rendered = "\n".join(contract["symbol"] for contract in contracts) + "\n"
    elif args.format == "json":
        rendered = render_json(
            contracts,
            endpoint=endpoint,
            server_time=payload.get("serverTime"),
        )
    elif args.format == "csv":
        rendered = render_csv(contracts)
    else:
        rendered = render_table(contracts) + "\n"

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"Wrote {len(contracts)} RPI contracts to {args.output}")
    else:
        sys.stdout.write(rendered)
        print(f"\nTotal: {len(contracts)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
