# Operations: AWS host, service, and resource bounds

How the Paper engine runs on a small Linux host: the systemd unit, every
bounded queue and memory cap, the benchmark used to decide against a Rust
rewrite, and the development toolchain. For the short version see the
[README](../README.md).

## systemd service

The installer (`sudo bash scripts/install_systemd_service.sh --start`) enables
the unit at boot. To start ChronosHFT only by hand, disable it afterwards with
`sudo systemctl disable chronoshft`.

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
oversubscribing both `t3.medium` vCPUs. glibc is limited to two malloc arenas to
reduce long-lived per-thread heap fragmentation, and Python's fatal-signal
handler emits all thread stacks to journald before a watchdog `SIGABRT`. The
complete service cgroup is capped at
128 tasks, enters memory pressure at 2,800 MiB, and cannot exceed 3,200 MiB;
service swap and core dumps are disabled. The in-process 2.5 GiB
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

The tracked AWS Paper profile is intentionally sized for a `t3.medium` (2 vCPU /
4 GiB) and a
10,000 USDT simulated account. It subscribes one symbol (`KORUUSDT` today),
matching the one concurrent-symbol slot derived from capital scaling. A broader symbol universe
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

## Starting by hand and the development watchdog

For an interactive development watchdog, `launcher.py` resolves both files
from its own directory and refuses Live configuration:

```bash
.venv/bin/python launcher.py
```

Do not use the interactive watchdog as long-term AWS process supervision: it is
attached to its terminal unless separately detached. Use the `systemd` service
described in the AWS quick start so an SSH disconnect cannot terminate the
engine.

The monitoring page starts with the engine at
`http://127.0.0.1:8765/`. It covers account, PnL, positions, market data,
orders, fills, A-S/GLFT strategy telemetry, risk limits, kill/freeze
states, runtime queues, RPI eligibility/routing/fees, alerts, and logs.
The service binds to loopback only. Its only state-changing actions are the
operator buttons Rearm, Flatten all and Restart, posted to the local admin
endpoint.
The browser opens automatically after `127.0.0.1:8765` binds. If Binance clock
calibration cannot complete, the same page remains available in
`STARTUP_BLOCKED / OBSERVE_ONLY` mode: no OMS, strategy, Gateway, or execution
worker starts, and a process restart is required even if clock telemetry later
recovers.
Before trusting the run, verify the top banner says `PAPER · LIVE DATA`,
`本地模拟资金与撮合`, and `私有 API 已禁用`. If it says
`LIVE · REAL MONEY`, stop the process and inspect `execution.mode`.
