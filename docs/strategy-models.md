# Clock discipline and strategy models

The software exchange clock, the GLFT and Avellaneda-Stoikov implementations,
the multi-asset Riccati extension, and the adaptive Paper controls.

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
- private Paper fills are evaluated at every configured signed-markout
  horizon (100 ms to 30 s in the tracked profile); at `cost_horizon_ms`
  (1 s) a one-sided confidence bound becomes a side-specific adverse-selection
  cost only after the minimum sample count; each side/horizon uses a finite rolling
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
