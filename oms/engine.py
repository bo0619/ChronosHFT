# file: oms/engine.py

import uuid
import threading
from infrastructure.logger import logger
from event.type import Event, OrderIntent, OrderStatus, ExchangeOrderUpdate, CancelRequest
from event.type import EVENT_ORDER_UPDATE, EVENT_TRADE_UPDATE, EVENT_POSITION_UPDATE, EVENT_ORDER_SUBMITTED
from .order import Order
from .exposure import ExposureManager
from .validator import OrderValidator
from .account_manager import AccountManager
from .order_manager import OrderManager

class OMS:
    """
    OMS Core Engine (Single Source of Truth)
    统筹管理 Validator, Exposure, Account, OrderManager
    """
    def __init__(self, engine, gateway, config):
        self.event_engine = engine
        self.gateway = gateway
        self.config = config
        
        self.orders = {} # client_oid -> Order
        self.exchange_id_map = {} # exchange_oid -> Order
        
        self.lock = threading.RLock()

        # --- 初始化子组件 ---
        self.validator = OrderValidator(config)
        self.exposure = ExposureManager()
        
        # OrderManager 负责掉单检测 (传入 engine 和 gateway)
        self.order_monitor = OrderManager(engine, gateway)
        
        # AccountManager 负责资金 (传入 exposure 和 order_monitor 以获取最新状态)
        # 注意：AccountManager 原先依赖 pm/om，现在我们让它依赖 exposure 和 order_monitor
        # 我们需要微调 AccountManager 或者在这里做适配。
        # 为了架构整洁，建议让 AccountManager 变得被动，由 OMS 推送数据给它，
        # 但为了复用 Step 4 代码，我们传入 self 作为上下文
        self.account = AccountManager(engine, self.exposure, self.order_monitor, config)

    def submit_order(self, intent: OrderIntent) -> str:
        """策略层调用的唯一发单入口"""
        client_oid = str(uuid.uuid4())
        
        with self.lock:
            # 1. 静态校验
            if not self.validator.validate_params(intent):
                logger.warn(f"OMS Reject: Invalid Params {intent}")
                return None
            
            # 2. 资金/保证金检查 (Account)
            notional = intent.price * intent.volume
            if not self.account.check_margin(notional):
                # logger.warn(f"OMS Reject: Insufficient Margin")
                return None

            # 3. 仓位限额检查 (Exposure)
            ok, msg = self.exposure.check_risk(
                intent.symbol, intent.side, intent.price, intent.volume, 
                self.config["risk"]["limits"]["max_pos_notional"]
            )
            if not ok:
                logger.warn(f"OMS Reject: Exposure Limit - {msg}")
                return None

            # 4. 创建订单并入库
            order = Order(client_oid, intent)
            self.orders[client_oid] = order
            order.mark_submitting()
            
            # 5. 更新 Exposure 的挂单占用
            self.exposure.update_open_orders(self.orders)
            
        # 6. 发送给网关
        # 这里我们需要适配 Gateway。Gateway 目前接受 OrderRequest。
        # 我们把 Intent 转回 Request 发送，或者修改 Gateway 接受 Order 对象。
        # 为了兼容，我们构造一个 Request 对象传给 Gateway 的底层发送逻辑，
        # 但 Gateway.send_order 也是我们要改造的。
        # 简单处理：我们调用 Gateway.send_order，它返回 exchange_id (或 None)
        
        # 此时需要构造一个符合 Gateway 接口的 req 对象
        # (这部分代码稍微有点胶水味，但在重构 Gateway 前必须这样)
        from event.type import OrderRequest
        req = OrderRequest(
            symbol=intent.symbol, price=intent.price, volume=intent.volume,
            side=intent.side.value, order_type=intent.order_type,
            time_in_force=intent.time_in_force, post_only=intent.is_post_only
        )
        
        # Gateway 发送 (可能会阻塞网络IO)
        exchange_oid = self.gateway.send_order(req)
        
        if exchange_oid:
            # 7. 发送成功，通知 OrderManager 开始监控掉单
            # 构造一个模拟的 OrderSubmitted 事件
            from event.type import OrderSubmitted
            import time
            event_data = OrderSubmitted(req, exchange_oid, time.time())
            self.order_monitor.on_order_submitted(Event(EVENT_ORDER_SUBMITTED, event_data))
            
            # 更新内部映射
            with self.lock:
                self.exchange_id_map[exchange_oid] = order
                
        return client_oid

    def cancel_order(self, client_oid: str):
        """策略层调用的唯一撤单入口"""
        with self.lock:
            order = self.orders.get(client_oid)
            if not order or not order.is_active():
                return
            
            # 获取 exchange_oid 用于撤单
            exch_oid = order.exchange_oid
            symbol = order.intent.symbol
            
        # 调用 Gateway
        if exch_oid:
            from event.type import CancelRequest
            self.gateway.cancel_order(CancelRequest(symbol, exch_oid))

    # --- 核心：处理交易所回报 ---
    def on_exchange_update(self, event):
        """Gateway 收到 WS 数据后推送到这里"""
        update: ExchangeOrderUpdate = event.data
        
        with self.lock:
            order = self.orders.get(update.client_oid)
            if not order and update.exchange_oid:
                order = self.exchange_id_map.get(update.exchange_oid)
            
            if not order: return # 可能是其他策略的单子，或者启动前的单子

            # 更新 OrderManager (让它知道订单活着)
            # 这里的转换有点繁琐，理想情况是 OrderManager 也读取 Event
            # 这里简化：直接透传给 order_monitor
            # self.order_monitor.on_order_update(...) # 稍后处理

            # 状态机流转
            ex_status = update.status
            if ex_status == "NEW":
                order.mark_new(update.exchange_oid)
                self.exchange_id_map[update.exchange_oid] = order
            elif ex_status == "CANCELED":
                order.mark_cancelled()
            elif ex_status == "REJECTED":
                order.mark_rejected()
            elif ex_status == "FILLED" or ex_status == "PARTIALLY_FILLED":
                delta_qty = update.cum_filled_qty - order.filled_volume
                if delta_qty > 0:
                    order.add_fill(delta_qty, update.filled_price)
                    
                    # 核心：Exposure 更新持仓
                    self.exposure.on_fill(
                        order.intent.symbol, order.intent.side, 
                        delta_qty, update.filled_price
                    )
                    
                    # 核心：Account 更新余额 (扣手续费)
                    fee = delta_qty * update.filled_price * 0.0005 # 估算
                    self.account.update_balance(0, fee) # 盈亏暂不计入Balance，只扣费
                    
                    # 推送 Trade Event
                    # ...

            # 每次状态变化，都重新计算挂单敞口和保证金
            self.exposure.update_open_orders(self.orders)
            self.account.calculate()
            
            # 推送 Order Update Event 给策略
            snapshot = order.to_snapshot()
            self.event_engine.put(Event(EVENT_ORDER_UPDATE, snapshot))
            
            # 推送给 Position Update (因为 Exposure 变了)
            pos_data = self.exposure.get_position_data(order.intent.symbol)
            if pos_data:
                self.event_engine.put(Event(EVENT_POSITION_UPDATE, pos_data))

    def on_order_submitted(self, event):
        # 兼容旧接口，这里可能不需要了，因为 submit_order 内部已经处理
        pass

    def stop(self):
        self.order_monitor.stop()