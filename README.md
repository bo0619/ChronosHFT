# ChronosHFT
An institutional-grade high-frequency trading framework for cryptocurrencies written in Python.

## AWS Paper quick start

The tracked configuration is Paper-only and requires no Binance API key. The
bootstrap script installs a project-local, pinned `uv` and CPython, restores
the exact runtime dependency lock, creates local runtime directories, and runs
the offline configuration gate. It does not modify the host Python installation
or start a Live session.

On a fresh Ubuntu host, install the bootstrap prerequisites and clone the
repository:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
git clone https://github.com/bo0619/ChronosHFT.git
cd ChronosHFT
bash scripts/bootstrap_aws.sh
```

Amazon Linux 2023 uses `dnf` for the prerequisite step:

```bash
sudo dnf install -y ca-certificates curl git
```

Install, enable, and start the Paper service from the repository root:

```bash
sudo bash scripts/install_systemd_service.sh --start
sudo systemctl status chronoshft
```

The installer uses the non-root `SUDO_USER` as the service account, validates
that `config.json` is Paper configuration, installs the rendered unit under
`/etc/systemd/system/`, and enables it at boot. It refuses to start a second
project Python process blindly. After starting or restarting the service, it
reads systemd's effective watchdog, task, memory-pressure, hard-memory, and
swap limits and fails installation if they differ from the tracked unit. An
install without `--start` deliberately leaves an already-running process on
its old cgroup settings and reports that effective limits were not checked.
When invoking the installer directly as root,
pass `--user ubuntu` explicitly.

`systemd` runs `main.py` directly, limits process starts to 10 per hour, and
does not retry the non-recoverable startup exit code `2`. A normal
`systemctl stop` sends `SIGINT`, allowing the existing verified shutdown path to
cancel orders, persist state, and stop the independent supervisor before the
service timeout. The unit also pins OpenBLAS, OpenMP, MKL, and NumExpr to one
thread each; the strategy's small covariance matrices do not benefit from
oversubscribing both `t3.small` vCPUs. glibc is limited to two malloc arenas to
reduce long-lived per-thread heap fragmentation, and Python's fatal-signal
handler emits all thread stacks to journald before a watchdog `SIGABRT`. The
complete service cgroup is capped at
128 tasks, enters memory pressure at 1,400 MiB, and cannot exceed 1,600 MiB;
service swap and core dumps are disabled. The in-process 1.25 GiB
sustained-RSS guard should
freeze quoting first, while the cgroup remains the final defense if Python or a
native library can no longer run that guard. A 60-second systemd watchdog is
pulsed only by the Python main loop; if that loop stalls, the independent risk
sidecar loses its faster parent heartbeat and cancels first, then systemd sends
`SIGABRT` (with core dumps disabled) and applies the existing restart policy.
The watchdog also covers read-only clock-blocked dashboard mode and does
nothing outside a systemd watchdog environment. Follow and control the service
with:

```bash
sudo journalctl -u chronoshft -f
sudo systemctl stop chronoshft
sudo systemctl start chronoshft
sudo systemctl restart chronoshft
```

The runtime uses production Binance public market data but all balances,
orders, and fills remain local simulations. The SSH session and EC2 web console
may be closed after the service is active.

The tracked AWS Paper profile is intentionally sized for a `t3.small` and a
10,000 USDT simulated account. It subscribes only `SNDKUSDT`, matching the one
concurrent-symbol slot derived from capital scaling. A broader symbol universe
requires correlation-aware capital allocation and coordinated risk changes;
adding symbols alone causes the OMS to reject repeated quote attempts at the
concurrency boundary.

All three EventEngine lanes, the strategy control-truth queue, the Paper venue
command queue, the async log queue, and the SQLite projection queue are
bounded. Event queue
overflow and strategy control queue overflow use the existing fail-closed OMS
callbacks rather than growing memory indefinitely. The tracked strategy
control capacity is 2,048 events; the EventEngine capacities are 2,048 market,
1,024 execution, and 2,048 cold events. The logger retains at most 4,096
redacted 16 KB records and exposes per-level drop counters. A sustained backlog
freezes quoting before the trading-event hard limits are reached.
The OMS also keeps only the most recent 2,048 exchange events as an in-memory
diagnostic window and publishes its eviction count. Eviction does not skip
state-machine processing or durable journal writes; the journal remains the
authoritative audit source.
Local dashboard/CLI admin artifacts are likewise diagnostic: `results/` and
`archive/` retain at most 256 JSON files each and remove entries older than
seven days. Cleanup runs at admin-server startup and after each handled
command; these bounds do not alter the OMS journal or Paper trade database.
The loopback Dashboard accepts at most eight concurrent request threads, gives
each connection a five-second socket deadline, and caps the listen backlog at
16. Active, peak, accepted, and rejected connection counters are published in
`runtime.dashboard`, so a slow browser or SSH tunnel cannot silently consume
unbounded threads on the AWS host.

EventEngine shutdown first seals admission, then drains queued and in-flight
hot work together with any resulting cold-lane handoff. Events submitted after
the seal return `false`. A drain timeout leaves workers alive and the engine
closed to new events so shutdown can be retried without silently abandoning
the queue.

Order-book memory is also bounded. A stream may buffer at most 2,048 deltas,
retain at most 4,096 price levels per side, and accept at most 2,048 levels per
side in one delta. Exceeding any cap invalidates the local book and starts a
fresh snapshot synchronization. Live and Paper recovery workers share a
four-thread cap, use interruptible retry waits, and are joined before their
REST session is closed.

Raw HDF5 recording runs in a separate process with an 8,192-command queue and
500-row flush batches. On Linux that child is assigned niceness 10 so a
Pandas/PyTables flush yields CPU to market data and OMS work. Every flush
preserves 512 MiB of free disk; queue overflow, writer exit, or disk-reserve
failure marks the recorder unhealthy instead of allowing unbounded memory or
disk use. Application logging writes `logs/hft_trading.log` at `INFO`, rotates
at 32 MiB, and retains seven backups, so daily service restarts do not bypass
the storage bound.

Current profiling does not justify a Rust rewrite. A reproducible local
reference run measured a 2,000-level order-book delta at about 137
microseconds, six-scenario A-S at 0.57 milliseconds, and twelve-scenario GLFT
at 1.61 milliseconds. At 10 book events per second and a 0.5-second quote
cycle, those paths consume about 0.14%, 0.11%, and 0.32% of one core. All five
repeat ratios were below 1.14. These are
development-host references, not AWS evidence. Rust remains appropriate if
deployment profiles later identify a stable pure-compute hotspot, but moving
OMS, persistence, or lifecycle code across an FFI boundary now would add more
operational risk than performance.
Re-run the offline benchmark on the actual EC2 instance after deployment:

```bash
.venv/bin/python scripts/benchmark_runtime_hot_paths.py \
  --iterations 1000 --repeats 5 --book-event-rate-hz 10 --quote-cycle-sec 0.5
