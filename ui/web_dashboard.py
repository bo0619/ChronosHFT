"""Thread-safe, read-only local web dashboard backend.

The event callbacks in :class:`LocalWebDashboard` only copy primitive values
into a small in-memory cache.  ``publish_snapshot`` creates one coherent JSON
document on the caller's thread and atomically publishes it.  HTTP request
threads only ever read those already-serialized bytes; they never touch the
OMS, gateway, strategy, or risk components.
"""

from __future__ import annotations

from collections import Counter, OrderedDict, defaultdict, deque
from copy import deepcopy
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
from pathlib import Path
import re
import socket
import threading
import time
from typing import Any, Mapping
from urllib.parse import urlsplit


_DEFAULT_STATIC_PATH = Path(__file__).resolve().parents[1] / "web" / "dashboard.html"
_ACTIVE_ORDER_STATUSES = frozenset(
    {
        "CREATED",
        "SUBMITTING",
        "SUBMIT_UNKNOWN",
        "PENDING_ACK",
        "NEW",
        "PARTIALLY_FILLED",
        "CANCELLING",
        "CANCEL_UNKNOWN",
    }
)
_TERMINAL_ORDER_STATUSES = frozenset(
    {"FILLED", "CANCELLED", "REJECTED", "REJECTED_LOCALLY", "EXPIRED"}
)
_SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
    "api_secret",
    "secret",
    "signature",
    "listen_key",
    "listenkey",
    "authorization",
    "password",
    "session_id",
    "rearm_token",
    "access_token",
    "refresh_token",
    "cookie",
)
_PATH_KEY_PARTS = ("state_path", "journal_path", "fence_path", "lock_path")
_IDENTIFIER_KEYS = frozenset(
    {
        "client_oid",
        "exchange_oid",
        "order_id",
        "orig_client_order_id",
        "clientorderid",
        "orderid",
    }
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)(signature|listenkey|api[_-]?key|api[_-]?secret)=([^&\s]+)"),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)([^\s]+)"),
)


def _utc_iso(timestamp: float | None = None) -> str:
    value = time.time() if timestamp is None else float(timestamp)
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _get_value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _finite_float(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _finite_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _timestamp(value: Any) -> float | None:
    if isinstance(value, datetime):
        return value.timestamp()
    return _finite_float(value)


def _mask_identifier(value: Any) -> str:
    rendered = str(value or "")
    if not rendered:
        return ""
    if len(rendered) <= 8:
        return "***"
    return f"{rendered[:4]}...{rendered[-4:]}"


def _redact_text(value: Any, limit: int = 1024) -> str:
    rendered = str(value or "")
    for pattern in _SECRET_PATTERNS:
        rendered = pattern.sub(lambda match: f"{match.group(1)}=<redacted>", rendered)
    if len(rendered) > limit:
        rendered = rendered[: max(0, limit - 3)] + "..."
    return rendered


def _safe_value(value: Any, *, key_hint: str = "", depth: int = 0) -> Any:
    """Return a bounded, JSON-safe copy with operational secrets removed."""
    normalized_key = str(key_hint or "").lower().replace("-", "_")
    if any(part in normalized_key for part in _SECRET_KEY_PARTS):
        return "<redacted>"
    if (
        normalized_key == "path"
        or normalized_key.endswith("_path")
        or any(part in normalized_key for part in _PATH_KEY_PARTS)
    ):
        return "<redacted:path>"
    if normalized_key in _IDENTIFIER_KEYS or normalized_key.endswith("_oid"):
        return _mask_identifier(value)
    if depth >= 10:
        return "<max-depth>"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        return _safe_value(value.value, key_hint=key_hint, depth=depth + 1)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return "<redacted:path>"
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, bytes):
        return "<redacted:bytes>"
    if is_dataclass(value):
        return {
            field.name: _safe_value(
                getattr(value, field.name),
                key_hint=field.name,
                depth=depth + 1,
            )
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        result = {}
        for raw_key, item in list(value.items())[:5000]:
            key = str(raw_key)
            result[key] = _safe_value(item, key_hint=key, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset, deque)):
        return [
            _safe_value(item, key_hint=key_hint, depth=depth + 1)
            for item in list(value)[:5000]
        ]
    return _redact_text(value)


def _is_loopback_host(host: str) -> bool:
    candidate = str(host or "").strip()
    if not candidate:
        return False
    if candidate.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        pass
    try:
        addresses = socket.getaddrinfo(candidate, None)
    except OSError:
        return False
    return bool(addresses) and all(
        ipaddress.ip_address(address[4][0]).is_loopback for address in addresses
    )


def _section_unavailable(**extra: Any) -> dict[str, Any]:
    return {"available": False, **extra}


class _DashboardHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class _DashboardHTTPServerV6(_DashboardHTTPServer):
    address_family = socket.AF_INET6


