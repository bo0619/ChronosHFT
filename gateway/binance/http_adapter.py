"""Low-latency HTTP adapter used by the Binance REST session."""

import socket

from requests.adapters import HTTPAdapter


class HFTAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["socket_options"] = [
            (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        ]
        super().init_poolmanager(connections, maxsize, block, **pool_kwargs)