```

The JSON report estimates single-core utilization for the order-book delta,
six-scenario A-S, and twelve-scenario GLFT paths. The default Rust candidate
requires at least 10% of one core at the configured production frequency and
a maximum-to-minimum repeat ratio no greater than 1.5. The benchmark never
opens a network connection, creates an OMS, or places an order.

The 1-second Binance mark-price stream has a Paper-only public REST safety net.
After 1.5 seconds without a WebSocket mark, one batch `premiumIndex` request
refreshes all stale configured symbols. The 3-second market-freshness circuit
breaker remains unchanged and still fails closed if both sources are stale.

The dashboard deliberately listens only on EC2 loopback. Do not open port
`8765` in the security group. From the local computer, create an SSH tunnel
(use `ec2-user` instead of `ubuntu` on Amazon Linux):

```bash
ssh -N -L 8765:127.0.0.1:8765 ubuntu@EC2_PUBLIC_IP
```

Then open `http://127.0.0.1:8765/` locally. A headless EC2 host may print that
it could not open a browser; this is expected and does not stop the runtime.

Dependency metadata has one canonical source: direct constraints live in
`pyproject.toml`, while `uv.lock` pins the complete transitive environment.
`.python-version` pins CPython. After pulling a dependency update, rerun the
same bootstrap command; `--frozen` prevents an AWS host from silently resolving
a dependency set different from the committed lock.

For development tools and tests, install the separate development group:

```bash
.tools/uv sync --frozen --dev
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest -q
```

## Paper Trade: production public data, local execution

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
- `paper_fill_markouts` stores exact 100/500/1000 ms signed post-fill markout
  observations together with actual sampling lag and fill identity;
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
.venv/bin/python scripts/query_paper_trades.py --symbol SNDKUSDT --summary
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

## Configuration architecture

`config.json` is a tracked Paper-only manifest, not a runtime settings bucket.
Its `includes` array composes 29 single-purpose fragments from `config/` into
one effective configuration. Include paths are resolved relative to the
manifest rather than the process working directory.

| File | Owns |
| --- | --- |
| `config/execution.json` | Runtime mode, exchange environment, and private API policy |
| `config/paper_trade.json` | Local simulator balances, fills, fees, latency, and reset policy |
| `config/paper_trade_database.json` | Queryable Paper run and fill persistence |
| `config/symbols.json` | Traded instrument list |
| `config/data_recording.json` | Market-data recording switch |
| `config/account.json` | Account asset, margin mode, leverage, and configuration policy |
| `config/oms.json` | OMS identity, persistence, order limits, and lifecycle policy |
| `config/alerts.json` | External alert transport and failure handling |
| `config/backtest.json` | Backtest-only starting state and costs |
| `config/system/logging.json` | Log level, sinks, and bounded async queue |
| `config/system/dashboard.json` | Local monitoring server |
| `config/system/admin_control.json` | Local admin command TTL, session, and bounded result/archive retention |
| `config/system/event_engine.json` | Event-lane capacity, latency alerts, and shutdown drain timeout |
| `config/system/strategy_runtime.json` | Strategy control-event capacity, backlog watchdog, and handler timing |
| `config/system/resource_monitor.json` | Linux process RSS, CPU, thread, and file-descriptor thresholds |
| `config/system/market_data.json` | Order-book depth, freshness, and stream handling |
| `config/system/rate_limit.json` | Runtime request-rate controls |
| `config/system/time_sync.json` | Exchange-clock calibration and health limits |
| `config/risk/core.json` | Risk engine switch, checks, and kill/freeze behavior |
| `config/risk/limits.json` | Order, position, exposure, drawdown, and loss limits |
| `config/risk/price_sanity.json` | Fat-finger and reference-price validation |
| `config/risk/technical_health.json` | Technical-health thresholds and recovery policy |
| `config/risk/black_swan.json` | Tail-risk detection and emergency actions |
| `config/strategy/core.json` | Strategy registry, primary model, routing, and common quoting policy |
| `config/strategy/capital_scaling.json` | Capital multiplier and all capital-derived targets |
| `config/strategy/order_sizing.json` | Paper quote quantity mode |
| `config/strategy/model_readiness.json` | Model approval and evidence requirements |
| `config/strategy/glft.json` | GLFT parameters |
| `config/strategy/avellaneda_stoikov.json` | Avellaneda-Stoikov parameters |

