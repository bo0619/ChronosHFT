# ChronosHFT
An institutional-grade high-frequency trading framework for cryptocurrencies written in Python.

## Paper Trade: production public data, local execution

`config.example.json` defaults to Paper mode. It consumes Binance USDⓈ-M
**production public** market data (not Binance Testnet), while balances, orders,
fills, fees and PnL are produced by the local simulator. Paper mode does not
require an API key or secret and must not call private order, account, user-data
stream, listen-key or income endpoints.

The local dashboard must display `PAPER · LIVE DATA`. In this mode:

- `BINANCE_MAINNET_PUBLIC` means live production prices from public endpoints.
- `LOCAL_SIMULATOR` means funds, order acknowledgements, cancels and fills are simulated.
- `private_api_enabled=false` means no private Binance session is permitted.
- Paper OMS journals and locks live under `storage/paper/`, separate from live state.
- The current simulator uses `reset_on_start=true`: balances, positions and
  venue orders start fresh on every process launch, and older Paper OMS
  journals are retained for audit but are not replayed into the fresh ledger.

Paper RPI fills are model assumptions only. They do not prove that Binance
accepted a real RPI order, that it joined a real RPI queue, or that a Binance
App/Web retail counterparty traded against it. The example therefore keeps
`paper_trade.rpi_fill_model` set to `disabled` until an explicit, calibrated
local RPI fill model is chosen.

## Start the engine and local dashboard

For a first Paper run, create `config.json` from the safe example, then start
the main program. No credential environment variables are needed:

```powershell
Copy-Item config.example.json config.json
```

```powershell
.\.venv\Scripts\python.exe main.py --config config.json
```

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
A-S remains the simpler, interpretable baseline and cold-start fallback. This
is a Paper-validation choice, not live approval. Both implementations still
need dimensionally consistent volatility/intensity calibration and walk-forward
validation, with RPI fill intensity calibrated separately from ordinary public
trade flow.

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

## Roadmap / TODO

- [x] Paper Trade on production public market data (not Binance Testnet):
      consume real live market streams, route every order to a local matching
      and fill simulator, never call private exchange trading endpoints, and
      label the UI environment as `PAPER · LIVE DATA`.
- [ ] Audit the Avellaneda-Stoikov and GLFT implementations against the original
      papers and top-tier institutional practice: intensity calibration, dynamic risk
      aversion, volatility estimation, fees/adverse selection, queue and
      latency models, multi-level quoting, regime switching, and empirical
      validation.
- [ ] Remove the remaining legacy TUI modules, tests, and Rich dependency. The
      main runtime and event/log wiring are now web-only. Redesign the web UI around a
      top-tier institutional trading-console hierarchy with fewer simultaneous
      panels, stronger prioritization, progressive disclosure, and materially
      lower visual noise while retaining full telemetry access.

# Key Features:
🚀 Asynchronous Event-Driven Architecture: Ultra-low latency signal processing.

🧠 L2 Market Reconstruction: Local orderbook management with incremental depth updates.

🔬 High-Fidelity Simulation: Discrete Event Simulator (DES) with Gaussian latency, packet loss injection, and queue position tracking.

🛡️ Pre-Trade Risk Engine: Fat-finger protection and rate limiting.

📊 Chaos Engineering: Monte Carlo analysis to quantify strategy robustness against network jitter and exchange failures.
