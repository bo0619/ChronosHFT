from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


DEFAULT_REQUEST_WEIGHT_LIMIT = 2400
DEFAULT_EMERGENCY_RESERVE = 300
DEFAULT_TRADING_RESERVE = 300
DEFAULT_STATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "storage"
    / "binance_rate_limit_budget.sqlite3"
)


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    reason: str = ""
    retry_after_sec: float = 0.0
    emergency_bypass: bool = False


class BinanceRateLimitBudget:
    """Cross-process Binance IP request-weight coordinator.

    Binance publishes one IP-scoped one-minute weight counter. ChronosHFT has
    multiple REST clients in the parent process plus two clients in the risk
    sidecar, so process-local sleeps cannot protect that shared exchange
    budget. SQLite supplies a small host-local transaction boundary without
    coupling emergency risk actions to the parent process.
    """

    def __init__(
        self,
        state_path: str | os.PathLike = DEFAULT_STATE_PATH,
        *,
        request_weight_limit: int = DEFAULT_REQUEST_WEIGHT_LIMIT,
        emergency_reserve: int = DEFAULT_EMERGENCY_RESERVE,
        trading_reserve: int = DEFAULT_TRADING_RESERVE,
        enabled: bool = True,
        sqlite_timeout_sec: float = 2.0,
    ):
        self.enabled = bool(enabled)
        self.request_weight_limit = int(request_weight_limit)
        self.emergency_reserve = int(emergency_reserve)
        self.trading_reserve = int(trading_reserve)
        self.sqlite_timeout_sec = max(0.05, float(sqlite_timeout_sec))
        if self.request_weight_limit <= 0:
            raise ValueError("request_weight_limit must be positive")
        if self.request_weight_limit > DEFAULT_REQUEST_WEIGHT_LIMIT:
            raise ValueError(
                "request_weight_limit cannot exceed Binance's published "
                f"{DEFAULT_REQUEST_WEIGHT_LIMIT} weight/minute limit"
            )
        if not 0 <= self.emergency_reserve < self.request_weight_limit:
            raise ValueError(
                "emergency_reserve must be non-negative and below "
                "request_weight_limit"
            )
        if (
            self.trading_reserve < 0
            or self.trading_reserve + self.emergency_reserve
            >= self.request_weight_limit
        ):
            raise ValueError(
                "trading_reserve must be non-negative and leave positive "
                "background request capacity"
            )
        if self.enabled and (
            self.trading_reserve <= 0 or self.emergency_reserve <= 0
        ):
            raise ValueError(
                "enabled coordination requires positive trading and "
                "emergency reserves"
            )

        path = Path(state_path or DEFAULT_STATE_PATH).expanduser()
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
        self.state_path = str(path.resolve())
        self._thread_lock = threading.RLock()
        if self.enabled:
            self._initialize()

    @classmethod
    def from_config(cls, config: dict | None = None):
        settings = dict(config or {})
        return cls(
            settings.get("state_path", DEFAULT_STATE_PATH),
            request_weight_limit=settings.get(
                "request_weight_limit",
                DEFAULT_REQUEST_WEIGHT_LIMIT,
            ),
            emergency_reserve=settings.get(
                "emergency_reserve",
                DEFAULT_EMERGENCY_RESERVE,
            ),
            trading_reserve=settings.get(
                "trading_reserve",
                DEFAULT_TRADING_RESERVE,
            ),
            enabled=settings.get("enabled", True),
            sqlite_timeout_sec=settings.get("sqlite_timeout_sec", 2.0),
        )

    @property
    def normal_limit(self) -> int:
        return self.background_limit

    @property
    def background_limit(self) -> int:
        return (
            self.request_weight_limit
            - self.emergency_reserve
            - self.trading_reserve
        )

    @property
    def trading_limit(self) -> int:
        return self.request_weight_limit - self.emergency_reserve

    def acquire(
        self,
        weight: int,
        *,
        priority: str = "normal",
        endpoint: str = "",
        now_epoch: float | None = None,
    ) -> RateLimitDecision:
        weight = max(0, int(weight or 0))
        priority = str(priority or "normal").strip().lower()
        emergency = priority in {"emergency", "safety", "reduce_only"}
        trading = priority in {"trading", "order"}
        if not self.enabled or weight == 0:
            return RateLimitDecision(True)
        if weight > self.request_weight_limit:
            return RateLimitDecision(
                False,
                "request_weight_exceeds_exchange_limit",
                60.0,
            )

        now_epoch = self._valid_now(now_epoch)
        bucket = int(now_epoch // 60)
        retry_after_sec = max(0.05, (bucket + 1) * 60.0 - now_epoch)
        try:
            with self._thread_lock:
                connection = self._connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "DELETE FROM attempts WHERE minute_bucket < ?",
                        (bucket - 1,),
                    )
                    state = connection.execute(
                        "SELECT remote_bucket, remote_used, blocked_until, "
                        "retry_reason FROM state WHERE singleton = 1"
                    ).fetchone()
                    remote_bucket, remote_used, blocked_until, retry_reason = (
                        state or (-1, 0, 0.0, "")
                    )
                    blocked_until = float(blocked_until or 0.0)
                    if blocked_until > now_epoch:
                        connection.commit()
                        return RateLimitDecision(
                            False,
                            str(retry_reason or "exchange_retry_after"),
                            blocked_until - now_epoch,
                        )

                    local_used = int(
                        connection.execute(
                            "SELECT COALESCE(SUM(weight), 0) FROM attempts "
                            "WHERE minute_bucket = ?",
                            (bucket,),
                        ).fetchone()[0]
                        or 0
                    )
                    observed_used = (
                        int(remote_used or 0)
                        if int(remote_bucket or -1) == bucket
                        else 0
                    )
                    effective_used = max(local_used, observed_used)
                    if emergency:
                        limit = self.request_weight_limit
                        recorded_priority = "emergency"
                    elif trading:
                        limit = self.trading_limit
                        recorded_priority = "trading"
                    else:
                        limit = self.background_limit
                        recorded_priority = "background"
                    if effective_used + weight > limit:
                        connection.commit()
                        if emergency:
                            reason = "exchange_request_weight_limit_exhausted"
                        elif trading:
                            reason = "emergency_request_weight_reserve_protected"
                        else:
                            reason = "trading_request_weight_reserve_protected"
                        return RateLimitDecision(
                            False,
                            reason,
                            retry_after_sec,
                        )

                    connection.execute(
                        "INSERT INTO attempts("
                        "minute_bucket, weight, priority, endpoint, created_at"
                        ") VALUES (?, ?, ?, ?, ?)",
                        (
                            bucket,
                            weight,
                            recorded_priority,
                            str(endpoint or "")[:160],
                            now_epoch,
                        ),
                    )
                    connection.commit()
                    return RateLimitDecision(True)
                except BaseException:
                    connection.rollback()
                    raise
                finally:
                    connection.close()
        except Exception as exc:
            if emergency:
                # A local accounting failure must never suppress a risk-
                # reducing command. The exchange remains the final rate-limit
                # authority and the caller records this degraded bypass.
                return RateLimitDecision(
                    True,
                    f"coordinator_unavailable:{type(exc).__name__}",
                    emergency_bypass=True,
                )
            return RateLimitDecision(
                False,
                f"coordinator_unavailable:{type(exc).__name__}",
                retry_after_sec,
            )

    def record_response(
        self,
        headers,
        *,
        status_code: int,
        now_epoch: float | None = None,
    ) -> None:
        if not self.enabled:
            return
        normalized = {
            str(key).strip().lower(): value
            for key, value in (headers or {}).items()
        }
        used_weight = self._non_negative_int(
            normalized.get("x-mbx-used-weight-1m")
        )
        now_epoch = self._valid_now(now_epoch)
        bucket = int(now_epoch // 60)
        status_code = int(status_code or 0)
        retry_after = self._parse_retry_after(
            normalized.get("retry-after"),
            now_epoch,
        )
        if status_code in {418, 429} and retry_after <= 0.0:
            retry_after = (
                120.0
                if status_code == 418
                else max(0.05, (bucket + 1) * 60.0 - now_epoch)
            )

        if used_weight is None and retry_after <= 0.0:
            return

        with self._thread_lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                state = connection.execute(
                    "SELECT remote_bucket, remote_used, blocked_until, "
                    "retry_reason FROM state WHERE singleton = 1"
                ).fetchone()
                remote_bucket, remote_used, blocked_until, retry_reason = (
                    state or (-1, 0, 0.0, "")
                )
                if used_weight is not None:
                    if int(remote_bucket or -1) == bucket:
                        remote_used = max(
                            int(remote_used or 0),
                            used_weight,
                        )
                    else:
                        remote_bucket = bucket
                        remote_used = used_weight
                if retry_after > 0.0:
                    blocked_until = max(
                        float(blocked_until or 0.0),
                        now_epoch + retry_after,
                    )
                    retry_reason = f"exchange_retry_after_status_{status_code}"
                connection.execute(
                    "UPDATE state SET remote_bucket = ?, remote_used = ?, "
                    "blocked_until = ?, retry_reason = ?, updated_at = ? "
                    "WHERE singleton = 1",
                    (
                        int(remote_bucket or -1),
                        int(remote_used or 0),
                        float(blocked_until or 0.0),
                        str(retry_reason or ""),
                        now_epoch,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def snapshot(self, now_epoch: float | None = None) -> dict:
        now_epoch = self._valid_now(now_epoch)
        bucket = int(now_epoch // 60)
        if not self.enabled:
            return {
                "enabled": False,
                "request_weight_limit": self.request_weight_limit,
                "emergency_reserve": self.emergency_reserve,
                "trading_reserve": self.trading_reserve,
            }
        with self._thread_lock:
            connection = self._connect()
            try:
                local_used = int(
                    connection.execute(
                        "SELECT COALESCE(SUM(weight), 0) FROM attempts "
                        "WHERE minute_bucket = ?",
                        (bucket,),
                    ).fetchone()[0]
                    or 0
                )
                state = connection.execute(
                    "SELECT remote_bucket, remote_used, blocked_until, "
                    "retry_reason FROM state WHERE singleton = 1"
                ).fetchone()
            finally:
                connection.close()
        remote_bucket, remote_used, blocked_until, retry_reason = (
            state or (-1, 0, 0.0, "")
        )
        observed_used = (
            int(remote_used or 0)
            if int(remote_bucket or -1) == bucket
            else 0
        )
        return {
            "enabled": True,
            "state_path": self.state_path,
            "minute_bucket": bucket,
            "local_used_weight": local_used,
            "remote_used_weight": observed_used,
            "effective_used_weight": max(local_used, observed_used),
            "request_weight_limit": self.request_weight_limit,
            "background_limit": self.background_limit,
            "trading_limit": self.trading_limit,
            "emergency_reserve": self.emergency_reserve,
            "trading_reserve": self.trading_reserve,
            "blocked_for_sec": max(
                0.0,
                float(blocked_until or 0.0) - now_epoch,
            ),
            "retry_reason": str(retry_reason or ""),
        }

    def _initialize(self) -> None:
        path = Path(self.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            connection = sqlite3.connect(
                self.state_path,
                timeout=self.sqlite_timeout_sec,
                isolation_level=None,
            )
            try:
                connection.execute(
                    f"PRAGMA busy_timeout = {int(self.sqlite_timeout_sec * 1000)}"
                )
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS attempts("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "minute_bucket INTEGER NOT NULL,"
                    "weight INTEGER NOT NULL,"
                    "priority TEXT NOT NULL,"
                    "endpoint TEXT NOT NULL,"
                    "created_at REAL NOT NULL"
                    ")"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS "
                    "idx_attempts_minute_bucket "
                    "ON attempts(minute_bucket)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS state("
                    "singleton INTEGER PRIMARY KEY CHECK(singleton = 1),"
                    "remote_bucket INTEGER NOT NULL,"
                    "remote_used INTEGER NOT NULL,"
                    "blocked_until REAL NOT NULL,"
                    "retry_reason TEXT NOT NULL,"
                    "updated_at REAL NOT NULL"
                    ")"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO state("
                    "singleton, remote_bucket, remote_used, blocked_until, "
                    "retry_reason, updated_at"
                    ") VALUES (1, -1, 0, 0, '', 0)"
                )
            finally:
                connection.close()

    def _connect(self):
        connection = sqlite3.connect(
            self.state_path,
            timeout=self.sqlite_timeout_sec,
            isolation_level=None,
        )
        connection.execute(
            f"PRAGMA busy_timeout = {int(self.sqlite_timeout_sec * 1000)}"
        )
        return connection

    @staticmethod
    def _valid_now(value) -> float:
        result = time.time() if value is None else float(value)
        if not math.isfinite(result) or result <= 0.0:
            raise ValueError("now_epoch must be a finite positive timestamp")
        return result

    @staticmethod
    def _non_negative_int(value):
        try:
            result = int(float(value))
        except (TypeError, ValueError):
            return None
        return max(0, result)

    @staticmethod
    def _parse_retry_after(value, now_epoch: float) -> float:
        if value in (None, ""):
            return 0.0
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            try:
                parsed = parsedate_to_datetime(str(value))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                seconds = parsed.timestamp() - now_epoch
            except (TypeError, ValueError, OverflowError):
                return 0.0
        if not math.isfinite(seconds):
            return 0.0
        return max(0.0, seconds)
