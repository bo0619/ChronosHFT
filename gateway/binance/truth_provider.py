import socket
import time

import requests
from requests.adapters import HTTPAdapter

from .rest_api import BinanceRestApi


class TruthPlaneAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["socket_options"] = [
            (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        ]
        super().init_poolmanager(connections, maxsize, block, **pool_kwargs)


class BinanceTruthSnapshotProvider:
    gateway_name = "BINANCE"
    source_name = "BINANCE_TRUTH"
    supports_emergency_query_priority = True

    def __init__(
        self,
        api_key,
        api_secret,
        testnet=True,
        session=None,
        rest_api_cls=BinanceRestApi,
        *,
        rate_limit_budget=None,
        symbols=None,
        full_open_orders_audit_interval_sec=60.0,
    ):
        self.session = session or requests.Session()
        self._owns_session = session is None

        if self._owns_session:
            adapter = TruthPlaneAdapter(pool_connections=4, pool_maxsize=4)
            self.session.mount("https://", adapter)
            self.session.headers.update({"Content-Type": "application/json"})

        rest_kwargs = {}
        if rate_limit_budget is not None:
            rest_kwargs["rate_limit_budget"] = rate_limit_budget
        self.rest = rest_api_cls(
            api_key,
            api_secret,
            self.session,
            testnet,
            **rest_kwargs,
        )
        self.symbols = tuple(
            dict.fromkeys(
                str(symbol or "").strip().upper()
                for symbol in (symbols or ())
                if str(symbol or "").strip()
            )
        )
        self.full_open_orders_audit_interval_sec = max(
            5.0,
            float(full_open_orders_audit_interval_sec or 60.0),
        )
        self._last_full_open_orders_audit_monotonic = 0.0
        self._known_open_order_symbols = set()

    def get_account_info(self):
        response = self.rest.get_account()
        return response.json() if response and response.status_code == 200 else None

    def get_all_positions(self, *, emergency: bool = False):
        response = (
            self.rest.get_positions(emergency=True)
            if emergency
            else self.rest.get_positions()
        )
        return response.json() if response and response.status_code == 200 else None

    def get_open_orders(self, *, emergency: bool = False):
        now = time.perf_counter()
        full_audit_due = (
            emergency
            or not self.symbols
            or self._last_full_open_orders_audit_monotonic <= 0.0
            or now - self._last_full_open_orders_audit_monotonic
            >= self.full_open_orders_audit_interval_sec
        )
        if full_audit_due:
            response = (
                self.rest.get_open_orders(emergency=True)
                if emergency
                else self.rest.get_open_orders()
            )
            if not response or response.status_code != 200:
                return None
            rows = response.json()
            if not isinstance(rows, list):
                return None
            self._last_full_open_orders_audit_monotonic = now
            self._remember_open_order_symbols(rows)
            return rows

        rows = []
        query_symbols = tuple(
            sorted(set(self.symbols) | self._known_open_order_symbols)
        )
        for symbol in query_symbols:
            response = (
                self.rest.get_open_orders(symbol, emergency=True)
                if emergency
                else self.rest.get_open_orders(symbol)
            )
            if not response or response.status_code != 200:
                return None
            symbol_rows = response.json()
            if not isinstance(symbol_rows, list):
                return None
            rows.extend(symbol_rows)
        self._remember_open_order_symbols(rows)
        return rows

    def _remember_open_order_symbols(self, rows):
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol", "") or "").strip().upper()
            if symbol:
                self._known_open_order_symbols.add(symbol)

    def get_income_history(self, **kwargs):
        response = self.rest.get_income_history(**kwargs)
        return response.json() if response and response.status_code == 200 else None

    def get_commission_rate(self, symbol: str):
        response = self.rest.get_commission_rate(symbol)
        return response.json() if response and response.status_code == 200 else None

    def close(self):
        if self._owns_session and self.session:
            self.session.close()
        return True
