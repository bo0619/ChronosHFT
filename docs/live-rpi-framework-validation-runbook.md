# Live RPI Framework Validation Runbook

This runbook is for validating ChronosHFT's real-money control plane with a
small Binance USD-M RPI calibration canary. It is not a profitability approval
and it does not authorize the normal `canary` stage.

## Non-negotiable boundary

- Do not run the Live process from mainland China or any environment where
  Binance access or account use is not permitted.
- The mainland development machine may perform source review, strict JSON
  checks, offline readiness checks, key generation, and permit signing only.
- Never use Testnet or Paper fills as evidence that the Live RPI path works.
- Never disable a failing gate to obtain a first order.
- `live_launch.declared_account_equity_usdt` means actual USD-M Futures account
  equity, not the operator's total assets. Never overstate it to pass a ratio
  check. The checked-in 50 USDT profile requires roughly 10,000 USDT of actual
  Futures equity because calibration deployment is capped at 0.5% of equity.
- Keep capital outside the validation Futures account when the account structure
  permits it, and scale every related configuration limit down coherently. The
  software limits are not an exchange-enforced subaccount balance cap.

## What this validation proves

The minimum useful result proves all of the following:

1. The allowed deployment host can synchronize exchange time and maintain
   Binance public and private sessions.
2. Binance reports the chosen symbol as `TRADING` with explicit RPI support.
3. The account-specific `rpiCommissionRate` is exactly zero at startup and on
   every successful 30-second runtime poll. A non-zero observation halts the
   system; a query failure freezes the venue and repeated failures halt it.
4. A deployment-bound signed permit reaches the final OMS send boundary, and
   the observed order path respects its RPI-only, active-order, and notional
   constraints. Cadence is Live-observed only if at least two orders are sent;
   count, cadence, and loss caps are also validated offline. This run does not
   deliberately create losses merely to trip a cap.
5. REST acknowledgement, user-stream order truth, TTL cancellation, durable
   journaling, alerts, DMS renewal, and verified `Ctrl+C` shutdown are observable.
6. If a natural RPI fill occurs, execution, fee, position, account, and
   reduce-only emergency-exit truth reconcile.

The run does not prove GLFT profitability, stable queue priority, or sufficient
OOS evidence. Those remain requirements for the normal `canary` stage.

## Required external setup

- One legally permitted deployment host with stable time and storage.
- Two distinct IP-allowlisted API keys for the same USD-M Futures account.
- Reading and Futures trading enabled on both keys; withdrawals disabled.
- The supervisor key must be able to cancel orders and submit reduce-only
  market exits.
- One HTTPS alert endpoint reachable from the deployment host.
- One operator-supplied off-host heartbeat/alert monitor. This repository does
  not currently provision that independent external monitor.
- Isolated margin, one-way position mode, and 1x leverage for the selected
  symbol, configured manually before launch. Keep
  `account.configuration_mode="VERIFY_ONLY"`; Live startup reads and verifies
  these settings and must never change them.
- Zero account-wide positions and zero open orders before startup.
- A fresh deployment ID and unused live journal, fence, evidence, alert spool,
  admin-control directory, and supervisor-state paths.

RPI orders are API-submitted maker orders, but Binance prevents them from
matching API-originated counterparties. A fill must arrive naturally from an
eligible counterparty. Ten attempts may produce no fill; that is not evidence
of a broken submit/cancel path.

## Prepare the offline signer

Keep the private key outside the repository and outside the deployment host.
On Windows, also restrict its NTFS ACL; `chmod` semantics alone are not a full
Windows access-control policy.

Set a temporary passphrase environment variable without placing the secret on
the command line:

```powershell
$secure = Read-Host "Permit key passphrase" -AsSecureString
$env:CHRONOSHFT_PERMIT_KEY_PASSPHRASE = [System.Net.NetworkCredential]::new("", $secure).Password
```

Generate the encrypted Ed25519 key and a public trust fragment:

