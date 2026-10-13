"""Cumulative Paper P&L across runs, read from the Paper trade database.

The Paper venue starts every run from ``initial_balance_usdt`` and a clean
shutdown cancels and flattens, so each run's result is its booked fills:
``realized_pnl - commission``. Summing runs gives the cumulative result the
per-run account cannot show.

* A run that ended without a clean shutdown may have been holding a position;
  its last account sample's ``unrealized_pnl`` is reported as
  ``open_unrealized`` and included in net so a crash cannot hide a loss.
* The run this process is writing (``current_run_id``) has its open position
  priced from the live account by the caller, not from samples. Without a
  current run id the newest run is assumed to be it.

Funding is not modelled by the Paper venue, so it is not part of net.
"""

from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path
from typing import Any

_RUN_TOTALS_SQL = """
SELECT
    r.run_id,
    r.started_at_utc,
    r.stopped_at_utc,
    r.status,
    r.clean_shutdown,
    r.symbols_json,
    r.initial_balance_usdt,
    COUNT(f.execution_id) AS fills,
    COALESCE(SUM(CASE WHEN f.is_maker = 1 THEN 1 ELSE 0 END), 0) AS maker_fills,
    COALESCE(SUM(f.fill_notional), 0.0) AS notional,
    COALESCE(SUM(f.realized_pnl), 0.0) AS realized_pnl,
    COALESCE(SUM(f.commission), 0.0) AS commission
FROM paper_runs AS r
LEFT JOIN paper_fills AS f ON f.run_id = r.run_id
GROUP BY r.run_id
ORDER BY r.started_at_utc
"""

# One equity sample per run per time bucket (the latest in the bucket; SQLite
# returns the bare column from the MAX row).
_CURVE_SQL = """
SELECT run_id, MAX(sample_time) AS sample_time, equity
FROM paper_account_samples
WHERE sample_time IS NOT NULL
GROUP BY run_id, CAST(sample_time / ? AS INTEGER)
ORDER BY sample_time
"""

# Upper bound on P&L curve points across all runs.
CURVE_MAX_POINTS = 1500

# Signed markout: side * ln(mid_after / fill_price) in bps, so positive means
# the price moved our way after the fill (spread capture included).
_MARKOUT_SQL = """
SELECT
    horizon_ms,
    side,
    COUNT(*) AS n,
    SUM(signed_markout_bps) AS total,
    SUM(signed_markout_bps * signed_markout_bps) AS total_sq,
    SUM(CASE WHEN signed_markout_bps > 0 THEN 1 ELSE 0 END) AS favourable
FROM paper_fill_markouts
GROUP BY horizon_ms, side
"""

_SYMBOL_SQL = """
SELECT
    symbol,
    COUNT(*) AS fills,
    SUM(CASE WHEN is_maker = 1 THEN 1 ELSE 0 END) AS maker_fills,
    SUM(fill_notional) AS notional,
    SUM(realized_pnl) AS realized_pnl,
    SUM(commission) AS commission
FROM paper_fills
GROUP BY symbol
ORDER BY symbol
"""

# Strategy targets against the touch, sampled once a second. A bid at or
# above the best bid (an ask at or below the best ask) is at the touch.
_QUOTE_POSITION_SQL = """
SELECT
    run_id,
    symbol,
    COUNT(*) AS samples,
    SUM(CASE WHEN state = 'QUOTING' THEN 1 ELSE 0 END) AS quoting,
    SUM(CASE WHEN target_bid > 0 AND best_bid > 0 THEN 1 ELSE 0 END) AS bid_samples,
    SUM(CASE WHEN target_bid > 0 AND best_bid > 0
             AND target_bid >= best_bid - 1e-9 THEN 1 ELSE 0 END) AS bid_at_touch,
    AVG(CASE WHEN target_bid > 0 AND best_bid > 0 AND mid_price > 0
             THEN (best_bid - target_bid) / mid_price * 10000.0 END) AS bid_behind_bps,
    SUM(CASE WHEN target_ask > 0 AND best_ask > 0 THEN 1 ELSE 0 END) AS ask_samples,
    SUM(CASE WHEN target_ask > 0 AND best_ask > 0
             AND target_ask <= best_ask + 1e-9 THEN 1 ELSE 0 END) AS ask_at_touch,
    AVG(CASE WHEN target_ask > 0 AND best_ask > 0 AND mid_price > 0
             THEN (target_ask - best_ask) / mid_price * 10000.0 END) AS ask_behind_bps
FROM paper_strategy_samples
GROUP BY run_id, symbol
"""

_LAST_SAMPLE_SQL = """
SELECT unrealized_pnl
FROM paper_account_samples
WHERE run_id = ?
ORDER BY sample_time DESC
LIMIT 1
"""


