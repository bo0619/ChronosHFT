# 200 USDT RPI calibration deployment template

Strict v3 manifests for the smallest RPI calibration canary that the Live
guard accepts on roughly 200 USDT of USD-M Futures equity:

| File | Purpose |
| --- | --- |
| `rpi-calibration.json` | Calibration canary manifest (`stage=rpi_calibration_canary`) |
| `canary.json` | Target canary manifest bound by the calibration permit |
| `live.rpi-calibration.json`, `live.canary.json` | Live-only fields: launch envelope, alerts, journals, risk caps |
| `fragments/` | Strategy, OMS, system and risk fragments; `*.rpi-calibration.json` / `*.canary.json` hold per-profile state paths |

Envelope: 8 USDT deployed capital, order, position and gross notional;
0.4 USDT deployment loss; 0.2 USDT daily loss; one active order; 1x isolated
one-way; RPI only with no GTX fallback; 10-second GLFT cycle. Sign the permit
with `--max-calibration-loss-usdt` no higher than 0.4.

## Before use

1. Copy this whole directory outside the repository to the deployment host,
   for example `~/chronoshft-deploy/`. Run every tool from that directory;
   Live requires the working directory to equal the config directory.
2. Replace every placeholder in `live.*.json` and
   `fragments/**/*.{rpi-calibration,canary}.json`:
   - `EDIT-ME-rpi-200u-001`: one fresh deployment ID, identical in both profiles.
   - `EDITMEUSDT`: the RPI symbol you chose (in `fragments/symbols.json`).
     Its exchange minimum notional must be at most 5 USDT.
   - `EDIT-ME-account-scope` and `EDIT-ME-state-genesis` in
     `fragments/risk/independent_supervisor.*.json`, and set
     `cash_flow_deployment_start_ms` there to the deployment start time in
     epoch milliseconds (the template holds 2026-10-07T00:00:00Z).
3. Generate the offline signing key and paste its public signer entry into
   `live_launch.calibration_permit_trusted_signers` in
   `live.rpi-calibration.json` (see `docs/live-rpi-framework-validation-runbook.md`).
4. Set credentials only as environment variables on the deployment host:
   `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `BINANCE_RISK_API_KEY`,
   `BINANCE_RISK_API_SECRET`, `CHRONOSHFT_ALERT_WEBHOOK_URL`. The Live schema
   rejects key or secret values in any JSON file.
5. Freeze both profiles, then sign the permit, collect evidence and run the
   offline readiness check as the runbook describes.

The `canary.json` target still needs a signed model approval
(`canary.approval.json`) before the normal canary stage can run.
