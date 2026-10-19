# GRVT maker priced off Binance

`scripts/run_grvt_maker.py` quotes one GRVT perpetual around a Binance-based
fair value, with the rules the cross-venue replay tested
(`strategy/grvt_cross_maker.py`, shared by both). One process per symbol.

| File | Purpose |
| --- | --- |
| `grvt_maker.example.json` | Settings: per-symbol quote and risk limits over `defaults` |
| `grvt_secrets.example.json` | Shape of the secrets file (API key, sub-account id, trading key) |

## Shadow (no keys, no orders)

```bash
cp deploy/live/grvt-maker/grvt_maker.example.json config/local/grvt_maker.json
.venv/bin/python scripts/run_grvt_maker.py --symbol XRPUSDT --minutes 30
```

Market data is live; each order the engine would send is logged to
`storage/live/grvt_maker/<SYMBOL>_<YYYYMMDD>.jsonl` and "rests" until
cancelled. Shadow orders never fill.

## Live

1. A dedicated GRVT sub-account holding only what this may lose; an API key
   with trading permission and its trading (signing) key.
2. `config/local/grvt_secrets.json` from the example, `chmod 600`. Never
   commit it or paste it anywhere.
3. In `config/local/grvt_maker.json`: `"live_enabled": true`, the size
   (`quote.notional`, `quote.max_inventory_lots`) and the risk limits.
4. Start with `--live`. On start it cancels open orders on the base, reads
   the position, arms cancel-on-disconnect and subscribes the private feed.

Stopping: SIGTERM/SIGINT cancels everything; `touch storage/live/KILL` halts
every runner (remove the file before restarting). A halt for any other
reason (taker fill, maker fee above `max_maker_fee_bps`, daily loss beyond
`daily_loss_halt_usd`, repeated venue errors) exits with status 1 and needs a
look before restarting. Beyond `daily_loss_reduce_usd` the engine only
quotes reduce-only on the side that cuts the position until the UTC day
ends.

Do not run it from mainland China.