```powershell
.\.venv\Scripts\python.exe scripts\create_rpi_calibration_permit.py generate-key `
  --private-key C:\ChronosHFT-offline\rpi-calibration-key.pem `
  --trust-output C:\ChronosHFT-offline\rpi-calibration-trust.json `
  --key-id personal-rpi-calibration-2026
```

Copy only the public signer entry into
`live_launch.calibration_permit_trusted_signers` in the real calibration
configuration. Never copy the private PEM to the repository or deployment
host. Configuration digests change after this edit, so sign the permit only
after both deployment configurations are final.

## Prepare deployment files

1. Create operator-owned `config.live.rpi-calibration.json` and
   `config.live.canary.json` files when Live deployment work resumes. These
   files are intentionally ignored by Git.
2. Set one fresh, matching `deployment_id` in both files.
3. From the allowed host, inspect current symbol metadata, RPI eligibility,
   account-specific commission, tick size, spread, and minimum notional. Select
   exactly one symbol; treat `XAUUSDT` as illustrative only. This early check is
   for configuration and does not replace the final short-lived evidence.
4. Copy the public signer entry into the calibration config and make its
   `target_deployment_config_path` and
   `calibration_permit_path` resolve to the exact final files.
5. Scale the capital, account, strategy, risk, and permit values together for
   the actual USD-M equity, then freeze both configs before signing.
6. After transfer, configure `BINANCE_API_KEY`, `BINANCE_API_SECRET`,
   `BINANCE_RISK_API_KEY`, `BINANCE_RISK_API_SECRET`, and the alert-webhook
   environment variable on the deployment host. Do not store their values in
   JSON, shell history, logs, or the dashboard.

For a framework-only run, sign a permit for 6-10 attempts, one active order,
a target near 8 USDT with a permitted 5-8 USDT range, 10-second TTL/cadence,
and no more than 1 USDT calibration loss. Pick at least three increasing depths
spanning at least 0.5 bps after inspecting the real symbol's tick size and
spread. Generic example depths are not an instruction to trade them.

The permit count is only a hard deployment ceiling. It is not required to
cover training and OOS datasets in the same deployment, and it is not a reason
to issue 100 orders during framework validation. Do not count multiple fills
of one partially filled order as independent order samples.

The 8 USDT target requires at least 8 USDT deployed capital. At the 0.5%
calibration ratio, that implies at least about 1,600 USDT of actual USD-M
Futures equity. The example instead declares 10,000 USDT because its deployed
capital cap is 50 USDT. If the real Futures equity is lower, reduce both
configs' declared equity, deployed capital, account budgets, target and permit
notional, order/position/gross limits, and loss limits together, while still
meeting the exchange's current minimum notional. Do not edit only the declared
equity number.

## Sign the permit offline

The output path must exactly equal the calibration config's
`live_launch.calibration_permit_path`. The tool refuses overwrite, verifies
that both configs are production Live files, binds their digests and strategy
implementation, matches the configured public key, and re-validates the final
signature before writing.

```powershell
$now = (Get-Date).ToUniversalTime()
$issuedAt = $now.ToString("yyyy-MM-ddTHH:mm:ssZ")
$notBefore = $now.AddMinutes(5).ToString("yyyy-MM-ddTHH:mm:ssZ")
$expiresAt = $now.AddHours(1).ToString("yyyy-MM-ddTHH:mm:ssZ")
$permitId = "rpi-framework-" + $now.ToString("yyyyMMddTHHmmssZ")
$depth1 = Read-Host "First permitted depth in bps"
$depth2 = Read-Host "Second permitted depth in bps"
$depth3 = Read-Host "Third permitted depth in bps"

