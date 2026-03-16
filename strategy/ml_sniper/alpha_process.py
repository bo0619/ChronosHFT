from collections import defaultdict
from collections import deque
from datetime import datetime
from multiprocessing import get_context
from queue import Empty, Full
import time

from alpha.engine import FeatureEngine
from alpha.factors import GLFTCalibrator
from event.type import AggTradeData, OrderBook
from infrastructure.logger import logger

from .predictor import TimeHorizonPredictor


def _serialize_orderbook(orderbook: OrderBook) -> dict:
    top_bids = sorted(orderbook.bids.items(), key=lambda item: item[0], reverse=True)[:5]
    top_asks = sorted(orderbook.asks.items(), key=lambda item: item[0])[:5]
    return {
        "symbol": orderbook.symbol,
        "exchange": orderbook.exchange,
        "datetime_ts": orderbook.datetime.timestamp(),
        "bids": [(float(price), float(volume)) for price, volume in top_bids],
        "asks": [(float(price), float(volume)) for price, volume in top_asks],
        "exchange_timestamp": float(getattr(orderbook, "exchange_timestamp", 0.0) or 0.0),
        "received_timestamp": float(getattr(orderbook, "received_timestamp", 0.0) or 0.0),
        "wall_time": time.time(),
    }


def _deserialize_orderbook(payload: dict) -> OrderBook:
    return OrderBook(
        symbol=payload["symbol"],
        exchange=payload.get("exchange", ""),
        datetime=datetime.fromtimestamp(payload.get("datetime_ts", time.time())),
        bids={float(price): float(volume) for price, volume in payload.get("bids", ())},
        asks={float(price): float(volume) for price, volume in payload.get("asks", ())},
        exchange_timestamp=float(payload.get("exchange_timestamp", 0.0) or 0.0),
        received_timestamp=float(payload.get("received_timestamp", 0.0) or 0.0),
    )


def _serialize_trade(trade: AggTradeData) -> dict:
    return {
        "symbol": trade.symbol,
        "trade_id": int(trade.trade_id),
        "price": float(trade.price),
        "quantity": float(trade.quantity),
        "maker_is_buyer": bool(trade.maker_is_buyer),
        "datetime_ts": trade.datetime.timestamp(),
    }


def _deserialize_trade(payload: dict) -> AggTradeData:
    return AggTradeData(
        symbol=payload["symbol"],
        trade_id=int(payload.get("trade_id", 0)),
        price=float(payload.get("price", 0.0) or 0.0),
        quantity=float(payload.get("quantity", 0.0) or 0.0),
        maker_is_buyer=bool(payload.get("maker_is_buyer", False)),
        datetime=datetime.fromtimestamp(payload.get("datetime_ts", time.time())),
    )


def _worker_main(in_queue, out_queue, config: dict):
    feature_engine = FeatureEngine()
    predictors = {}
    calibrators = {}
    latest_mid = defaultdict(float)
    last_tick_ts = defaultdict(float)
    last_cycle_ts = defaultdict(float)

    tick_interval = float(config.get("tick_interval_sec", 0.1))
    cycle_interval = float(config.get("cycle_interval_sec", 1.0))
    label_config = dict(config.get("labeling", {}) or {})

    def get_predictor(symbol: str):
        predictor = predictors.get(symbol)
        if predictor is None:
            predictor = TimeHorizonPredictor(num_features=9, label_config=label_config)
            predictors[symbol] = predictor
        return predictor

    def get_calibrator(symbol: str):
        calibrator = calibrators.get(symbol)
        if calibrator is None:
            calibrator = GLFTCalibrator(window=500)
            calibrators[symbol] = calibrator
        return calibrator

    while True:
        message = in_queue.get()
        kind = message.get("kind")
        if kind == "stop":
            return

        if kind == "trade":
            trade = _deserialize_trade(message["payload"])
            feature_engine.on_trade(trade)
            current_mid = latest_mid.get(trade.symbol, 0.0)
            if current_mid > 0.0:
                get_calibrator(trade.symbol).on_market_trade(trade, current_mid)
            continue

        if kind != "orderbook":
            continue

        orderbook = _deserialize_orderbook(message["payload"])
        now = float(message["payload"].get("wall_time", time.time()))
        symbol = orderbook.symbol
        if last_tick_ts[symbol] and now - last_tick_ts[symbol] < tick_interval:
            continue
        last_tick_ts[symbol] = now

        bid_1, _ = orderbook.get_best_bid()
        ask_1, _ = orderbook.get_best_ask()
        if bid_1 <= 0 or ask_1 <= 0:
            continue

        mid = (bid_1 + ask_1) / 2.0
        latest_mid[symbol] = mid

        feature_engine.on_orderbook(orderbook)
        calibrator = get_calibrator(symbol)
        calibrator.on_orderbook(orderbook)
        predictor = get_predictor(symbol)

        feats = feature_engine.get_features(symbol)
        spread_bps = max(0.0, (ask_1 - bid_1) / mid * 10000.0) if mid > 0 else 0.0
        sigma_bps = float(max(0.0, getattr(calibrator, "sigma_bps", 10.0)))
        preds = predictor.update_and_predict(feats, mid, now, spread_bps=spread_bps, sigma_bps=sigma_bps)

        if now - last_cycle_ts[symbol] >= cycle_interval:
            last_cycle_ts[symbol] = now
            feature_engine.reset_interval(symbol)

        diagnostics = predictor.get_last_diagnostics()
        output = {
            "kind": "alpha_snapshot",
            "symbol": symbol,
            "now": now,
            "bid_1": bid_1,
            "ask_1": ask_1,
            "mid": mid,
            "preds": {horizon: float(preds.get(horizon, 0.0)) for horizon in predictor.horizons},
            "spread_bps": spread_bps,
            "sigma_bps": sigma_bps,
            "diagnostics": diagnostics,
            "weights_1s": predictor.get_model_weights("1s"),
            "warmup_progress": predictor.warmup_progress(),
            "predictor_warmed_up": predictor.is_warmed_up,
        }
        try:
            out_queue.put_nowait(output)
        except Full:
            pass


