import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from data.oos_reconstruction import (
    Execution,
    FundingCashFlow,
    MarkObservation,
    OMSJournalIdentity,
    OOSReconstructionError,
    OOSReconstructionRequirements,
    RawEvidence,
    reconstruct_oos_evidence,
)
from strategy.model_readiness import _validate_raw_oos_evidence


SYMBOL = "XAUUSDT"
DEPLOYMENT_ID = "unit-oos-001"
CONFIG_SHA256 = "a" * 64


def _execution(
    execution_id: str,
    *,
    side: str,
    price: str,
    timestamp: float,
    realized_pnl: str,
) -> Execution:
    return Execution(
        execution_id=execution_id,
        symbol=SYMBOL,
        side=side,
        quantity=Decimal("0.01"),
        price=Decimal(price),
        exchange_time=timestamp,
        commission=Decimal("0"),
        booked_fee=Decimal("0"),
        commission_asset="USDT",
        realized_pnl=Decimal(realized_pnl),
        is_maker=True,
        order_type="LIMIT",
        time_in_force="RPI",
        is_rpi=True,
        reduce_only=False,
    )


def _raw_evidence():
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    training_ended = started - timedelta(seconds=1)
    executions = []
    marks = [
        MarkObservation(
            symbol=SYMBOL,
            exchange_time=started.timestamp(),
            mark_price=Decimal("100"),
            funding_rate=Decimal("0"),
            next_funding_time=started.timestamp() + 28_800,
        )
    ]
    for day in range(5):
        buy_time = (started + timedelta(days=day, hours=1)).timestamp()
        sell_time = buy_time + 60
        executions.extend(
            [
                _execution(
                    f"buy-{day}",
                    side="BUY",
                    price="100",
                    timestamp=buy_time,
                    realized_pnl="0",
                ),
                _execution(
                    f"sell-{day}",
                    side="SELL",
                    price="101",
                    timestamp=sell_time,
                    realized_pnl="0.01",
                ),
            ]
        )
        marks.extend(
            [
                MarkObservation(
                    SYMBOL,
                    buy_time,
                    Decimal("100"),
                    Decimal("0"),
                    buy_time + 28_800,
                ),
                MarkObservation(
                    SYMBOL,
                    buy_time + 1,
                    Decimal("100.5"),
                    Decimal("0"),
                    buy_time + 28_800,
                ),
                MarkObservation(
                    SYMBOL,
                    buy_time + 5,
                    Decimal("100.6"),
                    Decimal("0"),
                    buy_time + 28_800,
                ),
                MarkObservation(
                    SYMBOL,
                    sell_time,
                    Decimal("101"),
                    Decimal("0"),
                    sell_time + 28_800,
                ),
                MarkObservation(
                    SYMBOL,
                    sell_time + 1,
                    Decimal("100.5"),
                    Decimal("0"),
                    sell_time + 28_800,
                ),
                MarkObservation(
                    SYMBOL,
                    sell_time + 5,
                    Decimal("100.4"),
                    Decimal("0"),
                    sell_time + 28_800,
                ),
            ]
        )
    ended = started + timedelta(days=4, hours=1, seconds=66)
    identity = OMSJournalIdentity(
        path="oms.jsonl",
        sha256="b" * 64,
        record_count=100,
        first_seq=1,
        last_seq=100,
        final_hash="c" * 64,
        last_kind="oms_stopped",
    )
    raw = RawEvidence(
        oms_identity=identity,
        executions=tuple(executions),
        external_cash_flow_times=(),
        market_journal_sha256="d" * 64,
        market_journal_record_count=len(marks) + 2,
        market_journal_final_hash="e" * 64,
        market_journal_mark_count=len(marks),
        market_journal_account_count=0,
        marks=tuple(marks),
        funding_cash_flows=(),
    )
    return raw, training_ended, started, ended


def _reconstruct(raw=None):
    base, training_ended, started, ended = _raw_evidence()
    return reconstruct_oos_evidence(
        raw or base,
        deployment_id=DEPLOYMENT_ID,
        deployment_config_sha256=CONFIG_SHA256,
        symbols=(SYMBOL,),
        training_ended_at=training_ended,
        started_at=started,
        ended_at=ended,
        requirements=OOSReconstructionRequirements(
            min_utc_day_clusters=5,
        ),
    )


