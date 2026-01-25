# file: oms/validator.py

from data.ref_data import ref_data_manager
from event.type import OrderIntent

class OrderValidator:
    def __init__(self, config):
        self.limits = config.get("risk", {}).get("limits", {})

    def validate_params(self, intent:OrderIntent) -> bool:
        # 1. 价格数量精度检查
        if intent.price <= 0 or intent.volume <= 0: return False
        
        # 2. 最小名义价值检查
        info = ref_data_manager.get_info(intent.symbol)
        if info:
            notional = intent.price * intent.volume
            if notional < max(info.min_notional, 5.0):
                return False
                
        # 3. 单笔上限
        if intent.volume > self.limits.get("max_order_qty", 1000): return False
        
        return True