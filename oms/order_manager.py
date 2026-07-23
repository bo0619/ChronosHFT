import threading
import time

from infrastructure.logger import logger
from event.type import OrderSubmitted, OrderStatus


class OrderManager:
    def __init__(self, engine, gateway, dirty_callback=None, monitor_config=None, start_thread=True):
        self.engine = engine
        self.gateway = gateway
        self.dirty_callback = dirty_callback

        self.monitored_orders = {}
        self.lock = threading.RLock()

        monitor_config = monitor_config or {}
        self.ACK_TIMEOUT = float(monitor_config.get("ack_timeout_sec", 5.0))
        self.ACK_TIMEOUT_RECHECK = float(monitor_config.get("ack_timeout_recheck_sec", 60.0))
        self.UNKNOWN_RECHECK = float(monitor_config.get("unknown_recheck_sec", 1.0))
        self.CANCEL_TIMEOUT = float(monitor_config.get("cancel_timeout_sec", 5.0))
        self.ACTIVE_ORDER_AUDIT_INTERVAL = float(
            monitor_config.get("active_order_audit_interval_sec", 30.0)
        )
        self.CHECK_INTERVAL = float(monitor_config.get("monitor_check_interval_sec", 1.0))

        self.active = True
        self.check_thread = None
        if start_thread:
            self.check_thread = threading.Thread(target=self._check_loop, daemon=True)
            self.check_thread.start()

    def on_order_submitted(self, event):
        data: OrderSubmitted = event.data
        submitted_monotonic = float(
            getattr(data, "monotonic_timestamp", 0.0) or data.timestamp
        )
        with self.lock:
            self.monitored_orders[data.order_id] = {
                "symbol": data.req.symbol,
                "submit_time": submitted_monotonic,
                "last_ack_time": 0.0,
                "status": data.status,
                "ack_timeout_reported": False,
                "last_timeout_reported_at": 0.0,
            }

    def recover_order(self, order):
        """Resume truth auditing for an active order restored from the ledger."""
        recovered_at = time.perf_counter()
        with self.lock:
            self.monitored_orders[order.client_oid] = {
                "symbol": order.intent.symbol,
                "submit_time": recovered_at,
                "last_ack_time": recovered_at,
                "status": order.status,
                "ack_timeout_reported": False,
                "last_timeout_reported_at": 0.0,
            }

    def on_order_update(self, order_id, status):
        with self.lock:
            if order_id not in self.monitored_orders:
                return

            if status in {
                OrderStatus.FILLED,
                OrderStatus.CANCELLED,
                OrderStatus.REJECTED,
                OrderStatus.REJECTED_LOCALLY,
                OrderStatus.EXPIRED,
            }:
                del self.monitored_orders[order_id]
                return

            self.monitored_orders[order_id]["status"] = status
            if status in {
                OrderStatus.NEW,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.CANCELLING,
                OrderStatus.SUBMIT_UNKNOWN,
                OrderStatus.CANCEL_UNKNOWN,
            }:
                self.monitored_orders[order_id]["last_ack_time"] = time.perf_counter()
                self.monitored_orders[order_id]["ack_timeout_reported"] = False
                self.monitored_orders[order_id]["last_timeout_reported_at"] = 0.0

    def _check_once(self, now=None):
        now = time.perf_counter() if now is None else now
        suspicious_oid = None

        with self.lock:
            for oid, info in list(self.monitored_orders.items()):
                status = info["status"]
                reference_time = info["last_ack_time"] or info["submit_time"]
                elapsed = now - reference_time

                if status == OrderStatus.SUBMIT_UNKNOWN:
                    timeout = self.UNKNOWN_RECHECK
                    reason = "Order submit outcome unknown"
                elif status in {OrderStatus.CANCELLING, OrderStatus.CANCEL_UNKNOWN}:
                    timeout = self.UNKNOWN_RECHECK if status == OrderStatus.CANCEL_UNKNOWN else self.CANCEL_TIMEOUT
                    reason = "Order cancel outcome unknown"
                elif status in {OrderStatus.SUBMITTING, OrderStatus.PENDING_ACK}:
                    timeout = self.ACK_TIMEOUT
                    reason = "Order ACK Timeout"
                elif status in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}:
                    timeout = self.ACTIVE_ORDER_AUDIT_INTERVAL
                    reason = "Active order truth audit"
                else:
                    continue

                if elapsed <= timeout:
                    continue
                if info["ack_timeout_reported"]:
                    elapsed_since_report = now - info["last_timeout_reported_at"]
                    recheck = (
                        self.UNKNOWN_RECHECK
                        if status in {OrderStatus.SUBMIT_UNKNOWN, OrderStatus.CANCEL_UNKNOWN}
                        else self.ACK_TIMEOUT_RECHECK
                    )
                    if elapsed_since_report < recheck:
                        continue

                logger.error(f"[OMS] {reason}: {oid}. Verifying exchange truth.")
                info["ack_timeout_reported"] = True
                info["last_timeout_reported_at"] = now
                suspicious_oid = (oid, reason)
                break

        if suspicious_oid and self.dirty_callback:
            oid, reason = suspicious_oid
            self.dirty_callback(reason, suspicious_oid=oid)

    def _check_loop(self):
        while self.active:
            time.sleep(self.CHECK_INTERVAL)
            self._check_once()

    def stop(self):
        self.active = False
