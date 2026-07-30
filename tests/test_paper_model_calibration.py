import hashlib
import json
import math
import sqlite3
import warnings
from pathlib import Path

import numpy as np
import pytest

from alpha.paper_calibration import MARKOUT_FEATURE_NAMES
from alpha.paper_calibration import MarkoutObservation
from alpha.paper_calibration import QuoteExposure
from alpha.paper_calibration import bootstrap_intensity_intervals
from alpha.paper_calibration import fit_conditional_markout
from alpha.paper_calibration import fit_exponential_intensity
from alpha.paper_calibration import load_markout_observations
from alpha.paper_calibration import load_quote_exposures
from alpha.paper_calibration import walk_forward_intensity
from oms.journal import OMSJournal
from oms.paper_trade_database import PaperTradeDatabase
from scripts.calibrate_paper_models import main as calibrate_main


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _exposure(
    index: int,
    *,
    depth_bps: float,
    exposure_seconds: float,
    filled: bool,
    start_time: float | None = None,
) -> QuoteExposure:
    return QuoteExposure(
        run_id="run-1",
        client_oid=f"quote-{index}",
        symbol="SNDKUSDT",
        side="BUY",
        start_time=float(index if start_time is None else start_time),
        exposure_seconds=exposure_seconds,
        depth_bps=depth_bps,
        filled=filled,
    )


def _simulated_exposures(
    *,
    seed: int = 7,
    A_per_s: float = 0.7,
    k_per_bps: float = 0.55,
    count_per_depth: int = 500,
) -> list[QuoteExposure]:
    generator = np.random.default_rng(seed)
    result = []
    for depth in (0.0, 1.0, 2.0, 3.0, 4.0):
        intensity = A_per_s * math.exp(-k_per_bps * depth)
        for _ in range(count_per_depth):
            wait = float(generator.exponential(1.0 / intensity))
            index = len(result)
            result.append(
                _exposure(
                    index,
                    depth_bps=depth,
                    exposure_seconds=min(wait, 3.0),
                    filled=wait < 3.0,
                    start_time=index * 10.0,
                )
            )
    return result


def test_censored_intensity_fit_recovers_synthetic_parameters():
    exposures = _simulated_exposures()

    fit = fit_exponential_intensity(exposures)

    assert fit.ready
    assert fit.A_per_s == pytest.approx(0.7, rel=0.08)
    assert fit.k_per_bps == pytest.approx(0.55, rel=0.08)


def test_unfilled_exposure_reduces_estimated_arrival_rate():
    fills = [
        _exposure(
            index,
            depth_bps=float(index % 2),
            exposure_seconds=0.1,
            filled=True,
        )
        for index in range(20)
    ]
    censored = [
        _exposure(
            index + 20,
            depth_bps=float(index % 2),
            exposure_seconds=1.0,
            filled=False,
        )
        for index in range(80)
    ]

    fills_only = fit_exponential_intensity(
        fills,
        min_exposures=1,
        min_fills=1,
        min_depth_span_bps=0.0,
    )
    with_censoring = fit_exponential_intensity(
        fills + censored,
        min_exposures=1,
        min_fills=1,
        min_depth_span_bps=0.0,
    )

    assert fills_only.ready and with_censoring.ready
    assert with_censoring.A_per_s < fills_only.A_per_s


def test_block_bootstrap_and_walk_forward_are_finite_and_train_only():
    exposures = _simulated_exposures(count_per_depth=60)
    intervals = bootstrap_intensity_intervals(
        exposures,
        block_seconds=300.0,
        replicates=30,
        seed=11,
    )

    assert intervals["replicates"] > 0
    assert np.all(np.isfinite(intervals["A_per_s_95pct"]))
    assert np.all(np.isfinite(intervals["k_per_bps_95pct"]))

    original = walk_forward_intensity(exposures, train_fraction=0.5)
    split = len(exposures) // 2
    changed_test = exposures[:split] + [
        _exposure(
            index + split,
            depth_bps=item.depth_bps,
            exposure_seconds=10.0,
            filled=False,
            start_time=item.start_time,
        )
        for index, item in enumerate(exposures[split:])
    ]
    modified = walk_forward_intensity(changed_test, train_fraction=0.5)

    assert original["ready"] and modified["ready"]
    assert original["A_per_s"] == modified["A_per_s"]
    assert original["k_per_bps"] == modified["k_per_bps"]
    assert original["test_log_likelihood"] != modified["test_log_likelihood"]


