# file: dashboard/aggregator.py

from datetime import datetime
from .models import DashboardState, PositionRow, OrderHealth, SystemStatus
from .views import LocalView, ExchangeView
from data.cache import data_cache

class DashboardAggregator:
    def __init__(self, oms, gateway, config):
        self.local_view = LocalView(oms)
        self.exch_view = ExchangeView(gateway)
        self.config = config
        
        self.risk_limit = config["risk"]["limits"]["max_pos_notional"]
        
        # 缓存上一帧状态
        self.state = DashboardState(
            status=SystemStatus.CLEAN,
            update_time=datetime.now()
        )

    def update(self):
        """每秒调用一次，重新计算所有状态"""
        # 1. 触发交易所视图刷新 (实际应由独立线程控制频率)
        # self.exch_view.refresh() 
        
        rows = []
        global_status = SystemStatus.CLEAN
        total_exp = 0.0
        
        # --- 模块 1: 仓位核对 ---
        local_pos = self.local_view.get_net_positions()
        # 获取所有涉及的 Symbol
        all_symbols = set(local_pos.keys()) | set(self.exch_view.cached_positions.keys())
        
        for symbol in all_symbols:
            l_qty = local_pos.get(symbol, 0.0)
            e_qty = self.exch_view.get_position(symbol)
            delta = l_qty - e_qty
            
            # 计算名义价值
            price = data_cache.get_mark_price(symbol)
            notional = abs(l_qty) * price
            total_exp += notional
            
            is_dirty = abs(delta) > 1e-6
            is_danger = notional > self.risk_limit
            
            if is_dirty: global_status = SystemStatus.DIRTY
            if is_danger: global_status = SystemStatus.DANGER
            
            rows.append(PositionRow(
                symbol=symbol,
                local_qty=l_qty,
                exch_qty=e_qty,
                delta_qty=delta,
                notional=notional,
                is_dirty=is_dirty,
                is_danger=is_danger
            ))
            
        # --- 模块 2: 订单健康 ---
        loc_orders = self.local_view.get_active_order_count()
        # exch_orders = self.exch_view.cached_open_orders_count 
        # 暂时假设 Exchange 挂单数未知(需额外API)，先用 Local 代替展示
        exch_orders = loc_orders 
        
        cancelling = self.local_view.get_cancelling_count()
        
        order_health = OrderHealth(
            local_active=loc_orders,
            exch_active=exch_orders,
            cancelling_count=cancelling,
            stuck_orders=0, # 需 OMS 支持统计
            is_sync=(loc_orders == exch_orders)
        )
        
        if cancelling > 5: global_status = SystemStatus.DIRTY # 太多撤单卡住
        
        # --- 生成快照 ---
        self.state = DashboardState(
            status=global_status,
            update_time=datetime.now(),
            positions=rows,
            total_exposure=total_exp,
            order_health=order_health
        )
        
        return self.state