def _number(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


class PaperLedgerSummary:
    """Read-only, rate-limited cumulative P&L view of the Paper database."""

    def __init__(self, path: str | Path, *, refresh_interval_sec: float = 5.0):
        self.path = Path(path)
        self.refresh_interval_sec = max(0.0, float(refresh_interval_sec))
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0

    def refresh(self) -> None:
        """Re-read the database if the cached view is older than the interval.

        Call this outside any hot lock; :meth:`summary` does no I/O.
        """
        now = time.monotonic()
        if (
            self._cached is None
            or now - self._cached_at >= self.refresh_interval_sec
        ):
            self._cached = self._read()
            self._cached_at = now

    def summary(
        self,
        *,
        current_run_id: str | None = None,
        live_unrealized_pnl: float | None = None,
    ) -> dict[str, Any]:
        if self._cached is None:
            return {"available": False, "reason": "not_loaded"}
        return self._summarize(
            self._cached,
            current_run_id=current_run_id,
            live_unrealized_pnl=live_unrealized_pnl,
        )

    def snapshot(
        self,
        *,
        current_run_id: str | None = None,
        live_unrealized_pnl: float | None = None,
    ) -> dict[str, Any]:
        self.refresh()
        return self.summary(
            current_run_id=current_run_id,
            live_unrealized_pnl=live_unrealized_pnl,
        )

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"available": False, "reason": "database_missing"}
        try:
            connection = sqlite3.connect(
                f"{self.path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=1.0,
            )
        except sqlite3.Error as exc:
            return {"available": False, "reason": f"open_failed:{exc}"}
        try:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(_RUN_TOTALS_SQL).fetchall()
            runs = []
            for row in rows:
                clean = row["clean_shutdown"] == 1
                last_unrealized = 0.0
                if not clean:
                    sample = connection.execute(
                        _LAST_SAMPLE_SQL, (row["run_id"],)
                    ).fetchone()
                    last_unrealized = _number(sample[0]) if sample else 0.0
                runs.append(
                    {
                        "run_id": str(row["run_id"]),
                        "started_at": row["started_at_utc"],
                        "stopped_at": row["stopped_at_utc"],
                        "status": row["status"],
                        "clean_shutdown": clean,
                        "symbols": row["symbols_json"],
                        "initial_balance": _number(row["initial_balance_usdt"]),
                        "fills": int(row["fills"] or 0),
                        "maker_fills": int(row["maker_fills"] or 0),
                        "notional": _number(row["notional"]),
                        "realized_pnl": _number(row["realized_pnl"]),
                        "commission": _number(row["commission"]),
                        "last_sample_unrealized": last_unrealized,
                    }
                )
            self._attach_curves(connection, runs)
            # Optional sections: an older database may lack a table.
            markouts = _optional(connection, _markout_summary)
            symbols = _optional(connection, _symbol_summary)
            quote_position = _optional(connection, _quote_position_rows)
        except sqlite3.Error as exc:
            return {"available": False, "reason": f"query_failed:{exc}"}
        finally:
            connection.close()
        return {
            "available": True,
            "source": str(self.path),
            "runs": runs,
            "markouts": markouts,
            "symbols": symbols,
            "quote_position": quote_position,
        }

    @staticmethod
    def _attach_curves(connection, runs: list[dict[str, Any]]) -> None:
        """Give each run its downsampled equity P&L (equity - initial)."""
        first, last = connection.execute(
            "SELECT MIN(sample_time), MAX(sample_time) FROM paper_account_samples"
        ).fetchone()
        by_run = {run["run_id"]: run for run in runs}
        for run in runs:
            run["curve"] = []
        if first is None or last is None:
            return
        bucket_sec = max(1.0, (float(last) - float(first)) / CURVE_MAX_POINTS)
        for run_id, sample_time, equity in connection.execute(
            _CURVE_SQL, (bucket_sec,)
        ):
            run = by_run.get(run_id)
            if run is not None:
                run["curve"].append(
                    (_number(sample_time), _number(equity) - run["initial_balance"])
                )

    @staticmethod
    def _summarize(
        cached: dict[str, Any],
        *,
        current_run_id: str | None,
        live_unrealized_pnl: float | None,
    ) -> dict[str, Any]:
        if not cached.get("available"):
            return dict(cached)
        stored = cached["runs"]
        if not current_run_id and stored:
            current_run_id = stored[-1]["run_id"]
        live = _number(live_unrealized_pnl)
        runs = []
        curve = []
        run_starts = []
        cumulative = 0.0
        for stored_run in stored:
            run = dict(stored_run)
            points = run.pop("curve", [])
            if points:
                run_starts.append(points[0][0])
                curve.extend(
                    {"t": sample_time, "net": cumulative + pnl}
                    for sample_time, pnl in points
                )
            current = run["run_id"] == current_run_id
            if current:
                open_unrealized = live
            elif not run["clean_shutdown"]:
                open_unrealized = run["last_sample_unrealized"]
            else:
                open_unrealized = 0.0
            run.pop("last_sample_unrealized")
            net = run["realized_pnl"] - run["commission"] + open_unrealized
            cumulative += net
            run.update(
                {
                    "run_id": run["run_id"][:8],
                    "current": current,
                    "open_unrealized": open_unrealized,
                    "net_pnl": net,
                    "cumulative_net_pnl": cumulative,
                }
            )
            runs.append(run)
        if any(run["current"] for run in runs):
            # The live account is newer than the last stored sample.
            curve.append({"t": time.time(), "net": cumulative})
        notional = sum(run["notional"] for run in runs)
        fills = sum(run["fills"] for run in runs)
        current_full_id = next(
            (stored_run["run_id"] for stored_run, run in zip(stored, runs) if run["current"]),
            None,
        )
        quote_position = cached.get("quote_position")
        return {
            "available": True,
            "source": cached["source"],
            "funding_included": False,
            "markouts": cached.get("markouts"),
            "symbols": cached.get("symbols"),
            "quote_position": (
                [row for row in quote_position if row["run_id"] == current_full_id]
                if isinstance(quote_position, list)
                else quote_position
            ),
            "runs": runs,
            "curve": curve,
            "run_starts": run_starts,
            "totals": {
                "runs": len(runs),
                "first_started_at": runs[0]["started_at"] if runs else None,
                "fills": fills,
                "maker_fills": sum(run["maker_fills"] for run in runs),
                "notional": notional,
                # Size-independent edge: these do not change when order size
                # is scaled, unlike net P&L in USDT.
                "net_bps": cumulative / notional * 10_000.0 if notional else None,
                "net_per_fill": cumulative / fills if fills else None,
                "realized_pnl": sum(run["realized_pnl"] for run in runs),
                "commission": sum(run["commission"] for run in runs),
                "open_unrealized": sum(run["open_unrealized"] for run in runs),
                "net_pnl": cumulative,
            },
        }


