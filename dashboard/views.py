# file: dashboard/views.py

from event.type import OrderStatus

class LocalView:
    """
    [Fixed] 读取 OMS 内部状态 (适配新版 OMS Engine)
    """
    def __init__(self, oms):
        # oms 是 oms.engine.OMS 的实例
        self.oms = oms

    def get_net_positions(self):
        """
        读取净头寸
        """
        # 正确路径: oms.exposure.net_positions
        return self.oms.exposure.net_positions.copy()

    def get_active_order_count(self):
        """
        [修复] 
        统计 OMS 核心订单表 (self.oms.orders) 中状态为 active 的订单数量
        """
        count = 0
        with self.oms.lock:
            for order in self.oms.orders.values():
                if order.is_active():
                    count += 1
        return count

    def get_cancelling_count(self):
        """
        [修复]
        统计 OMS 核心订单表中状态为 CANCELLING 的数量
        """
        count = 0
        with self.oms.lock:
            for order in self.oms.orders.values():
                if order.status == OrderStatus.CANCELLING:
                    count += 1
        return count

class ExchangeView:
    """
    读取 Gateway 缓存的交易所状态
    """
    def __init__(self, gateway):
        self.gateway = gateway
        
        self.cached_positions = {} 
        self.cached_open_orders_count = 0
        self.last_sync_time = 0

    def refresh(self):
        """
        主动拉取快照 (低频调用)
        """
        try:
            raw_pos = self.gateway.get_all_positions()
            if raw_pos:
                # [优化] 在刷新前清空，确保旧的 symbol 被移除
                temp_pos = {}
                for p in raw_pos:
                    if float(p['positionAmt']) != 0:
                        temp_pos[p['symbol']] = float(p['positionAmt'])
                self.cached_positions = temp_pos
        except: pass
        
        # [TODO] 增加 get_open_orders 接口以同步挂单数
        pass

    def get_position(self, symbol):
        return self.cached_positions.get(symbol, 0.0)