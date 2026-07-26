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

Run the offline configuration gate before starting any runtime component:

```powershell
.\.venv\Scripts\python.exe main.py --config config.json --check-config
```

```powershell
.\.venv\Scripts\python.exe main.py --config config.json
```

For an automatically restarted Paper process, `launcher.py` resolves both
files from its own directory and refuses Live configuration:

```powershell
.\.venv\Scripts\python.exe launcher.py
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
is a Paper-validation choice, not live approval. Both implementations now use
the same explicit `bps/sqrt(second)` volatility and fixed-notional inventory
units. GLFT uses the full asymptotic Model A `c1/c2` equation; its live `A/k`
fit accepts only exchange-ACK-to-terminal RPI exposure, including zero-fill
quotes. Public `aggTrade` and Paper fills are never eligible live intensity
samples. Genuine RPI exposure, markout, and walk-forward/OOS evidence are still
required before the build's live formula approval list can be opened.

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

Paper and live state must remain separate. Do not turn the Paper example into
a live file in place. A future live launch must use a dedicated config and
must pass the offline configuration gate before any gateway is constructed.
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
quotes; it does not independently request a market flatten. OMS journal,
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
or exception text. A complete host or process loss still requires an
independent infrastructure heartbeat monitor; the exchange dead-man switch
remains responsible for cancelling orders after such a loss.

Use the blocked canary template and offline checker before moving any file to a
deployment host:

```powershell
.\.venv\Scripts\python.exe scripts\check_live_canary_readiness.py --config config.live.canary.example.json
```

The checked-in example is expected to report `BLOCKED`: its model manifest,
account attestations, and RPI truth are intentionally empty. The checker reads
only local JSON, never reads credential values, constructs no gateway or OMS,
performs no network request, and exercises no order path. A `PASS` is only an
offline prerequisite; startup still refreshes exchange and account truth and
can fail closed.

The accompanying blocked templates document the exact evidence contracts:
`config.live.canary.calibration.example.json` uses calibration artifact v3 and
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

The blocked template is
`config.live.rpi-calibration.example.json`. Its permit path intentionally does
not exist and its trusted-signer set is empty. Check it without making a
network request or constructing a Gateway/OMS:

```powershell
.\.venv\Scripts\python.exe scripts\check_live_canary_readiness.py --config config.live.rpi-calibration.example.json
```

The expected result is `BLOCKED`. Even a future offline `PASS` is only a local
prerequisite: startup must still re-fetch flat account state, RPI eligibility,
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
- [x] Audit the Avellaneda-Stoikov and GLFT implementations against the
      original papers and institutional practice. Formula units and inventory
      direction are internally consistent; GLFT remains the primary model and
      A-S remains a benchmark. The audit identified the implementation and
      evidence gaps listed below.
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