class LocalWebDashboard:
    """Loopback-only dashboard server backed by atomically published snapshots."""

    schema_version = 1

    def __init__(
        self,
        oms: Any = None,
        gateway: Any = None,
        risk_manager: Any = None,
        risk_supervisor: Any = None,
        config: Mapping[str, Any] | None = None,
        *,
        time_service: Any = None,
        truth_monitor: Any = None,
        venue_supervisor: Any = None,
        event_engine: Any = None,
        strategy_runtime: Any = None,
        host: str | None = None,
        port: int | None = None,
        static_path: str | Path | None = None,
        max_orders: int | None = None,
        max_trades: int | None = None,
        max_logs: int | None = None,
        max_alerts: int | None = None,
        max_depth_levels: int | None = None,
        history_points: int | None = None,
        history_interval_sec: float | None = None,
        publish_interval_sec: float | None = None,
    ):
        full_config = dict(config or {})
        web_config = self._extract_web_config(full_config)
        resolved_host = str(host or web_config.get("host", "127.0.0.1"))
        resolved_port = int(port if port is not None else web_config.get("port", 8765))
        if not _is_loopback_host(resolved_host):
            raise ValueError("LocalWebDashboard must bind to a loopback address")
        if not 0 <= resolved_port <= 65535:
            raise ValueError("LocalWebDashboard port must be between 0 and 65535")

        self.host = resolved_host
        self.port = resolved_port
        configured_static = static_path or web_config.get("static_path")
        self.static_path = Path(configured_static).resolve() if configured_static else _DEFAULT_STATIC_PATH
        self.max_orders = max(
            10,
            int(
                max_orders
                if max_orders is not None
                else web_config.get("orders_limit", 500)
            ),
        )
        self.max_trades = max(
            10,
            int(
                max_trades
                if max_trades is not None
                else web_config.get("trades_limit", 300)
            ),
        )
        self.max_logs = max(
            10,
            int(
                max_logs
                if max_logs is not None
                else web_config.get("logs_limit", 250)
            ),
        )
        self.max_alerts = max(
            10,
            int(
                max_alerts
                if max_alerts is not None
                else web_config.get("alerts_limit", 150)
            ),
        )
        self.max_depth_levels = max(
            1,
            int(
                max_depth_levels
                if max_depth_levels is not None
                else web_config.get("depth_levels", 20)
            ),
        )
        self.history_points = max(
            10,
            int(
                history_points
                if history_points is not None
                else web_config.get("history_limit", 300)
            ),
        )
        self.history_interval_sec = max(
            0.1,
            float(
                history_interval_sec
                if history_interval_sec is not None
                else web_config.get("history_interval_sec", 1.0)
            ),
        )
        self.publish_interval_sec = max(
            0.0,
            float(
                publish_interval_sec
                if publish_interval_sec is not None
                else web_config.get("publish_interval_sec", 0.20)
            ),
        )

        # Component references are only read by publish_snapshot on its caller's
        # thread.  Request handlers never dereference this dictionary.
        self._components = {
            "oms": oms,
            "gateway": gateway,
            "risk_manager": risk_manager,
            "risk_supervisor": risk_supervisor,
            "time_service": time_service,
            "truth_monitor": truth_monitor,
            "venue_supervisor": venue_supervisor,
            "event_engine": event_engine,
            "strategy_runtime": strategy_runtime,
        }
        self._config_view = self._safe_config_view(full_config)

        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._running = False
        self._service_state = "initialized"
        self._started_wall = 0.0
        self._started_monotonic = 0.0
        self._actual_port = resolved_port
        self._static_bytes = self._fallback_html()

        # ``available`` is the exchange available-balance field expected by
        # the frontend.  Section availability therefore uses
        # ``data_available`` for this section only.
        self._account = {"data_available": False, "available": None}
        self._markets: dict[str, dict[str, Any]] = {}
        self._positions: dict[str, dict[str, Any]] = {}
        self._strategies: dict[str, dict[str, Any]] = {}
        self._orders: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._trades: deque[dict[str, Any]] = deque(maxlen=self.max_trades)
        self._logs: deque[dict[str, Any]] = deque(maxlen=self.max_logs)
        self._alerts: deque[dict[str, Any]] = deque(maxlen=self.max_alerts)
        self._risk_sources: dict[str, dict[str, Any]] = {}
        self._runtime_metrics: dict[str, Any] = {}
        self._system_health: Any = None
        self._rpi_external: dict[str, Any] = {}
        self._rpi_capabilities: dict[str, bool | None] = {}

        self._order_status_seen: dict[str, set[str]] = defaultdict(set)
        self._order_filled_cumulative: dict[str, float] = {}
        self._execution_ids: set[str] = set()
        self._execution_id_order: deque[str] = deque(maxlen=max(5000, self.max_trades * 20))
        self._trade_ids: set[str] = set()
        self._trade_id_order: deque[str] = deque(maxlen=max(5000, self.max_trades * 20))
        self._performance = self._empty_performance()
        self._rpi_performance = self._empty_performance()
        self._commission_observed = False
        self._rpi_commission_observed = False

        self._account_history: deque[dict[str, Any]] = deque(maxlen=self.history_points)
        self._symbol_histories: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=self.history_points)
        )
        self._last_history_sample = 0.0
        self._last_publish_monotonic = 0.0
        self._sequence = 0
        self._published_sequence = 0
        self._published_at = 0.0
        self._published_snapshot: dict[str, Any] = {}
        self._published_json = b"{}"
        self._published_health_json = b'{"status":"starting"}'
        self.publish_snapshot(force=True)

    @staticmethod
    def _extract_web_config(config: Mapping[str, Any]) -> dict[str, Any]:
        system = config.get("system", {}) if isinstance(config, Mapping) else {}
        if not isinstance(system, Mapping):
            system = {}
        for key in ("web_dashboard", "local_web_dashboard", "dashboard"):
            value = system.get(key)
            if isinstance(value, Mapping):
                return dict(value)
        value = config.get("web_dashboard") if isinstance(config, Mapping) else None
        return dict(value) if isinstance(value, Mapping) else {}

    @staticmethod
    def _safe_config_view(config: Mapping[str, Any]) -> dict[str, Any]:
        strategy = config.get("strategy", {}) if isinstance(config, Mapping) else {}
        risk = config.get("risk", {}) if isinstance(config, Mapping) else {}
        limits = risk.get("limits", {}) if isinstance(risk, Mapping) else {}
        execution = config.get("execution", {}) if isinstance(config, Mapping) else {}
        paper_trade = config.get("paper_trade", {}) if isinstance(config, Mapping) else {}
        raw_mode = (
            str(execution.get("mode", "") or "").strip().lower()
            if isinstance(execution, Mapping)
            else ""
        )
        paper_enabled = (
            bool(paper_trade.get("enabled", False))
            if isinstance(paper_trade, Mapping)
            else bool(paper_trade)
        )
        if raw_mode in {"paper", "paper_trade", "simulation", "sim"} or paper_enabled:
            execution_mode = "paper"
        elif raw_mode in {"testnet", "sandbox"} or bool(config.get("testnet", False)):
            execution_mode = "testnet"
        else:
            execution_mode = "live"

        is_paper = execution_mode == "paper"
        is_testnet = execution_mode == "testnet"
        if is_paper:
            environment = "PAPER_LIVE_DATA"
            environment_label = "PAPER · LIVE DATA"
            market_data_environment = "BINANCE_MAINNET_PUBLIC"
            market_data_source = "production_public"
            execution_venue = "LOCAL_SIMULATOR"
        elif is_testnet:
            environment = "TESTNET"
            environment_label = "TESTNET"
            market_data_environment = "BINANCE_TESTNET"
            market_data_source = "testnet"
            execution_venue = "BINANCE_TESTNET"
        else:
            environment = "LIVE_REAL_MONEY"
            environment_label = "LIVE · REAL MONEY"
            market_data_environment = "BINANCE_MAINNET"
            market_data_source = "production"
            execution_venue = "BINANCE_MAINNET"

        if isinstance(strategy, Mapping):
            registered_models = strategy.get("registered_models", [])
            if not isinstance(registered_models, (list, tuple)):
                registered_models = []
            primary_model = str(
                strategy.get("primary_model", strategy.get("name", "ML_Sniper"))
                or "ML_Sniper"
            )
            execution_policy = str(
                strategy.get("execution_policy", "single_primary")
                or "single_primary"
            )
        else:
            registered_models = []
            primary_model = "ML_Sniper"
            execution_policy = "single_primary"

        return {
            "testnet": bool(config.get("testnet", False)),
            "execution_mode": execution_mode,
            "environment": environment,
            "environment_label": environment_label,
            "market_data_environment": market_data_environment,
            "market_data_source": market_data_source,
            "execution_venue": execution_venue,
            "simulated_execution": is_paper,
            "simulated_funds": is_paper,
            "private_api_enabled": not is_paper,
            "symbols": [str(symbol).upper() for symbol in config.get("symbols", [])],
            "exchange": "BINANCE",
            "strategy": str(strategy.get("name", "ML_Sniper") or "ML_Sniper")
            if isinstance(strategy, Mapping)
            else "ML_Sniper",
            "primary_strategy_model": primary_model,
            "registered_strategy_models": [
                str(model) for model in registered_models
            ],
            "strategy_execution_policy": execution_policy,
            "risk_limits": _safe_value(limits) if isinstance(limits, Mapping) else {},
            "rpi": {
                "enabled": bool(strategy.get("use_rpi", False))
                if isinstance(strategy, Mapping)
                else False,
                "avellaneda_stoikov": bool(
                    strategy.get("use_rpi_for_avellaneda_stoikov", False)
                )
                if isinstance(strategy, Mapping)
                else False,
                "glft": bool(strategy.get("use_rpi_for_glft", False))
                if isinstance(strategy, Mapping)
                else False,
                "passive_exit": bool(strategy.get("use_rpi_for_passive_exit", False))
                if isinstance(strategy, Mapping)
                else False,
                "fallback_to_gtx": bool(strategy.get("rpi_fallback_to_gtx", True))
                if isinstance(strategy, Mapping)
                else True,
            },
        }

    @staticmethod
    def _empty_performance() -> dict[str, Any]:
        return {
            "orders_observed": 0,
            "order_updates": 0,
            "filled_orders": 0,
            "partially_filled_orders": 0,
            "cancelled_orders": 0,
            "expired_orders": 0,
            "rejected_orders": 0,
            "fill_events": 0,
            "filled_quantity": 0.0,
            "filled_notional": 0.0,
            "commission": 0.0,
            "realized_pnl": 0.0,
            "maker_fill_events": 0,
            "taker_fill_events": 0,
            "unknown_liquidity_fill_events": 0,
        }

    @property
    def url(self) -> str:
        host = self.host
        rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        return f"http://{rendered_host}:{self._actual_port}/"

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def start(self) -> str:
        """Start the loopback HTTP server and return its URL."""
        with self._lifecycle_lock:
            if self._server is not None:
                return self.url
            self._static_bytes = self._load_static_page()
            server_class = _DashboardHTTPServerV6 if ":" in self.host else _DashboardHTTPServer
            server = server_class((self.host, self.port), self._handler_factory())
            self._server = server
            self._actual_port = int(server.server_address[1])
            with self._lock:
                self._running = True
                self._service_state = "running"
                self._started_wall = time.time()
                self._started_monotonic = time.monotonic()
            self.publish_snapshot(force=True)
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.20},
                name="ChronosLocalWebDashboard",
                daemon=True,
            )
            self._server_thread = thread
            thread.start()
            return self.url

    def stop(self) -> None:
        """Stop the server.  This operation is idempotent."""
        with self._lifecycle_lock:
            server = self._server
            thread = self._server_thread
            if server is None:
                return
            with self._lock:
                self._running = False
                self._service_state = "stopping"
            self.publish_snapshot(force=True)
            server.shutdown()
            server.server_close()
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=2.0)
            self._server = None
            self._server_thread = None
            with self._lock:
                self._service_state = "stopped"
            self.publish_snapshot(force=True)

    def __enter__(self) -> LocalWebDashboard:
        self.start()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Event callbacks.  Each method copies only bounded primitive data.
    # ------------------------------------------------------------------

    def update_account(self, data: Any) -> None:
        now = time.time()
        event_time = _timestamp(_get_value(data, "datetime")) or now
        available_balance = _finite_float(_get_value(data, "available"))
        account = {
            "data_available": True,
            "balance": _finite_float(_get_value(data, "balance")),
            "equity": _finite_float(_get_value(data, "equity")),
            "available": available_balance,
            "available_balance": available_balance,
            "used_margin": _finite_float(_get_value(data, "used_margin")),
            "budget_balance": _finite_float(_get_value(data, "budget_balance")),
            "budget_available": _finite_float(_get_value(data, "budget_available")),
            "balances": _safe_value(_get_value(data, "balances", {})),
            "available_balances": _safe_value(
                _get_value(data, "available_balances", {})
            ),
            "trading_budget_by_asset": _safe_value(
                _get_value(data, "trading_budget_by_asset", {})
            ),
            "maintenance_margin": _finite_float(
                _get_value(data, "maintenance_margin")
            ),
            "margin_balance": _finite_float(_get_value(data, "margin_balance")),
            "maintenance_margin_ratio": _finite_float(
                _get_value(data, "maintenance_margin_ratio")
            ),
            "margin_snapshot_time": _finite_float(
                _get_value(data, "margin_snapshot_time")
            ),
            "margin_snapshot_synced": bool(
                _get_value(data, "margin_snapshot_synced", False)
            ),
            "external_cash_flow_total": _finite_float(
                _get_value(data, "external_cash_flow_total")
            ),
            "cash_flow_snapshot_time": _finite_float(
                _get_value(data, "cash_flow_snapshot_time")
            ),
            "cash_flow_snapshot_synced": bool(
                _get_value(data, "cash_flow_snapshot_synced", False)
            ),
            "event_time": event_time,
            "event_time_iso": _utc_iso(event_time),
        }
        with self._lock:
            self._account = account

    def update_market(self, orderbook: Any) -> None:
        symbol = str(_get_value(orderbook, "symbol", "") or "").upper()
        if not symbol:
            return
        get_best_bid = getattr(orderbook, "get_best_bid", None)
        get_best_ask = getattr(orderbook, "get_best_ask", None)
        bid_pair = get_best_bid() if callable(get_best_bid) else (
            _get_value(orderbook, "best_bid_price", 0.0),
            _get_value(orderbook, "best_bid_volume", 0.0),
        )
        ask_pair = get_best_ask() if callable(get_best_ask) else (
            _get_value(orderbook, "best_ask_price", 0.0),
            _get_value(orderbook, "best_ask_volume", 0.0),
        )
        bid = _finite_float(bid_pair[0], 0.0) or 0.0
        bid_volume = _finite_float(bid_pair[1], 0.0) or 0.0
        ask = _finite_float(ask_pair[0], 0.0) or 0.0
        ask_volume = _finite_float(ask_pair[1], 0.0) or 0.0
        mid = (bid + ask) / 2.0 if bid > 0.0 and ask > 0.0 else None
        spread = ask - bid if mid is not None else None
        spread_bps = spread / mid * 10_000.0 if mid and spread is not None else None
        top_quantity = bid_volume + ask_volume
        imbalance = (
            (bid_volume - ask_volume) / top_quantity if top_quantity > 0.0 else None
        )
        microprice = (
            (ask * bid_volume + bid * ask_volume) / top_quantity
            if top_quantity > 0.0 and bid > 0.0 and ask > 0.0
            else None
        )
        get_top_bids = getattr(orderbook, "get_top_bids", None)
        get_top_asks = getattr(orderbook, "get_top_asks", None)
        top_bids = (
            get_top_bids(self.max_depth_levels)
            if callable(get_top_bids)
            else _get_value(orderbook, "top_bids", ())
        )
        top_asks = (
            get_top_asks(self.max_depth_levels)
            if callable(get_top_asks)
            else _get_value(orderbook, "top_asks", ())
        )
        received_time = _finite_float(_get_value(orderbook, "received_timestamp"))
        exchange_time = _finite_float(_get_value(orderbook, "exchange_timestamp"))
        now = time.time()
        market_update = {
            "available": True,
            "bid": bid,
            "bid_volume": bid_volume,
            "ask": ask,
            "ask_volume": ask_volume,
            "mid": mid,
            "best_bid": bid,
            "best_bid_qty": bid_volume,
            "best_ask": ask,
            "best_ask_qty": ask_volume,
            "mid_price": mid,
            "spread": spread,
            "spread_bps": spread_bps,
            "imbalance": imbalance,
            "microprice": microprice,
            "top_bids": self._safe_depth(top_bids),
            "top_asks": self._safe_depth(top_asks),
            "depth_levels": _finite_int(_get_value(orderbook, "depth_levels"), 0),
            "exchange": str(_get_value(orderbook, "exchange", "") or ""),
            "exchange_timestamp": exchange_time,
            "received_timestamp": received_time,
            "age_ms": max(0.0, (now - received_time) * 1000.0)
            if received_time
            else None,
            "book_age_ms": max(0.0, (now - received_time) * 1000.0)
            if received_time
            else None,
            "updated_at": now,
        }
        with self._lock:
            existing = self._markets.get(symbol, {})
            self._markets[symbol] = {**existing, **market_update}

    update_orderbook = update_market

    def update_mark_price(self, data: Any) -> None:
        symbol = str(_get_value(data, "symbol", "") or "").upper()
        if not symbol:
            return
        event_time = _timestamp(_get_value(data, "datetime")) or time.time()
        with self._lock:
            existing = self._markets.get(symbol, _section_unavailable())
            mid = _finite_float(existing.get("mid_price", existing.get("mid")))
        mark_price = _finite_float(_get_value(data, "mark_price"))
        update = {
            "mark_available": True,
            "mark_price": mark_price,
            "index_price": _finite_float(_get_value(data, "index_price")),
            "funding_rate": _finite_float(_get_value(data, "funding_rate")),
            "next_funding_time": _safe_value(_get_value(data, "next_funding_time")),
            "mark_event_time": event_time,
            "mark_age_ms": max(0.0, (time.time() - event_time) * 1000.0),
            "basis_bps": (mark_price - mid) / mid * 10_000.0
            if mark_price is not None and mid
            else None,
        }
        with self._lock:
            self._markets[symbol] = {**existing, **update}

    def update_market_trade(self, data: Any) -> None:
        symbol = str(_get_value(data, "symbol", "") or "").upper()
        if not symbol:
            return
        event_time = _timestamp(_get_value(data, "datetime")) or time.time()
        update = {
            "last_trade_available": True,
            "last_trade_id": _mask_identifier(_get_value(data, "trade_id", "")),
            "last_trade_price": _finite_float(_get_value(data, "price")),
            "last_trade_quantity": _finite_float(_get_value(data, "quantity")),
            "last_trade_maker_is_buyer": _get_value(data, "maker_is_buyer"),
            "last_trade_time": event_time,
            "last_trade_age_ms": max(0.0, (time.time() - event_time) * 1000.0),
            "trade_age_ms": max(0.0, (time.time() - event_time) * 1000.0),
        }
        with self._lock:
            existing = self._markets.get(symbol, _section_unavailable())
            self._markets[symbol] = {**existing, **update}

    def update_position(self, data: Any) -> None:
        symbol = str(_get_value(data, "symbol", "") or "").upper()
        if not symbol:
            return
        now = time.time()
        position = {
            "available": True,
            "volume": _finite_float(_get_value(data, "volume"), 0.0),
            "entry_price": _finite_float(
                _get_value(data, "price", _get_value(data, "entry_price"))
            ),
            "avg_price": _finite_float(
                _get_value(data, "price", _get_value(data, "entry_price"))
            ),
            "reported_unrealized_pnl": _finite_float(
                _get_value(data, "pnl", _get_value(data, "unrealized_pnl"))
            ),
            "updated_at": now,
        }
        with self._lock:
            existing = self._positions.get(symbol, {})
            self._positions[symbol] = {**existing, **position}

    def update_strategy(self, data: Any) -> None:
        symbol = str(_get_value(data, "symbol", "") or "").upper()
        if not symbol:
            return
        params = _safe_value(_get_value(data, "params", {}))
        event_time = _finite_float(_get_value(data, "timestamp"), time.time())
        strategy = {
            "available": True,
            "fair_value": _finite_float(_get_value(data, "fair_value")),
            "alpha_bps": _finite_float(_get_value(data, "alpha_bps")),
            "params": params if isinstance(params, dict) else {},
            "state": params.get("State") if isinstance(params, dict) else None,
            "mode": params.get("Mode") if isinstance(params, dict) else None,
            "confidence": params.get("Conf") if isinstance(params, dict) else None,
            "health": params.get("Health") if isinstance(params, dict) else None,
            "timestamp": event_time,
            "update_time": event_time,
            "updated_at": event_time,
        }
        with self._lock:
            self._strategies[symbol] = strategy

    def update_order(self, data: Any) -> None:
        raw_order_id = str(
            _get_value(data, "client_oid", _get_value(data, "order_id", "")) or ""
        )
        if not raw_order_id:
            return
        symbol = str(_get_value(data, "symbol", "") or "").upper()
        status = str(_enum_value(_get_value(data, "status", "")) or "").upper()
        tif = str(_get_value(data, "time_in_force", "") or "").upper()
        is_rpi = bool(_get_value(data, "is_rpi", False) or tif == "RPI")
        volume = _finite_float(_get_value(data, "volume"), 0.0) or 0.0
        filled = _finite_float(_get_value(data, "filled_volume"), 0.0) or 0.0
        price = _finite_float(_get_value(data, "price"), 0.0) or 0.0
        avg_price = _finite_float(_get_value(data, "avg_price"), 0.0) or 0.0
        update_time = _finite_float(_get_value(data, "update_time"), time.time())
        record = {
            "client_oid": _mask_identifier(raw_order_id),
            "exchange_oid": _mask_identifier(_get_value(data, "exchange_oid", "")),
            "symbol": symbol,
            "side": str(_enum_value(_get_value(data, "side", "")) or ""),
            "strategy_id": _redact_text(_get_value(data, "strategy_id", ""), 128),
            "status": status,
            "price": price,
            "volume": volume,
            "filled_volume": filled,
            "remaining_volume": max(0.0, volume - filled),
            "avg_price": avg_price,
            "time_in_force": tif,
            "post_only": bool(
                _get_value(data, "is_post_only", _get_value(data, "post_only", False))
            ),
            "reduce_only": bool(_get_value(data, "reduce_only", False)),
            "is_rpi": is_rpi,
            "error": _redact_text(_get_value(data, "error_msg", ""), 512),
            "error_msg": _redact_text(_get_value(data, "error_msg", ""), 512),
            "error_message": _redact_text(_get_value(data, "error_msg", ""), 512),
            "update_time": update_time,
            "updated_at": update_time,
        }
        with self._lock:
            previous = self._orders.get(raw_order_id)
            if previous:
                record = {**previous, **record}
                is_rpi = bool(record.get("is_rpi"))
            self._record_order_transition_locked(
                raw_order_id,
                status=status,
                is_rpi=is_rpi,
                is_new=previous is None,
            )
            self._record_fill_locked(
                raw_order_id,
                symbol=symbol,
                cumulative=filled,
                fill_price=avg_price or price,
                is_rpi=is_rpi,
            )
            self._orders[raw_order_id] = record
            self._orders.move_to_end(raw_order_id)
            self._prune_orders_locked()

    def update_exchange_order(self, data: Any) -> None:
        """Consume the richer Binance execution update before OMS fields are lost."""
        raw_order_id = str(
            _get_value(data, "client_oid", _get_value(data, "exchange_oid", "")) or ""
        )
        if not raw_order_id:
            return
        symbol = str(_get_value(data, "symbol", "") or "").upper()
        status = str(_get_value(data, "status", "") or "").upper()
        tif = str(_get_value(data, "time_in_force", "") or "").upper()
        cumulative = _finite_float(_get_value(data, "cum_filled_qty"), 0.0) or 0.0
        fill_price = _finite_float(_get_value(data, "filled_price"), 0.0) or 0.0
        trade_id = _get_value(data, "trade_id", -1)
        update_time = _finite_float(_get_value(data, "update_time"), time.time())
        commission = _finite_float(_get_value(data, "commission"))
        realized_pnl = _finite_float(_get_value(data, "realized_pnl"))
        is_maker = _get_value(data, "is_maker")
        with self._lock:
            existing = self._orders.get(raw_order_id, {})
            is_rpi = bool(tif == "RPI" or existing.get("is_rpi", False))
            self._record_order_transition_locked(
                raw_order_id,
                status=status,
                is_rpi=is_rpi,
                is_new=not existing,
            )
            execution_id = (
                f"{symbol}:{trade_id}"
                if trade_id not in (None, "", -1, "-1")
                else f"{raw_order_id}:{update_time:.9f}:{cumulative:.12g}"
            )
            self._record_fill_locked(
                raw_order_id,
                symbol=symbol,
                cumulative=cumulative,
                fill_price=fill_price,
                is_rpi=is_rpi,
                commission=commission,
                realized_pnl=realized_pnl,
                is_maker=is_maker,
                execution_id=execution_id,
            )
            record = {
                **existing,
                "client_oid": _mask_identifier(raw_order_id),
                "exchange_oid": _mask_identifier(_get_value(data, "exchange_oid", "")),
                "symbol": symbol or existing.get("symbol", ""),
                "status": status or existing.get("status", ""),
                "filled_volume": cumulative,
                "avg_price": fill_price or existing.get("avg_price", 0.0),
                "time_in_force": tif or existing.get("time_in_force", ""),
                "is_rpi": is_rpi,
                "last_fill_quantity": _finite_float(_get_value(data, "filled_qty"), 0.0),
                "last_fill_price": fill_price,
                "last_commission": commission,
                "commission_asset": str(_get_value(data, "commission_asset", "") or ""),
                "last_realized_pnl": realized_pnl,
                "last_fill_is_maker": is_maker,
                "updated_at": update_time,
            }
            self._orders[raw_order_id] = record
            self._orders.move_to_end(raw_order_id)
            self._prune_orders_locked()

    def update_trade(self, data: Any) -> None:
        if hasattr(data, "maker_is_buyer") and not hasattr(data, "order_id"):
            self.update_market_trade(data)
            return
        symbol = str(_get_value(data, "symbol", "") or "").upper()
        raw_order_id = str(_get_value(data, "order_id", "") or "")
        raw_trade_id = str(_get_value(data, "trade_id", "") or "")
        event_time = _timestamp(_get_value(data, "datetime")) or time.time()
        dedupe_id = f"{symbol}:{raw_trade_id or raw_order_id}:{event_time:.9f}"
        with self._lock:
            if dedupe_id in self._trade_ids:
                return
            self._remember_id_locked(dedupe_id, self._trade_ids, self._trade_id_order)
            order = self._orders.get(raw_order_id, {})
            record = {
                "symbol": symbol,
                "order_id": _mask_identifier(raw_order_id),
                "trade_id": _mask_identifier(raw_trade_id),
                "side": str(_enum_value(_get_value(data, "side", "")) or ""),
                "price": _finite_float(_get_value(data, "price")),
                "volume": _finite_float(_get_value(data, "volume")),
                "notional": self._notional(data),
                "time": event_time,
                "time_iso": _utc_iso(event_time),
                "time_in_force": order.get("time_in_force"),
                "is_rpi": bool(order.get("is_rpi", False)),
            }
            self._trades.append(record)

    def update_system_health(self, data: Any) -> None:
        now = time.time()
        safe = _safe_value(data)
        alert = {
            "time": now,
            "time_iso": _utc_iso(now),
            "level": self._health_level(safe),
            "source": "system_health",
            "message": _redact_text(safe, 1024),
            "data": safe,
        }
        with self._lock:
            self._system_health = safe
            self._alerts.append(alert)

    def update_api_limit(self, data: Any) -> None:
        timestamp = _finite_float(_get_value(data, "timestamp"), time.time())
        snapshot = {
            "available": True,
            "weight_used_1m": _finite_int(_get_value(data, "weight_used_1m")),
            "timestamp": timestamp,
            "age_sec": max(0.0, time.time() - timestamp) if timestamp else None,
        }
        extra = _safe_value(data)
        if isinstance(extra, dict):
            snapshot = {**extra, **snapshot}
        with self._lock:
            self._runtime_metrics["api_limit"] = snapshot

    def update_alert(self, data: Any) -> None:
        timestamp = _finite_float(_get_value(data, "timestamp"), time.time())
        level = str(_get_value(data, "level", "WARNING") or "WARNING").upper()
        message = _redact_text(
            _get_value(data, "msg", _get_value(data, "message", "")),
            1024,
        )
        with self._lock:
            self._alerts.append(
                {
                    "time": timestamp,
                    "time_iso": _utc_iso(timestamp),
                    "level": level,
                    "source": "alert_event",
                    "message": message,
                }
            )

    def add_log(self, message: Any) -> None:
        now = time.time()
        rendered = _redact_text(message, 1024)
        level = "INFO"
        match = re.search(r"\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]", rendered.upper())
        if match:
            level = match.group(1)
        record = {"time": now, "time_iso": _utc_iso(now), "level": level, "message": rendered}
        with self._lock:
            self._logs.append(record)
            if level in {"WARNING", "ERROR", "CRITICAL"}:
                self._alerts.append({**record, "source": "log"})

    def update_risk_snapshot(self, snapshot: Mapping[str, Any] | None, source: str = "risk_manager") -> None:
        safe = _safe_value(snapshot or {})
        with self._lock:
            self._risk_sources[str(source or "risk_manager")] = (
                safe if isinstance(safe, dict) else {"value": safe}
            )

    def update_oms_snapshot(self, snapshot: Mapping[str, Any] | None) -> None:
        self.update_risk_snapshot(snapshot, source="oms")

    def update_rpi_metrics(self, metrics: Mapping[str, Any] | None) -> None:
        safe = _safe_value(metrics or {})
        with self._lock:
            if isinstance(safe, dict):
                self._rpi_external = self._deep_merge(self._rpi_external, safe)

    def update_rpi_capabilities(self, capabilities: Mapping[str, Any] | list[str] | tuple[str, ...]) -> None:
        if isinstance(capabilities, Mapping):
            normalized = {
                str(symbol).upper(): None if value is None else bool(value)
                for symbol, value in capabilities.items()
            }
        else:
            supported = {str(symbol).upper() for symbol in capabilities}
            configured = set(self._config_view.get("symbols", []))
            normalized = {symbol: symbol in supported for symbol in configured | supported}
        with self._lock:
            self._rpi_capabilities.update(normalized)

    # ------------------------------------------------------------------
    # Snapshot publication.  Only these methods inspect component objects.
    # ------------------------------------------------------------------

    def update_runtime_metrics(self, metrics: Mapping[str, Any] | None) -> bool:
        return self.publish_snapshot(runtime_metrics=metrics, force=False)

    def publish_runtime_snapshot(
        self,
        runtime_metrics: Mapping[str, Any] | None = None,
        **sections: Any,
    ) -> bool:
        return self.publish_snapshot(runtime_metrics=runtime_metrics, **sections)

    def publish_snapshot(
        self,
        runtime_metrics: Mapping[str, Any] | None = None,
        *,
        risk: Mapping[str, Any] | None = None,
        rpi: Mapping[str, Any] | None = None,
        force: bool = True,
    ) -> bool:
        """Build and atomically publish a JSON snapshot on the caller's thread."""
        now_monotonic = time.monotonic()
        if (
            not force
            and self.publish_interval_sec > 0.0
            and now_monotonic - self._last_publish_monotonic < self.publish_interval_sec
        ):
            if runtime_metrics is not None:
                safe_runtime = _safe_value(runtime_metrics)
                with self._lock:
                    if isinstance(safe_runtime, dict):
                        self._runtime_metrics = self._deep_merge(
                            self._runtime_metrics,
                            safe_runtime,
                        )
            return False

        component_snapshots = self._collect_component_snapshots(runtime_metrics)
        safe_risk = _safe_value(risk) if risk is not None else None
        safe_rpi = _safe_value(rpi) if rpi is not None else None
        now = time.time()
        with self._lock:
            oms_primitives = component_snapshots.get("oms_primitives")
            if isinstance(oms_primitives, Mapping):
                self._merge_oms_primitives_locked(oms_primitives)
            if runtime_metrics is not None:
                supplied = _safe_value(runtime_metrics)
                if isinstance(supplied, dict):
                    self._runtime_metrics = self._deep_merge(
                        self._runtime_metrics,
                        supplied,
                    )
            if isinstance(safe_risk, dict):
                self._risk_sources = self._deep_merge(self._risk_sources, safe_risk)
            if isinstance(safe_rpi, dict):
                self._rpi_external = self._deep_merge(self._rpi_external, safe_rpi)
            for source, snapshot in component_snapshots.get("risk_sources", {}).items():
                self._risk_sources[source] = snapshot
            self._sample_histories_locked(now)
            self._sequence += 1
            sequence = self._sequence
            snapshot = self._build_snapshot_locked(
                sequence=sequence,
                now=now,
                components=component_snapshots,
            )

        encoded = json.dumps(
            snapshot,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        health = json.dumps(
            {
                "status": "ok" if self.is_running else self._service_state,
                "sequence": sequence,
                "generated_at": snapshot["meta"]["generated_at"],
                "snapshot_age_sec": 0.0,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        with self._lock:
            if sequence < self._published_sequence:
                return False
            self._published_sequence = sequence
            self._published_at = now
            self._published_snapshot = snapshot
            self._published_json = encoded
            self._published_health_json = health
            self._last_publish_monotonic = now_monotonic
        return True

    def get_snapshot(self) -> dict[str, Any]:
        """Return a copy of the last published snapshot, never live component state."""
        with self._lock:
            return deepcopy(self._published_snapshot)

    snapshot = get_snapshot

    def _collect_component_snapshots(
        self, runtime_metrics: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        supplied = dict(runtime_metrics or {})
        result: dict[str, Any] = {"risk_sources": {}}
        result["event_engine"] = self._safe_component_call(
            supplied.get("event_engine"),
            self._components.get("event_engine"),
            "get_metrics_snapshot",
        )
        result["strategy_runtime"] = self._safe_component_call(
            supplied.get("strategy_runtime"),
            self._components.get("strategy_runtime"),
            "get_metrics_snapshot",
        )
        result["gateway"] = self._gateway_snapshot()
        result["oms"] = self._oms_snapshot()
        result["oms_primitives"] = self._oms_primitive_snapshot()
        primitive_system = result["oms_primitives"].get("system", {})
        if isinstance(primitive_system, Mapping):
            result["oms"] = self._deep_merge(result["oms"], primitive_system)
        risk_manager = self._components.get("risk_manager")
        risk_snapshot = self._call_snapshot(risk_manager, "get_status_snapshot")
        if risk_snapshot is not None:
            result["risk_sources"]["status"] = risk_snapshot
        risk_supervisor = self._components.get("risk_supervisor")
        result["risk_supervisor"] = self._component_section(
            self._call_snapshot(risk_supervisor, "get_status_snapshot")
        )
        result["time_service"] = self._time_service_snapshot()
        result["truth_monitor"] = self._truth_monitor_snapshot()
        result["venue_supervisor"] = self._venue_supervisor_snapshot()
        return result

    @staticmethod
    def _safe_component_call(
        supplied: Any,
        component: Any,
        method_name: str,
    ) -> dict[str, Any]:
        if supplied is not None:
            return LocalWebDashboard._component_section(supplied)
        return LocalWebDashboard._component_section(
            LocalWebDashboard._call_snapshot(component, method_name)
        )

    @staticmethod
    def _call_snapshot(component: Any, method_name: str) -> dict[str, Any] | None:
        if component is None:
            return None
        method = getattr(component, method_name, None)
        if not callable(method):
            return None
        try:
            snapshot = _safe_value(method())
        except Exception as exc:  # Dashboard failures must never affect trading.
            return {"available": False, "error": f"{type(exc).__name__}:{_redact_text(exc)}"}
        return snapshot if isinstance(snapshot, dict) else {"value": snapshot}

    @staticmethod
    def _component_section(value: Any) -> dict[str, Any]:
        if value is None:
            return _section_unavailable()
        safe = _safe_value(value)
        if isinstance(safe, dict):
            return {"available": safe.get("available", True), **safe}
        return {"available": True, "value": safe}

    def _gateway_snapshot(self) -> dict[str, Any]:
        gateway = self._components.get("gateway")
        if gateway is None:
            return _section_unavailable(
                latency={"available": False, "reason": "not_instrumented"},
                api_limits={"available": False, "reason": "not_instrumented"},
            )
        try:
            state = _enum_value(getattr(gateway, "state", None))
            symbols = [str(symbol).upper() for symbol in getattr(gateway, "symbols", [])]
            ws = getattr(gateway, "ws", None)
            ws_active = bool(getattr(ws, "active", False)) if ws is not None else False
            streams = []
            ws_lock = getattr(ws, "lock", None)
            if ws is not None and ws_lock is not None:
                acquired = ws_lock.acquire(timeout=0.01)
                if acquired:
                    try:
                        streams = sorted(str(name) for name in getattr(ws, "stream_apps", {}))
                    finally:
                        ws_lock.release()
            return {
                "available": True,
                "name": str(getattr(gateway, "gateway_name", "") or ""),
                "gateway_name": str(getattr(gateway, "gateway_name", "") or ""),
                "state": state,
                "active": bool(getattr(gateway, "active", False)),
                "testnet": bool(getattr(gateway, "testnet", False)),
                "execution_mode": self._config_view["execution_mode"],
                "execution_venue": self._config_view["execution_venue"],
                "market_data_environment": self._config_view[
                    "market_data_environment"
                ],
                "simulated_execution": self._config_view[
                    "simulated_execution"
                ],
                "private_api_enabled": self._config_view[
                    "private_api_enabled"
                ],
                "symbols": symbols,
                "websocket": {
                    "available": ws is not None,
                    "active": ws_active,
                    "connected_streams": streams,
                },
                # These fields exist in the base class but are not populated by
                # the current gateway, so zero must not be represented as data.
                "latency": {"available": False, "reason": "not_instrumented"},
                "api_limits": {"available": False, "reason": "not_instrumented"},
            }
        except Exception as exc:
            return _section_unavailable(error=f"{type(exc).__name__}:{_redact_text(exc)}")

    def _oms_snapshot(self) -> dict[str, Any]:
        oms = self._components.get("oms")
        if oms is None:
            return _section_unavailable()
        try:
            lifecycle = _enum_value(getattr(oms, "state", None))
            base = {
                "available": True,
                "state": lifecycle,
                "manual_rearm_required": bool(
                    getattr(oms, "manual_rearm_required", False)
                ),
                "last_freeze_reason": _redact_text(
                    getattr(oms, "last_freeze_reason", ""), 512
                ),
                "last_halt_reason": _redact_text(
                    getattr(oms, "last_halt_reason", ""), 512
                ),
            }
            capability = self._call_snapshot(oms, "get_capability_snapshot")
            if capability:
                base["capability"] = capability
                base["capability_mode"] = capability.get("mode")
                base["capability_reason"] = capability.get("reason")
                base["outbound_message_budget"] = capability.get(
                    "outbound_message_budget",
                    _section_unavailable(),
                )
            return base
        except Exception as exc:
            return _section_unavailable(error=f"{type(exc).__name__}:{_redact_text(exc)}")

    def _oms_primitive_snapshot(self) -> dict[str, Any]:
        """Copy OMS-owned primitives while briefly holding ``oms.lock``.

        This runs before the dashboard lock is acquired.  Keeping the lock
        order OMS -> released -> dashboard prevents a dashboard/OMS inversion.
        """
        oms = self._components.get("oms")
        if oms is None:
            return _section_unavailable()
        oms_lock = getattr(oms, "lock", None)
        if oms_lock is None:
            return _section_unavailable(reason="oms_lock_unavailable")
        try:
            acquired = oms_lock.acquire(timeout=0.02)
        except TypeError:
            acquired = oms_lock.acquire(False)
        if not acquired:
            return _section_unavailable(reason="oms_snapshot_lock_busy")
        try:
            now = time.time()
            raw_orders = []
            for raw_order_id, order in getattr(oms, "orders", {}).items():
                intent = getattr(order, "intent", None)
                status = str(_enum_value(getattr(order, "status", "")) or "").upper()
                volume = _finite_float(getattr(intent, "volume", 0.0), 0.0) or 0.0
                filled = _finite_float(getattr(order, "filled_volume", 0.0), 0.0) or 0.0
                tif = str(getattr(intent, "time_in_force", "") or "").upper()
                raw_orders.append(
                    {
                        "_raw_order_id": str(raw_order_id or ""),
                        "client_oid": _mask_identifier(raw_order_id),
                        "exchange_oid": _mask_identifier(
                            getattr(order, "exchange_oid", "")
                        ),
                        "symbol": str(getattr(intent, "symbol", "") or "").upper(),
                        "strategy_id": _redact_text(
                            getattr(intent, "strategy_id", ""), 128
                        ),
                        "side": str(
                            _enum_value(getattr(intent, "side", "")) or ""
                        ).upper(),
                        "status": status,
                        "price": _finite_float(getattr(intent, "price", None)),
                        "volume": volume,
                        "filled_volume": filled,
                        "remaining_volume": max(0.0, volume - filled),
                        "avg_price": _finite_float(
                            getattr(order, "avg_price", None)
                        ),
                        "time_in_force": tif,
                        "post_only": bool(
                            getattr(
                                intent,
                                "is_post_only",
                                getattr(intent, "post_only", False),
                            )
                        ),
                        "reduce_only": bool(
                            getattr(intent, "reduce_only", False)
                        ),
                        "is_rpi": bool(
                            getattr(intent, "is_rpi", False) or tif == "RPI"
                        ),
                        "tag": _redact_text(getattr(intent, "tag", ""), 128),
                        "error": _redact_text(
                            getattr(order, "error_msg", ""), 512
                        ),
                        "error_msg": _redact_text(
                            getattr(order, "error_msg", ""), 512
                        ),
                        "error_message": _redact_text(
                            getattr(order, "error_msg", ""), 512
                        ),
                        "created_at": _finite_float(
                            getattr(order, "created_at", None)
                        ),
                        "update_time": _finite_float(
                            getattr(order, "updated_at", now), now
                        ),
                        "updated_at": _finite_float(
                            getattr(order, "updated_at", now), now
                        ),
                    }
                )

            exposure = getattr(oms, "exposure", None)
            net_positions = dict(getattr(exposure, "net_positions", {}) or {})
            avg_prices = dict(getattr(exposure, "avg_prices", {}) or {})
            open_buy = dict(getattr(exposure, "open_buy_qty", {}) or {})
            open_sell = dict(getattr(exposure, "open_sell_qty", {}) or {})
            reduce_buy = dict(getattr(exposure, "reduce_only_buy_qty", {}) or {})
            reduce_sell = dict(getattr(exposure, "reduce_only_sell_qty", {}) or {})

            account = getattr(oms, "account", None)
            account_snapshot = {
                "data_available": account is not None,
                "balance": _finite_float(getattr(account, "balance", None)),
                "equity": _finite_float(getattr(account, "equity", None)),
                "available": _finite_float(getattr(account, "available", None)),
                "available_balance": _finite_float(
                    getattr(account, "available", None)
                ),
                "used_margin": _finite_float(
                    getattr(account, "used_margin", None)
                ),
                "budget_balance": _finite_float(
                    getattr(account, "budget_balance", None)
                ),
                "budget_available": _finite_float(
                    getattr(account, "budget_available", None)
                ),
                "balances": _safe_value(
                    dict(getattr(account, "balances", {}) or {})
                ),
                "available_balances": _safe_value(
                    dict(getattr(account, "available_balances", {}) or {})
                ),
                "trading_budget_by_asset": _safe_value(
                    dict(getattr(account, "trading_budget_by_asset", {}) or {})
                ),
                "maintenance_margin": _finite_float(
                    getattr(account, "maintenance_margin", None)
                ),
                "margin_balance": _finite_float(
                    getattr(account, "margin_balance", None)
                ),
                "maintenance_margin_ratio": _finite_float(
                    getattr(account, "maintenance_margin_ratio", None)
                ),
                "margin_snapshot_time": _finite_float(
                    getattr(account, "margin_snapshot_time", None)
                ),
                "margin_snapshot_synced": bool(
                    getattr(account, "margin_snapshot_synced", False)
                ),
                "external_cash_flow_total": _finite_float(
                    getattr(account, "external_cash_flow_total", None)
                ),
                "cash_flow_snapshot_time": _finite_float(
                    getattr(account, "cash_flow_snapshot_time", None)
                ),
                "cash_flow_snapshot_synced": bool(
                    getattr(account, "cash_flow_snapshot_synced", False)
                ),
                "event_time": now,
                "event_time_iso": _utc_iso(now),
            }
            guards = {
                "frozen_symbols": _safe_value(
                    dict(getattr(oms, "symbol_guards", {}) or {})
                ),
                "frozen_venues": _safe_value(
                    dict(getattr(oms, "venue_guards", {}) or {})
                ),
                "frozen_strategies": _safe_value(
                    dict(getattr(oms, "strategy_guards", {}) or {})
                ),
            }
        except Exception as exc:
            return _section_unavailable(
                reason="oms_snapshot_failed",
                error=f"{type(exc).__name__}:{_redact_text(exc)}",
            )
        finally:
            oms_lock.release()

        all_symbols = (
            set(net_positions)
            | set(avg_prices)
            | set(open_buy)
            | set(open_sell)
            | set(reduce_buy)
            | set(reduce_sell)
        )
        positions = {
            str(symbol).upper(): {
                "available": True,
                "volume": _finite_float(net_positions.get(symbol), 0.0),
                "entry_price": _finite_float(avg_prices.get(symbol), 0.0),
                "avg_price": _finite_float(avg_prices.get(symbol), 0.0),
                "open_buy_qty": _finite_float(open_buy.get(symbol), 0.0),
                "open_sell_qty": _finite_float(open_sell.get(symbol), 0.0),
                "reduce_only_buy_qty": _finite_float(
                    reduce_buy.get(symbol), 0.0
                ),
                "reduce_only_sell_qty": _finite_float(
                    reduce_sell.get(symbol), 0.0
                ),
                "reduce_only_qty": (
                    (_finite_float(reduce_buy.get(symbol), 0.0) or 0.0)
                    + (_finite_float(reduce_sell.get(symbol), 0.0) or 0.0)
                ),
                "updated_at": now,
            }
            for symbol in all_symbols
        }
        active = [
            order for order in raw_orders if order.get("status") in _ACTIVE_ORDER_STATUSES
        ]
        terminal = sorted(
            (
                order
                for order in raw_orders
                if order.get("status") not in _ACTIVE_ORDER_STATUSES
            ),
            key=lambda order: float(order.get("updated_at", 0.0) or 0.0),
            reverse=True,
        )[: self.max_orders]
        return {
            "available": True,
            "orders": active + terminal,
            "positions": positions,
            "account": account_snapshot,
            "system": guards,
        }

    def _merge_oms_primitives_locked(self, snapshot: Mapping[str, Any]) -> None:
        if not snapshot.get("available"):
            return
        account = snapshot.get("account")
        if isinstance(account, Mapping):
            self._account = {**self._account, **deepcopy(dict(account))}
        for symbol, position in (snapshot.get("positions", {}) or {}).items():
            symbol_key = str(symbol).upper()
            existing_position = self._positions.get(symbol_key, {})
            self._positions[symbol_key] = {
                **existing_position,
                **deepcopy(dict(position)),
            }
        for raw_record in snapshot.get("orders", []) or []:
            if not isinstance(raw_record, Mapping):
                continue
            record = deepcopy(dict(raw_record))
            raw_order_id = str(record.pop("_raw_order_id", "") or "")
            if not raw_order_id:
                continue
            previous = self._orders.get(raw_order_id)
            merged = {**(previous or {}), **record}
            status = str(merged.get("status", "") or "").upper()
            is_rpi = bool(merged.get("is_rpi", False))
            self._record_order_transition_locked(
                raw_order_id,
                status=status,
                is_rpi=is_rpi,
                is_new=previous is None,
                count_update=previous is None,
            )
            self._record_fill_locked(
                raw_order_id,
                symbol=str(merged.get("symbol", "") or ""),
                cumulative=_finite_float(merged.get("filled_volume"), 0.0) or 0.0,
                fill_price=(
                    _finite_float(merged.get("avg_price"), 0.0)
                    or _finite_float(merged.get("price"), 0.0)
                    or 0.0
                ),
                is_rpi=is_rpi,
            )
            self._orders[raw_order_id] = merged
            self._orders.move_to_end(raw_order_id)
        self._prune_orders_locked()

    def _time_service_snapshot(self) -> dict[str, Any]:
        service = self._components.get("time_service")
        if service is None:
            return _section_unavailable()
        try:
            last_sync = _finite_float(getattr(service, "last_sync_time", 0.0), 0.0) or 0.0
            return {
                "available": True,
                "active": bool(getattr(service, "active", False)),
                "health": str(getattr(service, "_health_state", "unknown") or "unknown"),
                "offset_ms": _finite_float(getattr(service, "offset", None)),
                "rtt_ms": _finite_float(getattr(service, "last_rtt_ms", None)),
                "last_sync_time": last_sync or None,
                "last_sync_age_sec": max(0.0, time.time() - last_sync) if last_sync else None,
                "last_error": _redact_text(getattr(service, "last_error", ""), 512),
                "consecutive_failures": _finite_int(
                    getattr(service, "consecutive_failures", 0), 0
                ),
            }
        except Exception as exc:
            return _section_unavailable(error=f"{type(exc).__name__}:{_redact_text(exc)}")

    def _truth_monitor_snapshot(self) -> dict[str, Any]:
        monitor = self._components.get("truth_monitor")
        if monitor is None:
            return _section_unavailable()
        try:
            return {
                "available": True,
                "active": bool(getattr(monitor, "active", False)),
                "poll_interval_sec": _finite_float(
                    getattr(monitor, "poll_interval_sec", None)
                ),
                "consecutive_api_failures": _finite_int(
                    getattr(monitor, "consecutive_api_failures", 0), 0
                ),
                "consecutive_balance_drifts": _finite_int(
                    getattr(monitor, "consecutive_balance_drifts", 0), 0
                ),
                "clean_polls": _finite_int(getattr(monitor, "clean_polls", 0), 0),
                "cash_flow_truth_enabled": bool(
                    getattr(monitor, "cash_flow_truth_enabled", False)
                ),
            }
        except Exception as exc:
            return _section_unavailable(error=f"{type(exc).__name__}:{_redact_text(exc)}")

    def _venue_supervisor_snapshot(self) -> dict[str, Any]:
        supervisor = self._components.get("venue_supervisor")
        if supervisor is None:
            return _section_unavailable()
        try:
            return {
                "available": True,
                "active": bool(getattr(supervisor, "active", False)),
                "poll_interval_sec": _finite_float(
                    getattr(supervisor, "poll_interval_sec", None)
                ),
                "max_attempts": _finite_int(getattr(supervisor, "max_attempts", None)),
                "attempts_by_venue": _safe_value(
                    dict(getattr(supervisor, "attempts_by_venue", {}) or {})
                ),
            }
        except Exception as exc:
            return _section_unavailable(error=f"{type(exc).__name__}:{_redact_text(exc)}")

    def _build_snapshot_locked(
        self,
        *,
        sequence: int,
        now: float,
        components: Mapping[str, Any],
    ) -> dict[str, Any]:
        order_records = list(self._orders.values())
        active_orders = [record for record in order_records if record.get("status") in _ACTIVE_ORDER_STATUSES]
        recent_orders = sorted(
            order_records,
            key=lambda record: float(record.get("updated_at", 0.0) or 0.0),
            reverse=True,
        )[: min(100, self.max_orders)]
        status_counts = Counter(str(record.get("status", "") or "UNKNOWN") for record in order_records)
        tif_counts = Counter(str(record.get("time_in_force", "") or "UNKNOWN") for record in order_records)
        symbols = self._build_symbol_rows_locked(active_orders)
        risk_status = self._risk_sources.get("status", _section_unavailable())
        rpi = self._build_rpi_snapshot_locked(active_orders)
        performance = self._build_performance_locked()
        simulated_execution = self._config_view["simulated_execution"]
        execution_source = (
            "local_paper_simulator" if simulated_execution else "exchange_execution"
        )
        performance["simulated"] = simulated_execution
        performance["execution_venue"] = self._config_view["execution_venue"]
        performance["execution_source"] = execution_source
        uptime = (
            max(0.0, time.monotonic() - self._started_monotonic)
            if self._started_monotonic
            else 0.0
        )
        system = {
            "available": True,
            "gateway": components.get("gateway", _section_unavailable()),
            "oms": components.get("oms", _section_unavailable()),
            "risk_supervisor": components.get(
                "risk_supervisor", _section_unavailable()
            ),
            "event_engine": components.get("event_engine", _section_unavailable()),
            "strategy_runtime": components.get(
                "strategy_runtime", _section_unavailable()
            ),
            "time_service": components.get("time_service", _section_unavailable()),
            "truth_monitor": components.get("truth_monitor", _section_unavailable()),
            "venue_supervisor": components.get(
                "venue_supervisor", _section_unavailable()
            ),
            "health_event": self._system_health,
        }
        account = self._account_snapshot_locked()
        environment = {
            "available": True,
            "mode": self._config_view["execution_mode"],
            "code": self._config_view["environment"],
            "label": self._config_view["environment_label"],
            "market_data": {
                "exchange": self._config_view["exchange"],
                "environment": self._config_view["market_data_environment"],
                "source": self._config_view["market_data_source"],
                "public_only": simulated_execution,
            },
            "execution": {
                "venue": self._config_view["execution_venue"],
                "source": execution_source,
                "simulated": simulated_execution,
            },
            "funds": {
                "source": (
                    "local_paper_ledger" if simulated_execution else "exchange_account"
                ),
                "simulated": self._config_view["simulated_funds"],
            },
            "private_api_enabled": self._config_view["private_api_enabled"],
        }
        return {
            "meta": {
                "available": True,
                "schema_version": self.schema_version,
                "sequence": sequence,
                "generated_at": _utc_iso(now),
                "generated_at_unix": now,
                "uptime_sec": uptime,
                "service_state": self._service_state,
                "url": self.url,
                "testnet": self._config_view["testnet"],
                "execution_mode": self._config_view["execution_mode"],
                "environment": self._config_view["environment"],
                "environment_label": self._config_view["environment_label"],
                "market_data_environment": self._config_view[
                    "market_data_environment"
                ],
                "market_data_source": self._config_view["market_data_source"],
                "execution_venue": self._config_view["execution_venue"],
                "simulated_execution": simulated_execution,
                "simulated_funds": self._config_view["simulated_funds"],
                "private_api_enabled": self._config_view[
                    "private_api_enabled"
                ],
                "exchange": self._config_view["exchange"],
                "strategy": self._config_view["strategy"],
                "primary_strategy_model": self._config_view[
                    "primary_strategy_model"
                ],
                "registered_strategy_models": list(
                    self._config_view["registered_strategy_models"]
                ),
                "strategy_execution_policy": self._config_view[
                    "strategy_execution_policy"
                ],
                "configured_symbols": list(self._config_view["symbols"]),
            },
            "environment": environment,
            "system": system,
            "account": account,
            "risk": {
                "available": bool(self._risk_sources),
                "status": deepcopy(risk_status),
                "limits": deepcopy(self._config_view["risk_limits"]),
                "sources": deepcopy(self._risk_sources),
            },
            "rpi": rpi,
            "symbols": symbols,
            "orders": {
                "available": bool(order_records),
                "simulated": simulated_execution,
                "execution_venue": self._config_view["execution_venue"],
                "source": execution_source,
                "active": deepcopy(active_orders),
                "recent": deepcopy(recent_orders),
                "status_counts": dict(status_counts),
                "stats": dict(status_counts),
                "time_in_force_counts": dict(tif_counts),
                "tracked_count": len(order_records),
                "total_count": len(order_records),
                "active_count": len(active_orders),
            },
            "trades": {
                "available": bool(self._trades),
                "simulated": simulated_execution,
                "execution_venue": self._config_view["execution_venue"],
                "source": execution_source,
                "recent": list(reversed(deepcopy(self._trades))),
                "count": len(self._trades),
            },
            "performance": performance,
            "runtime": {
                "available": bool(self._runtime_metrics),
                **deepcopy(self._runtime_metrics),
                "dashboard": {
                    "available": True,
                    "publish_interval_sec": self.publish_interval_sec,
                    "history_interval_sec": self.history_interval_sec,
                    "history_points": self.history_points,
                },
            },
            "histories": {
                "available": bool(self._account_history or self._symbol_histories),
                "account": deepcopy(list(self._account_history)),
                "symbols": {
                    symbol: deepcopy(list(history))
                    for symbol, history in sorted(self._symbol_histories.items())
                },
            },
            "logs": deepcopy(list(self._logs)),
            "alerts": deepcopy(list(self._alerts)),
        }

    def _account_snapshot_locked(self) -> dict[str, Any]:
        account = deepcopy(self._account)
        account["simulated"] = self._config_view["simulated_funds"]
        account["source"] = (
            "local_paper_ledger"
            if self._config_view["simulated_funds"]
            else "exchange_account"
        )
        equity = _finite_float(account.get("equity"))
        balance = _finite_float(account.get("balance"))
        used_margin = _finite_float(account.get("used_margin"))
        account["margin_usage"] = (
            used_margin / equity
            if equity is not None and equity > 0.0 and used_margin is not None
            else None
        )
        position_unrealized = [
            self._position_unrealized_locked(symbol, position)[0]
            for symbol, position in self._positions.items()
        ]
        known_unrealized = [value for value in position_unrealized if value is not None]
        account["unrealized_pnl"] = (
            sum(known_unrealized)
            if known_unrealized
            else equity - balance
            if equity is not None and balance is not None
            else None
        )
        gross_notional = 0.0
        gross_available = False
        for symbol, position in self._positions.items():
            volume = _finite_float(position.get("volume"))
            market = self._markets.get(symbol, {})
            price = _finite_float(
                market.get("mark_price", market.get("mid_price", market.get("mid")))
            )
            if volume is not None and price is not None and price > 0.0:
                gross_available = True
                gross_notional += abs(volume) * price
        account["gross_notional"] = gross_notional if gross_available else None
        risk = self._risk_sources.get("status", {})
        account["daily_pnl"] = (
            risk.get("cash_flow_adjusted_daily_pnl")
            if isinstance(risk, Mapping)
            else None
        )
        return account

    def _build_symbol_rows_locked(self, active_orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        configured = set(self._config_view["symbols"])
        universe = configured | set(self._markets) | set(self._positions) | set(self._strategies)
        universe.update(str(order.get("symbol", "")) for order in active_orders if order.get("symbol"))
        rows = []
        for symbol in sorted(universe):
            market = deepcopy(self._markets.get(symbol, _section_unavailable()))
            position = deepcopy(self._positions.get(symbol, _section_unavailable()))
            strategy = deepcopy(self._strategies.get(symbol, _section_unavailable()))
            symbol_orders = [order for order in active_orders if order.get("symbol") == symbol]
            symbol_trades = [trade for trade in self._trades if trade.get("symbol") == symbol]
            contract = self._contract_snapshot(symbol)
            position_volume = _finite_float(position.get("volume"))
            mark_price = _finite_float(
                market.get("mark_price", market.get("mid_price", market.get("mid")))
            )
            position["notional"] = (
                abs(position_volume) * mark_price
                if position_volume is not None and mark_price is not None
                else None
            )
            unrealized_pnl, pnl_source = self._position_unrealized_locked(
                symbol,
                position,
            )
            position["unrealized_pnl"] = unrealized_pnl
            position["unrealized_pnl_source"] = pnl_source
            rows.append(
                {
                    "symbol": symbol,
                    "contract": contract,
                    "market": market,
                    "strategy": strategy,
                    "position": position,
                    "execution": {
                        "available": bool(symbol_orders or symbol_trades),
                        "simulated": self._config_view["simulated_execution"],
                        "venue": self._config_view["execution_venue"],
                        "active_orders": deepcopy(symbol_orders),
                        "active_order_count": len(symbol_orders),
                        "last_trade": deepcopy(symbol_trades[-1]) if symbol_trades else None,
                        "rpi_active_order_count": sum(
                            1 for order in symbol_orders if order.get("is_rpi")
                        ),
                        "rpi_active_orders": sum(
                            1 for order in symbol_orders if order.get("is_rpi")
                        ),
                    },
                }
            )
        return rows

    def _contract_snapshot(self, symbol: str) -> dict[str, Any]:
        supports = self._rpi_capabilities.get(symbol)
        snapshot = {
            "available": supports is not None,
            "status": None,
            "tick_size": None,
            "step_size": None,
            "min_qty": None,
            "min_notional": None,
            "price_precision": None,
            "qty_precision": None,
            "supports_rpi": supports,
        }
        try:
            from data.ref_data import ref_data_manager

            info = ref_data_manager.get_info(symbol)
        except Exception:
            info = None
        if info is None:
            return snapshot
        return {
            "available": True,
            "status": str(getattr(info, "status", "") or ""),
            "tick_size": _finite_float(getattr(info, "tick_size", None)),
            "step_size": _finite_float(getattr(info, "step_size", None)),
            "min_qty": _finite_float(getattr(info, "min_qty", None)),
            "min_notional": _finite_float(getattr(info, "min_notional", None)),
            "price_precision": _finite_int(getattr(info, "price_precision", None)),
            "qty_precision": _finite_int(getattr(info, "qty_precision", None)),
            "supports_rpi": bool(getattr(info, "supports_rpi", False)),
        }

    def _build_rpi_snapshot_locked(self, active_orders: list[dict[str, Any]]) -> dict[str, Any]:
        is_paper = self._config_view["simulated_execution"]
        configured_symbols = set(self._config_view["symbols"])
        eligibility = {}
        for symbol in sorted(configured_symbols | set(self._rpi_capabilities)):
            explicit = self._rpi_capabilities.get(symbol)
            contract = self._contract_snapshot(symbol)
            supported = explicit if explicit is not None else contract.get("supports_rpi")
            eligibility[symbol] = {
                "available": supported is not None,
                "supported": supported,
                "status": contract.get("status"),
            }
        status_counts = Counter()
        for raw_order_id, statuses in self._order_status_seen.items():
            order = self._orders.get(raw_order_id)
            if order and order.get("is_rpi"):
                status_counts.update(statuses)
        observed = int(self._rpi_performance["orders_observed"])
        base = {
            "available": bool(observed or eligibility or self._config_view["rpi"]["enabled"]),
            "enabled": self._config_view["rpi"]["enabled"],
            "fallback_to_gtx": self._config_view["rpi"]["fallback_to_gtx"],
            "configured": deepcopy(self._config_view["rpi"]),
            "eligibility": {
                "available": bool(eligibility),
                "symbols": eligibility,
                "supported_count": sum(
                    1 for item in eligibility.values() if item.get("supported") is True
                ),
                "unsupported_count": sum(
                    1 for item in eligibility.values() if item.get("supported") is False
                ),
            },
            "orders": {
                "available": bool(observed),
                "observed": observed,
                "active": sum(1 for order in active_orders if order.get("is_rpi")),
                "status_counts": dict(status_counts),
            },
            "fills": {
                "available": bool(self._rpi_performance["fill_events"]),
                "simulated": is_paper,
                "source": (
                    "local_paper_fill_model" if is_paper else "exchange_execution_report"
                ),
                "events": self._rpi_performance["fill_events"],
                "quantity": self._rpi_performance["filled_quantity"],
                "notional": self._rpi_performance["filled_notional"],
                "maker_events": self._rpi_performance["maker_fill_events"],
                "taker_events": self._rpi_performance["taker_fill_events"],
            },
            "commission": {
                "available": self._rpi_commission_observed,
                "value": self._rpi_performance["commission"]
                if self._rpi_commission_observed
                else None,
            },
            "realized_pnl": {
                "available": bool(self._rpi_performance["fill_events"]),
                "value": self._rpi_performance["realized_pnl"]
                if self._rpi_performance["fill_events"]
                else None,
            },
            "depth": {
                "available": False,
                "value": None,
                "reason": "rpi_depth_not_streamed; low_frequency_diagnostic_only",
            },
            "queue_position": {
                "available": False,
                "value": None,
                "reason": "venue_does_not_publish_rpi_queue_position",
            },
            "simulated": is_paper,
            "execution_venue": self._config_view["execution_venue"],
            "real_binance_retail_counterparty_verified": False,
            "matching_scope": (
                "Local paper fill model; no Binance retail counterparty is involved "
                "or verified"
                if is_paper
                else "Binance App/Web retail flow; API orders are ineligible counterparties"
            ),
        }
        terminal = (
            self._rpi_performance["filled_orders"]
            + self._rpi_performance["cancelled_orders"]
            + self._rpi_performance["expired_orders"]
            + self._rpi_performance["rejected_orders"]
        )
        base.update(
            {
                "supported_symbols_count": base["eligibility"]["supported_count"],
                "active_orders": base["orders"]["active"],
                "active_order_count": base["orders"]["active"],
                "total_orders": observed,
                "order_count": observed,
                "filled_orders": self._rpi_performance["filled_orders"],
                "fill_count": self._rpi_performance["fill_events"],
                "fill_ratio": self._rpi_performance["filled_orders"] / terminal
                if terminal
                else None,
                "total_commission": self._rpi_performance["commission"]
                if self._rpi_commission_observed
                else None,
                "commission_available": self._rpi_commission_observed,
                "depth_available": False,
                "rpi_depth_available": False,
                "queue_position_available": False,
                "ahead_quantity_available": False,
                "counterparty_identity_available": False,
                "fill_route_available": (
                    bool(self._rpi_performance["fill_events"]) and not is_paper
                ),
                "simulated_fill_route_available": (
                    bool(self._rpi_performance["fill_events"]) and is_paper
                ),
                "visibility_note": (
                    "Paper RPI fills are local simulation assumptions; they do not "
                    "prove a Binance App/Web retail counterparty. Standard API L2 "
                    "still excludes RPI liquidity."
                    if is_paper
                    else "Standard API L2 excludes RPI liquidity; queue position and "
                    "counterparty identity are unavailable."
                ),
            }
        )
        snapshot = self._deep_merge(base, deepcopy(self._rpi_external))
        if is_paper:
            # External telemetry must never make a local paper fill look like a
            # verified Binance retail match.
            snapshot.update(
                {
                    "simulated": True,
                    "execution_venue": "LOCAL_SIMULATOR",
                    "fill_route_available": False,
                    "real_binance_retail_counterparty_verified": False,
                    "matching_scope": (
                        "Local paper fill model; no Binance retail counterparty is "
                        "involved or verified"
                    ),
                    "visibility_note": (
                        "Paper RPI fills are local simulation assumptions; they do not "
                        "prove a Binance App/Web retail counterparty. Standard API L2 "
                        "still excludes RPI liquidity."
                    ),
                }
            )
            fills = snapshot.get("fills", {})
            if not isinstance(fills, Mapping):
                fills = {}
            snapshot["fills"] = {
                **fills,
                "simulated": True,
                "source": "local_paper_fill_model",
            }
        return snapshot

    def _build_performance_locked(self) -> dict[str, Any]:
        performance = deepcopy(self._performance)
        order_records = list(self._orders.values())
        tif_counts = Counter(
            str(order.get("time_in_force", "") or "UNKNOWN") for order in order_records
        )
        terminal = (
            performance["filled_orders"]
            + performance["cancelled_orders"]
            + performance["expired_orders"]
            + performance["rejected_orders"]
        )
        performance.update(
            {
                "available": bool(performance["orders_observed"] or self._trades),
                "fill_ratio": performance["filled_orders"] / terminal if terminal else None,
                "reject_ratio": performance["rejected_orders"] / terminal
                if terminal
                else None,
                "commission_available": self._commission_observed,
                "commission": performance["commission"] if self._commission_observed else None,
                "source": "dashboard_observed_events",
                "rpi": deepcopy(self._rpi_performance),
                "total_orders": performance["orders_observed"],
                "active_orders": sum(
                    1
                    for order in order_records
                    if order.get("status") in _ACTIVE_ORDER_STATUSES
                ),
                "rpi_orders": self._rpi_performance["orders_observed"],
                "gtx_orders": tif_counts.get("GTX", 0),
                "ioc_orders": tif_counts.get("IOC", 0),
            }
        )
        account = self._account_snapshot_locked()
        performance["gross_notional"] = account.get("gross_notional")
        risk = self._risk_sources.get("status", {})
        for key in (
            "equity",
            "cash_flow_adjusted_equity",
            "cash_flow_adjusted_daily_pnl",
            "peak_drawdown_pct",
        ):
            performance[key] = risk.get(key) if isinstance(risk, Mapping) else None
        return performance

    def _sample_histories_locked(self, now: float) -> None:
        if now - self._last_history_sample < self.history_interval_sec:
            return
        self._last_history_sample = now
        if self._account.get("data_available"):
            self._account_history.append(
                {
                    "time": now,
                    "equity": self._account.get("equity"),
                    "balance": self._account.get("balance"),
                    "available_balance": self._account.get("available_balance"),
                    "used_margin": self._account.get("used_margin"),
                    "maintenance_margin_ratio": self._account.get(
                        "maintenance_margin_ratio"
                    ),
                }
            )
        universe = set(self._markets) | set(self._positions) | set(self._strategies)
        for symbol in universe:
            market = self._markets.get(symbol, {})
            position = self._positions.get(symbol, {})
            strategy = self._strategies.get(symbol, {})
            self._symbol_histories[symbol].append(
                {
                    "time": now,
                    "bid": market.get("bid"),
                    "ask": market.get("ask"),
                    "mid": market.get("mid"),
                    "mark_price": market.get("mark_price"),
                    "spread_bps": market.get("spread_bps"),
                    "alpha_bps": strategy.get("alpha_bps"),
                    "position": position.get("volume"),
                    "unrealized_pnl": position.get("unrealized_pnl"),
                }
            )

    # ------------------------------------------------------------------
    # Internal accounting helpers.
    # ------------------------------------------------------------------

    def _record_order_transition_locked(
        self,
        raw_order_id: str,
        *,
        status: str,
        is_rpi: bool,
        is_new: bool,
        count_update: bool = True,
    ) -> None:
        targets = [self._performance]
        if is_rpi:
            targets.append(self._rpi_performance)
        if is_new:
            for target in targets:
                target["orders_observed"] += 1
        if count_update:
            for target in targets:
                target["order_updates"] += 1
        if not status or status in self._order_status_seen[raw_order_id]:
            return
        self._order_status_seen[raw_order_id].add(status)
        key_by_status = {
            "FILLED": "filled_orders",
            "PARTIALLY_FILLED": "partially_filled_orders",
            "CANCELLED": "cancelled_orders",
            "EXPIRED": "expired_orders",
            "REJECTED": "rejected_orders",
            "REJECTED_LOCALLY": "rejected_orders",
        }
        counter = key_by_status.get(status)
        if counter:
            for target in targets:
                target[counter] += 1

    def _record_fill_locked(
        self,
        raw_order_id: str,
        *,
        symbol: str,
        cumulative: float,
        fill_price: float,
        is_rpi: bool,
        commission: float | None = None,
        realized_pnl: float | None = None,
        is_maker: bool | None = None,
        execution_id: str = "",
    ) -> None:
        duplicate_execution = bool(execution_id and execution_id in self._execution_ids)
        previous = self._order_filled_cumulative.get(raw_order_id, 0.0)
        delta = max(0.0, cumulative - previous)
        self._order_filled_cumulative[raw_order_id] = max(previous, cumulative)
        targets = [self._performance]
        if is_rpi:
            targets.append(self._rpi_performance)
        if delta > 1e-12:
            for target in targets:
                target["fill_events"] += 1
                target["filled_quantity"] += delta
                target["filled_notional"] += delta * max(0.0, fill_price)
                if is_maker is True:
                    target["maker_fill_events"] += 1
                elif is_maker is False:
                    target["taker_fill_events"] += 1
                else:
                    target["unknown_liquidity_fill_events"] += 1
        if execution_id and not duplicate_execution:
            self._remember_id_locked(
                execution_id,
                self._execution_ids,
                self._execution_id_order,
            )
            if commission is not None:
                self._commission_observed = True
                self._performance["commission"] += commission
                if is_rpi:
                    self._rpi_commission_observed = True
                    self._rpi_performance["commission"] += commission
            if realized_pnl is not None:
                self._performance["realized_pnl"] += realized_pnl
                if is_rpi:
                    self._rpi_performance["realized_pnl"] += realized_pnl
        del symbol  # Kept in the signature for future per-symbol accounting.

    @staticmethod
    def _remember_id_locked(identity: str, target: set[str], order: deque[str]) -> None:
        if identity in target:
            return
        if order.maxlen is not None and len(order) >= order.maxlen:
            expired = order.popleft()
            target.discard(expired)
        order.append(identity)
        target.add(identity)

    def _prune_orders_locked(self) -> None:
        if len(self._orders) <= self.max_orders:
            return
        for raw_order_id in list(self._orders):
            if len(self._orders) <= self.max_orders:
                break
            record = self._orders[raw_order_id]
            if record.get("status") not in _ACTIVE_ORDER_STATUSES:
                self._orders.pop(raw_order_id, None)
                self._order_status_seen.pop(raw_order_id, None)
                self._order_filled_cumulative.pop(raw_order_id, None)

    def _safe_depth(self, levels: Any) -> list[list[float]]:
        result = []
        for level in list(levels or ())[: self.max_depth_levels]:
            try:
                price = _finite_float(level[0])
                quantity = _finite_float(level[1])
            except (IndexError, TypeError):
                continue
            if price is not None and quantity is not None:
                result.append([price, quantity])
        return result

    def _position_unrealized_locked(
        self,
        symbol: str,
        position: Mapping[str, Any],
    ) -> tuple[float | None, str]:
        volume = _finite_float(position.get("volume"))
        entry_price = _finite_float(
            position.get("entry_price", position.get("avg_price"))
        )
        market = self._markets.get(symbol, {})
        mark_price = _finite_float(
            market.get("mark_price", market.get("mid_price", market.get("mid")))
        )
        if (
            volume is not None
            and abs(volume) > 1e-12
            and entry_price is not None
            and entry_price > 0.0
            and mark_price is not None
            and mark_price > 0.0
        ):
            return (mark_price - entry_price) * volume, "derived_mark_to_market"
        reported = _finite_float(position.get("reported_unrealized_pnl"))
        if reported is not None:
            return reported, "reported"
        if volume is not None and abs(volume) <= 1e-12:
            return 0.0, "flat"
        return None, "unavailable"

    @staticmethod
    def _notional(data: Any) -> float | None:
        price = _finite_float(_get_value(data, "price"))
        volume = _finite_float(_get_value(data, "volume"))
        return price * volume if price is not None and volume is not None else None

    @staticmethod
    def _health_level(value: Any) -> str:
        rendered = str(value or "").upper()
        if "HALT" in rendered or "ERROR" in rendered or "FATAL" in rendered:
            return "ERROR"
        if "FREEZE" in rendered or "WARN" in rendered or "STALE" in rendered:
            return "WARNING"
        return "INFO"

    @staticmethod
    def _deep_merge(base: dict[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
        merged = deepcopy(base)
        for key, value in update.items():
            if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
                merged[key] = LocalWebDashboard._deep_merge(merged[key], value)
            else:
                merged[key] = deepcopy(value)
        return merged

    # ------------------------------------------------------------------
    # HTTP serving.  Handlers read only immutable published byte strings.
    # ------------------------------------------------------------------

    def _handler_factory(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class DashboardRequestHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                if not owner._valid_host_header(self.headers.get("Host", "")):
                    self._send(b'{"error":"invalid_host"}', "application/json", 400)
                    return
                path = urlsplit(self.path).path
                if path == "/":
                    self._send(owner._static_bytes, "text/html; charset=utf-8", 200)
                    return
                if path == "/api/snapshot":
                    self._send(owner._get_published_json(), "application/json; charset=utf-8", 200)
                    return
                if path == "/healthz":
                    self._send(owner._get_health_json(), "application/json; charset=utf-8", 200)
                    return
                self._send(b'{"error":"not_found"}', "application/json; charset=utf-8", 404)

            def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
                self._send(b"", "application/json; charset=utf-8", 405)

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
                self._method_not_allowed()

            def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
                self._method_not_allowed()

            def do_PATCH(self) -> None:  # noqa: N802 - stdlib handler API
                self._method_not_allowed()

            def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
                self._method_not_allowed()

            def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler API
                self._method_not_allowed()

            def _method_not_allowed(self) -> None:
                self._send(
                    b'{"error":"method_not_allowed","allow":"GET"}',
                    "application/json; charset=utf-8",
                    405,
                    extra_headers={"Allow": "GET"},
                )

            def _send(
                self,
                payload: bytes,
                content_type: str,
                status: int,
                extra_headers: Mapping[str, str] | None = None,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
                    "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
                )
                self.send_header("Connection", "close")
                for key, value in (extra_headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if payload:
                    try:
                        self.wfile.write(payload)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        return DashboardRequestHandler

    def _valid_host_header(self, header: str) -> bool:
        rendered = str(header or "").strip()
        if not rendered:
            return False
        if rendered.startswith("["):
            host = rendered[1:].split("]", 1)[0]
        else:
            host = rendered.rsplit(":", 1)[0] if rendered.count(":") == 1 else rendered
        return _is_loopback_host(host)

    def _get_published_json(self) -> bytes:
        with self._lock:
            return self._published_json

    def _get_health_json(self) -> bytes:
        with self._lock:
            age = max(0.0, time.time() - self._published_at) if self._published_at else None
            payload = {
                "status": "ok" if self._running else self._service_state,
                "sequence": self._published_sequence,
                "snapshot_age_sec": age,
                "generated_at": _utc_iso(self._published_at) if self._published_at else None,
            }
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")

    def _load_static_page(self) -> bytes:
        try:
            return self.static_path.read_bytes()
        except OSError:
            return self._fallback_html()

    @staticmethod
    def _fallback_html() -> bytes:
        return b"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ChronosHFT Dashboard</title>
<style>body{font:16px system-ui;background:#08111f;color:#d8e6ff;margin:3rem}
a{color:#69c7ff}code{background:#111d30;padding:.2rem .4rem;border-radius:.3rem}</style>
</head><body><h1>ChronosHFT Dashboard</h1>
<p>Frontend file is not available yet. The read-only backend is running.</p>
<p>Snapshot: <a href="/api/snapshot"><code>/api/snapshot</code></a></p></body></html>"""
