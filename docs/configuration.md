# Configuration and component architecture

How the JSON manifests compose, which fragment owns each setting, how capital
scaling derives every size and loss limit, and which module owns which runtime
responsibility. The manifest and fragment schema contract is described in
[`config/README.md`](../config/README.md).

## Manifests and fragments

`config.json` (Binance), `config.grvt.json` and `config.lighter.json` are
tracked Paper-only manifests, not runtime settings buckets. Each `includes`
array composes 31 single-purpose fragments into one effective configuration;
the GRVT and Lighter manifests swap in their own `config/grvt/` and
`config/lighter/` fragments for execution venue, fees, symbols and capital
scaling. Include paths are resolved relative to the
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
| `infrastructure/durability.py` | Shared durable-state failure contract used across OMS and risk boundaries |
| `infrastructure/oms_risk_port.py` | Stable risk-facing OMS protocol without importing OMS implementation types |
| `infrastructure/runtime_application.py` | Explicit application graph and ordered startup phases |
| `infrastructure/runtime_resources.py` | Named startup resources and mapping-compatible partial-startup cleanup state |
| `infrastructure/runtime_bindings.py` | Event-lane registration, dashboard/alert fan-out, paper observations, and watchdog tick state |
| `infrastructure/runtime_control_loop.py` | One-iteration watchdog, resource, dashboard, and health supervision loop |
| `infrastructure/runtime_failure_policy.py` | Fail-closed callbacks for clock, event-engine, strategy, and evidence failures |
| `infrastructure/runtime_shutdown.py` | Ordered quiesce, account-truth verification, transport drain, and durable shutdown coordinator |
| `gateway/base_gateway.py` | Exchange-facing command and query contract |
| `gateway/binance/gateway.py` | Stable live Binance facade and controller composition root |
| `gateway/binance/connection_controller.py` | Transport lifecycle, generation ownership, connect/recovery, and shutdown state |
| `gateway/binance/user_stream.py` | Listen-key generation, keepalive thread, and invalidation lifecycle |
| `gateway/binance/book_controller.py` | Local order-book ownership, delta buffering, and bounded recovery |
| `gateway/binance/websocket_dispatcher.py` | WebSocket decoding, event classification, normalization, and ingress-freshness checks |
| `gateway/binance/rest_gateway.py` | Order/query REST adaptation and failure classification |
| `gateway/binance/account_configuration.py` | Leverage, margin type, and position-mode verification |
| `gateway/binance/paper_gateway.py` | Public transport, admission barrier, ledger, and event publication facade |
| `gateway/binance/paper_state.py` | Paper order, position, and worker-command state records |
| `gateway/binance/paper_book_sync.py` | Public depth snapshot/delta synchronization, generation fencing, and bounded gap recovery |
| `gateway/binance/paper_ledger.py` | Single-writer fills, position basis, balances, fees, venue-event sequencing, and terminal-history pruning |
| `gateway/binance/paper_matching.py` | Immediate/passive matching, RPI priority, queue-ahead, price eligibility, and fee selection |
| `oms/component.py` | Per-component read/write capability bindings without a facade reference |
| `oms/component_state.py` | Canonical ownership registry for shared OMS fields and compatibility descriptors |
| `oms/outbound_gate.py` | New/reduce/cancel admission sealing and inflight drain coordination |
| `oms/recovery_state.py` | Durable recovery hydration into explicitly owned state cells |
| `oms/exchange_snapshot.py` | Stable exchange snapshot capture and normalization helpers |
| `oms/full_reset.py` | Full-reset transaction coordination and terminal-state clearing |
| `risk/state_repository.py` | Deployment-bound in-process risk state and durability handling |
| `risk/kill_switch.py` | Kill latch, cancel/flatten verification, and recovery policy |
| `risk/account_risk.py` | Margin, equity, cash-flow, and deployment-loss evaluation |
| `risk/market_risk.py` | Price, position, order-rate, and market-health risk evaluation |
| `risk/scope_guards.py` | Symbol/venue freeze scopes and recovery counters |
| `risk/venue_dms.py` | Venue dead-man-switch renewal and safety cancellation |
| `risk/binance_sidecar_exchange.py` | Dedicated sidecar Binance client and snapshot-worker wiring |
| `risk/binance_sidecar_clock.py` | Independent sidecar clock sampling, quality gates, phase-risk thresholds, and monotonic exchange-time anchor |
| `risk/binance_sidecar_settings.py` | Binance sidecar symbol, funding, cash-flow, open-order audit, clock, and REST coordination configuration |
| `risk/binance_sidecar_truth.py` | Consistent account/position/open-order snapshots, funding observations, and deduplicated external cash flow |
| `risk/binance_sidecar_emergency.py` | Independent emergency DMS/cancel and reduce-only flatten actions |
| `risk/sidecar_core.py` | Deterministic child-side heartbeat, risk-stage, rearm, and emergency-action coordinator |
| `risk/sidecar_account_risk.py` | Sidecar account, liquidation, daily-loss, and deployment-loss truth |
| `risk/sidecar_funding_risk.py` | Sidecar funding observations and guard decisions |
| `risk/sidecar_observation.py` | Snapshot generation fencing, freshness, health, and evaluated risk result ownership |
| `risk/sidecar_control_state.py` | Kill, quiesce, stop, staged action, and two-phase rearm state machine |
| `risk/sidecar_command_runtime.py` | STOP and two-phase rearm orchestration over explicit control and truth owners |
| `risk/sidecar_core_status.py` | Complete read-only child-core status projection, age calculations, and IPC field contract |
| `risk/sidecar_health.py` | Parent OMS heartbeat propagation, fail-closed mode constraints, and fresh-snapshot recovery gating |
| `risk/limit_contract.py` | Canonical defaults shared by in-process and sidecar risk caps |
| `risk/sidecar_policy.py` | Immutable normalized sidecar thresholds, timing limits, funding policy, and deployment identity |
| `risk/sidecar_process.py` | Child console isolation, dedicated dual-client initialization, failure status publication, and runtime handoff |
| `risk/sidecar_state_payload.py` | Strict v2 durable payload schema, identity checks, and semantic validation |
| `risk/sidecar_state_projection.py` | Explicit control/equity projection to and from the v2 state payload |
| `risk/sidecar_state_store.py` | Account-scoped SQLite CAS, writer fences, hash history, rollback anchors, and cash-flow ledger |
| `risk/sidecar_flat_proof.py` | Account-wide stable flatness proof capture bound to state and safety epochs |
| `risk/runtime_clock.py` | Injected monotonic, UTC, and sleep clock for the isolated child runtime |
| `risk/sidecar_protocol.py` | Versioned launch/capability handshake, wire validation, and request-correlated control ACK decoding |
| `risk/sidecar_runtime.py` | Child command routing, latest-only heartbeat consumption, status publication, and worker/client cleanup |
| `risk/sidecar_settings.py` | Parent timeout validation, cross-config child settings assembly, credential fingerprinting, and risk-state seeds |
| `risk/sidecar_snapshot_worker.py` | Capacity-one asynchronous exchange snapshot execution, latest-result delivery, and bounded thread shutdown |
| `risk/sidecar_status.py` | Read-only parent status projection, freshness calculation, and dashboard/admin snapshot contract |
| `risk/sidecar_transport.py` | Parent-side process lifecycle, bounded IPC queues, status draining, and reliable control requests |
| `risk/sidecar_supervisor.py` | Parent process lifecycle, heartbeat, health recovery, control requests, and shutdown |
| `risk/independent_supervisor.py` | Compatibility exports and child-process composition root |
| `strategy/contracts.py` | Structural event-handler contract consumed by the strategy runtime |
| `strategy/runtime.py` | Bounded event scheduling, coalescing, timing, and fail-closed dispatch |

