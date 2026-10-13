# Paper trading and the Paper execution database

What Paper mode simulates, what it cannot prove, and the SQLite history every
run writes. The dashboard's trading view reads the same database for the
cumulative P&L curve, fill quality, quote position and per-symbol P&L.

## What Paper mode is

The tracked `config.json` manifest composes the single-purpose JSON fragments
under `config/` and defaults to Paper mode. It consumes Binance USDⓈ-M
**production public** market data (not Binance Testnet), while balances, orders,
fills, fees and PnL are produced by the local simulator. Paper mode does not
require an API key or secret and must not call private order, account, user-data
stream, listen-key or income endpoints.

The local dashboard must display `PAPER · LIVE DATA`. In this mode:

- `BINANCE_MAINNET_PUBLIC` means live production prices from public endpoints.
- `LOCAL_SIMULATOR` means funds, order acknowledgements, cancels and fills are simulated.
- `private_api_enabled=false` means no private Binance session is permitted.
- Paper OMS journals and locks live under `storage/paper/`, separate from live state.
- Paper runs and fills are projected into `storage/paper/trades.sqlite3`.
- The current simulator uses `reset_on_start=true`: balances, positions and
  venue orders start fresh on every process launch, and older Paper OMS
  journals are retained for audit but are not replayed into the fresh ledger.

Paper RPI fills are model assumptions only. They do not prove that Binance
accepted a real RPI order, that it joined a real RPI queue, or that a Binance
App/Web retail counterparty traded against it. The Paper configuration keeps
`paper_trade.rpi_fill_model=public_trade_proxy`: a public aggregate trade at or
through a resting RPI quote is treated as simulated fill evidence. This proxy
does not observe the private RPI queue or retail-only counterparty eligibility,
so its fills and PnL must not be interpreted as expected Live performance. The
simulator deliberately retains through-price fills as a stress assumption; it
does not discard adverse fills merely to manufacture positive Paper PnL.

## Paper execution database

Paper execution history uses local SQLite in WAL mode. Schema v4 upgrades an
existing v1, v2, or v3 database in place without deleting historical rows.
`paper_runs` separates every process launch with a random `run_id`, records the
software version and Git revision, and seven fact datasets share that identity:

- `paper_fills` stores every partial or complete fill, its durable journal
  identity, price/quantity/PnL, trigger relation (`at_price`, `through`, or
  `orderbook`), triggering public trade, its transport/local matching age,
  queue-ahead estimate, fill-time L1 book, and quote age;
- `paper_order_events` stores every observed order lifecycle state, including
  unfilled cancels, expires, and rejects, so fill intensity has an exposure
  denominator rather than fills alone;
- `paper_strategy_samples` stores structured mid/fair/quote, L1 size,
  inventory, volatility, A/k, markout, flow, queue, stale-quote, and size
  decisions. It also separates exchange transport, gateway processing,
  strategy-queue, callback-age, clock-offset, and calculation latency, and
  identifies the formula, units, and intensity source used for each sample;
- `paper_fill_markouts` stores signed post-fill markouts at every configured
  horizon (`strategy.glft.adaptive.markout.horizons_ms`: 100 ms, 500 ms, 1 s,
  5 s and 30 s in the tracked profile) together with actual sampling lag and
  fill identity. The sign is `side * ln(mid_after / fill_price)` in bps, so a
  positive markout means the price moved our way, spread capture included;
- `paper_account_samples` stores balance, equity, unrealized PnL, available
  funds, budget usage, margin health, and external-cash-flow truth;
- `paper_system_events` stores system-health, freeze/recovery, alert, reconnect,
  watchdog, and API-weight observations;
- `paper_market_samples` stores mark/index prices, basis, funding rate and next
  funding time together with exchange/receive/dispatch clocks, clock offset,
  transport latency, and gateway processing latency.

Strategy telemetry is downsampled to one row per symbol per second by
`paper_trade_database.strategy_sample_interval_sec`. Account telemetry uses
`paper_trade_database.account_sample_interval_sec`, also one second by default.
Mark-price telemetry uses `paper_trade_database.market_sample_interval_sec`,
again one second by default and independently per symbol. Order events, fills,
markouts, and abnormal system events are not downsampled. The observation
tables use typed columns instead of duplicate raw JSON payloads.
`reset_on_start=true` resets the simulated venue and account but never deletes
this historical database.

The fsync-backed OMS JSONL journal remains the audit truth. SQLite is a
query-oriented projection written after the journal commit, outside the OMS hot
path. Missing SQLite rows are idempotently backfilled from the verified journal
on the next start. Do not delete `storage/paper/oms_journal.jsonl` after a fill
has been recorded merely because it also appears in SQLite.

