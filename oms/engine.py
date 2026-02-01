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
        [Fixed] 处理交易所回报
        修复了 delta_qty 未定义的问题
        """
        update: ExchangeOrderUpdate = event.data
        events_to_push = []
        
        # [修复] 在锁外初始化 delta_qty，确保后续判断逻辑永远可以访问该变量
        delta_qty = 0.0
        
        with self.lock:
            order = self.orders.get(update.client_oid)
            if not order and update.exchange_oid:
                order = self.exchange_id_map.get(update.exchange_oid)
            
            if not order: return

            prev_status = order.status
            ex_status = update.status
            
            if ex_status == "NEW":
                order.mark_new(update.exchange_oid)
                if update.exchange_oid:
                    self.exchange_id_map[update.exchange_oid] = order
            elif ex_status in ["CANCELED", "EXPIRED"]:
                order.mark_cancelled()
            elif ex_status == "REJECTED":
                order.mark_rejected()
            elif ex_status in ["FILLED", "PARTIALLY_FILLED"]:
                # 计算成交增量
                delta_qty = update.cum_filled_qty - order.filled_volume
                
                if delta_qty > 1e-9:
                    order.add_fill(delta_qty, update.filled_price)
                    self.exposure.on_fill(
                        order.intent.symbol, 
                        order.intent.side, 
                        delta_qty, 
                        update.filled_price
                    )
                    # 手续费扣除
                    fee = delta_qty * update.filled_price * self.config["backtest"].get("taker_fee", 0.0005)
                    self.account.update_balance(0, fee) 
                    
                    trade_data = TradeData(
                        symbol=order.intent.symbol,
                        order_id=order.client_oid,
                        trade_id=str(int(time.time()*1000)), 
                        side=order.intent.side.value,
                        price=update.filled_price,
                        volume=delta_qty,
                        datetime=datetime.now()
                    )
                    events_to_push.append(Event(EVENT_TRADE_UPDATE, trade_data))

            # 级联刷新
            if order.exchange_oid:
                self.order_monitor.on_order_update(order.exchange_oid, order.status)

            self.exposure.update_open_orders(self.orders)
            self.account.calculate()
            
            # 推送更新
            if order.status != prev_status or ex_status == "PARTIALLY_FILLED":
                events_to_push.append(Event(EVENT_ORDER_UPDATE, order.to_snapshot()))
                
                # [修复点] 只有在 delta_qty 真的发生变化（成交）时，才推送持仓更新
                if delta_qty > 0:
                    pos_data = self.exposure.get_position_data(order.intent.symbol)
                    events_to_push.append(Event(EVENT_POSITION_UPDATE, pos_data))

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

    def sync_with_exchange(self):
        """
        [Updated] 启动时同步：从交易所拉取真实持仓和账户余额
        """
        logger.info("OMS: Starting Full Synchronization with Binance...")
        
        # 1. 同步账户资金 (Balance & Margin)
        acc_data = self.gateway.get_account_info()
        if acc_data:
            total_balance = float(acc_data["totalWalletBalance"])
            total_used_margin = float(acc_data["totalInitialMargin"])
            self.account.force_sync(total_balance, total_used_margin)
            logger.info(f"OMS Account Synced: Balance={total_balance} U, UsedMargin={total_used_margin} U")
        else:
            logger.error("OMS: Failed to sync account info.")

        # 2. 同步持仓 (Positions)
        pos_res = self.gateway.get_all_positions()
        if pos_res:
            count = 0
            for item in pos_res:
                amt = float(item["positionAmt"])
                if amt == 0: continue
                symbol = item["symbol"]
                entry_price = float(item["entryPrice"])
                self.exposure.force_sync(symbol, amt, entry_price)
                
                # 广播持仓事件
                pos_data = self.exposure.get_position_data(symbol)
                self.event_engine.put(Event(EVENT_POSITION_UPDATE, pos_data))
                logger.info(f"OMS Position Synced: {symbol} Vol={amt} @ {entry_price}")
                count += 1
            logger.info(f"OMS Sync Complete. {count} positions loaded.")
        else:
            logger.error("OMS: Failed to sync positions.")