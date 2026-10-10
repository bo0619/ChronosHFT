from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from scripts.evaluate_fair_value import (
    discover_files,
    evaluate_symbol,
    parse_args,
)
from scripts.evaluate_fair_value import main as evaluate_main

pytest.importorskip("tables")

TICK = 0.01


FEED_LATENCY_SEC = 0.05
LOCAL_CLOCK_SKEW_SEC = 1.3


def _write_recording(directory, seed=3, rows=12000, v2=False):
    """A book whose L1 imbalance predicts the next mid move.

    v2 rows carry exchange/arrival timestamps; their depth ``datetime`` runs
    on a skewed local clock, as the recorder's receive time does.
    """
    rng = np.random.default_rng(seed)
    start = datetime(2026, 7, 27, tzinfo=UTC)
    mid_ticks = 100_000
    depth_rows, trade_rows = [], []
    imbalance = 0.5
    for index in range(rows):
        ts = start + timedelta(milliseconds=100 * index)
        if rng.random() < 0.3:
            direction = 1 if rng.random() < imbalance else -1
            mid_ticks += direction
            # The aggressor traded at the touch the move consumed.
            price = (mid_ticks if direction > 0 else mid_ticks + 1) * TICK
            trade_ts = ts - timedelta(milliseconds=20)
            trade = {
                "datetime": trade_ts,
                "symbol": "TESTUSDT",
                "price": round(price, 2),
                "qty": 1.0,
                "maker_is_buyer": direction < 0,
            }
            if v2:
                trade["exchange_ts"] = trade_ts.timestamp()
                trade["received_ts"] = trade_ts.timestamp() + FEED_LATENCY_SEC
                trade["corrected_received_ts"] = trade["received_ts"]
            trade_rows.append(trade)
            imbalance = float(np.clip(rng.beta(2, 2), 0.05, 0.95))
        bid = mid_ticks * TICK
        row = {"datetime": ts, "symbol": "TESTUSDT"}
        if v2:
            arrival = ts.timestamp() + FEED_LATENCY_SEC
            row["datetime"] = ts + timedelta(seconds=LOCAL_CLOCK_SKEW_SEC)
            row["exchange_ts"] = ts.timestamp()
            row["received_ts"] = arrival + LOCAL_CLOCK_SKEW_SEC
            row["corrected_received_ts"] = arrival
        for level in range(1, 6):
            row[f"bid{level}_p"] = round(bid - (level - 1) * TICK, 2)
            row[f"bid{level}_v"] = 10 * imbalance if level == 1 else 5.0
            row[f"ask{level}_p"] = round(bid + level * TICK, 2)
            row[f"ask{level}_v"] = 10 * (1 - imbalance) if level == 1 else 5.0
        depth_rows.append(row)
    suffix = "_v2" if v2 else ""
    pd.DataFrame(depth_rows).to_hdf(
        directory / f"TESTUSDT_depth_20260727{suffix}.h5", key="depth", format="table"
    )
    pd.DataFrame(trade_rows).to_hdf(
        directory / f"TESTUSDT_trade_20260727{suffix}.h5", key="trade", format="table"
    )


def test_microprice_beats_mid_when_imbalance_predicts_moves(tmp_path):
    _write_recording(tmp_path)
    report = tmp_path / "report.md"
    metrics = tmp_path / "metrics.csv"

    code = evaluate_main(
        [
            "--data-dir",
            str(tmp_path),
            "--horizons",
            "1,5",
            "--quote-horizon",
            "1",
            "--min-test-points",
            "500",
            "--output",
            str(report),
            "--csv",
            str(metrics),
        ]
    )

    assert code == 0
    frame = pd.read_csv(metrics)
    one_second = frame.loc[frame["horizon_s"] == 1.0].set_index("estimator")
    assert one_second.loc["mid", "skill"] == 0.0
    assert one_second.loc["microprice_l1", "skill"] > 0.0
    assert one_second.loc["microprice_l1_fitted", "skill"] > 0.0
    markout = pd.read_csv(tmp_path / "metrics_markout.csv")
    assert set(markout["half_spread_bps"]) == {0.0, 2.5}
    touch = markout.loc[markout["half_spread_bps"] == 0.0]
    assert (touch["fills"] > 0).all()
    assert "TESTUSDT" in report.read_text(encoding="utf-8")


def test_missing_data_dir_reports_failure(tmp_path):
    assert evaluate_main(["--data-dir", str(tmp_path)]) == 1


def _evaluate(directory, *extra):
    args = parse_args(
        [
            "--data-dir",
            str(directory),
            "--horizons",
            "1,5",
            "--quote-horizon",
            "1",
            "--min-test-points",
            "500",
            *extra,
        ]
    )
    files = discover_files(directory, set(), set())
    result, reason = evaluate_symbol("TESTUSDT", files["TESTUSDT"], args, [1.0, 5.0])
    assert reason is None
    return result


def test_v2_files_use_arrival_clock_and_report_feed_latency(tmp_path):
    _write_recording(tmp_path, v2=True)
    files = discover_files(tmp_path, set(), set())
    assert [p.name for p in files["TESTUSDT"]["depth"]] == [
        "TESTUSDT_depth_20260727_v2.h5"
    ]

    info = _evaluate(tmp_path)["info"]

    assert info["clock"] == "arrival"
    assert info["depth_lag_ms"] == 0.0
    assert info["depth_latency_ms_p50"] == pytest.approx(50.0, abs=1.0)
    assert info["trade_latency_ms_p50"] == pytest.approx(50.0, abs=1.0)
    # The recorded clock would need a lag estimate to undo the local skew.
    recorded = _evaluate(tmp_path, "--clock", "recorded")["info"]
    assert recorded["clock"] == "recorded"
    assert recorded["depth_lag_ms"] > 1000.0


def test_arrival_clock_is_refused_for_legacy_files(tmp_path):
    _write_recording(tmp_path)
    args = parse_args(["--data-dir", str(tmp_path), "--clock", "arrival"])
    files = discover_files(tmp_path, set(), set())
    result, reason = evaluate_symbol("TESTUSDT", files["TESTUSDT"], args, [1.0])
    assert result is None
    assert "v2 timestamps" in reason


def test_event_time_labels_score_next_mid_change(tmp_path):
    _write_recording(tmp_path, v2=True)

    result = _evaluate(tmp_path, "--event-horizons", "1,5")
    events = pd.DataFrame(result["events"]).set_index(["label", "estimator"])

    nxt = events.loc["next_change"]
    assert nxt.loc["mid", "coverage"] == 0.0
    assert nxt.loc["mid", "edge_bps"] == 0.0
    # Imbalance sets the odds of the next move, so microprice calls it.
    assert nxt.loc["microprice_l1", "hit_rate"] > 0.6
    assert nxt.loc["microprice_l1", "edge_bps"] > 0.0
    after_one = events.loc["after_1_updates"]
    assert after_one.loc["microprice_l1_fitted", "skill"] > 0.0
    # Mid always calls "flat", so its three-way accuracy is the flat share.
    mid_row = after_one.loc["mid"]
    assert mid_row["three_class_acc"] == pytest.approx(mid_row["flat_share"])
    assert (
        after_one.loc["microprice_l1_fitted", "three_class_acc"]
        > mid_row["three_class_acc"]
    )
    assert result["info"]["next_change_wait_ms_p50"] > 0.0
