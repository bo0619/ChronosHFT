import socket

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

    def __init__(self, api_key, api_secret, testnet=True, session=None, rest_api_cls=BinanceRestApi):
        self.session = session or requests.Session()
        self._owns_session = session is None

        if self._owns_session:
            adapter = TruthPlaneAdapter(pool_connections=4, pool_maxsize=4)
            self.session.mount("https://", adapter)
            self.session.headers.update({"Content-Type": "application/json"})

        self.rest = rest_api_cls(api_key, api_secret, self.session, testnet)

    def get_account_info(self):
        response = self.rest.get_account()
        return response.json() if response and response.status_code == 200 else None

    def get_all_positions(self):
        response = self.rest.get_positions()
        return response.json() if response and response.status_code == 200 else None

    def get_open_orders(self):
        response = self.rest.get_open_orders()
        return response.json() if response and response.status_code == 200 else None

    def close(self):
        if self._owns_session and self.session:
            self.session.close()
