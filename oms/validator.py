from data.ref_data import ref_data_manager
from event.type import OrderIntent


class OrderValidator:
    def __init__(self, config):
        self.limits = config.get("risk", {}).get("limits", {})

    def validate_params(self, intent: OrderIntent) -> tuple[bool, str]:
        if intent.price <= 0 or intent.volume <= 0:
            return False, "non_positive_price_or_volume"

        info = ref_data_manager.get_info(intent.symbol)
        if info:
            notional = intent.price * intent.volume
            if notional < max(info.min_notional, 5.0):
                return False, f"notional_below_min:{notional:.8f}"

        max_order_qty = self.limits.get("max_order_qty", 1000)
        if intent.volume > max_order_qty:
            return False, f"max_order_qty_exceeded:{intent.volume}>{max_order_qty}"

        return True, ""
