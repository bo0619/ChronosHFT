import threading
import time

from infrastructure.logger import logger


class TruthMonitor:
    def __init__(self, oms, snapshot_provider, config, start_thread=True):
        self.oms = oms
        self.snapshot_provider = snapshot_provider

        cfg = config.get("oms", {}).get("truth_monitor", {})
        self.poll_interval_sec = float(cfg.get("poll_interval_sec", 5.0))
        self.api_freeze_threshold = max(1, int(cfg.get("api_freeze_threshold", 2)))
        self.api_halt_threshold = max(
            self.api_freeze_threshold,
            int(cfg.get("api_halt_threshold", max(4, self.api_freeze_threshold + 1))),
        )
        self.account_balance_tolerance = float(cfg.get("account_balance_tolerance", 1.0))
        self.clean_polls_to_clear = max(1, int(cfg.get("clean_polls_to_clear", 2)))

        self.consecutive_api_failures = 0
        self.clean_polls = 0
        self.active = False
        self.thread = None

        if start_thread and self.poll_interval_sec > 0:
            self.start()

    def start(self):
        if self.active or self.poll_interval_sec <= 0:
            return
        self.active = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.active = False

    def _loop(self):
        while self.active:
            time.sleep(self.poll_interval_sec)
            try:
                self.poll_once()
            except Exception as exc:
                logger.error(f"[TruthMonitor] Poll failed: {exc}")

    def _venue_name(self):
        return getattr(
            self.snapshot_provider,
            "gateway_name",
            getattr(self.oms.gateway, "gateway_name", "UNKNOWN"),
        )

    def poll_once(self):
        account = self.snapshot_provider.get_account_info()
        positions = self.snapshot_provider.get_all_positions()
        open_orders = self.snapshot_provider.get_open_orders()

        if account is None or positions is None or open_orders is None:
            return self._handle_api_failure()

        self._handle_api_recovery()
        return self._compare_truth(account, positions, open_orders)

    def _handle_api_failure(self):
        self.consecutive_api_failures += 1
        self.clean_polls = 0
        venue = self._venue_name()
        logger.error(
            f"[TruthMonitor] Snapshot unavailable "
            f"({self.consecutive_api_failures}/{self.api_halt_threshold})"
        )

        if self.consecutive_api_failures >= self.api_freeze_threshold:
            self.oms.freeze_venue(
                venue,
                f"truth_plane:api_unreachable:{self.consecutive_api_failures}",
                cancel_active_orders=False,
            )

        if self.consecutive_api_failures >= self.api_halt_threshold:
            self.oms.halt_system("Truth plane unavailable")
        return False

    def _handle_api_recovery(self):
        self.consecutive_api_failures = 0
        venue = self._venue_name()
        venue_reason = self.oms.get_venue_freeze_reason(venue)
        if venue_reason.startswith("truth_plane:api_unreachable"):
            self.oms.clear_venue_freeze(venue, reason="truth plane API recovered")

    def _compare_truth(self, account, positions, open_orders):
        tracked_symbols = set(self.oms.config.get("symbols", []))
        exchange_positions = {}
        for pos in positions:
            symbol = pos.get("symbol")
            if not symbol:
                continue
            exchange_positions[symbol] = {
                "volume": float(pos.get("positionAmt", 0.0) or 0.0),
                "entry_price": float(pos.get("entryPrice", 0.0) or 0.0),
            }

        with self.oms.lock:
            local_active_orders = self.oms._collect_local_active_orders_locked()
            position_drift = self.oms._collect_exchange_position_drift_locked(
                exchange_positions,
                tracked_symbols,
            )
            local_balance = float(getattr(self.oms.account, "balance", 0.0) or 0.0)

        remote_active_orders = self.oms._normalize_remote_open_orders(open_orders)
        if local_active_orders != remote_active_orders:
            impacted_symbols = {
                item["symbol"]
                for item in local_active_orders + remote_active_orders
                if item.get("symbol")
            }
            for symbol in impacted_symbols:
                self.oms.freeze_symbol(
                    symbol,
                    "truth_plane:open_order_mismatch",
                    cancel_active_orders=True,
                )
            self.oms.trigger_reconcile("Truth plane open order mismatch")
            self.clean_polls = 0
            return False

        if position_drift:
            for symbol in position_drift:
                self.oms.freeze_symbol(
                    symbol,
                    "truth_plane:position_mismatch",
                    cancel_active_orders=True,
                )
            self.oms.trigger_reconcile("Truth plane position mismatch")
            self.clean_polls = 0
            return False

        remote_balance = float(account.get("totalWalletBalance", 0.0) or 0.0)
        if abs(local_balance - remote_balance) > self.account_balance_tolerance:
            self.oms.freeze_venue(
                self._venue_name(),
                f"truth_plane:balance_drift:{remote_balance - local_balance:+.6f}",
                cancel_active_orders=False,
            )
            self.oms.trigger_reconcile("Truth plane account balance drift")
            self.clean_polls = 0
            return False

        self.clean_polls += 1
        if self.clean_polls >= self.clean_polls_to_clear:
            self.oms.clear_transient_guards(prefixes=("truth_plane:",))
        return True
