import math
import time
from datetime import datetime

from data.cache import data_cache
from event.type import AccountData, Event, EVENT_ACCOUNT_UPDATE


class AccountManager:
    def __init__(self, engine, exposure_manager, config):
        self.engine = engine
        self.exposure = exposure_manager

        acc_conf = config.get("account", {})
        self.configured_balance = float(acc_conf.get("initial_balance_usdt", 10000.0) or 10000.0)
        self.balance = self.configured_balance
        self.leverage = acc_conf.get("leverage", 10)
        raw_budget_by_asset = acc_conf.get("trading_budget_by_asset", {}) or {}
        self.trading_budget_by_asset = {
            str(asset).upper(): float(value or 0.0)
            for asset, value in raw_budget_by_asset.items()
            if float(value or 0.0) > 0.0
        }
        self.trading_budget_total = float(
            acc_conf.get(
                "trading_budget_total",
                sum(self.trading_budget_by_asset.values()) or self.configured_balance,
            )
            or 0.0
        )

        self.equity = self.balance
        self.used_margin = 0.0
        self.available = self.balance
        self.budget_equity = min(self.balance, self.trading_budget_total) if self.trading_budget_total > 0 else self.balance
        self.budget_balance = self.budget_equity
        self.budget_available = self.budget_equity
        self.balances = {}
        self.available_balances = {}
        self.exchange_balance_synced = False
        self.maintenance_margin = 0.0
        self.margin_balance = self.equity
        self.maintenance_margin_ratio = 0.0
        self.margin_snapshot_time = 0.0
        self.margin_snapshot_monotonic = 0.0
        self.margin_snapshot_synced = False
        self.external_cash_flow_total = 0.0
        self.cash_flow_snapshot_time = 0.0
        self.cash_flow_snapshot_monotonic = 0.0
        self.cash_flow_snapshot_synced = False

    def force_sync(
        self,
        balance: float,
        used_margin: float,
        available: float = None,
        asset: str = "",
        balances: dict = None,
        maintenance_margin: float = None,
        margin_balance: float = None,
        margin_snapshot_time: float = None,
        margin_snapshot_monotonic: float = None,
    ):
        balance = self._require_finite(balance, "balance")
        used_margin = self._require_finite(
            used_margin,
            "used_margin",
            nonnegative=True,
        )
        available = self._optional_finite(available, "available")
        balances = self._normalize_balance_payload(balances)
        self.balance = balance
        self.exchange_balance_synced = True
        self._sync_balance_maps(asset=asset, balance=balance, available=available, balances=balances)
        self._set_margin_health(
            maintenance_margin,
            margin_balance,
            snapshot_time=margin_snapshot_time,
            snapshot_monotonic=margin_snapshot_monotonic,
        )
        self.calculate(used_margin_override=used_margin, available_override=available)

    def sync_exchange_balance(
        self,
        balance: float,
        available: float = None,
        asset: str = "",
        balances: dict = None,
    ):
        balance = self._require_finite(balance, "balance")
        available = self._optional_finite(available, "available")
        balances = self._normalize_balance_payload(balances)
        self.balance = balance
        self.exchange_balance_synced = True
        self._sync_balance_maps(asset=asset, balance=balance, available=available, balances=balances)
        self.calculate(available_override=available)

    def sync_margin_health(
        self,
        maintenance_margin: float,
        margin_balance: float,
        snapshot_time: float = None,
        snapshot_monotonic: float = None,
    ) -> bool:
        if not self._set_margin_health(
            maintenance_margin,
            margin_balance,
            snapshot_time=snapshot_time,
            snapshot_monotonic=snapshot_monotonic,
        ):
            return False
        self.calculate()
        return True

    def sync_external_cash_flow_truth(
        self,
        total: float,
        snapshot_time: float = None,
        snapshot_monotonic: float = None,
    ) -> bool:
        try:
            total = float(total)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(total):
            return False
        self.external_cash_flow_total = total
        (
            self.cash_flow_snapshot_time,
            self.cash_flow_snapshot_monotonic,
        ) = self._snapshot_times(
            snapshot_time,
            snapshot_monotonic,
        )
        self.cash_flow_snapshot_synced = True
        self.calculate()
        return True

    def mark_external_cash_flow_truth_unavailable(self):
        self.cash_flow_snapshot_synced = False
        self.cash_flow_snapshot_time = 0.0
        self.cash_flow_snapshot_monotonic = 0.0
        self.calculate()

    def update_balance(self, realized_pnl, commission):
        realized_pnl = self._require_finite(
            realized_pnl,
            "realized_pnl",
        )
        commission = self._require_finite(
            commission,
            "commission",
        )
        next_balance = self.balance + realized_pnl - commission
        if not math.isfinite(next_balance):
            raise ValueError("Account balance update overflowed")
        self.balance = next_balance
        self.calculate()

    def calculate(self, used_margin_override: float = None, available_override: float = None):
        if (
            not math.isfinite(float(self.balance))
            or not math.isfinite(float(self.leverage))
            or float(self.leverage) <= 0.0
        ):
            raise ValueError("Account state is not finite")
        if used_margin_override is not None:
            used_margin_override = self._require_finite(
                used_margin_override,
                "used_margin_override",
                nonnegative=True,
            )
        if available_override is not None:
            available_override = self._require_finite(
                available_override,
                "available_override",
            )
        unrealized_pnl = 0.0
        pos_margin = 0.0
        order_margin = 0.0

        for symbol, pos_vol in self.exposure.net_positions.items():
            if pos_vol == 0:
                continue
            if not math.isfinite(float(pos_vol)):
                raise ValueError(
                    f"Non-finite position volume for {symbol}"
                )

            mark_price = data_cache.get_mark_price(symbol)
            if not math.isfinite(mark_price) or mark_price <= 0:
                mark_price, _ = data_cache.get_best_quote(symbol)
            if not math.isfinite(mark_price) or mark_price <= 0:
                mark_price = self.exposure.avg_prices[symbol]

            avg_price = float(self.exposure.avg_prices[symbol])
            if not math.isfinite(avg_price):
                raise ValueError(
                    f"Non-finite average price for {symbol}"
                )
            if math.isfinite(mark_price) and mark_price > 0:
                unrealized_pnl += (mark_price - avg_price) * pos_vol
                pos_margin += (abs(pos_vol) * mark_price) / self.leverage

        for symbol, qty in self.exposure.open_buy_qty.items():
            if not math.isfinite(float(qty)) or qty < 0.0:
                raise ValueError(
                    f"Non-finite open buy quantity for {symbol}"
                )
            mark_price = self._get_price_safely(symbol)
            if mark_price > 0:
                order_margin += (qty * mark_price) / self.leverage

        for symbol, qty in self.exposure.open_sell_qty.items():
            if not math.isfinite(float(qty)) or qty < 0.0:
                raise ValueError(
                    f"Non-finite open sell quantity for {symbol}"
                )
            mark_price = self._get_price_safely(symbol)
            if mark_price > 0:
                order_margin += (qty * mark_price) / self.leverage

        self.equity = self.balance + unrealized_pnl
        local_used_margin = pos_margin + order_margin
        exchange_used_margin = max(0.0, used_margin_override or 0.0)
        if available_override is None:
            available_used_margin = 0.0
        else:
            available_used_margin = max(0.0, self.equity - max(0.0, available_override))

        self.used_margin = max(local_used_margin, exchange_used_margin, available_used_margin)
        self.available = max(0.0, self.equity - self.used_margin)
        self.budget_equity = self._budget_equity_value()
        self.budget_balance = self.budget_equity
        self.budget_available = max(0.0, self.budget_equity - self.used_margin)
        if not self.margin_snapshot_synced:
            self.margin_balance = self.equity
            self.maintenance_margin_ratio = 0.0
        elif self.margin_balance > 0.0:
            self.maintenance_margin_ratio = self.maintenance_margin / self.margin_balance
        else:
            self.maintenance_margin_ratio = (
                float("inf") if self.maintenance_margin > 0.0 else 0.0
            )

        data = AccountData(
            balance=self.balance,
            equity=self.equity,
            available=self.available,
            used_margin=self.used_margin,
            datetime=datetime.now(),
            balances=dict(self.balances),
            available_balances=dict(self.available_balances),
            budget_balance=self.budget_equity,
            budget_available=self.budget_available,
            trading_budget_by_asset=dict(self.trading_budget_by_asset),
            maintenance_margin=self.maintenance_margin,
            margin_balance=self.margin_balance,
            maintenance_margin_ratio=self.maintenance_margin_ratio,
            margin_snapshot_time=self.margin_snapshot_time,
            margin_snapshot_monotonic=self.margin_snapshot_monotonic,
            margin_snapshot_synced=self.margin_snapshot_synced,
            external_cash_flow_total=self.external_cash_flow_total,
            cash_flow_snapshot_time=self.cash_flow_snapshot_time,
            cash_flow_snapshot_monotonic=self.cash_flow_snapshot_monotonic,
            cash_flow_snapshot_synced=self.cash_flow_snapshot_synced,
        )
        self.engine.put(Event(EVENT_ACCOUNT_UPDATE, data))

    def _set_margin_health(
        self,
        maintenance_margin: float,
        margin_balance: float,
        snapshot_time: float = None,
        snapshot_monotonic: float = None,
    ) -> bool:
        if maintenance_margin is None or margin_balance is None:
            return False
        try:
            maintenance_margin = float(maintenance_margin)
            margin_balance = float(margin_balance)
        except (TypeError, ValueError):
            return False
        if (
            not math.isfinite(maintenance_margin)
            or not math.isfinite(margin_balance)
            or maintenance_margin < 0.0
        ):
            return False
        self.maintenance_margin = maintenance_margin
        self.margin_balance = margin_balance
        (
            self.margin_snapshot_time,
            self.margin_snapshot_monotonic,
        ) = self._snapshot_times(
            snapshot_time,
            snapshot_monotonic,
        )
        self.margin_snapshot_synced = True
        return True

    @staticmethod
    def _snapshot_times(
        snapshot_time: float | None,
        snapshot_monotonic: float | None,
    ) -> tuple[float, float]:
        if snapshot_time is None and snapshot_monotonic is None:
            return time.time(), time.perf_counter()
        wall_time = time.time() if snapshot_time is None else float(snapshot_time)
        monotonic_time = (
            0.0
            if snapshot_monotonic is None
            else float(snapshot_monotonic)
        )
        return wall_time, monotonic_time

    def _sync_balance_maps(self, asset: str = "", balance: float = None, available: float = None, balances: dict = None):
        if balances:
            self.balances = {
                str(key).upper(): float((payload or {}).get("wallet_balance", 0.0) or 0.0)
                for key, payload in balances.items()
            }
            self.available_balances = {
                str(key).upper(): float(payload.get("available_balance", 0.0) or 0.0)
                for key, payload in balances.items()
                if payload.get("available_balance") is not None
            }
            return

        if asset:
            asset = str(asset).upper()
            self.balances[asset] = float(balance or 0.0)
            if available is not None:
                self.available_balances[asset] = float(available)

    @classmethod
    def _normalize_balance_payload(cls, balances):
        if balances is None:
            return None
        if not isinstance(balances, dict):
            raise ValueError("balances must be a mapping")
        normalized = {}
        for raw_asset, raw_payload in balances.items():
            asset = str(raw_asset or "").upper().strip()
            if not asset or not isinstance(raw_payload, dict):
                raise ValueError("balances contains an invalid asset entry")
            payload = dict(raw_payload)
            payload["wallet_balance"] = cls._require_finite(
                payload.get("wallet_balance", 0.0),
                f"balances.{asset}.wallet_balance",
            )
            if payload.get("available_balance") is not None:
                payload["available_balance"] = cls._require_finite(
                    payload["available_balance"],
                    f"balances.{asset}.available_balance",
                )
            normalized[asset] = payload
        return normalized

    @staticmethod
    def _require_finite(
        value,
        field: str,
        *,
        nonnegative: bool = False,
    ) -> float:
        try:
            normalized = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be numeric") from exc
        if not math.isfinite(normalized):
            raise ValueError(f"{field} must be finite")
        if nonnegative and normalized < 0.0:
            raise ValueError(f"{field} must be nonnegative")
        return normalized

    @classmethod
    def _optional_finite(cls, value, field: str):
        if value is None:
            return None
        return cls._require_finite(value, field)

    def _budget_equity_value(self):
        if self.trading_budget_total > 0.0:
            return min(self.equity, self.trading_budget_total)
        return self.equity

    def _get_price_safely(self, symbol):
        price = data_cache.get_mark_price(symbol)
        if not math.isfinite(price) or price <= 0:
            price, _ = data_cache.get_best_quote(symbol)
        return price if math.isfinite(price) and price > 0.0 else 0.0

    def check_margin(self, notional_value):
        required = notional_value / self.leverage
        effective_available = self.budget_available if self.trading_budget_total > 0.0 else self.available
        if effective_available == 0:
            self.calculate()
            effective_available = self.budget_available if self.trading_budget_total > 0.0 else self.available
        return effective_available >= required

    def get_margin_ratio(self):
        if self.equity <= 0:
            return 0.0
        return self.used_margin / self.equity

    def get_maintenance_margin_ratio(self):
        return self.maintenance_margin_ratio if self.margin_snapshot_synced else None
