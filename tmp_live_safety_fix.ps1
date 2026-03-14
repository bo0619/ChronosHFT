function Write-Utf8NoBom([string]$Path, [string]$Content) {
    if ([System.IO.Path]::IsPathRooted($Path)) {
        $fullPath = $Path
    } else {
        $fullPath = Join-Path (Get-Location) $Path
    }
    $dir = Split-Path -Parent $fullPath
    if ($dir -and -not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    [System.IO.File]::WriteAllText($fullPath, $Content, [System.Text.UTF8Encoding]::new($false))
}

function Replace-Regex([string]$Path, [string]$Pattern, [string]$Replacement) {
    $content = Get-Content $Path -Raw
    $regex = [System.Text.RegularExpressions.Regex]::new($Pattern, [System.Text.RegularExpressions.RegexOptions]::Singleline)
    $newContent = $regex.Replace($content, $Replacement, 1)
    if ($newContent -eq $content) {
        throw "Pattern not found in $Path"
    }
    Write-Utf8NoBom $Path $newContent
}

Replace-Regex 'event/type.py' '(?ms)@dataclass\r?\nclass OrderBook:\r?\n    symbol: str\r?\n    exchange: str\r?\n    datetime: datetime\r?\n    asks: Dict\[float, float\] = field\(default_factory=dict\)\r?\n    bids: Dict\[float, float\] = field\(default_factory=dict\)\r?\n(?:    exchange_timestamp: float = 0\.0`r`n    received_timestamp: float = 0\.0`r`n)?' @"
@dataclass
class OrderBook:
    symbol: str
    exchange: str
    datetime: datetime
    asks: Dict[float, float] = field(default_factory=dict)
    bids: Dict[float, float] = field(default_factory=dict)
    exchange_timestamp: float = 0.0
    received_timestamp: float = 0.0
"@
Write-Utf8NoBom 'risk/manager.py' @"
import time
from collections import deque

from data.cache import data_cache
from event.type import (
    Event,
    OrderRequest,
    EVENT_ACCOUNT_UPDATE,
    EVENT_LOG,
    EVENT_MARK_PRICE,
    EVENT_ORDERBOOK,
    EVENT_ORDER_UPDATE,
)
from infrastructure.logger import logger


class RiskManager:
    def __init__(self, engine, config: dict, oms=None, gateway=None):
        self.engine = engine
        self.oms = oms
        self.gateway = gateway
        self.config = config.get("risk", {})

        self.active = self.config.get("active", True)
        self.kill_switch_triggered = False
        self.kill_reason = ""

        limits = self.config.get("limits", {})
        self.max_order_qty = limits.get("max_order_qty", 1000.0)
        self.max_order_notional = limits.get("max_order_notional", 5000.0)
        self.max_pos_notional = limits.get("max_pos_notional", 20000.0)
        self.max_daily_loss = limits.get("max_daily_loss", 500.0)
        self.max_drawdown_pct = limits.get("max_drawdown_pct", 0.0)

        sanity = self.config.get("price_sanity", {})
        self.max_deviation_pct = sanity.get("max_deviation_pct", 0.05)

        tech = self.config.get("tech_health", {})
        self.max_latency_ms = tech.get("max_latency_ms", 1000)
        self.max_orders_per_sec = tech.get("max_order_count_per_sec", 20)
        self.consecutive_error_limit = max(1, int(tech.get("consecutive_error_limit", 10)))

        black_swan = self.config.get("black_swan", {})
        self.volatility_halt_threshold = black_swan.get("volatility_halt_threshold", 0.05)

        self.order_history = deque()
        self.initial_equity = 0.0
        self.peak_equity = 0.0
        self.latency_breach_count = 0

        self.engine.register(EVENT_ORDER_UPDATE, self.on_order_update)
        self.engine.register(EVENT_MARK_PRICE, self.on_mark_price)
        self.engine.register(EVENT_ACCOUNT_UPDATE, self.on_account_update)
        self.engine.register(EVENT_ORDERBOOK, self.on_orderbook)

    def check_order(self, req: OrderRequest) -> bool:
        if self.kill_switch_triggered:
            return False
        if not self.active:
            return True

        now = time.time()
        while self.order_history and self.order_history[0] < now - 1.0:
            self.order_history.popleft()
        if len(self.order_history) >= self.max_orders_per_sec:
            self._log_warn("Order rate limit exceeded")
            return False

        if req.volume > self.max_order_qty:
            self._log_warn(f"Order volume {req.volume} > {self.max_order_qty}")
            return False

        notional = req.price * req.volume
        if notional > self.max_order_notional:
            self._log_warn(f"Order notional {notional:.2f} > {self.max_order_notional}")
            return False

        mark_price = data_cache.get_mark_price(req.symbol)
        if mark_price > 0:
            deviation = abs(req.price - mark_price) / mark_price
            if deviation > self.max_deviation_pct:
                self._log_warn(f"Order price deviation {deviation:.2%} > {self.max_deviation_pct:.2%}")
                return False

        if self.oms:
            current_vol = self.oms.exposure.net_positions.get(req.symbol, 0.0)
            new_notional = (abs(current_vol) + req.volume) * req.price
            if new_notional > self.max_pos_notional:
                self._log_warn(f"Projected position {new_notional:.2f} > {self.max_pos_notional}")
                return False
            if not self.oms.account.check_margin(notional):
                return False

        self.order_history.append(now)
        return True

    def on_mark_price(self, event: Event):
        if self.kill_switch_triggered or not self.active:
            return

        data = event.data
        if data.index_price <= 0 or self.volatility_halt_threshold <= 0:
            return

        divergence = abs(data.mark_price - data.index_price) / data.index_price
        if divergence > self.volatility_halt_threshold:
            self.trigger_kill_switch(
                f"Mark/index divergence {divergence:.2%} > {self.volatility_halt_threshold:.2%} ({data.symbol})"
            )

    def on_orderbook(self, event: Event):
        if self.kill_switch_triggered or not self.active:
            return

        orderbook = event.data
        exchange_ts = float(getattr(orderbook, "exchange_timestamp", 0.0) or 0.0)
        received_ts = float(getattr(orderbook, "received_timestamp", 0.0) or 0.0)
        reference_ts = exchange_ts or received_ts or orderbook.datetime.timestamp()
        latency_ms = max(0.0, (time.time() - reference_ts) * 1000.0)
        if latency_ms > self.max_latency_ms:
            self.latency_breach_count += 1
            self._log_warn(
                f"Market data latency {latency_ms:.1f}ms > {self.max_latency_ms}ms "
                f"({self.latency_breach_count}/{self.consecutive_error_limit})"
            )
            if self.latency_breach_count >= self.consecutive_error_limit:
                self.trigger_kill_switch(
                    f"Market data latency {latency_ms:.1f}ms exceeded {self.max_latency_ms}ms "
                    f"for {self.latency_breach_count} consecutive updates"
                )
        else:
            self.latency_breach_count = 0

    def on_account_update(self, event: Event):
        if self.kill_switch_triggered or not self.active:
            return

        account = event.data
        if self.initial_equity == 0:
            self.initial_equity = account.equity
        self.peak_equity = max(self.peak_equity, account.equity)

        drawdown = self.initial_equity - account.equity
        if self.max_daily_loss > 0 and drawdown > self.max_daily_loss:
            self.trigger_kill_switch(f"Daily loss limit breached: -{drawdown:.2f}")
            return

        if self.max_drawdown_pct > 0 and self.peak_equity > 0:
            drawdown_pct = max(0.0, (self.peak_equity - account.equity) / self.peak_equity)
            if drawdown_pct > self.max_drawdown_pct:
                self.trigger_kill_switch(
                    f"Drawdown {drawdown_pct:.2%} > {self.max_drawdown_pct:.2%}"
                )

    def on_order_update(self, event: Event):
        return None

    def trigger_kill_switch(self, reason: str):
        if self.kill_switch_triggered:
            return

        self.kill_switch_triggered = True
        self.kill_reason = reason
        logger.critical(f"KILL SWITCH TRIGGERED: {reason}")

        if self.gateway:
            symbols = set()
            if self.oms:
                symbols.update(self.oms.config.get("symbols", []))
                symbols.update(self.oms.exposure.net_positions.keys())
            for symbol in symbols:
                try:
                    self.gateway.cancel_all_orders(symbol)
                except Exception as exc:
                    logger.error(f"[KillSwitch] cancel_all_orders({symbol}) failed: {exc}")

        if self.oms:
            try:
                self.oms.halt_system(f"KillSwitch: {reason}")
            except Exception as exc:
                logger.error(f"[KillSwitch] oms.halt_system failed: {exc}")

    def _log_warn(self, msg: str):
        self.engine.put(Event(EVENT_LOG, f"[Risk] {msg}"))
"@

Write-Utf8NoBom 'gateway/binance/ws_api.py' @"
# file: gateway/binance/ws_api.py

import threading
import time

import websocket

from infrastructure.logger import logger
from .constants import *


class BinanceWsApi:
    def __init__(self, callback, error_callback, testnet=False):
        self.base_url = WS_URL_TEST if testnet else WS_URL_MAIN
        self.callback = callback
        self.error_callback = error_callback
        self.active = False
        self.ws = None

    def start_market_stream(self, symbols):
        streams = []
        for s in symbols:
            sl = s.lower()
            streams += [f"{sl}@depth@100ms", f"{sl}@aggTrade", f"{sl}@markPrice@1s"]

        url = self.base_url.replace("/ws", "") + "/stream?streams=" + "/".join(streams)
        self._start_thread(url, "MarketWS")

    def start_user_stream(self, listen_key):
        url = f"{self.base_url}/{listen_key}"
        self._start_thread(url, "UserWS")

    def _start_thread(self, url, name):
        self.active = True
        threading.Thread(target=self._run, args=(url, name), daemon=True).start()

    def _run(self, url, name):
        logger.info(f"[{name}] Connecting...")
        while self.active:
            try:
                self.ws = websocket.WebSocketApp(
                    url,
                    on_open=lambda ws: logger.info(f"[{name}] Connected."),
                    on_message=lambda ws, msg: self.callback(msg),
                    on_error=lambda ws, err: self.error_callback(f"[{name}] Error: {err}"),
                    on_close=lambda ws, code, msg: logger.info(f"[{name}] Closed: {code} {msg}"),
                )
                self.ws.run_forever(ping_interval=30)
            except Exception as e:
                self.error_callback(f"[{name}] Exception: {e}")

            if self.active:
                logger.info(f"[{name}] Reconnecting in 5s...")
                time.sleep(5)

    def close(self):
        self.active = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
"@

Write-Utf8NoBom 'infrastructure/watchdog.py' @"
import time

from event.type import Event, EVENT_SYSTEM_HEALTH
from infrastructure.logger import logger


def emit_market_data_stale_if_needed(event_engine, last_tick_time: float, triggered: bool, threshold_sec: float = 60.0, now: float = None) -> bool:
    if triggered or last_tick_time <= 0:
        return triggered

    now = time.time() if now is None else now
    silence_sec = now - last_tick_time
    if silence_sec <= threshold_sec:
        return triggered

    message = f"MARKET_DATA_STALE:{silence_sec:.1f}s>{threshold_sec:.1f}s"
    logger.critical(f"SYSTEM WATCHDOG: {message}")
    event_engine.put(Event(EVENT_SYSTEM_HEALTH, message))
    return True
"@

Write-Utf8NoBom 'tests/test_market_data_watchdog.py' @"
import unittest

from infrastructure.watchdog import emit_market_data_stale_if_needed


class DummyEngine:
    def __init__(self):
        self.events = []

    def put(self, event):
        self.events.append(event)


class MarketDataWatchdogTests(unittest.TestCase):
    def test_emit_market_data_stale_if_needed_emits_once(self):
        engine = DummyEngine()

        triggered = emit_market_data_stale_if_needed(
            engine,
            last_tick_time=10.0,
            triggered=False,
            threshold_sec=60.0,
            now=71.0,
        )
        self.assertTrue(triggered)
        self.assertEqual(len(engine.events), 1)
        self.assertEqual(engine.events[0].type, "eSystemHealth")
        self.assertIn("MARKET_DATA_STALE", engine.events[0].data)

        triggered = emit_market_data_stale_if_needed(
            engine,
            last_tick_time=10.0,
            triggered=triggered,
            threshold_sec=60.0,
            now=72.0,
        )
        self.assertTrue(triggered)
        self.assertEqual(len(engine.events), 1)


if __name__ == "__main__":
    unittest.main()
"@
Write-Utf8NoBom 'gateway/binance/gateway.py' @"
import json
import socket
import threading
import time
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter

from event.type import (
    AggTradeData,
    CancelRequest,
    Event,
    ExchangeAccountUpdate,
    ExchangeOrderUpdate,
    GatewayState,
    MarkPriceData,
    OrderBookGapError,
    OrderRequest,
    EVENT_AGG_TRADE,
    EVENT_MARK_PRICE,
    EVENT_ORDERBOOK,
    EVENT_SYSTEM_HEALTH,
)
from gateway.base_gateway import BaseGateway
from infrastructure.logger import logger
from data.orderbook import LocalOrderBook

from .rest_api import BinanceRestApi
from .ws_api import BinanceWsApi


class HFTAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["socket_options"] = [
            (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        ]
        super().init_poolmanager(connections, maxsize, block, **pool_kwargs)


class BinanceGateway(BaseGateway):
    def __init__(self, event_engine, api_key, api_secret, testnet=True):
        super().__init__(event_engine, "BINANCE")
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet

        self.session = requests.Session()
        adapter = HFTAdapter(pool_connections=20, pool_maxsize=20)
        self.session.mount("https://", adapter)
        self.session.headers.update({"Content-Type": "application/json"})

        self.rest = BinanceRestApi(api_key, api_secret, self.session, testnet)
        self.ws = BinanceWsApi(self.on_ws_message, self.on_ws_error, testnet)

        self.symbols = []
        self.orderbooks = {}
        self.ws_buffer = {}
        self.active = False
        self.listen_key = ""
        self.target_leverage = 0

        self.global_sequence_id = 0
        self.seq_lock = threading.Lock()

    def _next_seq(self):
        with self.seq_lock:
            self.global_sequence_id += 1
            return self.global_sequence_id

    def connect(self, symbols: list):
        self.set_state(GatewayState.CONNECTING)
        self.symbols = [s.upper() for s in symbols]
        self.active = True

        for symbol in self.symbols:
            self.orderbooks[symbol] = LocalOrderBook(symbol)
            self.ws_buffer[symbol] = []

        target_leverage = int(getattr(self, "target_leverage", 0) or 0)
        for symbol in self.symbols:
            self.rest.set_margin_type(symbol, "CROSSED")
            if target_leverage > 0:
                self.rest.set_leverage(symbol, target_leverage)

        self.ws.start_market_stream(self.symbols)

        listen_key = self.rest.create_listen_key()
        if listen_key:
            self.listen_key = listen_key
            self.ws.start_user_stream(listen_key)
            threading.Thread(target=self._keep_alive_loop, daemon=True).start()

        threading.Thread(target=self._init_books, daemon=True).start()
        self.set_state(GatewayState.READY)

    def close(self):
        self.active = False
        self.set_state(GatewayState.DISCONNECTED)
        if self.ws:
            self.ws.close()
        if self.session:
            self.session.close()
        logger.info(f"[{self.gateway_name}] Closed.")

    def send_order(self, req: OrderRequest, client_oid: str = None) -> str:
        resp = self.rest.new_order(req, client_oid)
        if resp and resp.status_code == 200:
            data = resp.json()
            sym = req.symbol.replace("USDC", "").replace("USDT", "").lower()
            side_str = "long" if req.side == "BUY" else "short"
            tif_str = "GTX" if req.post_only else "IOC"

            if client_oid and client_oid.startswith("EXIT_"):
                action = "exit "
            elif client_oid and client_oid.startswith("ENTRY_"):
                action = "enter"
            else:
                action = "order"

            logger.info(
                f"{sym} {action} {side_str} @ {req.price:.6g}"
                f"  ({tif_str}, vol={req.volume})"
            )
            return str(data["orderId"])
        return None

    def cancel_order(self, req: CancelRequest):
        self.rest.cancel_order(req)

    def cancel_all_orders(self, symbol: str):
        self.rest.cancel_all_orders(symbol)

    def get_account_info(self):
        resp = self.rest.get_account()
        return resp.json() if resp and resp.status_code == 200 else None

    def get_all_positions(self):
        resp = self.rest.get_positions()
        return resp.json() if resp and resp.status_code == 200 else None

    def get_open_orders(self):
        resp = self.rest.get_open_orders()
        return resp.json() if resp and resp.status_code == 200 else None

    def get_depth_snapshot(self, symbol):
        return self.rest.get_depth_snapshot(symbol)

    def _keep_alive_loop(self):
        while self.active:
            time.sleep(1800)
            self.rest.keep_alive_listen_key()

    def on_ws_message(self, raw_msg):
        try:
            msg = json.loads(raw_msg)
        except Exception as exc:
            self._emit_ws_fault("WS_PARSE_ERROR", str(exc), raw_msg)
            return

        try:
            event_type = msg.get("e")
            if event_type == "ORDER_TRADE_UPDATE":
                self._handle_user_update(msg)
                return
            if event_type == "ACCOUNT_UPDATE":
                self._handle_account_update(msg)
                return
            if event_type == "listenKeyExpired":
                self._emit_ws_fault("USER_STREAM_EXPIRED", "listen key expired", msg)
                return
            if "stream" in msg:
                self._handle_market_update(msg)
                return
            if self._is_control_message(msg):
                return
            logger.warning(f"[{self.gateway_name}] Ignoring unsupported WS payload: {msg}")
        except Exception as exc:
            self._emit_ws_fault("WS_HANDLER_FAILURE", str(exc), msg)

    def on_ws_error(self, err_msg):
        logger.error(f"[{self.gateway_name}] {err_msg}")
        self.on_log(err_msg, "ERROR")

    def _is_control_message(self, msg):
        return isinstance(msg, dict) and "result" in msg and "id" in msg

    def _emit_ws_fault(self, code: str, detail: str = "", payload=None):
        message = f"{code}: {detail}" if detail else code
        if payload is not None:
            payload_preview = str(payload)
            if len(payload_preview) > 240:
                payload_preview = payload_preview[:237] + "..."
            logger.error(f"[{self.gateway_name}] {message} payload={payload_preview}")
        else:
            logger.error(f"[{self.gateway_name}] {message}")

        self.active = False
        if self.ws:
            self.ws.close()
        if self.state != GatewayState.ERROR:
            self.set_state(GatewayState.ERROR)
        self.event_engine.put(Event(EVENT_SYSTEM_HEALTH, message))

    def _handle_user_update(self, msg):
        order = msg.get("o", {})
        update_time_ms = order.get("T") or msg.get("T") or msg.get("E") or 0
        update = ExchangeOrderUpdate(
            seq=self._next_seq(),
            client_oid=order.get("c", ""),
            exchange_oid=str(order.get("i", "")),
            symbol=order.get("s", ""),
            status=order.get("X", ""),
            filled_qty=float(order.get("l", 0.0) or 0.0),
            filled_price=float(order.get("L", 0.0) or 0.0),
            cum_filled_qty=float(order.get("z", 0.0) or 0.0),
            update_time=float(update_time_ms) / 1000.0 if update_time_ms else time.time(),
            commission=self._parse_optional_float(order.get("n")),
            commission_asset=order.get("N") or "",
            realized_pnl=self._parse_optional_float(order.get("rp")),
            is_maker=bool(order.get("m")) if "m" in order else None,
        )
        self.on_order_update(update)

    def _handle_account_update(self, msg):
        payload = msg.get("a", {})
        balances = payload.get("B", [])
        balance_entry = self._select_balance_entry(balances)
        if not balance_entry:
            return

        balance_snapshot = self._extract_balance_snapshot(balances)
        positions = {}
        for raw_position in payload.get("P", []):
            symbol = raw_position.get("s")
            if not symbol:
                continue
            positions[symbol] = {
                "volume": float(raw_position.get("pa", 0.0) or 0.0),
                "entry_price": float(raw_position.get("ep", 0.0) or 0.0),
                "unrealized_pnl": float(raw_position.get("up", 0.0) or 0.0),
            }

        event_time_ms = msg.get("E") or msg.get("T") or 0
        update = ExchangeAccountUpdate(
            asset=balance_entry.get("a", ""),
            wallet_balance=float(balance_entry.get("wb", 0.0) or 0.0),
            available_balance=self._parse_optional_float(balance_entry.get("cw")),
            balances=balance_snapshot,
            positions=positions,
            reason=payload.get("m", ""),
            event_time=float(event_time_ms) / 1000.0 if event_time_ms else time.time(),
        )
        self.on_account_update(update)

    def _handle_market_update(self, msg):
        stream = msg["stream"]
        data = msg["data"]
        symbol = data.get("s")

        if "@aggTrade" in stream:
            self.on_market_data(
                EVENT_AGG_TRADE,
                AggTradeData(
                    symbol,
                    data["a"],
                    float(data["p"]),
                    float(data["q"]),
                    data["m"],
                    datetime.fromtimestamp(data["T"] / 1000),
                ),
            )
        elif "@markPrice" in stream:
            self.on_market_data(
                EVENT_MARK_PRICE,
                MarkPriceData(
                    symbol,
                    float(data["p"]),
                    float(data["i"]),
                    float(data["r"]),
                    datetime.fromtimestamp(data["T"] / 1000),
                    datetime.now(),
                ),
            )
        elif "@depth" in stream:
            self._process_book(symbol, data)

    def _process_book(self, symbol, raw):
        book = self.orderbooks[symbol]
        buf = self.ws_buffer[symbol]

        if buf is not None:
            buf.append(raw)
            return

        try:
            book.process_delta(raw)
            data = book.generate_event_data()
            if data:
                self.on_market_data(EVENT_ORDERBOOK, data)
        except OrderBookGapError:
            logger.critical(f"[{symbol}] FATAL: OrderBook Gap! Terminating Gateway.")
            self.event_engine.put(Event(EVENT_SYSTEM_HEALTH, "FATAL_GAP"))
            self.close()

    def _init_books(self):
        time.sleep(2)
        for symbol in self.symbols:
            self._resync_book(symbol)

    def _resync_book(self, symbol):
        snapshot = self.rest.get_depth_snapshot(symbol)
        if snapshot:
            self.orderbooks[symbol].init_snapshot(snapshot)
            if self.ws_buffer[symbol]:
                try:
                    for message in self.ws_buffer[symbol]:
                        self.orderbooks[symbol].process_delta(message)
                    self.ws_buffer[symbol] = None
                except OrderBookGapError:
                    logger.critical(f"[{symbol}] Gap during init. Failed.")
                    self.close()
                    return
        logger.info(f"[{symbol}] Initial Sync Done.")

    def _parse_optional_float(self, value):
        if value in (None, ""):
            return None
        return float(value)

    def _extract_balance_snapshot(self, balances):
        snapshot = {}
        for entry in balances or []:
            asset = entry.get("a")
            if not asset:
                continue
            snapshot[asset] = {
                "wallet_balance": float(entry.get("wb", 0.0) or 0.0),
                "available_balance": self._parse_optional_float(entry.get("cw")),
            }
        return snapshot

    def _select_balance_entry(self, balances):
        if not balances:
            return None

        tracked_assets = []
        for symbol in self.symbols:
            asset = self._extract_quote_asset(symbol)
            if asset and asset not in tracked_assets:
                tracked_assets.append(asset)

        for asset in tracked_assets:
            for entry in balances:
                if entry.get("a") == asset:
                    return entry

        for entry in balances:
            if entry.get("a") in {"USDT", "USDC", "BUSD", "FDUSD"}:
                return entry

        return balances[0]

    def _extract_quote_asset(self, symbol: str) -> str:
        for suffix in ("USDT", "USDC", "BUSD", "FDUSD", "BTC", "ETH", "BNB"):
            if symbol.endswith(suffix):
                return suffix
        return ""
"@