def test_reconstructs_flat_zero_fee_rpi_ledger():
    result = _reconstruct()

    assert result["fill_count"] == 10
    assert result["maker_fill_fraction"] == 1.0
    assert result["rpi_fill_fraction"] == 1.0
    assert result["rpi_commission_rate"] == "0"
    assert result["net_pnl_usdt"] == pytest.approx(0.05)
    assert result["exchange_net_pnl_usdt"] == pytest.approx(0.05)
    assert result["markout"]["1000"]["cluster_count"] == 5
    assert result["markout"]["1000"]["net_edge_bps_lcb95"] > 0
    assert result["markout"]["5000"]["net_edge_bps_lcb95"] > 0


def test_funding_is_included_in_both_pnl_paths():
    raw, _, started, _ = _raw_evidence()
    funding = FundingCashFlow(
        asset="USDT",
        event_time=(started + timedelta(days=2, hours=2)).timestamp(),
        amount=Decimal("-0.001"),
    )
    adjusted = replace(raw, funding_cash_flows=(funding,))

    result = _reconstruct(adjusted)

    assert result["funding_pnl_usdt"] == pytest.approx(-0.001)
    assert result["net_pnl_usdt"] == pytest.approx(0.049)


def test_non_maker_or_non_rpi_execution_is_blocked():
    raw, _, _, _ = _raw_evidence()
    non_maker = replace(raw.executions[0], is_maker=False)
    altered = replace(
        raw,
        executions=(non_maker, *raw.executions[1:]),
    )
    with pytest.raises(OOSReconstructionError, match="non-maker"):
        _reconstruct(altered)

    non_rpi = replace(raw.executions[0], is_rpi=False)
    altered = replace(
        raw,
        executions=(non_rpi, *raw.executions[1:]),
    )
    with pytest.raises(OOSReconstructionError, match="non-RPI"):
        _reconstruct(altered)


def test_nonzero_fee_or_open_boundary_is_blocked():
    raw, _, _, _ = _raw_evidence()
    fee = replace(
        raw.executions[0],
        commission=Decimal("0.001"),
        booked_fee=Decimal("0.001"),
    )
    altered = replace(raw, executions=(fee, *raw.executions[1:]))
    with pytest.raises(OOSReconstructionError, match="zero booked commission"):
        _reconstruct(altered)

    open_ended = replace(raw, executions=raw.executions[:-1])
    with pytest.raises(OOSReconstructionError, match="end boundary is not flat"):
        _reconstruct(open_ended)


def test_external_cash_flow_in_oos_is_blocked():
    raw, _, started, _ = _raw_evidence()
    altered = replace(
        raw,
        external_cash_flow_times=(
            (started + timedelta(days=1)).timestamp(),
        ),
    )

    with pytest.raises(OOSReconstructionError, match="external account cash"):
        _reconstruct(altered)


def test_live_approval_validates_raw_journal_identity_and_tolerances():
    result = _reconstruct()
    raw_evidence = copy.deepcopy(result["raw_evidence"])
    market = raw_evidence["market_evidence_journal"]
    market["account_update_count"] = 1
    market["record_count"] += 1

    validated = _validate_raw_oos_evidence(
        raw_evidence,
        deployment_id=DEPLOYMENT_ID,
        deployment_config_sha256=CONFIG_SHA256,
        oos_sample_count=result["sample_count"],
        oos_fill_count=result["fill_count"],
    )

    assert validated["pnl_tolerance"] == Decimal("0.000001")
    assert validated["max_markout_lag_ms"] == 2000
    assert validated["min_utc_day_clusters"] == 5

    raw_evidence["reconstruction"][
        "pnl_crosscheck_tolerance_usdt"
    ] = "0.000002"
    with pytest.raises(ValueError, match="cross-check tolerance exceeds"):
        _validate_raw_oos_evidence(
            raw_evidence,
            deployment_id=DEPLOYMENT_ID,
            deployment_config_sha256=CONFIG_SHA256,
            oos_sample_count=result["sample_count"],
            oos_fill_count=result["fill_count"],
        )