def test_conditional_markout_uses_chronological_oos_and_handles_empty_columns():
    observations = []
    for index, value in enumerate(np.linspace(-1.0, 1.0, 100)):
        features = (float(value),) + (None,) * (len(MARKOUT_FEATURE_NAMES) - 1)
        observations.append(
            MarkoutObservation(
                run_id="run-1",
                symbol="SNDKUSDT",
                side="BUY",
                horizon_ms=500,
                observed_time=float(index),
                signed_markout_bps=1.5 + 3.0 * float(value),
                features=features,
            )
        )

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        result = fit_conditional_markout(observations, min_samples=30)

    assert result["ready"]
    assert result["train_ended_at"] < observations[result["train_count"]].observed_time
    assert result["beats_constant_mean_baseline"] is True
    assert result["oos"]["rmse_bps"] < result["constant_mean_baseline_oos"]["rmse_bps"]
    assert result["promotion_eligible"] is False


def _paper_config(tmp_path: Path) -> dict:
    return {
        "execution": {"mode": "paper"},
        "paper_trade": {"enabled": True, "initial_balance_usdt": 10_000.0},
        "paper_trade_database": {
            "enabled": True,
            "path": str(tmp_path / "trades.sqlite3"),
            "sqlite_timeout_sec": 1.0,
            "queue_capacity": 100,
            "close_timeout_sec": 2.0,
            "strategy_sample_interval_sec": 0.1,
            "account_sample_interval_sec": 0.1,
            "market_sample_interval_sec": 0.1,
        },
        "symbols": ["SNDKUSDT"],
        "account": {"initial_balance_usdt": 10_000.0, "leverage": 5},
        "backtest": {"maker_fee": 0.0, "taker_fee": 0.0005},
        "risk": {"limits": {"max_pos_notional": 2_000.0}},
        "oms": {
            "journal_enabled": True,
            "journal_fsync": True,
            "journal_integrity_check": True,
            "replay_journal_on_startup": False,
            "journal_path": str(tmp_path / "oms.jsonl"),
        },
    }


