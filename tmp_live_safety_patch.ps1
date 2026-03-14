function Write-Utf8NoBom([string]$Path, [string]$Content) {
    $fullPath = (Resolve-Path $Path).Path
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

Replace-Regex 'event/type.py' '(?ms)(@dataclass\r?\nclass OrderBook:\r?\n    symbol: str\r?\n    exchange: str\r?\n    datetime: datetime\r?\n    asks: Dict\[float, float\] = field\(default_factory=dict\)\r?\n    bids: Dict\[float, float\] = field\(default_factory=dict\)\r?\n)' '$1    exchange_timestamp: float = 0.0`r`n    received_timestamp: float = 0.0`r`n'

Write-Utf8NoBom 'data/orderbook.py' @"
# file: data/orderbook.py

import time
from datetime import datetime

from event.type import OrderBook, OrderBookGapError
from infrastructure.logger import logger


class LocalOrderBook:
    def __init__(self, symbol):
        self.symbol = symbol
        self.bids = {}
        self.asks = {}
        self.last_update_id = 0
        self.initialized = False
        self.last_exchange_ts = 0.0
        self.last_received_ts = 0.0

    def init_snapshot(self, snapshot_data: dict):
        self.bids.clear()
        self.asks.clear()

        for entry in snapshot_data['bids']:
            self.bids[float(entry[0])] = float(entry[1])

        for entry in snapshot_data['asks']:
            self.asks[float(entry[0])] = float(entry[1])

        self.last_update_id = snapshot_data['lastUpdateId']
        self.initialized = True
        logger.info(f"[{self.symbol}] OrderBook Snapshot Loaded. ID={self.last_update_id}")

    def process_delta(self, delta: dict):
        """
        Process Binance incremental depth updates.
        """
        u = delta['u']
        U = delta['U']
        pu = delta['pu']

        if not self.initialized:
            return

        if u < self.last_update_id:
            return

        if pu != self.last_update_id:
            if U <= self.last_update_id and u >= self.last_update_id:
                pass
            else:
                logger.error(f"[{self.symbol}] OrderBook Gap Detected! Local={self.last_update_id}, Remote_PU={pu}")
                self.initialized = False
                raise OrderBookGapError(f"Gap detected for {self.symbol}")

        for entry in delta['b']:
            price = float(entry[0])
            qty = float(entry[1])
            if qty == 0:
                if price in self.bids:
                    del self.bids[price]
            else:
                self.bids[price] = qty

        for entry in delta['a']:
            price = float(entry[0])
            qty = float(entry[1])
            if qty == 0:
                if price in self.asks:
                    del self.asks[price]
            else:
                self.asks[price] = qty

        self.last_update_id = u
        self.last_exchange_ts = self._extract_exchange_ts(delta)
        self.last_received_ts = time.time()

    def generate_event_data(self):
        if not self.initialized:
            return None

        received_ts = self.last_received_ts or time.time()
        return OrderBook(
            symbol=self.symbol,
            exchange="BINANCE",
            datetime=datetime.fromtimestamp(received_ts),
            bids=self.bids.copy(),
            asks=self.asks.copy(),
            exchange_timestamp=self.last_exchange_ts,
            received_timestamp=received_ts,
        )

    def _extract_exchange_ts(self, delta: dict) -> float:
        raw_ts = delta.get('E') or delta.get('T') or 0
        return float(raw_ts) / 1000.0 if raw_ts else 0.0
"@

Replace-Regex 'risk/manager.py' '(?ms)        orderbook = event\.data\r?\n        latency_ms = max\(0\.0, \(time\.time\(\) - orderbook\.datetime\.timestamp\(\)\) \* 1000\.0\)' '        orderbook = event.data`r`n        exchange_ts = float(getattr(orderbook, "exchange_timestamp", 0.0) or 0.0)`r`n        received_ts = float(getattr(orderbook, "received_timestamp", 0.0) or 0.0)`r`n        reference_ts = exchange_ts or received_ts or orderbook.datetime.timestamp()`r`n        latency_ms = max(0.0, (time.time() - reference_ts) * 1000.0)'

Replace-Regex 'oms/engine.py' '(?ms)    def on_exchange_account_update\(self, event\):\r?\n        update: ExchangeAccountUpdate = event\.data\r?\n        tracked_symbols = set\(self\.config\.get\("symbols", \[\]\)\)\r?\n        tracked_positions = \{\r?\n            symbol: payload\r?\n            for symbol, payload in update\.positions\.items\(\)\r?\n            if not tracked_symbols or symbol in tracked_symbols\r?\n        \}\r?\n\r?\n        with self\.lock:\r?\n            self\.account\.sync_exchange_balance\(\r?\n                update\.wallet_balance,\r?\n                available=update\.available_balance,\r?\n                asset=update\.asset,\r?\n                balances=update\.balances,\r?\n            \)\r?\n            position_drift = self\._collect_exchange_position_drift_locked\(tracked_positions\)\r?\n            has_active_orders = self\._has_active_orders_locked\(tracked_positions\.keys\(\)\)\r?\n\r?\n        if position_drift:\r?\n            self\._audit\(\r?\n                "exchange_account_position_drift",\r?\n                reason=update\.reason,\r?\n                positions=position_drift,\r?\n            \)\r?\n            if not has_active_orders and self\.state != LifecycleState\.HALTED:\r?\n                logger\.warning\(f"\[OMS\] Exchange position drift detected: \{position_drift\}"\)' @"
    def on_exchange_account_update(self, event):
        update: ExchangeAccountUpdate = event.data
        tracked_symbols = set(self.config.get("symbols", []))
        tracked_positions = {
            symbol: payload
            for symbol, payload in update.positions.items()
            if not tracked_symbols or symbol in tracked_symbols
        }

        with self.lock:
            self.account.sync_exchange_balance(
                update.wallet_balance,
                available=update.available_balance,
                asset=update.asset,
                balances=update.balances,
            )
            position_drift = self._collect_exchange_position_drift_locked(
                tracked_positions,
                tracked_symbols,
            )
            has_active_orders = self._has_active_orders_locked(tracked_symbols)

        if not position_drift:
            return

        self._audit(
            "exchange_account_position_drift",
            reason=update.reason,
            positions=position_drift,
        )

        if self.state in {LifecycleState.HALTED, LifecycleState.RECONCILING}:
            return

        if has_active_orders:
            logger.warning(f"[OMS] Exchange position drift detected while orders are active: {position_drift}")
            return

        logger.error(f"[OMS] Exchange position drift detected without active orders: {position_drift}")
        self.trigger_reconcile("Exchange account position drift")
"@

Replace-Regex 'oms/engine.py' '(?ms)    def _collect_exchange_position_drift_locked\(self, exchange_positions\):\r?\n        drift = \{\}\r?\n        for symbol, payload in exchange_positions\.items\(\):\r?\n            local_pos = self\.exposure\.net_positions\.get\(symbol, 0\.0\)\r?\n            exchange_pos = float\(payload\.get\("volume", 0\.0\)\)\r?\n            if abs\(local_pos - exchange_pos\) > 1e-6:\r?\n                drift\[symbol\] = \{\r?\n                    "local": local_pos,\r?\n                    "exchange": exchange_pos,\r?\n                    "entry_price": float\(payload\.get\("entry_price", 0\.0\)\),\r?\n                \}\r?\n        return drift' @"
    def _collect_exchange_position_drift_locked(self, exchange_positions, tracked_symbols=None):
        drift = {}
        symbols = set(tracked_symbols or [])
        symbols.update(exchange_positions.keys())
        symbols.update(
            symbol
            for symbol, volume in self.exposure.net_positions.items()
            if abs(volume) > 1e-6 and (not symbols or symbol in symbols)
        )

        for symbol in symbols:
            local_pos = self.exposure.net_positions.get(symbol, 0.0)
            payload = exchange_positions.get(symbol, {})
            exchange_pos = float(payload.get("volume", 0.0))
            if abs(local_pos - exchange_pos) > 1e-6:
                drift[symbol] = {
                    "local": local_pos,
                    "exchange": exchange_pos,
                    "entry_price": float(payload.get("entry_price", 0.0)),
                }
        return drift
"@

Replace-Regex 'gateway/binance/gateway.py' '(?ms)    def close\(self\):\r?\n        self\.active = False\r?\n        self\.set_state\(GatewayState\.DISCONNECTED\)\r?\n        if self\.session:\r?\n            self\.session\.close\(\)\r?\n        logger\.info\(f"\[{self\.gateway_name}\] Closed\."\)' @"
    def close(self):
        self.active = False
        self.set_state(GatewayState.DISCONNECTED)
        if self.ws:
            self.ws.close()
        if self.session:
            self.session.close()
        logger.info(f"[{self.gateway_name}] Closed.")
"@

Replace-Regex 'gateway/binance/gateway.py' '(?ms)    def on_ws_message\(self, raw_msg\):\r?\n        try:\r?\n            msg = json\.loads\(raw_msg\)\r?\n            event_type = msg\.get\("e"\)\r?\n            if event_type == "ORDER_TRADE_UPDATE":\r?\n                self\._handle_user_update\(msg\)\r?\n                return\r?\n            if event_type == "ACCOUNT_UPDATE":\r?\n                self\._handle_account_update\(msg\)\r?\n                return\r?\n            if "stream" in msg:\r?\n                self\._handle_market_update\(msg\)\r?\n        except Exception:\r?\n            pass\r?\n\r?\n    def on_ws_error\(self, err_msg\):\r?\n        self\.on_log\(err_msg, "ERROR"\)\r?\n\r?\n    def _handle_user_update' @"
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

        if self.state != GatewayState.ERROR:
            self.set_state(GatewayState.ERROR)
        self.event_engine.put(Event(EVENT_SYSTEM_HEALTH, message))

    def _handle_user_update"@

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

Write-Utf8NoBom 'main.py' @"
import json
import os
import sys
import time

from rich.live import Live

from data.cache import data_cache
from data.recorder import DataRecorder
from data.ref_data import ref_data_manager
from event.engine import EventEngine
from event.type import (
    EVENT_ACCOUNT_UPDATE,
    EVENT_AGG_TRADE,
    EVENT_EXCHANGE_ACCOUNT_UPDATE,
    EVENT_EXCHANGE_ORDER_UPDATE,
    EVENT_MARK_PRICE,
    EVENT_ORDERBOOK,
    EVENT_ORDER_SUBMITTED,
    EVENT_ORDER_UPDATE,
    EVENT_POSITION_UPDATE,
    EVENT_STRATEGY_UPDATE,
    EVENT_SYSTEM_HEALTH,
    EVENT_TRADE_UPDATE,
)
from gateway.binance.gateway import BinanceGateway
from infrastructure.logger import logger
from infrastructure.system_health import handle_system_health_event
from infrastructure.time_service import time_service
from infrastructure.watchdog import emit_market_data_stale_if_needed
from oms.engine import OMS
from risk.manager import RiskManager
from strategy.ml_sniper.ml_sniper import MLSniperStrategy
from ui.dashboard import TUIDashboard


def load_config():
    if not os.path.exists("config.json"):
        print("Error: config.json not found.")
        return None
    with open("config.json", "r", encoding="utf-8") as handle:
        return json.load(handle)


def main():
    config = load_config()
    if not config:
        return

    config["system"]["log_console"] = False
    logger.init_logging(config)
    time_service.start(testnet=config["testnet"])
    ref_data_manager.init(testnet=config["testnet"])

    engine = EventEngine()
    dashboard = TUIDashboard()
    logger.set_ui_callback(dashboard.add_log)

    gateway = BinanceGateway(engine, config["api_key"], config["api_secret"], testnet=config["testnet"])
    oms_system = OMS(engine, gateway, config)
    risk_controller = RiskManager(engine, config, oms=oms_system, gateway=gateway)
    strategy = MLSniperStrategy(engine, oms_system)
    recorder = DataRecorder(engine, config["symbols"]) if config.get("record_data", False) else None

    engine.register(EVENT_ORDERBOOK, lambda e: data_cache.update_book(e.data))
    engine.register(EVENT_MARK_PRICE, lambda e: data_cache.update_mark_price(e.data))
    engine.register(EVENT_AGG_TRADE, lambda e: data_cache.update_trade(e.data))

    main.last_tick_time = time.time()
    main.stale_watchdog_triggered = False

    def on_tick(orderbook):
        main.last_tick_time = time.time()
        main.stale_watchdog_triggered = False
        strategy.on_orderbook(orderbook)
        dashboard.update_market(orderbook)

    engine.register(EVENT_ORDERBOOK, lambda e: on_tick(e.data))
    engine.register(EVENT_AGG_TRADE, lambda e: strategy.on_market_trade(e.data))

    engine.register(EVENT_EXCHANGE_ORDER_UPDATE, oms_system.on_exchange_update)
    engine.register(EVENT_EXCHANGE_ACCOUNT_UPDATE, oms_system.on_exchange_account_update)
    engine.register(EVENT_ORDER_SUBMITTED, lambda e: oms_system.order_monitor.on_order_submitted(e))

    engine.register(EVENT_ORDER_UPDATE, lambda e: strategy.on_order(e.data))
    engine.register(EVENT_TRADE_UPDATE, lambda e: strategy.on_trade(e.data))
    engine.register(EVENT_POSITION_UPDATE, lambda e: [strategy.on_position(e.data), dashboard.update_position(e.data)])
    engine.register(EVENT_ACCOUNT_UPDATE, lambda e: [strategy.on_account_update(e.data), dashboard.update_account(e.data)])
    engine.register(EVENT_STRATEGY_UPDATE, lambda e: dashboard.update_strategy(e.data))
    engine.register(EVENT_SYSTEM_HEALTH, lambda e: [strategy.on_system_health(e.data), handle_system_health_event(e, risk_controller)])

    engine.start()
    gateway.connect(config["symbols"])

    time.sleep(3)
    oms_system.bootstrap()

    logger.info("ChronosHFT Core Engine LIVE. (Minimalist Mode)")

    try:
        with Live(dashboard.render(), refresh_per_second=4, screen=True) as live:
            while True:
                live.update(dashboard.render())
                time.sleep(0.1)
                main.stale_watchdog_triggered = emit_market_data_stale_if_needed(
                    engine,
                    main.last_tick_time,
                    main.stale_watchdog_triggered,
                )
    except KeyboardInterrupt:
        logger.info("Shutdown signal received.")
        if recorder:
            recorder.close()
        time_service.stop()
        oms_system.stop()
        engine.stop()
        gateway.close()
        logger.info("ChronosHFT Shutdown Complete.")
        sys.exit(0)


if __name__ == "__main__":
    main()
"@

Replace-Regex 'tests/test_oms_survivability.py' '(?ms)if "requests" not in sys\.modules:\r?\n    requests_stub = types\.ModuleType\("requests"\)\r?\n    requests_stub\.get = lambda \*args, \*\*kwargs: None\r?\n    sys\.modules\["requests"\] = requests_stub' @"
if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")
    requests_stub.get = lambda *args, **kwargs: None
    requests_stub.Session = lambda *args, **kwargs: None
    requests_stub.Request = object
    sys.modules["requests"] = requests_stub
"@

Replace-Regex 'tests/test_exchange_truth_and_risk.py' '(?ms)    EVENT_EXCHANGE_ORDER_UPDATE,\r?\n\)' '    EVENT_EXCHANGE_ORDER_UPDATE,`r`n    EVENT_SYSTEM_HEALTH,`r`n    GatewayState,`r`n)'

Replace-Regex 'tests/test_exchange_truth_and_risk.py' '(?ms)class RiskExecutionTests\(unittest\.TestCase\):' @"
    def test_gateway_ws_parse_failure_emits_system_health_event(self):
        engine = DummyEngine()
        gateway = BinanceGateway.__new__(BinanceGateway)
        gateway.event_engine = engine
        gateway.gateway_name = "BINANCE"
        gateway.state = GatewayState.READY

        gateway.on_ws_message("{bad-json")

        self.assertEqual(engine.events[-1].type, EVENT_SYSTEM_HEALTH)
        self.assertIn("WS_PARSE_ERROR", engine.events[-1].data)

    def test_exchange_account_position_drift_triggers_reconcile_without_active_orders(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = OMS(engine, gateway, self.make_config())
        try:
            oms.exposure.force_sync("BTCUSDT", 1.0, 100.0)
            called = []
            oms.trigger_reconcile = lambda reason, suspicious_oid=None: called.append((reason, suspicious_oid))

            update = ExchangeAccountUpdate(
                asset="USDT",
                wallet_balance=1000.0,
                available_balance=1000.0,
                balances={"USDT": {"wallet_balance": 1000.0, "available_balance": 1000.0}},
                positions={},
                reason="ORDER",
                event_time=1.0,
            )
            oms.on_exchange_account_update(Event(EVENT_EXCHANGE_ACCOUNT_UPDATE, update))

            self.assertEqual(called, [("Exchange account position drift", None)])
        finally:
            oms.stop()

class RiskExecutionTests(unittest.TestCase):
"@

Replace-Regex 'tests/test_exchange_truth_and_risk.py' '(?ms)if __name__ == "__main__":' @"
    def test_latency_limit_uses_exchange_timestamp_over_local_datetime(self):
        engine = DummyEngine()
        gateway = DummyGateway()
        oms = DummyOMS()
        risk = RiskManager(engine, self.make_risk_config(), oms=oms, gateway=gateway)

        fresh_local_time = datetime.now()
        exchange_ts = (fresh_local_time - timedelta(milliseconds=250)).timestamp()
        stale_book = OrderBook(
            symbol="BTCUSDT",
            exchange="BINANCE",
            datetime=fresh_local_time,
            exchange_timestamp=exchange_ts,
        )
        risk.on_orderbook(Event("eOrderBook", stale_book))
        risk.on_orderbook(Event("eOrderBook", stale_book))

        self.assertTrue(risk.kill_switch_triggered)

if __name__ == "__main__":
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
