# file: gateway/binance/constants.py

# Production
REST_URL_MAIN = "https://fapi.binance.com"
WS_URL_MAIN = "wss://fstream.binance.com"
WS_PUBLIC_URL_MAIN = f"{WS_URL_MAIN}/public"
WS_MARKET_URL_MAIN = f"{WS_URL_MAIN}/market"
WS_PRIVATE_URL_MAIN = f"{WS_URL_MAIN}/private"

# Testnet
REST_URL_TEST = "https://testnet.binancefuture.com"
WS_URL_TEST = "wss://stream.binancefuture.com/ws"

# Endpoints
EP_DEPTH_SNAPSHOT = "/fapi/v1/depth"
EP_PREMIUM_INDEX = "/fapi/v1/premiumIndex"
EP_RPI_DEPTH = "/fapi/v1/rpiDepth"
EP_ORDER = "/fapi/v1/order"
EP_LISTEN_KEY = "/fapi/v1/listenKey"
EP_TIME = "/fapi/v1/time"
EP_EXCHANGE_INFO = "/fapi/v1/exchangeInfo"
EP_LEVERAGE = "/fapi/v1/leverage"
EP_MARGIN_TYPE = "/fapi/v1/marginType"
EP_POSITION_MODE = "/fapi/v1/positionSide/dual"

# Account trading configuration modes
ACCOUNT_CONFIGURATION_MODE_APPLY = "APPLY"
ACCOUNT_CONFIGURATION_MODE_VERIFY_ONLY = "VERIFY_ONLY"
ACCOUNT_CONFIGURATION_MODES = frozenset(
    {
        ACCOUNT_CONFIGURATION_MODE_APPLY,
        ACCOUNT_CONFIGURATION_MODE_VERIFY_ONLY,
    }
)

# Account
EP_ACCOUNT = "/fapi/v2/account"
EP_POSITION_RISK = "/fapi/v2/positionRisk"
EP_OPEN_ORDERS = "/fapi/v1/openOrders"
EP_ALL_OPEN_ORDERS = "/fapi/v1/allOpenOrders"
EP_ALL_ORDERS = "/fapi/v1/allOrders"
EP_USER_TRADES = "/fapi/v1/userTrades"
EP_INCOME = "/fapi/v1/income"
EP_COUNTDOWN_CANCEL_ALL = "/fapi/v1/countdownCancelAll"
EP_COMMISSION_RATE = "/fapi/v1/commissionRate"
