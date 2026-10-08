from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from scripts.evaluate_fair_value import main as evaluate_main

pytest.importorskip("tables")

TICK = 0.01


def _write_recording(directory, seed=3, rows=12000):
    """A book whose L1 imbalance predicts the next mid move."""
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
            trade_rows.append(
                {
                    "datetime": ts - timedelta(milliseconds=20),
                    "symbol": "TESTUSDT",
                    "price": round(price, 2),
                    "qty": 1.0,
                    "maker_is_buyer": direction < 0,
                }
            )
            imbalance = float(np.clip(rng.beta(2, 2), 0.05, 0.95))
        bid = mid_ticks * TICK
        row = {"datetime": ts, "symbol": "TESTUSDT"}
        for level in range(1, 6):
            row[f"bid{level}_p"] = round(bid - (level - 1) * TICK, 2)
            row[f"bid{level}_v"] = 10 * imbalance if level == 1 else 5.0
            row[f"ask{level}_p"] = round(bid + level * TICK, 2)
            row[f"ask{level}_v"] = 10 * (1 - imbalance) if level == 1 else 5.0
        depth_rows.append(row)
    pd.DataFrame(depth_rows).to_hdf(
        directory / "TESTUSDT_depth_20260727.h5", key="depth", format="table"
    )
    pd.DataFrame(trade_rows).to_hdf(
        directory / "TESTUSDT_trade_20260727.h5", key="trade", format="table"
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
