# file: data/ref_data.py

import math
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP

import requests
from infrastructure.logger import logger


def _flatten_permissions(value) -> frozenset[str]:
    """Normalize Binance's nested permissionSets payload."""
    pending = [value]
    permissions = set()
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            permissions.add(current.upper())
        elif isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, (list, tuple, set)):
            pending.extend(current)
    return frozenset(permissions)

@dataclass
class ContractInfo:
    symbol: str
    tick_size: float  # 价格最小跳动 (e.g., 0.1)
    step_size: float  # 数量最小跳动 (e.g., 0.001)
    min_qty: float    # 最小下单量
    min_notional: float # 最小名义价值 (USDT)
    price_precision: int # 价格小数位
    qty_precision: int   # 数量小数位

    status: str = "TRADING"
    permissions: frozenset[str] = field(default_factory=frozenset)

    @property
    def supports_rpi(self) -> bool:
        return self.status == "TRADING" and "RPI" in self.permissions


class ReferenceDataManager:
    """
    合约参考数据管理器 (单例)
    负责管理 TickSize, LotSize, MinNotional 等静态规则
    """
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(ReferenceDataManager, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "contracts"):
            return
        self.contracts = {}  # Symbol -> ContractInfo
        self.base_url = "https://fapi.binance.com" 

    def init(self, testnet=False):
        if testnet:
            self.base_url = "https://testnet.binancefuture.com"
        
        # 严格使用 Exchange Info 接口
        url = f"{self.base_url}/fapi/v1/exchangeInfo"
        logger.info(f"RefData fetching: {url} ...")
        
        try:
            res = requests.get(url, timeout=15).json()
            
            for s in res['symbols']:
                symbol = s['symbol']
                
                # 默认值
                tick_size = 0.0
                step_size = 0.0
                min_qty = 0.0
                min_notional = 5.0 # 币安通常默认5U
                
                # 解析过滤器 Filters
                for f in s['filters']:
                    if f['filterType'] == 'PRICE_FILTER':
                        tick_size = float(f['tickSize'])
                    elif f['filterType'] == 'LOT_SIZE':
                        step_size = float(f['stepSize'])
                        min_qty = float(f['minQty'])
                    elif f['filterType'] == 'MIN_NOTIONAL':
                        # 兼容不同版本的字段名
                        val = f.get('notional') or f.get('minNotional')
                        if val:
                            min_notional = float(val)
                        
                # 计算精度 (小数点后几位)
                # 0.01 -> 2, 0.0001 -> 4, 1.0 -> 0
                price_prec = 0
                if tick_size > 0:
                    price_prec = int(round(-math.log(tick_size, 10), 0))
                
                qty_prec = 0
                if step_size > 0:
                    qty_prec = int(round(-math.log(step_size, 10), 0))
                
                self.contracts[symbol] = ContractInfo(
                    symbol=symbol,
                    tick_size=tick_size,
                    step_size=step_size,
                    min_qty=min_qty,
                    min_notional=min_notional,
                    price_precision=price_prec,
                    qty_precision=qty_prec,
                    status=str(s.get("status", "") or "").upper(),
                    permissions=_flatten_permissions(s.get("permissionSets", [])),
                )
            
            logger.info(f"Loaded {len(self.contracts)} contracts info.")
            
        except Exception as e:
            logger.error(f"RefData Init Failed: {e}")
            # 如果初始化失败，可能需要重试或抛出致命错误阻止程序启动
            raise e

    def get_info(self, symbol: str) -> ContractInfo:
        return self.contracts.get(str(symbol or "").upper())

    def supports_rpi(self, symbol: str) -> bool:
        info = self.get_info(symbol)
        return bool(info and info.supports_rpi)

    def round_price(self, symbol, price, direction="nearest"):
        """Round a price to an exchange tick, optionally toward one side."""
        info = self.get_info(symbol)
        if not info or info.tick_size <= 0:
            return price

        rounding_modes = {
            "nearest": ROUND_HALF_UP,
            "down": ROUND_FLOOR,
            "up": ROUND_CEILING,
        }
        try:
            rounding = rounding_modes[str(direction or "nearest").lower()]
        except KeyError as exc:
            raise ValueError(
                f"Unsupported price rounding direction: {direction}"
            ) from exc

        price_dec = Decimal(str(price))
        tick_dec = Decimal(str(info.tick_size))
        ticks = (price_dec / tick_dec).to_integral_value(rounding=rounding)
        return float(ticks * tick_dec)

    def round_qty(self, symbol, qty):
        """将数量修整为符合 step_size"""
        info = self.get_info(symbol)
        if not info:
            return qty
        
        # 数量通常向下取整，防止超出余额或持仓
        # 这里使用严谨算法：
        # round(qty - (qty % step_size), precision)
        if info.step_size == 0:
            return qty

        # Use decimal arithmetic with a tiny step-relative epsilon so values
        # like 2.4/0.1 do not become 23.999999... and round down to 2.3.
        qty_dec = Decimal(str(qty))
        step_dec = Decimal(str(info.step_size))
        epsilon = step_dec * Decimal("1e-9")
        steps = ((qty_dec + epsilon) / step_dec).to_integral_value(rounding=ROUND_DOWN)
        rounded = steps * step_dec
        return round(float(rounded), info.qty_precision)

ref_data_manager = ReferenceDataManager()
