"""Build non-activating Paper intensity and markout calibration evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from alpha.paper_calibration import (  # noqa: E402
    bootstrap_intensity_intervals,
    fit_conditional_markout,
    fit_exponential_intensity,
    load_markout_observations,
    load_quote_exposures,
    walk_forward_intensity,
)


DEFAULT_DATABASE = PROJECT_ROOT / "storage" / "paper" / "trades.sqlite3"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Fit candidate-only Paper fill intensity and conditional markout models"
        )
    )
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    parser.add_argument("--output", default="", help="Optional JSON artifact path")
    parser.add_argument("--symbol", default="", help="Optional symbol filter")
    parser.add_argument("--run-id", default="", help="Optional run filter")
    parser.add_argument(
        "--include-non-rpi",
        action="store_true",
        help="Include non-RPI quote exposure (default: RPI only)",
    )
    parser.add_argument("--min-exposures", type=int, default=30)
    parser.add_argument("--min-fills", type=int, default=5)
    parser.add_argument("--min-depth-span-bps", type=float, default=0.25)
    parser.add_argument("--markout-min-samples", type=int, default=30)
    parser.add_argument("--train-fraction", type=float, default=0.7)
    parser.add_argument("--bootstrap-replicates", type=int, default=200)
    parser.add_argument("--bootstrap-block-sec", type=float, default=300.0)
    parser.add_argument("--seed", type=int, default=7)
    return parser.parse_args(argv)


def _connect_read_only(path: Path):
    if not path.is_file():
        raise FileNotFoundError(f"Paper trade database not found: {path}")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _run_metadata(connection, run_id: str) -> list[dict]:
    where = "WHERE run_id = ?" if run_id else ""
    parameters = (run_id,) if run_id else ()
    rows = connection.execute(
        f"SELECT * FROM paper_runs {where} ORDER BY started_at_utc",
        parameters,
    ).fetchall()
    allowed = (
        "run_id",
        "started_at_utc",
        "stopped_at_utc",
        "status",
        "clean_shutdown",
        "config_sha256",
        "symbols_json",
        "software_version",
        "code_revision",
    )
    return [
        {field: row[field] for field in allowed if field in row.keys()}
        for row in rows
    ]


def _market_summary(connection, *, symbol: str, run_id: str) -> list[dict]:
    if not _table_exists(connection, "paper_market_samples"):
        return []
    where = []
    parameters = []
    if symbol:
        where.append("symbol = ?")
        parameters.append(symbol.upper())
    if run_id:
        where.append("run_id = ?")
        parameters.append(run_id)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    rows = connection.execute(
        f"""
        SELECT symbol, basis_bps, funding_rate, transport_latency_ms
        FROM paper_market_samples
        {where_sql}
        ORDER BY sample_time
        """,
        parameters,
    ).fetchall()
    grouped = {}
    for row in rows:
        grouped.setdefault(str(row["symbol"]), []).append(row)
    result = []
    for current_symbol, values in sorted(grouped.items()):
        basis = np.asarray([row["basis_bps"] for row in values], dtype=float)
        funding = np.asarray([row["funding_rate"] for row in values], dtype=float)
        latency = np.asarray(
            [
                row["transport_latency_ms"]
                for row in values
                if row["transport_latency_ms"] is not None
            ],
            dtype=float,
        )
        result.append(
            {
                "symbol": current_symbol,
                "sample_count": len(values),
                "basis_bps_mean": float(np.mean(basis)),
                "basis_bps_05_50_95pct": np.quantile(
                    basis,
                    [0.05, 0.5, 0.95],
                ).tolist(),
                "funding_rate_mean": float(np.mean(funding)),
                "transport_latency_ms_50_95pct": (
                    np.quantile(latency, [0.5, 0.95]).tolist()
                    if latency.size
                    else None
                ),
            }
        )
    return result


def build_calibration_artifact(connection, args) -> dict:
    schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if schema_version < 3:
        raise ValueError(
            "Paper model calibration requires SQLite schema v3 or newer; "
            "start the current ChronosHFT runtime once to migrate this "
            f"schema v{schema_version} database to v4. Pre-v3 fills are "
            "preserved, but their missing quote-exposure and markout "
            "observations cannot be reconstructed retrospectively"
        )
    required_tables = {
        "paper_runs",
        "paper_order_events",
        "paper_strategy_samples",
        "paper_fills",
        "paper_fill_markouts",
    }
    missing = sorted(
        table for table in required_tables if not _table_exists(connection, table)
    )
    if missing:
        raise ValueError("Paper calibration tables missing: " + ",".join(missing))

    exposures = load_quote_exposures(
        connection,
        symbol=str(args.symbol or ""),
        run_id=str(args.run_id or ""),
        rpi_only=not bool(args.include_non_rpi),
    )
    exposure_groups = {}
    for exposure in exposures:
        exposure_groups.setdefault((exposure.symbol, exposure.side), []).append(
            exposure
        )
    intensity_fits = []
    for (symbol, side), group in sorted(exposure_groups.items()):
        fit = fit_exponential_intensity(
            group,
            min_exposures=max(1, int(args.min_exposures)),
            min_fills=max(1, int(args.min_fills)),
            min_depth_span_bps=max(0.0, float(args.min_depth_span_bps)),
        )
        intensity_fits.append(
            {
                "symbol": symbol,
                "side": side,
                **fit.as_dict(),
                "block_bootstrap": bootstrap_intensity_intervals(
                    group,
                    block_seconds=max(1.0, float(args.bootstrap_block_sec)),
                    replicates=max(0, int(args.bootstrap_replicates)),
                    seed=int(args.seed),
                ),
                "walk_forward": walk_forward_intensity(
                    group,
                    train_fraction=float(args.train_fraction),
                ),
            }
        )

    markouts = load_markout_observations(
        connection,
        symbol=str(args.symbol or ""),
        run_id=str(args.run_id or ""),
    )
    markout_groups = {}
    for observation in markouts:
        key = (observation.symbol, observation.side, observation.horizon_ms)
        markout_groups.setdefault(key, []).append(observation)
    markout_models = []
    for (symbol, side, horizon_ms), group in sorted(markout_groups.items()):
        markout_models.append(
            {
                "symbol": symbol,
                "side": side,
                "horizon_ms": horizon_ms,
                **fit_conditional_markout(
                    group,
                    min_samples=max(4, int(args.markout_min_samples)),
                    train_fraction=float(args.train_fraction),
                ),
            }
        )

    generated_at = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    artifact = {
        "schema": "chronoshft.paper_model_calibration.v1",
        "generated_at_utc": generated_at,
        "candidate_only": True,
        "activation_permitted": False,
        "data_source": "PAPER_PUBLIC_TRADE_PROXY",
        "warning": (
            "Paper public-trade proxy fits are not Live RPI execution evidence"
        ),
        "database_schema_version": schema_version,
        "filters": {
            "symbol": str(args.symbol or "").upper(),
            "run_id": str(args.run_id or ""),
            "rpi_only": not bool(args.include_non_rpi),
        },
        "runs": _run_metadata(connection, str(args.run_id or "")),
        "quote_exposure_count": len(exposures),
        "markout_observation_count": len(markouts),
        "intensity_fits": intensity_fits,
        "conditional_markout_models": markout_models,
        "market_summary": _market_summary(
            connection,
            symbol=str(args.symbol or ""),
            run_id=str(args.run_id or ""),
        ),
    }
    canonical = json.dumps(
        artifact,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    artifact["artifact_sha256"] = hashlib.sha256(canonical).hexdigest()
    return artifact


def main(argv=None) -> int:
    args = parse_args(argv)
    if not 0.5 <= float(args.train_fraction) < 1.0:
        print("Paper calibration failed: --train-fraction must be in [0.5, 1.0)")
        return 2
    try:
        with _connect_read_only(Path(args.database)) as connection:
            artifact = build_calibration_artifact(connection, args)
        rendered = json.dumps(
            artifact,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        if args.output:
            output = Path(args.output).resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_name(output.name + ".tmp")
            temporary.write_text(rendered + "\n", encoding="utf-8")
            temporary.replace(output)
            print(f"PAPER_CALIBRATION_OK output={output}")
        else:
            print(rendered)
    except (FileNotFoundError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"Paper calibration failed: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
