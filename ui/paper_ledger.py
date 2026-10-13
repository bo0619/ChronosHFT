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
        except sqlite3.Error as exc:
            return {"available": False, "reason": f"query_failed:{exc}"}
        finally:
            connection.close()
        return {"available": True, "source": str(self.path), "runs": runs}

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
        cumulative = 0.0
        for stored_run in stored:
            run = dict(stored_run)
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
        return {
            "available": True,
            "source": cached["source"],
            "funding_included": False,
            "runs": runs,
            "totals": {
                "runs": len(runs),
                "first_started_at": runs[0]["started_at"] if runs else None,
                "fills": sum(run["fills"] for run in runs),
                "maker_fills": sum(run["maker_fills"] for run in runs),
                "notional": sum(run["notional"] for run in runs),
                "realized_pnl": sum(run["realized_pnl"] for run in runs),
                "commission": sum(run["commission"] for run in runs),
                "open_unrealized": sum(run["open_unrealized"] for run in runs),
                "net_pnl": cumulative,
            },
        }


def paper_ledger_from_config(config: dict, *, is_paper: bool):
    """Return a summary reader for an enabled Paper database, else ``None``."""
    database = (config or {}).get("paper_trade_database")
    if not is_paper or not isinstance(database, dict):
        return None
    if database.get("enabled") is not True or not database.get("path"):
        return None
    return PaperLedgerSummary(database["path"])


__all__ = ["PaperLedgerSummary", "paper_ledger_from_config"]
