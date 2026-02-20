# file: gateway/binance/constants.py

# Production
REST_URL_MAIN = "https://fapi.binance.com"
WS_URL_MAIN = "wss://fstream.binance.com/ws"

# Testnet
REST_URL_TEST = "https://testnet.binancefuture.com"
WS_URL_TEST = "wss://stream.binancefuture.com/ws"

# Endpoints
EP_DEPTH_SNAPSHOT = "/fapi/v1/depth"
EP_ORDER = "/fapi/v1/order"
EP_LISTEN_KEY = "/fapi/v1/listenKey"
EP_TIME = "/fapi/v1/time"
EP_EXCHANGE_INFO = "/fapi/v1/exchangeInfo"
EP_LEVERAGE = "/fapi/v1/leverage"
EP_MARGIN_TYPE = "/fapi/v1/marginType"

# Account
EP_ACCOUNT = "/fapi/v2/account"
EP_POSITION_RISK = "/fapi/v2/positionRisk"
EP_OPEN_ORDERS = "/fapi/v1/openOrders"
EP_ALL_OPEN_ORDERS = "/fapi/v1/allOpenOrders"