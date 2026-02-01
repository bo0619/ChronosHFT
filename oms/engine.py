# file: oms/engine.py

import uuid
import threading
import time
from datetime import datetime
from infrastructure.logger import logger
from event.type import Event, OrderIntent, OrderRequest, OrderStatus, ExchangeOrderUpdate, CancelRequest
from event.type import EVENT_ORDER_UPDATE, EVENT_TRADE_UPDATE, EVENT_POSITION_UPDATE, EVENT_ORDER_SUBMITTED
from event.type import OrderSubmitted, TradeData

# 引入子组件
from .order import Order
from .exposure import ExposureManager
from .validator import OrderValidator
from .account_manager import AccountManager
from .order_manager import OrderManager

class OMS:
    """
    OMS Core Engine (Single Source of Truth)
    职责：
    1. 统筹管理子模块 (Validator, Exposure, Account, OrderManager)
    2. 维护订单主表 (Order Registry)
    3. 处理下行指令 (Submit/Cancel)
    4. 处理上行回报 (Exchange Update) 并同步所有状态
    """
    def __init__(self, event_engine, gateway, config):
        self.event_engine = event_engine
        self.gateway = gateway
        self.config = config
        
        # 核心数据：订单注册表
        self.orders = {}          # client_oid -> Order
        self.exchange_id_map = {} # exchange_oid -> Order
        
        self.lock = threading.RLock() # 递归锁，保证状态更新原子性

        # --- 初始化子组件 ---
        
        # 1. 静态规则校验器
        self.validator = OrderValidator(config)
        
        # 2. 仓位与敞口管理器 (真理源：持仓量 & 挂单量)
        self.exposure = ExposureManager()
        
        # 3. 订单生命周期管理器 (负责掉单检测线程)
        self.order_monitor = OrderManager(event_engine, gateway)
        
        # 4. 资金管理器 (真理源：余额 & 保证金)
        # [修复] 适配 Step 11: AccountManager 现在只依赖 Engine, Exposure 和 Config
        # 它不再需要 OrderManager，因为挂单占用的 Quantity 已经在 Exposure 中计算好了
        self.account = AccountManager(event_engine, self.exposure, config)

    def submit_order(self, intent: OrderIntent) -> str:
        """
        [下行] 策略发单入口
        """
        client_oid = str(uuid.uuid4())
        
        with self.lock:
            # 1. 静态参数校验
            if not self.validator.validate_params(intent):
                logger.warn(f"OMS Reject: Invalid Params {intent}")
                return None
            
            # 2. 资金/保证金检查 (Account Manager)
            notional = intent.price * intent.volume
            if not self.account.check_margin(notional):
                # 资金不足
                # logger.warning(f"OMS Reject: Insufficient Margin for {notional:.2f}")
                return None

            # 3. 仓位限额与系统敞口检查 (Exposure Manager)
            # 这里是 "系统级" 检查：包含当前持仓 + 所有同向挂单 + 本笔新单
            max_notional = self.config["risk"]["limits"].get("max_pos_notional", 20000.0)
            ok, msg = self.exposure.check_risk(
                intent.symbol, intent.side, intent.volume, max_notional
            )
            if not ok:
                logger.warn(f"OMS Reject: Exposure Limit - {msg}")
                return None

            # --- 通过所有检查，创建订单 ---
            order = Order(client_oid, intent)
            self.orders[client_oid] = order
            order.mark_submitting()
            
            # 4. 立即更新内部状态 (Pessimistic Locking)
            # 即使还没发出去，也先占用 Exposure 和 Margin，防止并发超限
            self.exposure.update_open_orders(self.orders)
            
            # 立即刷新资金占用 (因为 Exposure 里的 open_qty 变了，Account 会重算 Margin)
            self.account.calculate()
            
        # 5. 构造 Request
        req = OrderRequest(
            symbol=intent.symbol, 
            price=intent.price, 
            volume=intent.volume,
            side=intent.side.value, 
            order_type=intent.order_type,
            time_in_force=intent.time_in_force, 
            post_only=intent.is_post_only,
            is_rpi=intent.is_rpi # [NEW] 传递 RPI 标志
        )
        
        exchange_oid = self.gateway.send_order(req)
        
        if exchange_oid:
            # 6. 通知 OrderManager (包含 RPI 信息)
            event_data = OrderSubmitted(req, exchange_oid, time.time())
            self.order_monitor.on_order_submitted(Event(EVENT_ORDER_SUBMITTED, event_data))
            
            with self.lock:
                self.exchange_id_map[exchange_oid] = order
        else:
            # 发送失败 (如网络错误)，回滚状态 (标记为 Rejected)
            with self.lock:
                order.mark_rejected("Gateway Send Failed")
                self.exposure.update_open_orders(self.orders)
                self.account.calculate()
                
        return client_oid

    def cancel_order(self, client_oid: str):
        """
        [下行] 策略撤单入口
        """
        req_cancel = None
        
        with self.lock:
            order = self.orders.get(client_oid)
            # 只有活跃订单才能撤
            if not order or not order.is_active():
                return
            
            exch_oid = order.exchange_oid
            symbol = order.intent.symbol
            
            if exch_oid:
                req_cancel = CancelRequest(symbol, exch_oid)
            
        # 调用网关 (IO 放锁外)
        if req_cancel:
            self.gateway.cancel_order(req_cancel)

    # -----------------------------------------------------------
    # 处理交易所回报 (Single Source of Truth Update Flow)
    # -----------------------------------------------------------
    def on_exchange_update(self, event):
        """
        [上行] Gateway 收到 WebSocket 消息后推送到这里
        核心职责：
        1. 状态机流转 (NEW -> FILLED/CANCELED)
        2. 资金结算 (扣手续费)
        3. 持仓更新 (Net Position)
        4. 生命周期维护 (通知 OrderMonitor)
        5. 对外广播 (通知 Strategy/UI)
        """
        update: ExchangeOrderUpdate = event.data
        
        # 待推送事件列表 (在锁外推送，减少锁占用时间)
        events_to_push = []
        
        with self.lock:
            # 1. 寻找订单对象 (Single Source of Truth)
            # 优先用 client_oid (本地生成的UUID)，备用 exchange_oid (交易所ID)
            order = self.orders.get(update.client_oid)
            if not order and update.exchange_oid:
                order = self.exchange_id_map.get(update.exchange_oid)
            
            if not order: 
                # 可能是重启前的遗留单、手动单或乱序回报，暂忽略
                return

            # 2. 状态机流转
            prev_status = order.status
            ex_status = update.status # 交易所原始状态字符串
            
            if ex_status == "NEW":
                order.mark_new(update.exchange_oid)
                # 补录映射 (防止发单时没拿到 exchange_id 的情况)
                if update.exchange_oid:
                    self.exchange_id_map[update.exchange_oid] = order
                    
            elif ex_status == "CANCELED" or ex_status == "EXPIRED":
                order.mark_cancelled()
                
            elif ex_status == "REJECTED":
                order.mark_rejected()
                
            elif ex_status == "FILLED" or ex_status == "PARTIALLY_FILLED":
                # 计算本次增量成交量
                # Binance 推送的是 cum_filled_qty (累计成交)，需要减去内存里已知的 filled_volume
                delta_qty = update.cum_filled_qty - order.filled_volume
                
                # 只有增量 > 0 才处理 (防止重复推送导致重复扣费)
                if delta_qty > 1e-9:
                    # A. 更新订单自身状态 (Filled Volume, Avg Price)
                    order.add_fill(delta_qty, update.filled_price)
                    
                    # B. 更新 Exposure (核心净持仓变更)
                    self.exposure.on_fill(
                        order.intent.symbol, 
                        order.intent.side, 
                        delta_qty, 
                        update.filled_price
                    )
                    
                    # C. 更新 Account (余额变更：扣除手续费)
                    # 简化估算：统一按 Taker 费率扣除。
                    # 严谨做法：根据 update.is_maker 标志判断费率，或等待 Transaction 流
                    fee = delta_qty * update.filled_price * self.config["backtest"]["taker_fee"]
                    self.account.update_balance(0, fee) 
                    
                    # D. 生成成交事件 (Trade Event)
                    # TradeID 暂时用时间戳生成，实盘建议解析回报中的 TradeId
                    trade_data = TradeData(
                        symbol=order.intent.symbol,
                        order_id=order.client_oid,
                        trade_id=str(int(time.time()*1000)), 
                        side=order.intent.side.value,
                        price=update.filled_price,
                        volume=delta_qty,
                        datetime=datetime.now()
                    )
                    pos_data = self.exposure.get_position_data(order.intent.symbol)
                    events_to_push.append(Event(EVENT_TRADE_UPDATE, trade_data))

            # 3. 级联更新 (Cascade Update)
            
            # 3.1 通知 OrderManager 更新监控状态
            # (如果订单终结，OrderManager 会移除监控；如果成交，会更新最后活跃时间)
            if order.exchange_oid:
                self.order_monitor.on_order_update(order.exchange_oid, order.status)

            # 3.2 刷新挂单敞口 (Open Interest)
            # 订单状态变了(比如变Filled或Canceled)，占用的挂单额度就释放了
            self.exposure.update_open_orders(self.orders)
            
            # 3.3 刷新资金占用 (Margin)
            # 敞口或持仓变了，保证金占用就需要重算
            self.account.calculate()
            
            # 4. 准备对外推送
            
            # 只要状态变了，或者有部分成交，就通知策略
            if order.status != prev_status or ex_status == "PARTIALLY_FILLED":
                snapshot = order.to_snapshot()
                events_to_push.append(Event(EVENT_ORDER_UPDATE, snapshot))
                
                # 如果发生了成交，持仓必然变化，推送持仓更新
                if delta_qty > 1e-9: # 这里的 delta_qty 沿用上面的计算结果
                    pos_data = self.exposure.get_position_data(order.intent.symbol)
                    events_to_push.append(Event(EVENT_POSITION_UPDATE, pos_data))

        # 锁外推送事件 (避免阻塞核心锁，提高并发吞吐)
        for evt in events_to_push:
            self.event_engine.put(evt)
    # -----------------------------------------------------------
    # 辅助接口
    # -----------------------------------------------------------
    def on_order_submitted(self, event):
        """监听策略发出的 OrderSubmitted 事件"""
        self.order_monitor.on_order_submitted(event)

    def stop(self):
        self.order_monitor.stop()