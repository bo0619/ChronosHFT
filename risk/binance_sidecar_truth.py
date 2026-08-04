"""Consistent exchange-truth snapshots for the Binance risk sidecar."""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Protocol

from risk.exchange_port import (
    AccountTruthSnapshot,
    CashFlowTruth,
    SnapshotPurpose,
    TruthResult,
)
from risk.funding_guard import parse_binance_premium_index_payload


BINANCE_PREMIUM_INDEX_ENDPOINT = "/fapi/v1/premiumIndex"


class BinanceSidecarTruthOwner(Protocol):
    rest: object
    symbols: tuple[str, ...]
    funding_guard_enabled: bool
    funding_max_source_age_ms: float
    daily_loss_enabled: bool
    cash_flow_required: bool
    cash_flow_income_types: set[str]
    cash_flow_assets: set[str]
    cash_flow_max_pages: int
    cash_flow_poll_interval_sec: float
    cash_flow_deployment_start_ms: int
    _last_cash_flow_poll_monotonic: float
    _cached_external_cash_flow_total: float
    _cached_daily_external_cash_flow_total: float
    _cached_deployment_external_cash_flow_total: float
    _deployment_cash_flow_carry: float
    _cash_flow_cache_day: str
    _cash_flow_cache_generation: int
    _cash_flow_cache_initialized: bool
    full_open_orders_audit_interval_sec: float
    _last_full_open_orders_audit_monotonic: float
    _known_open_order_symbols: set[str]
    clock_reason: str
    clock_offset_ms: float
    clock_phase_error_ms: float
    clock_rtt_ms: float
    clock_uncertainty_ms: float
    clock_offset_dispersion_ms: float

    def _ensure_exchange_clock(self, force: bool = False): ...

    def _response_payload(self, response, expected_type, label: str): ...

    def _position_risk_fingerprint(self, positions): ...

    def _corrected_epoch_at(self, observed_monotonic: float): ...

    def _get_open_orders_snapshot(self): ...

    def _get_cached_daily_external_cash_flow(self): ...

    def _get_cached_external_cash_flow_truth(self): ...

    def _get_funding_observations(self): ...

    def _income_identity(self, row: dict): ...

    def _get_daily_external_cash_flow(self): ...

    def _remember_open_order_symbols(self, rows): ...