def _write_calibration_database(tmp_path: Path) -> Path:
    config = _paper_config(tmp_path)
    journal = OMSJournal(config)
    database = PaperTradeDatabase(config, journal)
    strategy_params = {
        "strategy": "GLFT_MultiScale",
        "mid_price": 100.0,
        "market_spread_bps": 2.0,
        "sigma_bps": 1.2,
        "formula_version": "chronoshft.glft.v1",
        "units_version": "chronoshft.units.v1",
        "intensity_source": "CONFIGURED_PAPER_PROXY",
        "adaptive": {
            "flow_toxicity": {
                "signed_trade_imbalance": -0.2,
                "microprice_offset_bps": -0.1,
                "bid_adverse_cost_bps": 0.3,
            }
        },
        "market_data_timing": {"callback_age_ms": 3.0},
    }
    assert database.record_strategy_sample(
        {
            "sample_time": 999.9,
            "symbol": "SNDKUSDT",
            "fair_value": 100.0,
            "alpha_bps": 0.0,
            "params": strategy_params,
        }
    )
    assert database.record_strategy_sample(
        {
            "sample_time": 1000.05,
            "symbol": "SNDKUSDT",
            "fair_value": 200.0,
            "alpha_bps": 0.0,
            "params": {**strategy_params, "mid_price": 200.0},
        }
    )
    assert database.record_market_sample(
        {
            "sample_time": 999.9,
            "symbol": "SNDKUSDT",
            "mark_price": 100.02,
            "index_price": 100.0,
            "basis_bps": 1.9998,
            "funding_rate": 0.0001,
        }
    )
    assert database.record_market_sample(
        {
            "sample_time": 1000.51,
            "symbol": "SNDKUSDT",
            "mark_price": 101.0,
            "index_price": 100.0,
            "basis_bps": 99.5033,
            "funding_rate": 0.0009,
        }
    )

    base_order = {
        "symbol": "SNDKUSDT",
        "strategy_id": "GLFT_MultiScale",
        "side": "BUY",
        "quantity": 1.0,
        "average_price": 0.0,
        "time_in_force": "RPI",
        "is_post_only": True,
        "is_rpi": True,
        "order_type": "LIMIT",
        "reduce_only": False,
        "tag": "glft_quote",
        "created_monotonic": 10.0,
        "error_message": "",
    }
    for client_oid, price, start, stop, final_status, filled_quantity in (
        ("quote-cancel", 99.98, 10.0, 12.0, "CANCELLED", 0.0),
        ("quote-fill", 99.99, 20.0, 20.5, "FILLED", 1.0),
    ):
        assert database.record_order_event(
            {
                **base_order,
                "client_oid": client_oid,
                "exchange_oid": client_oid,
                "status": "NEW",
                "price": price,
                "filled_quantity": 0.0,
                "created_monotonic": start,
                "updated_monotonic": start,
                "event_time": 1000.0,
            }
        )
        assert database.record_order_event(
            {
                **base_order,
                "client_oid": client_oid,
                "exchange_oid": client_oid,
                "status": final_status,
                "price": price,
                "filled_quantity": filled_quantity,
                "created_monotonic": start,
                "updated_monotonic": stop,
                "event_time": 1000.5,
            }
        )

    fill = {
        "paper_run_id": database.run_id,
        "execution_id": "BINANCE:SNDKUSDT:7",
        "venue": "BINANCE",
        "strategy_id": "GLFT_MultiScale",
        "client_oid": "quote-fill",
        "exchange_oid": "quote-fill",
        "trade_id": 7,
        "symbol": "SNDKUSDT",
        "side": "BUY",
        "fill_qty": 1.0,
        "fill_price": 99.99,
        "cum_filled_qty": 1.0,
        "exchange_status": "FILLED",
        "exchange_time": 1000.5,
        "commission": 0.0,
        "commission_asset": "USDT",
        "booked_fee": 0.0,
        "realized_pnl": 0.0,
        "is_maker": True,
        "order_type": "LIMIT",
        "time_in_force": "RPI",
        "is_rpi": True,
        "fill_model": "rpi_public_trade_proxy",
        "fill_trigger": "through",
        "market_trade_transport_latency_ms": 8.0,
        "market_trade_local_age_ms": 2.0,
        "queue_ahead_before": 0.5,
        "mid_at_fill": 100.0,
        "quote_age_ms": 500.0,
    }
    sequence = journal.append("execution_record", fill)
    metadata = journal.commit_metadata(sequence)
    assert database.record_execution(
        sequence,
        fill,
        journal_ts=metadata["ts"],
        journal_hash=metadata["hash"],
    )
    assert database.record_markout(
        {
            "client_oid": "quote-fill",
            "trade_id": "7",
            "symbol": "SNDKUSDT",
            "side": "BUY",
            "fill_price": 99.99,
            "horizon_ms": 500,
            "mid_price": 99.97,
            "signed_markout_bps": -2.0,
            "fill_observed_monotonic": 20.5,
            "mid_observed_monotonic": 21.0,
            "observation_lag_ms": 0.0,
        }
    )
    assert database.close(clean_shutdown=True, reason="calibration_test")
    return Path(config["paper_trade_database"]["path"])


def test_sqlite_loaders_reconstruct_exposure_and_markout(tmp_path):
    path = _write_calibration_database(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        exposures = load_quote_exposures(connection)
        markouts = load_markout_observations(connection)

    assert len(exposures) == 2
    assert sum(item.filled for item in exposures) == 1
    assert {item.client_oid for item in exposures} == {"quote-cancel", "quote-fill"}
    assert max(item.depth_bps for item in exposures) < 3.0
    assert len(markouts) == 1
    assert markouts[0].features[11] == pytest.approx(1.9998)
    assert markouts[0].features[12] == pytest.approx(1.0)


def test_cli_writes_candidate_only_hashed_artifact_without_config_mutation(
    tmp_path,
):
    path = _write_calibration_database(tmp_path)
    output = tmp_path / "calibration.json"
    strategy_config = PROJECT_ROOT / "config" / "strategy" / "glft.json"
    config_before = strategy_config.read_bytes()

    result = calibrate_main(
        [
            "--database",
            str(path),
            "--output",
            str(output),
            "--min-exposures",
            "1",
            "--min-fills",
            "1",
            "--min-depth-span-bps",
            "0",
            "--bootstrap-replicates",
            "0",
        ]
    )

    assert result == 0
    artifact = json.loads(output.read_text(encoding="utf-8"))
    expected_hash = artifact.pop("artifact_sha256")
    canonical = json.dumps(
        artifact,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert expected_hash == hashlib.sha256(canonical).hexdigest()
    assert artifact["candidate_only"] is True
    assert artifact["activation_permitted"] is False
    assert artifact["data_source"] == "PAPER_PUBLIC_TRADE_PROXY"
    assert strategy_config.read_bytes() == config_before