.\.venv\Scripts\python.exe scripts\create_rpi_calibration_permit.py sign `
  --calibration-config config.live.rpi-calibration.json `
  --target-config config.live.canary.json `
  --private-key C:\ChronosHFT-offline\rpi-calibration-key.pem `
  --output config.live.rpi-calibration.permit.json `
  --key-id personal-rpi-calibration-2026 `
  --authorized-by "Personal Operator" `
  --permit-id $permitId `
  --issued-at $issuedAt `
  --not-before $notBefore `
  --expires-at $expiresAt `
  --fixed-depth-bps $depth1 `
  --fixed-depth-bps $depth2 `
  --fixed-depth-bps $depth3 `
  --order-ttl-sec 10 `
  --min-order-interval-sec 10 `
  --max-order-count 10 `
  --min-order-notional-usdt 5 `
  --max-order-notional-usdt 8 `
  --max-cumulative-submitted-notional-usdt 80 `
  --max-calibration-loss-usdt 1
```

Clear the passphrase from the environment after signing:

```powershell
Remove-Item Env:CHRONOSHFT_PERMIT_KEY_PASSPHRASE
```

Transfer only the code, signed permit, public trust configuration, and final
deployment configs to the allowed host over an authenticated channel. Keep the
private key off that host. Collect the short-lived exchange/account evidence
last, on the allowed host itself; do not round-trip it through the mainland
machine and consume its 900-second validity window.

The collector has a fixed mainnet-host and GET-only allowlist. All six manual
confirmations are mandatory; in particular, matching balances do not prove
that the two API keys belong to the same Futures account. It writes v4 evidence
with separate, domain-bound HMAC-SHA256 tags from the primary and supervisor
API secrets. The evidence target must be a local regular file inside the config
directory; UNC, device, mapped-network, parent-traversal, symlink, and reparse
paths are rejected.

```powershell
Set-Location C:\ChronosHFT-deploy
.\.venv\Scripts\python.exe scripts\collect_live_canary_evidence.py `
  --config .\config.live.rpi-calibration.json `
  --confirm-legal-access `
  --confirm-single-process `
  --confirm-same-futures-account `
  --confirm-supervisor-emergency-permissions `
  --confirm-legacy-state-archived `
  --confirm-fresh-state-generation
```

## Offline preflight

On the deployment host, first change into the exact directory containing the
final config. The Live guard requires the process working directory to equal
the config directory so every relative durable path has one identity. Collect
fresh evidence there, then immediately run this preflight. It performs no
network request and constructs no Gateway or OMS:

```powershell
Set-Location C:\ChronosHFT-deploy
.\.venv\Scripts\python.exe scripts\check_live_canary_readiness.py `
  --config .\config.live.rpi-calibration.json
```

Do not start unless every offline check reports `PASS`. Offline preflight has no
access to either API secret, so it validates only the integrity envelope's
schema and tag format. `main.py` recomputes both HMACs with the resolved secrets
using constant-time comparisons before accepting the evidence. A PASS is only
a local prerequisite; startup must still refresh exchange/account/RPI/fee truth
and can fail closed.

## Live acceptance sequence

1. Start the independent external host/alert monitor, then start ChronosHFT
   from the same config directory. Do not use an IDE's arbitrary working
   directory and do not use `--rearm` on a fresh deployment:

   ```powershell
   Set-Location C:\ChronosHFT-deploy
   .\.venv\Scripts\python.exe main.py `
     --config .\config.live.rpi-calibration.json
   ```

   The Live canary fails before Gateway connection if the loopback dashboard
   cannot bind. A pre-existing process on port 8765 must be stopped or the
   configured loopback port must be changed and the configs re-signed.

2. Use the dashboard at `http://127.0.0.1:8765` for clock, supervisor,
   market/account truth, RPI, active-order state, and the **实盘验收** section.
   Require stage `rpi_calibration_canary`, the expected permit ID and all four
   binding digests, and healthy external-alert and live-evidence panels. Cross-
   check `http://127.0.0.1:8765/api/snapshot`: require
   `system.oms.capability.outbound_gate.rpi_calibration.enabled=true`, plus
   healthy `runtime.external_alerts` and `runtime.live_evidence`.
   For a remote host, use an SSH local-forward tunnel to `127.0.0.1:8765`.
   Never change the dashboard bind address to `0.0.0.0`.