class MLSniperAlphaProcess:
    def __init__(self, config: dict = None):
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", False))
        self.queue_size = max(16, int(self.config.get("queue_size", 256)))
        self.worker_count = max(1, int(self.config.get("processes", 1)))
        self.auto_restart = bool(self.config.get("auto_restart", True))
        self.restart_cooldown_sec = max(
            0.5,
            float(self.config.get("restart_cooldown_sec", 2.0) or 2.0),
        )
        self.max_restart_burst = max(1, int(self.config.get("max_restart_burst", 3)))
        self.restart_window_sec = max(
            self.restart_cooldown_sec,
            float(self.config.get("restart_window_sec", 30.0) or 30.0),
        )
        self.quarantine_sec = max(
            self.restart_cooldown_sec,
            float(self.config.get("quarantine_sec", 30.0) or 30.0),
        )
        self._context = get_context("spawn")
        self._workers = []
        self._symbol_worker = {}
        self._worker_symbols = defaultdict(set)
        self._recovering_symbols = set()
        self._restart_events = set()
        self._quarantined_symbols = set()
        self._quarantine_events = set()
        self._stopping = False
        self._stats = {
            "submitted": 0,
            "deferred": 0,
            "flushed": 0,
            "results": 0,
            "alive_workers": 0,
            "worker_count": self.worker_count,
            "restarts": 0,
            "restart_failures": 0,
        }

    def start(self):
        if not self.enabled:
            return False
        if self._workers and all(worker["process"].is_alive() for worker in self._workers if worker["process"]):
            return True

        self._stopping = False
        self._workers = []
        self._recovering_symbols.clear()
        self._restart_events.clear()
        self._quarantined_symbols.clear()
        self._quarantine_events.clear()
        started_workers = 0
        for worker_id in range(self.worker_count):
            worker = self._new_worker_state(worker_id)
            try:
                self._start_worker(worker, self._worker_config("primary"), profile_name="primary")
                self._workers.append(worker)
                started_workers += 1
            except Exception as exc:
                logger.error(
                    f"[MLSniperAlphaProcess] worker {worker_id} start failed, falling back inline: {exc}"
                )
                self._workers = []
                self.enabled = False
                self._stats["alive_workers"] = 0
                return False

        self._stats["alive_workers"] = started_workers
        logger.info(f"[MLSniperAlphaProcess] started workers={started_workers}")
        return started_workers > 0

    def stop(self):
        if not self._workers:
            return
        self._stopping = True
        for worker in self._workers:
            try:
                worker["in_queue"].put({"kind": "stop"}, timeout=0.2)
            except Exception:
                pass
        for worker in self._workers:
            process = worker["process"]
            process.join(timeout=2.0)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1.0)
        self._stats["alive_workers"] = 0
        self._recovering_symbols.clear()
        self._restart_events.clear()
        self._quarantined_symbols.clear()
        self._quarantine_events.clear()
        logger.info("[MLSniperAlphaProcess] stopped")

    def submit_orderbook(self, orderbook: OrderBook):
        if not self.enabled:
            return False
        symbol = orderbook.symbol.upper()
        worker_index = self._worker_index(symbol)
        return self._submit(
            worker_index,
            ("orderbook", symbol),
            {"kind": "orderbook", "payload": _serialize_orderbook(orderbook)},
        )

    def submit_trade(self, trade: AggTradeData):
        if not self.enabled:
            return False
        symbol = trade.symbol.upper()
        worker_index = self._worker_index(symbol)
        return self._submit(
            worker_index,
            ("trade", symbol),
            {"kind": "trade", "payload": _serialize_trade(trade)},
        )

    def poll(self, limit: int = 32):
        if not self.enabled or not self._workers:
            return []
        self._restart_dead_workers()
        results = []
        budget_per_worker = max(1, limit // max(len(self._workers), 1))
        alive_workers = 0
        for worker in self._workers:
            if worker["process"].is_alive():
                alive_workers += 1
            self._flush_deferred(worker, limit=budget_per_worker)
            for _ in range(budget_per_worker):
                try:
                    result = worker["out_queue"].get_nowait()
                except Empty:
                    break
                results.append(result)
                self._stats["results"] += 1
        self._stats["alive_workers"] = alive_workers
        return results

    def is_healthy(self) -> bool:
        if not self.enabled:
            return True
        return bool(self._workers) and all(worker["process"].is_alive() for worker in self._workers)

    def get_metrics_snapshot(self):
        snapshot = dict(self._stats)
        snapshot["alive"] = self.is_healthy()
        snapshot["deferred_depth"] = sum(len(worker["deferred"]) for worker in self._workers)
        snapshot["recovering_symbols"] = len(self._recovering_symbols)
        snapshot["quarantined_symbols"] = len(self._quarantined_symbols)
        snapshot["standby_workers"] = sum(
            1 for worker in self._workers if worker.get("profile_name") == "standby"
        )
        snapshot["workers"] = [
            {
                "worker_id": worker["worker_id"],
                "alive": worker["process"].is_alive(),
                "pid": int(getattr(worker["process"], "pid", 0) or 0),
                "deferred_depth": len(worker["deferred"]),
                "symbol_count": len(self._worker_symbols.get(worker["worker_id"], set())),
                "generation": int(worker.get("generation", 0)),
                "profile_name": str(worker.get("profile_name", "primary")),
                "quarantined": bool(worker.get("quarantined_until", 0.0) > time.time()),
            }
            for worker in self._workers
        ]
        return snapshot

    def get_unhealthy_symbols(self):
        unhealthy = set()
        for worker in self._workers:
            if not worker["process"].is_alive():
                unhealthy.update(self._worker_symbols.get(worker["worker_id"], set()))
        return unhealthy

    def get_recovering_symbols(self):
        return set(self._recovering_symbols)

    def get_quarantined_symbols(self):
        return set(self._quarantined_symbols)

    def drain_restart_events(self):
        restarted = set(self._restart_events)
        self._restart_events.clear()
        return restarted

    def drain_quarantine_events(self):
        quarantined = set(self._quarantine_events)
        self._quarantine_events.clear()
        return quarantined

    def mark_symbol_recovered(self, symbol: str):
        symbol = (symbol or "").upper()
        if not symbol:
            return
        self._recovering_symbols.discard(symbol)

    def _worker_index(self, symbol: str) -> int:
        symbol = (symbol or "").upper()
        if symbol in self._symbol_worker:
            return self._symbol_worker[symbol]
        index = sum(ord(ch) for ch in symbol) % max(self.worker_count, 1)
        self._symbol_worker[symbol] = index
        self._worker_symbols[index].add(symbol)
        return index

    def _submit(self, worker_index: int, key, message):
        if worker_index >= len(self._workers):
            return False
        worker = self._workers[worker_index]
        process = worker["process"]
        if not process or not process.is_alive():
            self._restart_worker(worker)
            process = worker["process"]
        if not process or not process.is_alive():
            self._stats["alive_workers"] = sum(1 for item in self._workers if item["process"].is_alive())
            worker["deferred"][key] = message
            self._stats["deferred"] += 1
            return False
        try:
            worker["in_queue"].put_nowait(message)
            self._stats["submitted"] += 1
            return True
        except Full:
            worker["deferred"][key] = message
            self._stats["deferred"] += 1
            return False

    def _flush_deferred(self, worker: dict, limit: int = 16):
        if not worker["deferred"] or not worker["process"] or not worker["process"].is_alive():
            return
        keys = list(worker["deferred"].keys())[:limit]
        for key in keys:
            try:
                worker["in_queue"].put_nowait(worker["deferred"][key])
            except Full:
                break
            del worker["deferred"][key]
            self._stats["flushed"] += 1

    def _new_worker_state(self, worker_id: int) -> dict:
        return {
            "worker_id": worker_id,
            "in_queue": None,
            "out_queue": None,
            "process": None,
            "deferred": {},
            "generation": 0,
            "last_restart_attempt_ts": 0.0,
            "last_restart_ts": 0.0,
            "restart_history": deque(),
            "quarantined_until": 0.0,
            "profile_name": "primary",
            "next_profile_name": "primary",
        }

    def _start_worker(self, worker: dict, worker_config: dict, profile_name: str = "primary"):
        in_queue = self._context.Queue(maxsize=self.queue_size)
        out_queue = self._context.Queue(maxsize=self.queue_size)
        process = self._context.Process(
            target=_worker_main,
            args=(in_queue, out_queue, worker_config),
            daemon=True,
            name=f"MLSniperAlphaProcess-{worker['worker_id']}",
        )
        process.start()
        worker["in_queue"] = in_queue
        worker["out_queue"] = out_queue
        worker["process"] = process
        worker["generation"] = int(worker.get("generation", 0)) + 1
        worker["last_restart_attempt_ts"] = time.time()
        worker["profile_name"] = profile_name

    def _restart_dead_workers(self):
        for worker in self._workers:
            process = worker.get("process")
            if process and process.is_alive():
                continue
            self._restart_worker(worker)

    def _restart_worker(self, worker: dict):
        if self._stopping or not self.auto_restart:
            return False

        process = worker.get("process")
        if process and process.is_alive():
            return True

        now = time.time()
        if self._is_worker_quarantined(worker, now):
            return False

        last_attempt = float(worker.get("last_restart_attempt_ts", 0.0) or 0.0)
        if last_attempt and now - last_attempt < self.restart_cooldown_sec:
            return False

        restart_history = self._trim_restart_history(worker, now)
        if len(restart_history) >= self.max_restart_burst:
            self._enter_quarantine(worker, now, reason="restart_burst")
            return False

        profile_name = str(worker.get("next_profile_name", "") or worker.get("profile_name", "primary"))
        worker_config = self._worker_config(profile_name)
        try:
            self._start_worker(worker, worker_config, profile_name=profile_name)
        except Exception as exc:
            worker["last_restart_attempt_ts"] = now
            self._stats["restart_failures"] += 1
            logger.error(
                f"[MLSniperAlphaProcess] worker {worker['worker_id']} restart failed: {exc}"
            )
            return False

        worker["last_restart_ts"] = now
        restart_history.append(now)
        recovered_symbols = set(self._worker_symbols.get(worker["worker_id"], set()))
        if recovered_symbols:
            self._quarantined_symbols.difference_update(recovered_symbols)
            self._recovering_symbols.update(recovered_symbols)
            self._restart_events.update(recovered_symbols)
        self._stats["restarts"] += 1
        logger.warning(
            f"[MLSniperAlphaProcess] worker {worker['worker_id']} restarted profile={profile_name}, recovering={sorted(recovered_symbols)}"
        )
        return True

    def _worker_config(self, profile_name: str = "primary") -> dict:
        base_config = {
            "tick_interval_sec": float(self.config.get("tick_interval_sec", 0.1)),
            "cycle_interval_sec": float(self.config.get("cycle_interval_sec", 1.0)),
            "labeling": dict(self.config.get("labeling", {}) or {}),
        }
        if profile_name != "standby":
            return base_config

        standby_config = dict(self.config.get("standby_profile", {}) or {})
        labeling = dict(base_config["labeling"])
        labeling.update(dict(standby_config.get("labeling", {}) or {}))
        return {
            "tick_interval_sec": float(
                standby_config.get(
                    "tick_interval_sec",
                    max(base_config["tick_interval_sec"] * 2.0, base_config["tick_interval_sec"], 0.25),
                )
            ),
            "cycle_interval_sec": float(
                standby_config.get(
                    "cycle_interval_sec",
                    max(base_config["cycle_interval_sec"] * 2.0, base_config["cycle_interval_sec"]),
                )
            ),
            "labeling": labeling,
        }

    def _trim_restart_history(self, worker: dict, now: float):
        history = worker.setdefault("restart_history", deque())
        cutoff = now - self.restart_window_sec
        while history and history[0] < cutoff:
            history.popleft()
        return history

    def _is_worker_quarantined(self, worker: dict, now: float) -> bool:
        quarantined_until = float(worker.get("quarantined_until", 0.0) or 0.0)
        if quarantined_until <= now:
            if quarantined_until > 0.0:
                worker["quarantined_until"] = 0.0
            return False
        return True

    def _enter_quarantine(self, worker: dict, now: float, reason: str):
        worker["quarantined_until"] = max(
            float(worker.get("quarantined_until", 0.0) or 0.0),
            now + self.quarantine_sec,
        )
        worker["next_profile_name"] = "standby"
        worker["last_restart_attempt_ts"] = now
        worker["restart_history"] = deque()
        affected_symbols = set(self._worker_symbols.get(worker["worker_id"], set()))
        if affected_symbols:
            self._recovering_symbols.difference_update(affected_symbols)
            self._quarantined_symbols.update(affected_symbols)
            self._quarantine_events.update(affected_symbols)
        logger.error(
            f"[MLSniperAlphaProcess] worker {worker['worker_id']} quarantined reason={reason}, symbols={sorted(affected_symbols)}"
        )