class BinanceSidecarTruthReader:
    """Read fail-closed account truth without owning emergency actions."""

    __slots__ = ("_owner",)

    def __init__(self, owner: BinanceSidecarTruthOwner):
        self._owner = owner

    def check_account_channel(self):
        owner = self._owner
        try:
            response = owner.rest.get_account()
            status_code = getattr(response, "status_code", None)
            if status_code != 200:
                return False, f"account_status={status_code or 'unavailable'}"
            payload = response.json()
            if not isinstance(payload, dict):
                return False, "account_payload_invalid"
            return True, ""
        except Exception as exc:
            return False, f"account_exception:{type(exc).__name__}:{exc}"

    @staticmethod
    def response_payload(response, expected_type, label: str):
        status_code = getattr(response, "status_code", None)
        if status_code != 200:
            return (
                False,
                None,
                f"{label}_status={status_code or 'unavailable'}",
            )
        try:
            payload = response.json()
        except Exception as exc:
            return False, None, f"{label}_json:{type(exc).__name__}:{exc}"
        if not isinstance(payload, expected_type):
            return False, None, f"{label}_payload_invalid"
        return True, payload, ""

    @staticmethod
    def position_risk_fingerprint(positions) -> tuple:
        def normalized_number(value):
            try:
                parsed = Decimal(str(value or "0"))
            except (InvalidOperation, ValueError):
                return str(value or "").strip()
            if not parsed.is_finite():
                return str(value or "").strip()
            return str(parsed.normalize())

        rows = []
        for position in positions:
            if not isinstance(position, dict):
                rows.append(("<invalid>", repr(position)))
                continue
            rows.append(
                (
                    str(position.get("symbol", "") or "").upper(),
                    str(
                        position.get("positionSide", "BOTH") or "BOTH"
                    ).upper(),
                    normalized_number(position.get("positionAmt")),
                    normalized_number(position.get("entryPrice")),
                    normalized_number(position.get("breakEvenPrice")),
                    normalized_number(position.get("isolatedWallet")),
                    normalized_number(position.get("liquidationPrice")),
                    normalized_number(position.get("leverage")),
                    str(position.get("marginType", "") or "").upper(),
                    str(position.get("isolated", "") or "").lower(),
                    normalized_number(position.get("updateTime")),
                )
            )
        return tuple(sorted(rows))

    @staticmethod
    def open_order_fingerprint(open_orders) -> tuple:
        rows = []
        for order in open_orders:
            if not isinstance(order, dict):
                rows.append(("<invalid>", repr(order)))
                continue
            rows.append(
                tuple(
                    str(order.get(field, "") or "").strip()
                    for field in (
                        "symbol",
                        "orderId",
                        "clientOrderId",
                        "side",
                        "positionSide",
                        "type",
                        "status",
                        "price",
                        "origQty",
                        "executedQty",
                        "reduceOnly",
                        "closePosition",
                        "time",
                        "updateTime",
                    )
                )
            )
        return tuple(sorted(rows))

    def corrected_now(self) -> tuple[float, float] | None:
        observed_monotonic = time.perf_counter()
        correct = getattr(self._owner, "_corrected_epoch_at", None)
        corrected = (
            correct(observed_monotonic) if callable(correct) else None
        )
        if corrected is None:
            wall_time = getattr(self._owner, "_wall_time", time.time)
            corrected = float(wall_time())
        try:
            corrected = float(corrected)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(corrected) or corrected <= 0.0:
            return None
        return observed_monotonic, corrected

    def get_funding_observations(self):
        owner = self._owner
        if not getattr(owner, "funding_guard_enabled", False):
            return True, {}, ""
        if not owner.symbols:
            return False, {}, "funding_guard_symbols_missing"

        observations = {}
        for symbol in owner.symbols:
            ok, payload, reason = owner._response_payload(
                owner.rest.request(
                    "GET",
                    BINANCE_PREMIUM_INDEX_ENDPOINT,
                    {"symbol": symbol},
                    signed=False,
                ),
                dict,
                f"funding_rate:{symbol}",
            )
            if not ok:
                return False, {}, reason
            received_monotonic = time.perf_counter()
            corrected_received_epoch = owner._corrected_epoch_at(
                received_monotonic
            )
            if corrected_received_epoch is None:
                return (
                    False,
                    {},
                    f"funding_clock_anchor_unavailable:{symbol}",
                )
            try:
                observation = parse_binance_premium_index_payload(
                    payload,
                    expected_symbol=symbol,
                    corrected_received_epoch=corrected_received_epoch,
                    received_monotonic=received_monotonic,
                    max_source_age_ms=owner.funding_max_source_age_ms,
                    clock_healthy=not bool(owner.clock_reason),
                )
            except ValueError as exc:
                return (
                    False,
                    {},
                    f"funding_payload_invalid:{symbol}:{exc}",
                )
            observations[symbol] = {
                "observation_id": observation.observation_id,
                "funding_rate": observation.funding_rate,
                "next_funding_epoch": observation.next_funding_epoch,
                "corrected_received_epoch": (
                    observation.corrected_received_epoch
                ),
                "received_monotonic": observation.received_monotonic,
                "clock_healthy": observation.clock_healthy,
            }
        return True, observations, ""

    def get_risk_snapshot(self):
        owner = self._owner
        try:
            clock_ok, reason = owner._ensure_exchange_clock()
            if not clock_ok:
                return False, {}, reason
            account_ok, account, reason = owner._response_payload(
                owner.rest.get_account(),
                dict,
                "account",
            )
            if not account_ok:
                return False, {}, reason
            positions_ok, positions, reason = owner._response_payload(
                owner.rest.get_positions(),
                list,
                "positions",
            )
            if not positions_ok:
                return False, {}, reason
            orders_ok, open_orders, reason = (
                owner._get_open_orders_snapshot()
            )
            if not orders_ok:
                return False, {}, reason
            positions_after_ok, positions_after, reason = (
                owner._response_payload(
                    owner.rest.get_positions(),
                    list,
                    "positions_after_open_orders",
                )
            )
            if not positions_after_ok:
                return False, {}, reason
            if owner._position_risk_fingerprint(
                positions
            ) != owner._position_risk_fingerprint(positions_after):
                return (
                    False,
                    {},
                    "snapshot_inconsistent:positions_changed_during_"
                    "open_orders_query",
                )
            positions = positions_after
            cash_flow_truth = None
            if getattr(
                owner,
                "cash_flow_required",
                getattr(owner, "daily_loss_enabled", False),
            ):
                cash_flow_ok, cash_flow_truth, reason = (
                    owner._get_cached_external_cash_flow_truth()
                )
                if not cash_flow_ok:
                    return False, {}, reason
            funding_ok, funding_observations, reason = (
                owner._get_funding_observations()
            )
            if not funding_ok:
                return False, {}, reason
            corrected_now = self.corrected_now()
            if corrected_now is None:
                return False, {}, "snapshot_clock_timestamp_unavailable"
            _, captured_at = corrected_now
            cash_flow_fields = (
                cash_flow_truth.as_snapshot_fields()
                if isinstance(cash_flow_truth, CashFlowTruth)
                else {
                    "daily_external_cash_flow_total": 0.0,
                    "deployment_external_cash_flow_total": 0.0,
                    "external_cash_flow_total": 0.0,
                }
            )

            return (
                True,
                {
                    "account": account,
                    "positions": positions,
                    "open_orders": open_orders,
                    "funding_observations": funding_observations,
                    **cash_flow_fields,
                    "clock_offset_ms": float(
                        getattr(owner, "clock_offset_ms", 0.0) or 0.0
                    ),
                    "clock_phase_error_ms": float(
                        getattr(owner, "clock_phase_error_ms", 0.0)
                        or 0.0
                    ),
                    "clock_rtt_ms": float(
                        getattr(owner, "clock_rtt_ms", 0.0) or 0.0
                    ),
                    "clock_uncertainty_ms": float(
                        getattr(owner, "clock_uncertainty_ms", 0.0)
                        or 0.0
                    ),
                    "clock_offset_dispersion_ms": float(
                        getattr(
                            owner,
                            "clock_offset_dispersion_ms",
                            0.0,
                        )
                        or 0.0
                    ),
                    "captured_at": captured_at,
                },
                "",
            )
        except Exception as exc:
            return (
                False,
                {},
                f"snapshot_exception:{type(exc).__name__}:{exc}",
            )

    def read_account_truth(
        self,
        purpose: SnapshotPurpose,
    ) -> TruthResult:
        """Return normalized truth, forcing a double full-account read for proof."""
        if purpose != SnapshotPurpose.FLAT_PROOF:
            ok, snapshot, reason = self.get_risk_snapshot()
            if not ok:
                return TruthResult(False, reason=reason)
            return self._normalized_truth(
                snapshot,
                orders_scope="MONITORING",
                positions_scope="ACCOUNT_WIDE",
            )

        owner = self._owner
        try:
            clock_ok, reason = owner._ensure_exchange_clock(force=True)
            if not clock_ok:
                return TruthResult(False, reason=reason)
            account_ok, account, reason = owner._response_payload(
                owner.rest.get_account(),
                dict,
                "flat_proof_account",
            )
            if not account_ok:
                return TruthResult(False, reason=reason)
            snapshots = []
            for sample in (1, 2):
                positions_ok, positions, reason = owner._response_payload(
                    owner.rest.get_positions(emergency=True),
                    list,
                    f"flat_proof_positions_{sample}",
                )
                if not positions_ok:
                    return TruthResult(False, reason=reason)
                orders_ok, orders, reason = owner._response_payload(
                    owner.rest.get_open_orders(emergency=True),
                    list,
                    f"flat_proof_open_orders_{sample}",
                )
                if not orders_ok:
                    return TruthResult(False, reason=reason)
                if not all(isinstance(row, dict) for row in positions):
                    return TruthResult(
                        False,
                        reason="flat_proof_position_row_invalid",
                    )
                if not all(isinstance(row, dict) for row in orders):
                    return TruthResult(
                        False,
                        reason="flat_proof_order_row_invalid",
                    )
                snapshots.append((positions, orders))
            first_positions, first_orders = snapshots[0]
            positions, open_orders = snapshots[1]
            if self.position_risk_fingerprint(
                first_positions
            ) != self.position_risk_fingerprint(positions):
                return TruthResult(
                    False,
                    reason="flat_proof_positions_changed_during_double_read",
                )
            if self.open_order_fingerprint(
                first_orders
            ) != self.open_order_fingerprint(open_orders):
                return TruthResult(
                    False,
                    reason="flat_proof_orders_changed_during_double_read",
                )
            snapshot = {
                "account": account,
                "positions": positions,
                "open_orders": open_orders,
                "funding_observations": {},
            }
            return self._normalized_truth(
                snapshot,
                orders_scope="ACCOUNT_WIDE",
                positions_scope="ACCOUNT_WIDE",
            )
        except Exception as exc:
            return TruthResult(
                False,
                reason=(
                    "flat_proof_exception:"
                    f"{type(exc).__name__}:{exc}"
                ),
            )

    def _normalized_truth(
        self,
        snapshot: dict,
        *,
        orders_scope: str,
        positions_scope: str,
    ) -> TruthResult:
        owner = self._owner
        corrected_now = self.corrected_now()
        if corrected_now is None:
            return TruthResult(
                False,
                reason="truth_clock_timestamp_unavailable",
            )
        captured_monotonic, captured_at = corrected_now
        positions = tuple(snapshot.get("positions", ()) or ())
        open_orders = tuple(snapshot.get("open_orders", ()) or ())
        owner._truth_sequence = int(
            getattr(owner, "_truth_sequence", 0) or 0
        ) + 1
        fingerprints = {
            "positions": self.position_risk_fingerprint(positions),
            "orders": self.open_order_fingerprint(open_orders),
        }
        digest = hashlib.sha256(
            json.dumps(
                fingerprints,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        cash_flow = None
        if isinstance(snapshot.get("cash_flow_truth"), CashFlowTruth):
            cash_flow = snapshot["cash_flow_truth"]
        return TruthResult(
            True,
            AccountTruthSnapshot(
                account_scope_id=str(
                    getattr(
                        owner,
                        "account_scope_id",
                        getattr(owner, "account_key_fingerprint", ""),
                    )
                    or ""
                ),
                truth_sequence=owner._truth_sequence,
                captured_monotonic=captured_monotonic,
                captured_utc_ms=int(captured_at * 1000),
                orders_scope=orders_scope,
                positions_scope=positions_scope,
                complete=True,
                consistency_digest=digest,
                account=dict(snapshot.get("account", {}) or {}),
                positions=positions,
                open_orders=open_orders,
                cash_flow=cash_flow,
                funding=dict(
                    snapshot.get("funding_observations", {}) or {}
                ),
                clock_health={
                    "offset_ms": float(
                        getattr(owner, "clock_offset_ms", 0.0) or 0.0
                    ),
                    "phase_error_ms": float(
                        getattr(owner, "clock_phase_error_ms", 0.0) or 0.0
                    ),
                },
            ),
        )

    @staticmethod
    def income_identity(row: dict) -> str:
        income_type = str(
            row.get("incomeType", row.get("income_type", "")) or ""
        ).upper()
        transaction_id = row.get("tranId", row.get("trandId"))
        if transaction_id not in (None, ""):
            return f"{income_type}:{transaction_id}"
        fingerprint = "|".join(
            str(row.get(key, "") or "")
            for key in ("time", "asset", "income", "symbol", "info")
        )
        return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()

    def get_daily_external_cash_flow(self):
        corrected_now = self.corrected_now()
        if corrected_now is None:
            return False, 0.0, "cash_flow_clock_timestamp_unavailable"
        _, corrected_epoch = corrected_now
        now = datetime.fromtimestamp(corrected_epoch, tz=timezone.utc)
        day_start_ms = int(
            now.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            ).timestamp()
            * 1000
        )
        end_time_ms = int(now.timestamp() * 1000)
        return self.get_external_cash_flow_total(day_start_ms, end_time_ms)

    def get_external_cash_flow_total(
        self,
        start_time_ms: int,
        end_time_ms: int,
    ):
        owner = self._owner
        total = 0.0
        seen = set()
        limit = 1000
        for page in range(1, owner.cash_flow_max_pages + 1):
            ok, rows, reason = owner._response_payload(
                owner.rest.get_income_history(
                    start_time=int(start_time_ms),
                    end_time=end_time_ms,
                    page=page,
                    limit=limit,
                ),
                list,
                "income_history",
            )
            if not ok:
                return False, 0.0, reason
            for row in rows:
                if not isinstance(row, dict):
                    return False, 0.0, "income_history_row_invalid"
                income_type = str(
                    row.get(
                        "incomeType",
                        row.get("income_type", ""),
                    )
                    or ""
                ).upper()
                if income_type not in owner.cash_flow_income_types:
                    continue
                asset = str(row.get("asset", "") or "").upper()
                if asset not in owner.cash_flow_assets:
                    return (
                        False,
                        0.0,
                        f"cash_flow_asset_unsupported:{asset or 'empty'}",
                    )
                identity = owner._income_identity(row)
                if identity in seen:
                    continue
                seen.add(identity)
                try:
                    amount = float(row.get("income", 0.0) or 0.0)
                except (TypeError, ValueError):
                    return False, 0.0, "cash_flow_amount_invalid"
                if not math.isfinite(amount):
                    return False, 0.0, "cash_flow_amount_non_finite"
                total += amount
            if len(rows) < limit:
                return True, total, ""
        return False, 0.0, "income_history_page_limit_exceeded"

    def get_cached_external_cash_flow_truth(self):
        owner = self._owner
        corrected_now = self.corrected_now()
        if corrected_now is None:
            return False, None, "cash_flow_clock_timestamp_unavailable"
        now_monotonic, corrected_epoch = corrected_now
        now_datetime = datetime.fromtimestamp(
            corrected_epoch,
            tz=timezone.utc,
        )
        risk_day = now_datetime.date().isoformat()
        end_time_ms = int(corrected_epoch * 1000)
        day_start_ms = int(
            now_datetime.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            ).timestamp()
            * 1000
        )
        initialized = bool(
            getattr(owner, "_cash_flow_cache_initialized", False)
        )
        cache_day = str(
            getattr(owner, "_cash_flow_cache_day", "") or ""
        )
        last_poll = float(
            getattr(owner, "_last_cash_flow_poll_monotonic", 0.0) or 0.0
        )
        interval = max(
            1.0,
            float(
                getattr(owner, "cash_flow_poll_interval_sec", 30.0)
                or 30.0
            ),
        )
        if (
            initialized
            and cache_day == risk_day
            and now_monotonic - last_poll < interval
        ):
            return (
                True,
                CashFlowTruth(
                    risk_day=risk_day,
                    daily_external_cash_flow_total=float(
                        getattr(
                            owner,
                            "_cached_daily_external_cash_flow_total",
                            getattr(
                                owner,
                                "_cached_external_cash_flow_total",
                                0.0,
                            ),
                        )
                        or 0.0
                    ),
                    deployment_external_cash_flow_total=float(
                        getattr(
                            owner,
                            "_cached_deployment_external_cash_flow_total",
                            0.0,
                        )
                        or 0.0
                    ),
                    ledger_generation=int(
                        getattr(owner, "_cash_flow_cache_generation", 0)
                        or 0
                    ),
                    complete_through_ms=int(
                        getattr(
                            owner,
                            "_cash_flow_cache_complete_through_ms",
                            end_time_ms,
                        )
                        or end_time_ms
                    ),
                    captured_monotonic=now_monotonic,
                ),
                "",
            )

        ok, daily_total, reason = owner._get_daily_external_cash_flow()
        if not ok:
            return False, None, reason
        previous_daily = float(
            getattr(
                owner,
                "_cached_daily_external_cash_flow_total",
                getattr(owner, "_cached_external_cash_flow_total", 0.0),
            )
            or 0.0
        )
        carry = float(
            getattr(owner, "_deployment_cash_flow_carry", 0.0) or 0.0
        )
        if initialized and cache_day and cache_day != risk_day:
            carry += previous_daily

        deployment_start_ms = max(
            0,
            int(
                getattr(owner, "cash_flow_deployment_start_ms", 0) or 0
            ),
        )
        if deployment_start_ms > 0:
            ok, deployment_total, reason = self.get_external_cash_flow_total(
                deployment_start_ms,
                end_time_ms,
            )
            if not ok:
                return False, None, reason
        else:
            deployment_total = carry + float(daily_total)

        generation = int(
            getattr(owner, "_cash_flow_cache_generation", 0) or 0
        ) + 1
        owner._cached_external_cash_flow_total = float(daily_total)
        owner._cached_daily_external_cash_flow_total = float(daily_total)
        owner._cached_deployment_external_cash_flow_total = float(
            deployment_total
        )
        owner._deployment_cash_flow_carry = float(carry)
        owner._cash_flow_cache_day = risk_day
        owner._cash_flow_cache_generation = generation
        owner._cash_flow_cache_complete_through_ms = end_time_ms
        owner._last_cash_flow_poll_monotonic = now_monotonic
        owner._cash_flow_cache_initialized = True
        return (
            True,
            CashFlowTruth(
                risk_day=risk_day,
                daily_external_cash_flow_total=float(daily_total),
                deployment_external_cash_flow_total=float(deployment_total),
                ledger_generation=generation,
                complete_through_ms=end_time_ms,
                captured_monotonic=now_monotonic,
            ),
            "",
        )

    def get_cached_daily_external_cash_flow(self):
        ok, truth, reason = self.get_cached_external_cash_flow_truth()
        if not ok:
            return False, 0.0, reason
        return True, float(truth.daily_external_cash_flow_total), ""

    def get_open_orders_snapshot(self):
        owner = self._owner
        symbols = tuple(getattr(owner, "symbols", ()) or ())
        now = time.perf_counter()
        last_full_audit = float(
            getattr(
                owner,
                "_last_full_open_orders_audit_monotonic",
                0.0,
            )
            or 0.0
        )
        interval = max(
            5.0,
            float(
                getattr(
                    owner,
                    "full_open_orders_audit_interval_sec",
                    60.0,
                )
                or 60.0
            ),
        )
        if (
            not symbols
            or last_full_audit <= 0.0
            or now - last_full_audit >= interval
        ):
            ok, rows, reason = owner._response_payload(
                owner.rest.get_open_orders(),
                list,
                "open_orders",
            )
            if not ok:
                return False, [], reason
            owner._last_full_open_orders_audit_monotonic = now
            owner._remember_open_order_symbols(rows)
            return True, rows, ""

        rows = []
        known_symbols = set(
            getattr(owner, "_known_open_order_symbols", set()) or set()
        )
        for symbol in sorted(set(symbols) | known_symbols):
            ok, symbol_rows, reason = owner._response_payload(
                owner.rest.get_open_orders(symbol),
                list,
                f"open_orders:{symbol}",
            )
            if not ok:
                return False, [], reason
            rows.extend(symbol_rows)
        owner._remember_open_order_symbols(rows)
        return True, rows, ""

    def remember_open_order_symbols(self, rows):
        owner = self._owner
        known_symbols = getattr(
            owner,
            "_known_open_order_symbols",
            None,
        )
        if known_symbols is None:
            known_symbols = set()
            owner._known_open_order_symbols = known_symbols
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol", "") or "").strip().upper()
            if symbol:
                known_symbols.add(symbol)
