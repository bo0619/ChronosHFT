# file: data/ref_data.py

import requests
import math
from dataclasses import dataclass
from infrastructure.logger import logger

@dataclass
class ContractInfo:
    symbol: str
    tick_size: float  # 价格最小跳动
    step_size: float  # 数量最小跳动
    min_qty: float    # 最小数量
    min_notional: float # [NEW] 最小名义价值 (USDT)
    price_precision: int
    qty_precision: int

class ReferenceDataManager:
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(ReferenceDataManager, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "contracts"): return
        self.contracts = {} 
        self.base_url = "https://fapi.binance.com" 

    def init(self, testnet=False):
        if testnet:
            self.base_url = "https://testnet.binancefuture.com"
        
        logger.info("Fetching Exchange Info...")
        try:
            url = f"{self.base_url}/fapi/v1/exchangeInfo"
            res = requests.get(url, timeout=10).json()
            
            for s in res['symbols']:
                symbol = s['symbol']
                tick_size = 0.0
                step_size = 0.0
                min_qty = 0.0
                min_notional = 5.0 # 默认值
                
                for f in s['filters']:
                    if f['filterType'] == 'PRICE_FILTER':
                        tick_size = float(f['tickSize'])
                    elif f['filterType'] == 'LOT_SIZE':
                        step_size = float(f['stepSize'])
                        min_qty = float(f['minQty'])
                    elif f['filterType'] == 'MIN_NOTIONAL':
                        min_notional = float(f.get('notional', 0) or f.get('minNotional', 0))
                        
                price_prec = int(round(-math.log(tick_size, 10), 0))
                qty_prec = int(round(-math.log(step_size, 10), 0))
                
                self.contracts[symbol] = ContractInfo(
                    symbol=symbol,
                    tick_size=tick_size,
                    step_size=step_size,
                    min_qty=min_qty,
                    min_notional=min_notional,
                    price_precision=price_prec,
                    qty_precision=qty_prec
                )
            
            logger.info(f"Loaded {len(self.contracts)} Contracts Reference Data.")
            
        except Exception as e:
            logger.error(f"Failed to fetch Exchange Info: {e}")

    def get_info(self, symbol: str) -> ContractInfo:
        return self.contracts.get(symbol)

    def round_price(self, symbol, price):
        info = self.get_info(symbol)
        if not info: return price
        return round(price, info.price_precision)

    def round_qty(self, symbol, qty):
        info = self.get_info(symbol)
        if not info: return qty
        # 数量必须向下取整到 step_size 的倍数，否则可能报错
        # 简单算法：floor(qty / step) * step
        steps = math.floor(qty / info.step_size)
        return round(steps * info.step_size, info.qty_precision)

ref_data_manager = ReferenceDataManager()