3. Observe one RPI submit and a true exchange acknowledgement. A local intent
   without exchange acknowledgement is not a pass. After reservation, require
   the same API snapshot's `permit_activated=true`. If two or more orders are
   observed, confirm their exchange-time reservations are separated by at least
   the signed `min_order_interval_sec`.
4. Let one unfilled order reach its TTL. There is currently no supported admin
   cancel command in the CLI or dashboard. Confirm exchange truth reaches a
   terminal state and the account-wide open-order snapshot is empty.
5. On any GTX route, non-RPI order, second active order, unknown order state,
   stale clock, stale market/account truth, alert failure, evidence failure,
   DMS failure, or non-zero RPI commission observation, immediately enter the
   single-`Ctrl+C` controlled shutdown in step 7 and wait for it to finish.
6. If a natural fill occurs, confirm the execution is maker and RPI, exchange
   commission and booked fee are exactly zero, and position truth matches the
   fill. Observe any passive reduce-only exit for no longer than one signed TTL;
   it may remain unfilled or partially filled and is not required to flatten
   naturally.
7. After the ordinary TTL path is terminal, press `Ctrl+C` once and allow the
   supported shutdown sequence to cancel orders and, if needed, execute its
   reduce-only market flatten. Do not close the terminal window or use Task
   Manager as a kill test.
8. Stop before permit expiry where practical. In the journal, require
   `oms_stopped.cancel_verified=true`. Separately require the terminal
   `ChronosHFT Shutdown Complete` record to show `verified_cancel=True`,
   `flatten_verified=True`, `oms_clean=True`, and `terminal=True`, backed by two
   consecutive independent flat account snapshots. Account-wide open orders
   and positions must both be zero.
   If the permit expires while the process is still running, also require the
   dashboard/API calibration state to show
   `terminal_convergence_verified=true` and `terminal_empty_snapshots>=2`.
   `Expired` by itself is not proof that late fills were flattened.

An emergency market flatten can incur taker fees and slippage. Zero RPI maker
commission does not mean every exit, funding payment, or loss is free.

## Pass, partial pass, and fail

`CONTROL-PLANE PASS` requires submit/ACK, terminal TTL-cancel truth, observed
permit order constraints, healthy alert/evidence/DMS/supervisor planes, and a
clean flat shutdown. It does not require a natural fill, 30 training orders,
100 OOS fills, or evidence of profitability.

Negative PnL inside every signed and configured loss limit does not by itself
fail framework validation. Breaching a loss limit, continuing to submit after
a limit is reached, failing to cancel, or failing to return to verified flat is
always `FAIL`.

Offline validation proves that count and loss caps are correctly signed and
bound. Reaching the order-count ceiling and observing submission stop adds a
Live count-cap observation, but it is not required for this minimal pass. Do
not intentionally lose money merely to claim that the loss cap fired in Live.

`FILL-PLANE PASS` additionally requires at least one natural RPI maker fill,
exact zero RPI commission for that fill, execution/position/account
reconciliation, and a final exchange-verified return to flat. It does not
require a passive exit order itself to fill.

No natural fill means `CONTROL-PLANE PASS / FILL-PLANE NOT OBSERVED`. Do not
manufacture a counterparty or use another API key to self-trade.

Any unresolved order, non-flat shutdown, non-zero RPI commission, non-RPI
route, evidence gap, duplicate process, stale truth, or failed emergency exit
is `FAIL`. Archive all state and investigate; do not rearm or reuse the permit.

## Incident boundary

The sidecar is independent from the parent process but normally shares its
host. A total host or network loss can leave an existing position open after
the venue DMS cancels orders. Keep the first position at the configured gross
cap (8 USDT in the example), maintain an off-host alert/heartbeat, and retain a
separately authenticated manual Binance close path.
