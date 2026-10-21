# GRVT maker priced off Binance

`scripts/run_grvt_maker.py` quotes one GRVT perpetual around a Binance-based
fair value, with the rules the cross-venue replay tested
(`strategy/grvt_cross_maker.py`, shared by both). One process per symbol.

| File | Purpose |
| --- | --- |
| `grvt_maker.example.json` | Settings: per-symbol quote and risk limits over `defaults` |
| `grvt_secrets.example.json` | Shape of the secrets file (API key, sub-account id, trading key) |
| `chronoshft-grvt-maker@.service` | systemd template, one instance per symbol |
| `start.sh` / `stop.sh` / `status.sh` | Start live (check, confirm, enable the services), stop (cancel, disable), show state |

## Shadow (no keys, no orders)

```bash
cp deploy/live/grvt-maker/grvt_maker.example.json config/local/grvt_maker.json
.venv/bin/python scripts/run_grvt_maker.py --symbol ADAUSDT --minutes 30
```

Market data is live; each order the engine would send is logged to
`storage/live/grvt_maker/<SYMBOL>_<YYYYMMDD>.jsonl` and "rests" until
cancelled. Shadow orders never fill.

## Live

The short way, once `config/local/grvt_secrets.json` is filled in:
`bash deploy/live/grvt-maker/start.sh`. It runs the read-only account check,
shows each symbol's size and loss limits, asks for `yes`, turns
`live_enabled` on, writes `GRVT_MAKER_MODE=--live` to
`config/local/grvt_maker.env` and enables `chronoshft-grvt-maker@<SYMBOL>`
for every configured symbol. The services run under systemd, so they keep
going without an SSH session and come back after a reboot. `stop.sh`
stops them (each cancels its orders first), disables them and turns live off;
positions stay open and are listed. A crash restarts after 15 s (at most 3
times an hour); a risk halt (exit 3) does not.

The steps it automates:

1. A dedicated GRVT sub-account holding only what this may lose; an API key
   with trading permission and its trading (signing) key.
2. `config/local/grvt_secrets.json` from the example, `chmod 600`. Never
   commit it or paste it anywhere.
3. `.venv/bin/python scripts/check_grvt_account.py`: logs in and prints the
   sub-account's balance, positions and open orders and each configured
   symbol's order rules. Read-only, it never orders or cancels.
4. In `config/local/grvt_maker.json`: `"live_enabled": true`, the size
   (`quote.notional`, `quote.max_inventory_lots`) and the risk limits.
5. Start with `--live`. On start it cancels open orders on the base, reads
   the position, arms cancel-on-disconnect and subscribes the private feed.

Both market-data sockets are pinged every 2 s. Quotes are pulled when a feed
has sent neither an update nor a pong for `stale_binance_sec` /
`stale_grvt_sec`; a quiet market with a live connection keeps its quotes.

Stopping: SIGTERM/SIGINT cancels everything; `touch storage/live/KILL` halts
every runner (remove the file before restarting). A halt for any other
reason (taker fill, maker fee above `max_maker_fee_bps`, daily loss beyond
`daily_loss_halt_usd`, repeated venue errors) exits with status 3 and needs a
look before restarting. Beyond `daily_loss_reduce_usd` the engine only
quotes reduce-only on the side that cuts the position until the UTC day
ends.

Do not run it from mainland China.
