from datetime import datetime

from data.cache import data_cache
from event.type import AccountData, Event, EVENT_ACCOUNT_UPDATE


class AccountManager:
    def __init__(self, engine, exposure_manager, config):
        self.engine = engine
        self.exposure = exposure_manager

        acc_conf = config.get("account", {})
        self.balance = acc_conf.get("initial_balance_usdt", 10000.0)
        self.leverage = acc_conf.get("leverage", 10)

        self.equity = self.balance
        self.used_margin = 0.0
        self.available = self.balance

    def force_sync(self, balance: float, used_margin: float, available: float = None):
        self.balance = balance
        self.calculate(used_margin_override=used_margin, available_override=available)

    def sync_exchange_balance(self, balance: float, available: float = None):
        self.balance = balance
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

        data = AccountData(
            balance=self.balance,
            equity=self.equity,
            available=self.available,
            used_margin=self.used_margin,
            datetime=datetime.now(),
        )
        self.engine.put(Event(EVENT_ACCOUNT_UPDATE, data))

    def _get_price_safely(self, symbol):
        price = data_cache.get_mark_price(symbol)
        if price <= 0:
            price, _ = data_cache.get_best_quote(symbol)
        return price

    def check_margin(self, notional_value):
        required = notional_value / self.leverage
        if self.available == 0:
            self.calculate()
        return self.available >= required

    def get_margin_ratio(self):
        if self.equity <= 0:
            return 0.0
        return self.used_margin / self.equity