def _optional(connection, reader):
    try:
        return reader(connection)
    except sqlite3.Error as exc:
        return {"available": False, "reason": f"query_failed:{exc}"}


def _moments(n: int, total: float, total_sq: float, favourable: int) -> dict[str, Any]:
    mean = total / n if n else None
    if n > 1:
        variance = max(0.0, (total_sq - n * mean * mean) / (n - 1))
        standard_error = math.sqrt(variance / n)
    else:
        standard_error = None
    return {
        "samples": n,
        "mean_bps": mean,
        "standard_error_bps": standard_error,
        # mean / standard error; |t| > 2 is roughly 95% confidence that the
        # mean is not zero.
        "t_stat": (
            mean / standard_error
            if mean is not None and standard_error
            else None
        ),
        "favourable_share": favourable / n if n else None,
    }


def _markout_summary(connection) -> list[dict[str, Any]]:
    by_horizon: dict[int, dict[str, list[float]]] = {}
    for horizon_ms, side, n, total, total_sq, favourable in connection.execute(
        _MARKOUT_SQL
    ):
        sides = by_horizon.setdefault(int(horizon_ms), {})
        sides[str(side).upper()] = [
            int(n or 0),
            _number(total),
            _number(total_sq),
            int(favourable or 0),
        ]
    rows = []
    for horizon_ms in sorted(by_horizon):
        sides = by_horizon[horizon_ms]
        combined = [sum(values[index] for values in sides.values()) for index in range(4)]
        row = {"horizon_ms": horizon_ms, **_moments(*combined)}
        for side in ("BUY", "SELL"):
            if side in sides:
                row[side.lower()] = _moments(*sides[side])
        rows.append(row)
    return rows


def _symbol_summary(connection) -> list[dict[str, Any]]:
    rows = []
    for symbol, fills, maker_fills, notional, realized, commission in connection.execute(
        _SYMBOL_SQL
    ):
        notional = _number(notional)
        net = _number(realized) - _number(commission)
        rows.append(
            {
                "symbol": symbol,
                "fills": int(fills or 0),
                "maker_fills": int(maker_fills or 0),
                "notional": notional,
                "realized_pnl": _number(realized),
                "commission": _number(commission),
                "net_pnl": net,
                "net_bps": net / notional * 10_000.0 if notional else None,
            }
        )
    return rows


def _quote_position_rows(connection) -> list[dict[str, Any]]:
    rows = []
    for row in connection.execute(_QUOTE_POSITION_SQL):
        (run_id, symbol, samples, quoting, bid_samples, bid_at_touch,
         bid_behind, ask_samples, ask_at_touch, ask_behind) = row
        rows.append(
            {
                "run_id": run_id,
                "symbol": symbol,
                "samples": int(samples or 0),
                "quoting_share": (quoting or 0) / samples if samples else None,
                "bid_at_touch_share": (
                    (bid_at_touch or 0) / bid_samples if bid_samples else None
                ),
                "ask_at_touch_share": (
                    (ask_at_touch or 0) / ask_samples if ask_samples else None
                ),
                "bid_behind_bps": bid_behind,
                "ask_behind_bps": ask_behind,
            }
        )
    return rows


def paper_ledger_from_config(config: dict, *, is_paper: bool):
    """Return a summary reader for an enabled Paper database, else ``None``."""
    database = (config or {}).get("paper_trade_database")
    if not is_paper or not isinstance(database, dict):
        return None
    if database.get("enabled") is not True or not database.get("path"):
        return None
    return PaperLedgerSummary(database["path"])


__all__ = ["PaperLedgerSummary", "paper_ledger_from_config"]