The risk sidecar's wire fields, strict same-version rule, capability handshake,
and version-bump policy are documented in
[`docs/sidecar-ipc-protocol.md`](sidecar-ipc-protocol.md).

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
samples at 2.5 GiB RSS, 96 main threads, 4,096 main file descriptors, 112
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
limits; `target_order_notional`; and `max_pos_usdt` from that
single source. Do not declare these derived fields in fragments; the loader
always computes their effective values.

The tracked Binance Paper profile uses notional sizing for a 10,000 USDT
simulated account. Each normal bid or ask targets 500 USDT (5% of capital),
then converts that notional to contract quantity and rounds down to the
exchange lot step. The one-symbol inventory and account gross-notional caps
are both 2,500 USDT (25%); the daily-loss cap is 500 USDT (5%). The profile was
sized up 5x from 1%/5%/1% to make Paper P&L readable; per-fill edge in bps does
not change with size. GRVT and Lighter keep the 100 USDT quote. Remaining
inventory capacity can reduce or suppress a quote, and safety exits may use the
residual position quantity.

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
correlation, so highly correlated pairs are not treated as independent
diversification; the tracked profile quotes one symbol.

After any configuration edit, run the offline gate before starting the engine:

```bash
.venv/bin/python main.py --config config.json --check-config
```

A valid tracked configuration currently reports:

```text
CONFIG_OK mode=paper symbols=1 primary_model=glft capital_usdt=10000 order_notional_usdt=500 max_position_usdt=2500 max_gross_usdt=2500 max_daily_loss_usdt=500
```

Live deployment files remain operator-owned, single-file JSON documents and
are ignored by Git. Fragmented manifests intentionally fail closed in Live
mode until the deployment evidence digest can bind every included fragment.
