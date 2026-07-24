import threading
import time
from copy import deepcopy
from decimal import Decimal

from infrastructure.commission_truth import parse_commission_rate_payload
from infrastructure.logger import logger
from infrastructure.paper_trade import is_paper_trade
from infrastructure.rpi_policy import (
    effective_rpi_route_enabled,
    requires_zero_rpi_commission,
)


class TruthMonitor:
    def __init__(self, oms, snapshot_provider, config, start_thread=True):
        self.oms = oms
        self.snapshot_provider = snapshot_provider
        self.config = config or {}
        self.is_testnet = bool(self.config.get("testnet", False))

        cfg = config.get("oms", {}).get("truth_monitor", {})
        self.poll_interval_sec = float(cfg.get("poll_interval_sec", 5.0))
        self.api_freeze_threshold = max(1, int(cfg.get("api_freeze_threshold", 2)))
        self.api_halt_threshold = max(
            self.api_freeze_threshold,
            int(cfg.get("api_halt_threshold", max(4, self.api_freeze_threshold + 1))),
        )
        base_balance_tolerance = float(cfg.get("account_balance_tolerance", 1.0))
        if self.is_testnet:
            base_balance_tolerance = max(
                base_balance_tolerance,
                float(cfg.get("testnet_account_balance_tolerance", 5.0) or 5.0),
            )
        self.account_balance_tolerance = base_balance_tolerance
        self.balance_drift_trigger_count = max(
            1,
            int(cfg.get("balance_drift_trigger_count", 3 if self.is_testnet else 2)),
        )
        self.ignore_flat_balance_drift = bool(
            cfg.get("ignore_flat_balance_drift", self.is_testnet)
        )
        self.clean_polls_to_clear = max(1, int(cfg.get("clean_polls_to_clear", 2)))

        cash_flow_cfg = config.get("risk", {}).get("cash_flow_truth", {})
        self.cash_flow_truth_enabled = bool(cash_flow_cfg.get("enabled", False))
        self.cash_flow_poll_interval_sec = max(
            1.0,
            float(cash_flow_cfg.get("poll_interval_sec", 30.0) or 30.0),
        )
        execution = self.config.get("execution", {})
        execution_mode = (
            str(execution.get("mode", "") or "").strip().lower()
            if isinstance(execution, dict)
            else ""
        )
        self.rpi_commission_truth_required = bool(
            execution_mode == "live"
            and not self.is_testnet
            and not is_paper_trade(self.config)
            and effective_rpi_route_enabled(self.config)
            and requires_zero_rpi_commission(self.config)
        )
        self.rpi_commission_poll_interval_sec = min(
            60.0,
            max(
                5.0,
                float(
                    cfg.get(
                        "rpi_commission_poll_interval_sec",
                        30.0,
                    )
                    or 30.0
                ),
            ),
        )
        self.rpi_commission_halt_threshold = min(
            2,
            max(
                1,
                int(cfg.get("rpi_commission_halt_threshold", 2) or 2),
            ),
        )
        self.rpi_commission_clean_polls_to_clear = max(
            2,
            int(
                cfg.get(
                    "rpi_commission_clean_polls_to_clear",
                    2,
                )
                or 2
            ),
        )
        self.last_cash_flow_poll_at = 0.0
        self.last_rpi_commission_poll_monotonic = 0.0
        self.last_rpi_commission_rates = {}
        self.last_account_snapshot = None
        self.last_positions_snapshot = None
        self.last_open_orders_snapshot = None
        self.last_account_snapshot_monotonic = 0.0

        self.consecutive_api_failures = 0
        self.consecutive_balance_drifts = 0
        self.consecutive_rpi_commission_failures = 0
        self.clean_rpi_commission_polls = 0
        self.clean_polls = 0
        self.active = False
        self.thread = None
        self._stop_event = threading.Event()

        if start_thread and self.poll_interval_sec > 0:
            self.start()

    def start(self):
        if self.active or self.poll_interval_sec <= 0:
            return
        self.active = True
        self._stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.active = False
        self._stop_event.set()
        thread = self.thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.poll_interval_sec + 0.5))
        stopped = not thread or not thread.is_alive()
        if not stopped:
            logger.critical(
                "[TruthMonitor] Poll worker did not stop before timeout"
            )
        return stopped

    def _loop(self):
        while self.active:
            if self._stop_event.wait(self.poll_interval_sec):
                break
            try:
                self.poll_once()
            except Exception as exc:
                self._handle_poll_exception(exc)

    def _handle_poll_exception(self, exc: Exception) -> bool:
        logger.error(
            "[TruthMonitor] Poll raised an exception: "
            f"{type(exc).__name__}:{exc}"
        )
        try:
            return self._handle_api_failure()
        except Exception as guard_exc:
            logger.critical(
                "[TruthMonitor] Could not apply fail-closed API guard: "
                f"{type(guard_exc).__name__}:{guard_exc}"
            )
            return False

    def _venue_name(self):
        return getattr(
            self.snapshot_provider,
            "gateway_name",
            getattr(self.oms.gateway, "gateway_name", "UNKNOWN"),
        )

    def poll_once(self):
        commission_truth_ok = self._poll_rpi_commission_truth()
        try:
            account = self.snapshot_provider.get_account_info()
            positions = self.snapshot_provider.get_all_positions()
            open_orders = self.snapshot_provider.get_open_orders()
        except Exception as exc:
            return self._handle_poll_exception(exc)

        if account is None or positions is None or open_orders is None:
            return self._handle_api_failure()

        self.last_account_snapshot = deepcopy(account)
        self.last_positions_snapshot = deepcopy(positions)
        self.last_open_orders_snapshot = deepcopy(open_orders)
        self.last_account_snapshot_monotonic = time.perf_counter()
        try:
            sync_margin_health = getattr(
                self.oms,
                "sync_account_margin_health",
                None,
            )
            if callable(sync_margin_health):
                sync_margin_health(account, snapshot_time=time.time())
            cash_flow_truth_ok = self._poll_cash_flow_truth()
            exchange_truth_ok = self._compare_truth(
                account,
                positions,
                open_orders,
            )
        except Exception as exc:
            return self._handle_poll_exception(exc)
        self._handle_api_recovery()
        return (
            commission_truth_ok
            and cash_flow_truth_ok
            and exchange_truth_ok
        )

    def _poll_rpi_commission_truth(
        self,
        now_monotonic: float = None,
    ) -> bool:
        if not self.rpi_commission_truth_required:
            return True

        now_monotonic = float(
            now_monotonic
            if now_monotonic is not None
            else time.perf_counter()
        )
        if (
            self.last_rpi_commission_poll_monotonic > 0.0
            and now_monotonic - self.last_rpi_commission_poll_monotonic
            < self.rpi_commission_poll_interval_sec
        ):
            return True
        self.last_rpi_commission_poll_monotonic = now_monotonic

        query = getattr(
            self.snapshot_provider,
            "get_commission_rate",
            None,
        )
        if not callable(query):
            return self._handle_rpi_commission_failure(
                "independent commission endpoint unavailable"
            )

        symbols = tuple(
            dict.fromkeys(
                str(symbol or "").strip().upper()
                for symbol in self.config.get("symbols", ())
                if str(symbol or "").strip()
            )
        )
        if not symbols:
            return self._handle_rpi_commission_failure(
                "configured symbol set is empty"
            )

        rates_by_symbol = {}
        for symbol in symbols:
            try:
                payload = query(symbol)
            except Exception as exc:
                return self._handle_rpi_commission_failure(
                    f"{type(exc).__name__} during {symbol} commission query"
                )
            try:
                rates_by_symbol[symbol] = parse_commission_rate_payload(
                    payload,
                    symbol=symbol,
                )
            except ValueError as exc:
                return self._handle_rpi_commission_failure(str(exc))

        nonzero_rpi_rates = {
            symbol: rates["rpiCommissionRate"]
            for symbol, rates in rates_by_symbol.items()
            if rates["rpiCommissionRate"] != Decimal(0)
        }
        if nonzero_rpi_rates:
            reason = "commission_truth:nonzero_rpi_rate:" + ",".join(
                f"{symbol}={rate}"
                for symbol, rate in sorted(nonzero_rpi_rates.items())
            )
            logger.critical(
                "[TruthMonitor] Account-specific RPI commission is no "
                f"longer zero: {reason}"
            )
            self._record_rpi_commission_truth(
                rates_by_symbol,
                accepted=False,
                reason=reason,
            )
            self.oms.halt_system(reason)
            return False

        self._publish_rpi_commission_truth(rates_by_symbol)
        if not self._record_rpi_commission_truth(
            rates_by_symbol,
            accepted=True,
            reason="",
        ):
            return self._handle_rpi_commission_failure(
                "durable commission-truth audit unavailable"
            )
        self.consecutive_rpi_commission_failures = 0
        self.clean_rpi_commission_polls += 1
        if (
            self.clean_rpi_commission_polls
            >= self.rpi_commission_clean_polls_to_clear
        ):
            self._clear_rpi_commission_guard()
        return True

    def _publish_rpi_commission_truth(self, rates_by_symbol) -> None:
        maker_rates = [
            float(rates["makerCommissionRate"])
            for rates in rates_by_symbol.values()
        ]
        taker_rates = [
            float(rates["takerCommissionRate"])
            for rates in rates_by_symbol.values()
        ]
        rpi_rates = {
            symbol: float(rates["rpiCommissionRate"])
            for symbol, rates in rates_by_symbol.items()
        }
        current_fee_config = self.config.get("backtest", {})
        next_fee_config = (
            dict(current_fee_config)
            if isinstance(current_fee_config, dict)
            else {}
        )
        next_fee_config.update(
            {
                "maker_fee": max(maker_rates),
                "taker_fee": max(taker_rates),
                "rpi_commission_rate": max(rpi_rates.values()),
                "rpi_commission_rates": rpi_rates,
            }
        )
        self.config["backtest"] = next_fee_config
        self.last_rpi_commission_rates = dict(rpi_rates)

    def _record_rpi_commission_truth(
        self,
        rates_by_symbol,
        *,
        accepted: bool,
        reason: str,
    ) -> bool:
        record = getattr(
            self.oms,
            "record_rpi_commission_truth",
            None,
        )
        if not callable(record):
            return False
        try:
            result = record(
                {
                    symbol: {
                        field: str(value)
                        for field, value in rates.items()
                    }
                    for symbol, rates in rates_by_symbol.items()
                },
                accepted=bool(accepted),
                reason=str(reason or ""),
                source="GET /fapi/v1/commissionRate",
            )
        except Exception as exc:
            logger.critical(
                "[TruthMonitor] Could not persist runtime RPI commission "
                f"truth: {type(exc).__name__}:{exc}"
            )
            return False
        return result is not False

    def _handle_rpi_commission_failure(self, detail: str) -> bool:
        self.consecutive_rpi_commission_failures += 1
        self.clean_rpi_commission_polls = 0
        reason = (
            "commission_truth:unavailable:"
            f"{self.consecutive_rpi_commission_failures}:"
            f"{str(detail or 'unknown')}"
        )
        logger.error(
            "[TruthMonitor] Runtime RPI commission truth unavailable "
            f"({self.consecutive_rpi_commission_failures}/"
            f"{self.rpi_commission_halt_threshold}): {detail}"
        )
        self._record_rpi_commission_truth(
            {},
            accepted=False,
            reason=reason,
        )
        self.oms.freeze_venue(
            self._venue_name(),
            reason,
            cancel_active_orders=True,
        )
        if (
            self.consecutive_rpi_commission_failures
            >= self.rpi_commission_halt_threshold
        ):
            self.oms.halt_system(reason)
        return False

    def _clear_rpi_commission_guard(self) -> bool:
        get_owners = getattr(self.oms, "get_venue_freeze_owners", None)
        clear = getattr(self.oms, "clear_venue_freeze", None)
        if not callable(get_owners) or not callable(clear):
            return False
        venue = self._venue_name()
        owners = get_owners(venue)
        record = (
            owners.get("commission_truth", {})
            if isinstance(owners, dict)
            else {}
        )
        reason = str(record.get("reason", "") or "")
        epoch = int(record.get("epoch", 0) or 0)
        if not reason or epoch <= 0:
            return False
        return bool(
            clear(
                venue,
                reason="commission truth restored after two clean polls",
                expected_epoch=epoch,
                expected_reason=reason,
                expected_owner="commission_truth",
            )
        )

    def _poll_cash_flow_truth(self, now: float = None) -> bool:
        if not self.cash_flow_truth_enabled:
            return True

        now = float(now or time.time())
        if now - self.last_cash_flow_poll_at < self.cash_flow_poll_interval_sec:
            return True
        self.last_cash_flow_poll_at = now

        query = getattr(self.snapshot_provider, "get_income_history", None)
        backfill = getattr(self.oms, "backfill_external_cash_flow_history", None)
        if not callable(query) or not callable(backfill):
            mark_unavailable = getattr(
                self.oms,
                "mark_external_cash_flow_truth_unavailable",
                None,
            )
            if callable(mark_unavailable):
                mark_unavailable("truth_plane_income_query_unavailable")
            return False

        return bool(
            backfill(
                query=query,
                end_time_ms=int(now * 1000),
            )
        )

    def _handle_api_failure(self):
        self.consecutive_api_failures += 1
        self.clean_polls = 0
        if self.cash_flow_truth_enabled:
            mark_unavailable = getattr(
                self.oms,
                "mark_external_cash_flow_truth_unavailable",
                None,
            )
            if callable(mark_unavailable):
                mark_unavailable("truth_plane_snapshot_unavailable")
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

    def _compare_truth(self, account, positions, open_orders):
        tracked_symbols = set(self.oms.config.get("symbols", []))
        tracked_assets = self._tracked_assets(tracked_symbols)
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
            local_balance, _local_source = self._local_balance_value(tracked_assets)

        remote_active_orders = self.oms._normalize_remote_open_orders(open_orders)
        off_config_symbols = {
            item["symbol"]
            for item in remote_active_orders
            if item.get("symbol") not in tracked_symbols
        }
        if off_config_symbols:
            self.consecutive_balance_drifts = 0
            self.clean_polls = 0
            for symbol in sorted(off_config_symbols):
                self.oms.freeze_symbol(
                    symbol,
                    "truth_plane:off_config_open_order",
                    cancel_active_orders=True,
                )
            self.oms.trigger_reconcile("Truth plane off-config open order")
            return False

        if local_active_orders != remote_active_orders:
            self.consecutive_balance_drifts = 0
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
            self.consecutive_balance_drifts = 0
            for symbol in position_drift:
                self.oms.freeze_symbol(
                    symbol,
                    "truth_plane:position_mismatch",
                    cancel_active_orders=True,
                )
            self.oms.trigger_reconcile("Truth plane position mismatch")
            self.clean_polls = 0
            return False

        if local_balance is None:
            self.consecutive_balance_drifts = 0
            self.clean_polls += 1
            if self.clean_polls >= self.clean_polls_to_clear:
                self.oms.clear_transient_guards(prefixes=("truth_plane:",))
            return True

        remote_balance, remote_source = self._remote_balance_value(account, tracked_assets)
        balance_delta = remote_balance - local_balance
        has_risk = self._has_live_risk(
            tracked_symbols,
            exchange_positions,
            local_active_orders,
            remote_active_orders,
        )
        if abs(balance_delta) > self.account_balance_tolerance:
            logger.warning(
                f"[TruthMonitor] Balance drift local={local_balance:.6f} "
                f"remote={remote_balance:.6f} delta={balance_delta:+.6f} "
                f"tol={self.account_balance_tolerance:.6f} source={remote_source or 'totalWalletBalance'} "
                f"risk={'Y' if has_risk else 'N'}"
            )
            if self.ignore_flat_balance_drift and not has_risk:
                self.consecutive_balance_drifts = 0
                self.clean_polls += 1
                if self.clean_polls >= self.clean_polls_to_clear:
                    self.oms.clear_transient_guards(prefixes=("truth_plane:",))
                logger.warning("[TruthMonitor] Ignoring flat account balance drift")
                return True

            self.consecutive_balance_drifts += 1
            self.clean_polls = 0
            if self.consecutive_balance_drifts >= self.balance_drift_trigger_count:
                self.oms.freeze_venue(
                    self._venue_name(),
                    f"truth_plane:balance_drift:{balance_delta:+.6f}",
                    cancel_active_orders=False,
                )
                self.oms.trigger_reconcile("Truth plane account balance drift")
            return False

        self.consecutive_balance_drifts = 0
        self.clean_polls += 1
        if self.clean_polls >= self.clean_polls_to_clear:
            self.oms.clear_transient_guards(prefixes=("truth_plane:",))
        return True

    def _tracked_assets(self, tracked_symbols):
        assets = []
        for symbol in tracked_symbols:
            asset = self._extract_quote_asset(symbol)
            if asset and asset not in assets:
                assets.append(asset)
        return assets

    def _extract_quote_asset(self, symbol: str) -> str:
        symbol = (symbol or "").upper()
        for suffix in ("USDT", "USDC", "BUSD", "FDUSD"):
            if symbol.endswith(suffix):
                return suffix
        return ""

    def _local_balance_value(self, tracked_assets):
        local_balances = dict(getattr(self.oms.account, "balances", {}) or {})
        if tracked_assets:
            if all(asset in local_balances for asset in tracked_assets):
                tracked_values = [
                    float(local_balances.get(asset, 0.0) or 0.0)
                    for asset in tracked_assets
                ]
                return float(sum(tracked_values)), f"tracked_assets:{','.join(tracked_assets)}"
            return None, "tracked_assets_unsynced"

        if not getattr(self.oms.account, "exchange_balance_synced", False):
            return None, "exchange_balance_unsynced"

        local_balance = float(getattr(self.oms.account, "balance", 0.0) or 0.0)
        return local_balance, "totalWalletBalance"

    def _remote_balance_value(self, account, tracked_assets):
        remote_assets = {}
        for entry in account.get("assets", []) or []:
            asset = str(entry.get("asset", "") or "")
            if not asset:
                continue
            remote_assets[asset] = float(entry.get("walletBalance", 0.0) or 0.0)

        tracked_values = [
            remote_assets[asset]
            for asset in tracked_assets
            if asset in remote_assets
        ]
        if tracked_values:
            return float(sum(tracked_values)), f"tracked_assets:{','.join(tracked_assets)}"

        return float(account.get("totalWalletBalance", 0.0) or 0.0), "totalWalletBalance"

    def _has_live_risk(self, tracked_symbols, exchange_positions, local_active_orders, remote_active_orders):
        with self.oms.lock:
            local_positions = {
                symbol: float(volume or 0.0)
                for symbol, volume in getattr(self.oms.exposure, "net_positions", {}).items()
                if not tracked_symbols or symbol in tracked_symbols
            }

        has_local_positions = any(abs(volume) > 1e-9 for volume in local_positions.values())
        has_remote_positions = any(
            abs(float(payload.get("volume", 0.0) or 0.0)) > 1e-9
            for symbol, payload in exchange_positions.items()
            if not tracked_symbols or symbol in tracked_symbols
        )
        return bool(local_active_orders or remote_active_orders or has_local_positions or has_remote_positions)