The projection queue is capped at 4,096 records and the writer commits up to 64
records per SQLite transaction. If a batch contains an invalid record, the
transaction is rolled back and each record is retried separately so valid rows
are retained and the bad projection is reported unhealthy. Short-lived SQLite
connections are explicitly closed; shutdown drains every record before the
stop marker. Journal verification and startup backfill stream records instead
of retaining the complete JSONL file in an additional list.

The tracked AWS profile reserves 512 MiB of free disk for both the OMS journal
and the SQLite projection. The journal reserves the exact encoded batch size
before every append; SQLite uses a conservative per-transaction reservation.
Checks are cached for one second and expose free bytes, last-check time,
rejection count, and check failures in their health snapshots. Falling below
the reserve, failing to inspect the filesystem, `ENOSPC`, a projection queue
overflow, or a SQLite write failure marks the database unhealthy, freezes OMS,
and requests cancellation of active quotes. The pre-trade gate continues to
reject new risk while the database is unhealthy; reduce-only orders remain
available. The relevant settings are
`oms.journal_min_free_bytes`,
`oms.journal_space_check_interval_sec`,
`paper_trade_database.min_free_bytes`, and
`paper_trade_database.space_check_interval_sec`.

Query recent fills or aggregate them by run and symbol:

```bash
.venv/bin/python scripts/query_paper_trades.py --limit 100
.venv/bin/python scripts/query_paper_trades.py --summary
.venv/bin/python scripts/query_paper_trades.py --symbol KORUUSDT --summary
.venv/bin/python scripts/query_paper_trades.py --dataset orders --limit 100
.venv/bin/python scripts/query_paper_trades.py --dataset strategy --limit 100
.venv/bin/python scripts/query_paper_trades.py --dataset markouts --limit 100
.venv/bin/python scripts/query_paper_trades.py --dataset accounts --limit 100
.venv/bin/python scripts/query_paper_trades.py --dataset system --limit 100
.venv/bin/python scripts/query_paper_trades.py --dataset runtime --limit 10000
.venv/bin/python scripts/query_paper_trades.py --dataset markets --limit 100
.venv/bin/python scripts/analyze_runtime_soak.py --minimum-hours 24
```

The query tool opens SQLite read-only and is safe while the service is running.
Paper mode records one versioned runtime-resource event for each actual Linux
resource sample (five seconds in the tracked profile), rather than once per
0.1-second main-loop iteration. `--dataset runtime` filters those events and
decodes the compact JSON payload. It preserves RSS and RSS-growth trend,
main/aggregate threads and file descriptors, CPU, process-tree discovery, and
systemd-watchdog delivery counters for AWS soak analysis. At the tracked
interval a 72-hour run produces about 51,840 small rows; the existing SQLite
queue, batching, disk-reserve, and failure-freeze rules apply unchanged.
`analyze_runtime_soak.py` streams the selected run (latest by default), ignores
the configured 30-minute warmup for trend fitting, and fails unless duration,
sample continuity, process discovery, watchdog delivery, resource ceilings,
and the default 5 MiB/hour RSS-slope gate all pass. Use 72 hours before treating
the result as strong evidence; the 24-hour default is only the minimum gate.
For a raw file-level backup, stop `chronoshft` first or use SQLite's online
backup API; copying only the main `.sqlite3` file while WAL writes are active
can produce an incomplete backup.

Build offline candidate estimates of fill intensity
`lambda(delta)=A*exp(-k*delta)` and conditional post-fill markout from the
database:

```bash
.venv/bin/python scripts/calibrate_paper_models.py \
  --database storage/paper/trades.sqlite3 \
  --output storage/paper/model-calibration.json
```

The intensity likelihood includes both time-to-first-fill and right-censored
unfilled quote exposure. The report also contains time-block bootstrap
intervals, chronological walk-forward comparison against a constant-intensity
baseline, and chronological ridge markout results against a constant-mean
baseline. The artifact is always marked `candidate_only=true` and
`activation_permitted=false`; the script never edits strategy configuration.
Paper public-trade-proxy observations cannot establish Live RPI queue position,
counterparty eligibility, or fill probability, so this artifact is evidence
for Paper iteration only and is not accepted by the Live calibration approval
path.

Databases upgraded from v1 or v2 begin collecting the new exposure, markout,
strategy, account, system, and market observations after the upgrade. Legacy
fills remain queryable, but fills alone cannot reconstruct observations that
were never recorded.