Configuration leaf paths must have exactly one owner. The loader rejects an
empty fragment, duplicate include, duplicate leaf path, nested manifest,
absolute path, parent traversal, symlink, or more than 128 fragments. Do not
repeat a setting in another file to override it; edit the owning fragment.

### Runtime component boundaries

The exchange and strategy runtimes keep stable facades while stateful policy is
split into independently testable components:

| Module | Owns |
| --- | --- |
| `gateway/base_gateway.py` | Exchange-facing command and query contract |
| `gateway/binance/paper_gateway.py` | Public transport, admission barrier, ledger, and event publication facade |
| `gateway/binance/paper_state.py` | Paper order, position, and worker-command state records |
| `gateway/binance/paper_book_sync.py` | Public depth snapshot/delta synchronization, generation fencing, and bounded gap recovery |
| `gateway/binance/paper_ledger.py` | Single-writer fills, position basis, balances, fees, venue-event sequencing, and terminal-history pruning |
| `gateway/binance/paper_matching.py` | Immediate/passive matching, RPI priority, queue-ahead, price eligibility, and fee selection |
| `risk/binance_sidecar_clock.py` | Independent sidecar clock sampling, quality gates, phase-risk thresholds, and monotonic exchange-time anchor |
| `risk/binance_sidecar_truth.py` | Consistent account/position/open-order snapshots, funding observations, and deduplicated external cash flow |
| `risk/binance_sidecar_emergency.py` | Independent emergency DMS/cancel and reduce-only flatten actions |
| `risk/sidecar_policy.py` | Immutable normalized sidecar thresholds, timing limits, funding policy, and deployment identity |
| `risk/sidecar_durable_state.py` | Checksummed identity-bound kill/rearm state, corruption quarantine, fsync, and atomic replacement |
| `risk/sidecar_protocol.py` | Parent/sidecar status validation plus request-correlated control ACK decoding |
| `risk/sidecar_transport.py` | Parent-side process lifecycle, bounded IPC queues, status draining, and reliable control requests |
| `risk/independent_supervisor.py` | Durable kill/rearm state machine and parent orchestration facade |
| `strategy/contracts.py` | Structural event-handler contract consumed by the strategy runtime |
| `strategy/runtime.py` | Bounded event scheduling, coalescing, timing, and fail-closed dispatch |

`BinancePaperGateway` retains its existing method surface so OMS integration
and operator instrumentation do not depend on component layout. The book-sync
component owns snapshot/delta sequencing and recovery policy while the facade
retains transport and state; recovery threads deliberately call through the
facade so existing instrumentation remains valid. The matching component
selects simulated fills but cannot mutate balances or publish venue events
directly. Those effects are committed together by the ledger component on the
gateway's single matching worker; order and account events observe the updated
position and balance state. This makes recovery-token fencing, position-basis
transitions, and RPI `at_price`/`through` behavior testable without starting
REST, WebSocket, OMS, or background workers.

On Linux, `system.resource_monitor` samples `/proc` every five seconds and
recursively discovers the complete descendant process tree from the main
process plus the known data-recorder and risk-supervisor PIDs. Discovery scans
children forked by every thread, deduplicates descendants, and is capped at 128
processes. An incomplete or over-limit traversal is itself a sustained
fail-closed condition, so a newly added helper cannot silently escape the
application-level RSS, thread, or file-descriptor guard. Main-process counts
remain separate for diagnosis. A warning starts at 768 MiB RSS; three consecutive
samples at 1.25 GiB RSS, 96 main threads, 4,096 main file descriptors, 112
aggregate threads, or 8,192 aggregate file descriptors freeze new risk and
cancel active orders. The aggregate thread guard fires before systemd's
`TasksMax=128`. Six healthy samples clear the monitor latch, but the OMS still
requires an explicit manual rearm.
CPU at 150% of one core is warning-only. History is bounded to 12 samples.
Windows has no `/proc`, so the metric reports unavailable and does not block
Paper startup; the production target for this guard is the AWS Linux service.

For capital changes, edit only `strategy.capital_multiplier` in
`config/strategy/capital_scaling.json`. The loader derives the Paper, account,
and backtest starting capital; order, position, exposure, and daily-loss
limits; `lot_multiplier`; `target_order_notional`; and `max_pos_usdt` from that
single source. Do not declare these derived fields in fragments; the loader
always computes their effective values.

The tracked Paper profile uses notional sizing for a 10,000 USDT simulated
account. Each normal bid or ask targets 100 USDT (1% of capital), then converts
that notional to contract quantity and rounds down to the exchange lot step.
The one-symbol inventory and account gross-notional caps are both 500 USDT
(5%); the daily-loss cap is 100 USDT (1%). At the last locally recorded SNDK
price of 1296.56 USDT and a 0.01 quantity step, a normal quote is approximately
0.07 SNDK, not a fixed 30 units. Remaining inventory capacity can reduce or
suppress a quote, and safety exits may use the residual position quantity.

