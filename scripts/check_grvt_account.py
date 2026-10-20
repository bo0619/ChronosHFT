#!/usr/bin/env python3
"""Check a GRVT trading account before going live. Read-only: never places or cancels an order.

Logs in with ``config/local/grvt_secrets.json`` and prints the balance,
positions and open orders of the sub-account, and for each symbol in the
maker config the instrument rules against the configured order size. Exits
with status 1 if anything needs fixing first; open positions are only
reported, since a restarted maker takes over its position.

    .venv/bin/python scripts/check_grvt_account.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from eth_account import Account  # noqa: E402

from gateway.grvt.rest_api import GrvtApiError, GrvtRestClient  # noqa: E402
from run_grvt_maker import fetch_instrument, load_config, load_secrets  # noqa: E402

SUMMARY_FIELDS = ("total_equity", "available_balance", "initial_margin", "maintenance_margin", "unrealized_pnl",
                  "settle_currency")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="config/local/grvt_maker.json")
    args = parser.parse_args(argv)

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    environment = config.get("environment", "prod")
    secrets = load_secrets(config["secrets_file"])
    problems: list[str] = []

    try:
        signer = Account.from_key(secrets["private_key"]).address
        print(f"signing key ok (signer address {signer[:6]}...{signer[-4:]})")
    except Exception as exc:  # malformed key: no point logging in
        print(f"signing key does not parse: {type(exc).__name__}")
        return 1

    rest = GrvtRestClient(api_key=secrets["api_key"], sub_account_id=secrets["sub_account_id"],
                          private_key=secrets["private_key"], environment=environment)
    try:
        session = rest.session()
        summary = rest.account_summary()
        positions = rest.positions()
        orders = rest.open_orders()
    except GrvtApiError as exc:
        print(f"GRVT refused: {exc}")
        return 1
    print(f"login ok ({environment}), account id {'present' if session.account_id else 'missing'}")

    print("\nsub-account summary:")
    for key in SUMMARY_FIELDS:
        if key in summary:
            print(f"  {key:20} {summary[key]}")
    warnings: list[str] = []
    if "total_equity" not in summary:
        equity = 0.0
        warnings.append(f"no total_equity in the summary (fields: {', '.join(sorted(summary))[:200]})")
    else:
        equity = float(summary.get("total_equity") or 0)
        if equity <= 0:
            problems.append("the sub-account holds no equity: transfer USDT into this trading sub-account")

    print(f"\npositions: {len(positions)}")
    for p in positions:
        print(f"  {p.get('instrument')}: size {p.get('size')} entry {p.get('entry_price')} "
              f"unrealized {p.get('unrealized_pnl')}")
    print(f"open orders: {len(orders)}")
    configured = {fetch_instrument(sym, environment)["instrument"] for sym in config.get("symbols") or {}}
    stray = sorted({str(x.get("instrument") or (x.get("legs") or [{}])[0].get("instrument")) for x in positions + orders}
                   - configured)
    if stray:
        warnings.append(f"positions or orders in instruments this maker does not trade: {', '.join(stray)}")
    if positions:
        warnings.append("open positions are taken over by the maker on start (it reads the position)")

    print("\nconfigured symbols:")
    for symbol in sorted(config.get("symbols") or {}):
        settings = load_config(args.config, symbol)
        info = fetch_instrument(symbol, environment)
        quote, risk = settings["quote"], settings["risk"]
        min_notional = float(info.get("min_notional") or 0)
        print(f"  {symbol:13} tick {info['tick_size']:>10} min size {info['min_size']:>8} min notional {min_notional:>6g}"
              f" | order {quote.notional:g} USDT, +/-{quote.half_spread_bps:g} bps, max position "
              f"{risk.max_position_usd:g} USDT, loss reduce/halt {risk.daily_loss_reduce_usd:g}/{risk.daily_loss_halt_usd:g}")
        if quote.notional < min_notional:
            problems.append(f"{symbol}: order notional {quote.notional:g} is below GRVT's minimum {min_notional:g}")
    total_cap = sum(load_config(args.config, s)["risk"].max_position_usd for s in config.get("symbols") or {})
    print(f"\nsum of position caps {total_cap:g} USDT against equity {equity:g} USDT")

    print(f"\nlive_enabled in config: {bool(config.get('live_enabled'))}")
    if warnings:
        print("\nnote:\n  - " + "\n  - ".join(warnings))
    if problems:
        print("\nfix first:\n  - " + "\n  - ".join(problems))
        return 1
    print("\nready: nothing was ordered or cancelled")
    return 0


if __name__ == "__main__":
    sys.exit(main())
