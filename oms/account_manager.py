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

    def force_sync(
        self,
        balance: float,
        used_margin: float,
        available: float = None,
        asset: str = "",
        balances: dict = None,
    ):
        self.balance = balance
        self.exchange_balance_synced = True
        self._sync_balance_maps(asset=asset, balance=balance, available=available, balances=balances)
        self.calculate(used_margin_override=used_margin, available_override=available)

    def sync_exchange_balance(
        self,
        balance: float,
        available: float = None,
        asset: str = "",
        balances: dict = None,
    ):
        self.balance = balance
        self.exchange_balance_synced = True
        self._sync_balance_maps(asset=asset, balance=balance, available=available, balances=balances)
        self.calculate(available_override=available)

    def update_balance(self, realized_pnl, commission):
        self.balance += (realized_pnl - commission)
        self.calculate()

    def calculate(self, used_margin_override: float = None, available_override: float = None):
        unrealized_pnl = 0.0
        pos_margin = 0.0
        order_margin = 0.0

        for symbol, pos_vol in self.exposure.net_positions.items():
            if pos_vol == 0:
                continue

            mark_price = data_cache.get_mark_price(symbol)
            if mark_price <= 0:
                mark_price, _ = data_cache.get_best_quote(symbol)
            if mark_price <= 0:
                mark_price = self.exposure.avg_prices[symbol]

            if mark_price > 0:
                avg_price = self.exposure.avg_prices[symbol]
                unrealized_pnl += (mark_price - avg_price) * pos_vol
                pos_margin += (abs(pos_vol) * mark_price) / self.leverage

        for symbol, qty in self.exposure.open_buy_qty.items():
            mark_price = self._get_price_safely(symbol)
            if mark_price > 0:
                order_margin += (qty * mark_price) / self.leverage

        for symbol, qty in self.exposure.open_sell_qty.items():
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
        )
        self.engine.put(Event(EVENT_ACCOUNT_UPDATE, data))

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

    def _budget_equity_value(self):
        if self.trading_budget_total > 0.0:
            return min(self.equity, self.trading_budget_total)
        return self.equity

    def _get_price_safely(self, symbol):
        price = data_cache.get_mark_price(symbol)
        if price <= 0:
            price, _ = data_cache.get_best_quote(symbol)
        return price

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