No market-making paper prescribes one universal contract count for a 10,000
USDT account. The profile applies the common result that quote size must be
expressed relative to capital and bounded by inventory risk. The original
[GLFT inventory model](https://arxiv.org/abs/1105.3115), the 2025
[AAAI inventory-constrained online-learning result](https://doi.org/10.1609/aaai.v39i20.35492),
and the 2024 [adaptive partial-fill model](https://arxiv.org/abs/2405.11444)
all support explicit inventory constraints and state-dependent execution. A
2026 [perpetual-futures preprint](https://arxiv.org/abs/2607.11888) further
shows that multi-pair diversification saturates as pair correlation rises. In
the local 2026-07-27/28 sample, SNDK and SOXL one-minute returns had 0.9961
correlation, while SNDK recorded roughly three times as many trades per second;
the tracked profile therefore keeps SNDK only rather than treating the pair as
independent diversification.

After any configuration edit, run the offline gate before starting the engine:

```powershell
.\.venv\Scripts\python.exe main.py --config config.json --check-config
```

A valid tracked configuration currently reports:

```text
CONFIG_OK mode=paper symbols=1 primary_model=glft capital_usdt=10000 order_notional_usdt=100 max_position_usdt=500 max_gross_usdt=500 max_daily_loss_usdt=100
```

Live deployment files remain operator-owned, single-file JSON documents and
are ignored by Git. Fragmented manifests intentionally fail closed in Live
mode until the deployment evidence digest can bind every included fragment.

## Start the engine and local dashboard

The repository already contains the Paper manifest and fragments. No
credential environment variables or configuration copy step is needed.

Run the offline configuration gate before starting any runtime component:

```powershell
.\.venv\Scripts\python.exe main.py --config config.json --check-config
```

```powershell
.\.venv\Scripts\python.exe main.py --config config.json
```

For an interactive development watchdog, `launcher.py` resolves both files
from its own directory and refuses Live configuration:

```powershell
.\.venv\Scripts\python.exe launcher.py
```

Do not use the interactive watchdog as long-term AWS process supervision: it is
attached to its terminal unless separately detached. Use the `systemd` service
described in the AWS quick start so an SSH disconnect cannot terminate the
engine.

The read-only monitoring page starts with the engine at
`http://127.0.0.1:8765/`. It covers account, PnL, positions, market data,
orders, fills, A-S/GLFT strategy telemetry, risk limits, kill/freeze
states, runtime queues, RPI eligibility/routing/fees, alerts, and logs.
The service binds to loopback only and exposes no trading action endpoint.
The browser opens automatically after `127.0.0.1:8765` binds. If Binance clock
calibration cannot complete, the same page remains available in
`STARTUP_BLOCKED / OBSERVE_ONLY` mode: no OMS, strategy, Gateway, or execution
worker starts, and a process restart is required even if clock telemetry later
recovers.
Before trusting the run, verify the top banner says `PAPER · LIVE DATA`,
`本地模拟资金与撮合`, and `私有 API 已禁用`. If it says
`LIVE · REAL MONEY`, stop the process and inspect `execution.mode`.

## HFT time system

ChronosHFT uses one high-resolution monotonic domain (`perf_counter` /
QueryPerformanceCounter on Windows) for scheduling, freshness, cooldowns,
processing latency, OMS fences, and strategy timing. Exchange epoch time is
calibrated from multiple Binance server-time samples, selecting the lowest-RTT
subset and validating initial offset, subsequent phase error, RTT, MAD
dispersion, and estimated uncertainty before a candidate can replace the
last-known-good anchor. A stable OS wall-clock offset is corrected rather than
treated as drift; only a broad startup bound and movement against the existing
exchange-time/monotonic anchor are safety signals.

Each market event carries exchange time, local ingress wall time, ingress
monotonic time, corrected exchange-epoch ingress time, clock-offset snapshot,
and gateway dispatch timestamps. Startup and the final live/paper order-send
boundary fail closed when clock health is unavailable. Risk-reducing orders and
cancels remain available. Stale calibration, wall-clock steps, bad sample
quorums, and excessive uncertainty move the clock to an unhealthy state.

This is a software HFT clock discipline, not hardware timestamping. Sub-
millisecond production claims still require host PTP/chrony discipline, NIC
hardware timestamps, kernel bypass or equivalent capture, stable CPU topology,
and venue-proximate infrastructure. The dashboard exposes the software clock's
offset, phase error, RTT, uncertainty, dispersion, sync age, source, and health
state.

## Strategy model registry

The engine registers `glft` and `avellaneda_stoikov`, while constructing exactly
one order-owning strategy. The current Paper configuration uses GLFT as the
primary, with A-S available as a manually selected alternative:

```json
"strategy": {
  "primary_model": "glft",
  "registered_models": ["glft", "avellaneda_stoikov"],
  "execution_policy": "single_primary"
}
```

To make A-S primary, change only `primary_model` to
`avellaneda_stoikov`, then restart the engine. There is no automatic model
failover. The loader canonicalizes `strategy.name` to the stable OMS/risk ID
automatically. A-S and GLFT are never concurrent execution owners
for the same symbol: simultaneous quoting would duplicate orders, split
inventory ownership, and corrupt per-strategy risk accounting.

GLFT is the architectural primary for continuous multi-symbol market making;
A-S is the finite-horizon, interpretable Paper alternative. This is a
Paper-validation choice, not live approval. Both implementations use the same
explicit `bps/sqrt(second)` volatility and fixed-notional inventory units, and
both have portfolio/adaptive Paper extensions. GLFT uses the full asymptotic
Model A `c1/c2` equation; its live `A/k`
fit accepts only exchange-ACK-to-terminal RPI exposure, including zero-fill
quotes. Public `aggTrade` and Paper fills are never eligible live intensity
samples. Genuine RPI exposure, markout, and walk-forward/OOS evidence are still
required before the build's live formula approval list can be opened.

The Paper profile also enables an adaptive Riccati Portfolio GLFT extension,
following the quadratic construction in
[Closed-form approximations in multi-asset market making](https://arxiv.org/abs/1810.04383).
Bid and ask use separate exponential intensities
`lambda_side(delta) = A_side exp(-k_side delta)`. Their Hamiltonian
curvatures are combined as
`D_eff^-1 = 0.5 * (D_bid^-1 + D_ask^-1)`, which exactly recovers the original
GLFT `D = diag(c2_i^2)` when both sides match. With instantaneous log-return
covariance `Sigma` in `bps^2/second`, finite time-to-horizon `T`, and zero
terminal inventory penalty, the risk matrix follows:

```text
dH/dT = Sigma - H D_eff^-1 H
H(0) = 0
Phi(q) = 0.5 q' H q
```

The code evaluates this Riccati equation analytically by symmetric
eigendecomposition using `sqrt(lambda) * tanh(sqrt(lambda) * T)`, not by an
Euler approximation. As `T` grows it converges to the previous algebraic
solution `H D_eff^-1 H = Sigma`. Bid and ask inventory charges remain exact
finite differences of `Phi`. Correlated inventory therefore moves quotes in
other books through `(Hq)_i`.

The A-S alternative uses a different, exact finite-horizon risk potential:

```text
H = gamma T Sigma
Phi(q) = 0.5 q' H q
```

For an order of size `Delta_i`, each inventory charge is the exact finite
difference of `Phi(q +/- Delta_i e_i) / Delta_i`. With one asset, equal bid/ask
`k`, zero extra cost, and a one-lot order, this reduces exactly to the original
finite-horizon A-S reservation price and spread. Bid and ask may use different
`k` and separately add queue, flow-toxicity, and conservative markout costs.
The Paper implementation evaluates all six corners of the configured `k` and
volatility bounds and keeps the widest depth independently on each side.
Unlike GLFT, the exponential intensity amplitude `A` cancels from the A-S
optimal-price condition; its bounded Hawkes-adjusted Paper proxy is used only
by size optimization and is explicitly labelled `CONFIGURED_PAPER_PROXY`.

Configured `SYMBOL|SYMBOL` correlations provide the cold-start covariance.
After every symbol contributes enough synchronized fixed-time mid samples,
runtime covariance switches to an EWMA outer-product estimate, shrinks toward
its diagonal, projects numerical noise back to the PSD cone, and rejects stale
symbols. The portfolio still requires one common CARA `gamma` and one common
fixed-notional inventory lot, and fails closed if those units differ.

The adaptive Paper layer also adds bounded online controls:

- public aggressive trades drive side-specific, exponentially decaying Hawkes
  multipliers with a hard cap; these are Paper proxies, never Live RPI evidence;
- a time-decayed signed trade imbalance and L1 microprice add a capped adverse
  cost only to the side currently under pressure; the signal decays when flow
  stops instead of leaving a stale directional bias;
- private Paper fills are evaluated at 100/500/1000 ms signed markouts, and a
  one-sided confidence bound becomes a side-specific adverse-selection cost
  only after the minimum sample count; each side/horizon uses a finite rolling
  window so a recent toxic regime is not diluted by the entire process history;
- Both models' Paper-only stale-quote guard checks every order-book callback and requests
  cancellation when either resting quote moves within the configured minimum
  depth of mid, without waiting for the next full strategy cycle;
- L1 queue volume and side-specific trade service rates estimate queue delay;
  queue and configured network latency become a volatility-scaled quote cost;
- GLFT evaluates all 12 configured intensity, `k`, and volatility corners;
  A-S evaluates its six relevant `k` and volatility corners because `A`
  cancels from its price equation. Both keep the widest bid and ask depth
  independently;
- size is selected from `[0, 0.25, 0.5, 1.0]` by fill edge minus fee, markout,
  inventory change, queue delay, and quadratic size cost. Zero suppresses an
  uneconomic side. The optimizer can only reduce volume already approved by
  the existing sizing path and cannot expand an OMS or risk limit.

GLFT parameters live under `strategy.glft.adaptive` in
`config/strategy/glft.json`; A-S equivalents live under
`strategy.avellaneda_stoikov.adaptive` in
`config/strategy/avellaneda_stoikov.json`. The tracked one-symbol profile exercises the same
math with a scalar covariance. Its Paper overrides use a 0.5-second quote cycle,
an 8 bps minimum total spread, a 1.5 bps stale-side cancellation threshold, and
a 200-observation markout window with 12 samples required before activation.
The 1-second cycle and 5 bps minimum remain the Live baselines; Paper overrides
do not alter them. These settings reduce selection risk but do not guarantee a
positive expected value. Both `adaptive.enabled` and
`portfolio_risk.enabled` are Paper-only; the Live gate requires them to be
false until separate RPI calibration, side-specific markout, OOS, and formula
approval artifacts exist.

## List Binance RPI contracts

Query the public USDⓈ-M `exchangeInfo` endpoint and list every currently
trading contract whose `permissionSets` contains `RPI`:
No API key or secret is required.

```powershell
.\.venv\Scripts\python.exe scripts\list_binance_rpi_contracts.py
```

Useful filters and export formats:

```powershell
.\.venv\Scripts\python.exe scripts\list_binance_rpi_contracts.py --quote-asset USDT --contract-type PERPETUAL --format symbols
.\.venv\Scripts\python.exe scripts\list_binance_rpi_contracts.py --format json --output rpi_contracts.json
.\.venv\Scripts\python.exe scripts\list_binance_rpi_contracts.py --format csv --output rpi_contracts.csv
```

## Live canary configuration gate

Paper and live state must remain separate. Do not turn the tracked Paper
manifest into a live file in place. A future live launch must use a dedicated
config and must pass the offline configuration gate before any gateway is constructed.
For Live, start the process from the directory containing that config; the
guard rejects a config in another working directory so the runtime and offline
reconstruction tool cannot resolve durable relative paths differently.
For a 10,000 USDT personal account, the first stage remains a one-symbol
canary with an explicitly smaller deployment envelope, for example:

```json
"live_launch": {
  "stage": "canary",
  "deployment_id": "rpi-canary-20260723-001",
  "declared_account_equity_usdt": 10000.0,
  "max_deployed_capital_usdt": 100.0,
  "max_deployment_loss_usdt": 5.0,
  "deployment_loss_reduce_only_fraction": 0.8,
  "rpi_only": true
}
```

The canary gate also requires exactly one configured symbol, isolated margin,
exactly 1x leverage, at most two active orders in both OMS and the independent
supervisor, and every account budget, order notional,
symbol notional, gross notional, and daily-loss limit to stay within the two
declared deployment caps. When `rpi_only` is true,
`strategy.use_rpi` must be true and `strategy.rpi_fallback_to_gtx` must be
false, so an ineligible symbol cannot silently change the fee model by routing
to GTX.

Immediately before OMS bootstrap, the live truth plane must also show zero
open orders across the account and exact zero position amounts for every
symbol. A prior local attestation is not enough, and a new deployment never
adopts an old position or order.

Live startup is rejected unless mainnet, risk, clock, market-data freshness,
margin truth, cash-flow truth, independent risk supervision, liquidation
proximity, durable sidecar state, durable OMS journal replay, single-writer
fencing, and the venue dead-man switch are all explicitly enabled. The live
journal and sidecar state paths must never point into `storage/paper/`.
Archived state from another account, symbol set, or strategy generation must
not be reused as a fresh canary ledger.

The main process and independent risk sidecar must use different API keys and
secrets, loaded from different environment variables. Withdrawal permission
must be disabled and both keys must be IP-restricted in the Binance account;
both keys must be confirmed against the same USD-M Futures account, and the
sidecar key must retain Futures cancel and reduce-only close capability.
The local evidence therefore contains two fresh, flat-start account snapshots
whose balances must match exactly and whose API-key fingerprints must differ.
It also binds a fresh `/sapi/v1/account/apiRestrictions` response to each key
and requires Futures/reading enabled, withdrawals disabled, and IP restriction
enabled. Those account-side facts still require deployment evidence and are
not inferred from local configuration.

RPI zero commission is runtime truth, not a hardcoded assumption. Before OMS
bootstrap, Live synchronizes each configured symbol's account-specific
`rpiCommissionRate`; with
`strategy.rpi_live_policy.require_zero_commission=true`, any missing or
non-zero rate blocks the launch. The independent truth plane rechecks every
30 seconds during the session. One unavailable observation seals new risk and
cancels active orders; two consecutive failures halt the OMS, while any
non-zero RPI rate halts immediately. Each observation is written to the
durable OMS journal. The RPI rate is the final route rate and replaces the
ordinary maker rate rather than being added to it.
This applies to ordinary RPI fills only. Shutdown, kill-switch, and tail-risk
flattening may use taker execution and therefore can incur taker commission,
spread, and slippage.

Live Canary also requires a fresh funding snapshot from the mark-price stream.
The funding clock advances from the exchange-corrected receipt timestamp using
the monotonic process clock, so a local wall-clock step cannot move the guard
window. Missing, stale, non-finite, elapsed, or implausibly distant funding
data; an absolute funding rate of 5 bps or more per settlement; or entry into
the final 10 minutes before settlement moves the OMS to `REDUCE_ONLY`.
Non-reduce orders are cancelled while explicit reduce-only exits remain
available. A funding-time rollover retains that constraint for two minutes,
then requires five distinct healthy mark updates before recovery. This main
process also treats every restart as an unknown settlement state and repeats
the full two-minute hold before those recovery updates. The independent risk
sidecar applies the same policy from its own public
`GET /fapi/v1/premiumIndex` observations, independently validates Binance's
payload timestamp against its exchange-synchronized clock, and continuously
ages each observation with its monotonic clock. It starts fail-closed, polls
inside the configured freshness window, and reports `REDUCE_ONLY` until every
configured symbol has completed the same healthy-recovery quorum. A funding
breach alone does not request emergency flattening.

Every Live canary must also run the deployment-bound
`system.evidence_recorder`. It records raw mark-price/funding observations and
exchange account updates into an independent SHA-256 chained JSONL journal,
with a bounded queue, one-second `fsync` policy, and its own single-writer
fence. The recorder starts before the gateway, anchors its start and clean
stop to the durable OMS journal, and must seal every session with zero dropped
records. A writer error or queue overflow freezes new risk and cancels active
quotes; it does not independently request a market flatten. Before advancing
its in-memory sequence or hash tail, each encoded batch must leave at least
512 MiB free according to `system.evidence_recorder.min_free_bytes`. A failed
space inspection or exhausted reserve follows the same fail-closed path and is
reported with free bytes and rejection/check-failure counters. OMS journal,
market-evidence journal, both writer fences, sidecar state, and external-alert
failure spool must be distinct deployment-bound files.

Live also requires a generic HTTPS webhook alert channel. Set the
credential-bearing endpoint only in the environment variable named by
`alert.webhook_url_env` (`CHRONOSHFT_ALERT_WEBHOOK_URL` in the templates).
Never place the URL, a Telegram token, or another provider credential in JSON.
The resolved endpoint is held only by the transport and is excluded from
runtime configuration, dashboard snapshots, logs, response handling, and the
durable failure spool. A generic webhook was selected instead of the old
unused Telegram placeholders so the deployment can use an operator-chosen
phone notification relay without embedding a bot token in a request URL.

Before constructing a Binance gateway or OMS, Live starts the bounded alert
worker and requires a bounded startup-probe delivery to return HTTP 2xx.
Failure blocks startup. During operation, Logger `WARNING` and above plus
structured `EVENT_ALERT` records only perform a bounded in-memory enqueue;
the alert worker owns HTTPS timeouts, retries, recovery probes, and durable
failure writes. A delivery failure, worker failure, queue overflow, or spool
failure marks the channel unhealthy. The main control loop, not the worker,
then adds an `external_alerts:` `REDUCE_ONLY` constraint and cancels
non-reduce orders. A successful worker recovery probe allows the main loop to
clear only that constraint. Delivery failures and queue-overflow summaries
are appended with `fsync` to the deployment-bound
`external_alert_failures.jsonl`; records contain no endpoint, response body,
or exception text. The spool checks the exact encoded record before append and
must preserve the 512 MiB configured by
`alert.failure_spool_min_free_bytes`; inability to inspect or preserve that
reserve makes the alert channel unhealthy. These append-only audit chains are
not silently rotated. Expand the volume or archive a cleanly stopped
deployment rather than deleting an active chain. A complete host or process
loss still requires an
independent infrastructure heartbeat monitor; the exchange dead-man switch
remains responsible for cancelling orders after such a loss.

When Live work resumes, create the operator-owned `config.live.canary.json`
and run the offline checker before moving it to a deployment host:

```powershell
.\.venv\Scripts\python.exe scripts\check_live_canary_readiness.py --config config.live.canary.json
```

Live deployment JSON is intentionally operator-owned and ignored by Git. The
checker reads only local JSON, never reads credential values, constructs no
gateway or OMS, performs no network request, and exercises no order path. A
`PASS` is only an offline prerequisite; startup still refreshes exchange and
account truth and can fail closed.

The runtime validators define the exact evidence contracts.
`config.live.canary.calibration.json` uses calibration artifact v3 and
accepts only genuine `LIVE_BINANCE_RPI_ACK` exposure bins. Source evidence v4
binds the artifact digest, exact redacted deployment-config digest, validated
calibration journal, raw OMS and market-evidence journal digests, and canonical
OOS digest. It requires every OOS fill to be maker and RPI, exchange and booked
commission to be exactly zero, exchange realized PnL to agree with fill-ledger
reconstruction within 0.000001 USDT, and positive 95% lower confidence bounds
for both 1-second and 5-second markout using UTC-day clustered t estimates.
Approval v2 binds both files through one canonical evidence-bundle digest and
requires a signature from a configured Ed25519 public key, verified by the
pinned `cryptography` backend; a missing or failed backend blocks startup. The
dependency is deliberately one-way
(`config -> artifact -> source/OOS -> approval`), so approval paths and runtime
secrets are excluded from the deployment digest and no hash cycle exists.
Private signing keys never belong in the runtime config. Empty public-key
trust, empty bins, placeholder hashes, `approved=false`, Paper fills, and
public-flow calibration all fail closed.

### RPI calibration canary

`live_launch.stage="rpi_calibration_canary"` is a separate, real-order
bootstrap stage for collecting the first genuine Binance RPI
ACK-to-terminal exposure. Its risk-limited permit neither grants, weakens, nor
replaces the ordinary canary's model approval. A normal `stage="canary"` still
requires the complete signed model manifest, source evidence, OOS evidence,
approved formula version, and runtime calibration artifact.

The calibration permit is an operational risk budget, not a statistical
sample budget. A framework-validation run may stop after 6-10 attempts and a
clean verified shutdown; it does not require a fill, 30 training orders, 100
OOS fills, or profitability. Partial fills remain executions on the original
order and are never counted as independent order samples. None of these
framework-validation observations promote the strategy to the normal
profitability-gated `canary` stage.

The operator procedure is documented in
[`docs/live-rpi-framework-validation-runbook.md`](docs/live-rpi-framework-validation-runbook.md).
Use `scripts/create_rpi_calibration_permit.py` to generate an encrypted
offline Ed25519 key and create a deployment-bound permit. The tool imports no
Gateway or OMS, performs no network operation, refuses to keep the private key
inside this repository, refuses output overwrite, and independently verifies
the signed permit before writing it.

The calibration stage is allowed only by a dedicated Ed25519 permit loaded
from `live_launch.calibration_permit_path`. The permit is bound to both the
normalized calibration config and
`live_launch.target_deployment_config_path`; it also binds the deployment,
symbol, GLFT policy, and implementation digests. Calibration and target
configs use the same `deployment_id` so captured journals can be promoted into
the existing artifact/source-evidence chain, but their OMS journal, writer
fence, and independent-supervisor state paths must be disjoint subpaths. The
private signing key remains offline, and the calibration signer keyring is
independent from the model-approval keyring.

The calibration and target configs must also produce the same GLFT
`strategy_policy_sha256`. The checked-in target therefore keeps the first
canary at the calibrated 8 USDT maximum position and gross notional with a
10-second GLFT cycle. Increasing size, changing quote parameters, or increasing
frequency creates a different strategy policy and requires new calibration
evidence and a new model approval; evidence collected under the smaller policy
does not authorize that change.

For an approximately 10,000 USDT personal account, the calibration guard
enforces all of the following:

- Exactly one symbol, GLFT only, RPI only, no GTX fallback, isolated margin,
  1x leverage, `account.configuration_mode="VERIFY_ONLY"`, and fresh
  account-specific zero `rpiCommissionRate` truth. ONE_WAY, ISOLATED, and 1x
  must be configured manually before launch; ChronosHFT refuses to modify
  them during Live startup.
- At most 50 USDT deployed, 8 USDT per order, 8 USDT position and gross
  notional, 1 USDT daily loss, and 2 USDT deployment loss.
- All four OMS active-order caps and the independent supervisor open-order
  cap are exactly one.
- A permit TTL no longer than 24 hours, order TTL no longer than 60 seconds,
  at least five seconds between orders, at most 100 orders, and at least three
  strictly increasing fixed depths spanning at least 0.5 bps.
- Signed minimum/maximum order notional, cumulative submitted-notional quota,
  order-count quota, and calibration-loss quota enforced at the final OMS
  send boundary.

Artifact promotion replays the complete local journal offline. Every exposure
sample must map to one earlier send reservation and its immutable OrderIntent.
Every activation or unused-permit expiry carries the complete signed permit;
the builder re-verifies its Ed25519 signature, configuration/policy/
implementation digests, non-overlapping validity window, deployment-wide
loss baseline, global counters, and per-permit relative quotas. Renewing a
permit cannot reset cumulative order notional, order count, or peak loss.
Promotion is allowed only after every activated permit has a durable expiry
and the final journal record is a clean `oms_stopped` with cancellation
verification. The builder acquires the calibration OMS single-writer fence
before reading, so a running or still-stopping process cannot be packaged as
immutable evidence, and it retains that fence through artifact `fsync`.
Live-readiness validation reacquires the same fence through journal replay,
artifact/OOS binding, and approval-signature verification.

```powershell
.\.venv\Scripts\python.exe scripts\build_rpi_calibration_artifact.py --journal storage\live\EDIT-ME-rpi-canary-001\calibration\oms_journal.jsonl --output config.live.canary.calibration.json --config config.live.canary.json --calibration-config config.live.rpi-calibration.json --symbol XAUUSDT
```

This command reads only local source and JSON files. It creates no Gateway or
OMS and performs no network or order operation. The source-evidence manifest
must retain the exact calibration-config path and effective digest so model
approval can repeat the same permit/reservation replay rather than trusting
the generated artifact.

After a cleanly stopped evidence session, reconstruct the OOS section only
from the two sealed journals:

```powershell
.\.venv\Scripts\python.exe scripts\build_live_oos_evidence.py --config config.live.canary.json --output storage\live\rpi-canary-001\oos.json --training-ended-at 2026-08-01T00:00:00Z --oos-started-at 2026-08-01T00:00:01Z --oos-ended-at 2026-08-08T00:00:00Z
```

This offline tool acquires both writer fences, validates both hash chains and
their OMS anchors, requires flat start/end boundaries and no external
transfers, and recomputes funding, fees, net PnL, drawdown, and markout. It
performs no network or order operation.

Create the operator-owned `config.live.rpi-calibration.json` only when the
deployment permit and trusted signer are available. Check it without making a
network request or constructing a Gateway/OMS:

```powershell
.\.venv\Scripts\python.exe scripts\check_live_canary_readiness.py --config config.live.rpi-calibration.json
```

An offline `PASS` is only a local prerequisite: startup must still re-fetch
flat account state, RPI eligibility,
and account-specific commission truth on an allowed deployment host. This
stage must not be run from mainland China or any environment where Binance
access or account use is not permitted.

The current writer fence is host-local. A canary permit and deployment ID may
run on exactly one host and one process; do not clone or start the same permit
from another worktree or machine. Both API keys must remain IP-restricted to
that deployment host. Multi-host active/passive operation is not supported.

## Roadmap / TODO

- [x] Paper Trade on production public market data (not Binance Testnet):
      consume real live market streams, route every order to a local matching
      and fill simulator, never call private exchange trading endpoints, and
      label the UI environment as `PAPER · LIVE DATA`.
- [x] Audit and extend the Avellaneda-Stoikov and GLFT implementations against
      the original papers and institutional practice. Formula units, inventory
      direction, finite-horizon portfolio risk, and exact risk-potential
      differences are tested; GLFT remains the primary and A-S the manually
      selected Paper alternative. The evidence gaps below still apply.
- [x] Implement the separate, signed and independently loss-capped
      `rpi_calibration_canary`, including offline permit, reservation, quota,
      journal, artifact, and model-readiness replay.
- [ ] Provision and execute the first genuine RPI calibration deployment.
      This requires an allowed deployment host, two restricted API keys,
      current account/RPI/commission evidence, an offline-signed permit, and
      genuine `LIVE_BINANCE_RPI_ACK` samples. No checked-in placeholder is an
      authorization to send a real order.
- [ ] Close the remaining model-practice gaps before increasing capital:
      robust microstructure volatility, real RPI queue and latency models,
      toxicity-conditioned markout, parameter-drift governance, regime/event
      controls, and multi-level quoting. Paper fills and public aggregate
      trades must not be used as substitutes for RPI execution evidence.
- [x] Remove the remaining legacy TUI modules, tests, and Rich dependency. The
      main runtime and event/log wiring are now web-only.
- [ ] Redesign the web UI around a top-tier institutional trading-console
      hierarchy with fewer simultaneous panels, stronger prioritization,
      progressive disclosure, and materially lower visual noise while retaining
      full telemetry access.

# Key Features:
🚀 Asynchronous Event-Driven Architecture: Ultra-low latency signal processing.

🧠 L2 Market Reconstruction: Local orderbook management with incremental depth updates.

🔬 High-Fidelity Simulation: Discrete Event Simulator (DES) with Gaussian latency, packet loss injection, and queue position tracking.

🛡️ Pre-Trade Risk Engine: Fat-finger protection and rate limiting.

📊 Chaos Engineering: Monte Carlo analysis to quantify strategy robustness against network jitter and exchange failures.
