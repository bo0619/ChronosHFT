# file: sim_engine/exchange.py

from collections import deque, defaultdict
import numpy as np
from datetime import datetime

from event.type import OrderRequest, OrderData, TradeData, OrderBook, AggTradeData
from event.type import Direction_LONG, Direction_SHORT, Action_OPEN, Action_CLOSE
from event.type import Status_SUBMITTED, Status_PARTTRADED, Status_ALLTRADED, Status_CANCELLED, Status_REJECTED
from event.type import Event, EVENT_ORDERBOOK, EVENT_AGG_TRADE, EVENT_TRADE_UPDATE, EVENT_ORDER_UPDATE

class SimOrder:
    """
    仿真环境中的订单状态对象
    """
    def __init__(self, req: OrderRequest, order_id: str, entry_time: datetime):
        self.req = req
        self.order_id = order_id
        self.entry_time = entry_time
        
        # 排队相关
        self.initial_queue_vol = 0.0
        self.queue_ahead = 0.0
        self.filled = 0.0
        self.active = True
        self.is_maker = False

class ExchangeEmulator:
    """
    高保真交易所仿真器 (Step 9 完整版)
    包含：
    1. Stateful OrderBook (全量状态维护)
    2. Volatility Calculation (波动率计算)
    3. Queue Decay Model (基于深度变化的排队衰减)
    4. FIFO Matching (严格的先入先出撮合)
    5. Taker/Maker Hybrid Logic (穿价立即成交，不穿价排队)
    """
    def __init__(self, sim_engine, event_engine, clock, config):
        self.sim_engine = sim_engine
        self.event_engine = event_engine 
        self.clock = clock
        
        # 基础撤单概率 (来自配置)
        self.base_cancel_prob = config["backtest"].get("cancel_base_prob", 0.5)
        
        # 波动率计算器
        self.mid_prices = deque(maxlen=100) # 记录最近100个 Tick 的中间价
        self.current_volatility = 0.0
        
        # 订单队列: Price -> List[SimOrder]
        self.bids = defaultdict(list) 
        self.asks = defaultdict(list) 
        
        # 市场盘口快照 (Price -> Volume)
        self.book_bids = {}
        self.book_asks = {}
        
        # 成交计数ID
        self.trade_cnt = 0

    def _update_volatility(self, mid_price):
        """更新市场波动率因子"""
        self.mid_prices.append(mid_price)
        if len(self.mid_prices) > 10:
            # 计算标准差作为波动率指标
            self.current_volatility = np.std(self.mid_prices)

    def on_market_depth(self, ob: OrderBook):
        """
        处理 L2 深度快照更新
        触发：波动率更新、行情推送、撤单衰减计算
        """
        # 1. 更新波动率
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 > 0 and ask_1 > 0:
            self._update_volatility((bid_1 + ask_1) / 2)

        # 2. 推送行情给策略 (立即推送)
        self.event_engine.put(Event(EVENT_ORDERBOOK, ob))
        
        # 3. 动态调整撤单概率 (Queue Decay Probability)
        # 逻辑：波动率越高，市场越不稳定，排在你前面的单子撤单概率越高
        # 模型：P = BaseP * (1 + k * Vol)
        adj_cancel_prob = min(1.0, self.base_cancel_prob * (1 + 0.5 * self.current_volatility))
        
        # 4. 应用撤单衰减
        self._apply_cancel_decay(self.bids, self.book_bids, ob.bids, adj_cancel_prob)
        self._apply_cancel_decay(self.asks, self.book_asks, ob.asks, adj_cancel_prob)
        
        # 5. 更新本地 OrderBook 状态
        self.book_bids = ob.bids.copy()
        self.book_asks = ob.asks.copy()

    def on_market_trade(self, trade: AggTradeData):
        """
        处理市场真实成交 (AggTrade)
        触发：排队量消耗、Maker成交
        """
        self.event_engine.put(Event(EVENT_AGG_TRADE, trade))
        
        # 记录消息到达以计算负载 (用于 LatencyModel 的拥堵计算)
        if hasattr(self.sim_engine, 'latency_model'):
            self.sim_engine.latency_model.record_message(self.clock.now())

        # 核心撮合逻辑：
        # 如果 maker_is_buyer=True，说明是卖方(Taker)主动砸盘，消耗买单(Bids)队列
        # 如果 maker_is_buyer=False，说明是买方(Taker)主动拉盘，消耗卖单(Asks)队列
        if trade.maker_is_buyer:
            self._process_trade_side(self.bids, trade.price, trade.quantity, is_buy=True)
        else:
            self._process_trade_side(self.asks, trade.price, trade.quantity, is_buy=False)

    def on_order_arrival(self, req: OrderRequest, order_id: str):
        """
        处理策略订单到达交易所
        包含：Taker 立即成交逻辑 + Maker 排队逻辑
        """
        # 判断方向
        is_buy = (req.direction == Direction_LONG and req.action == Action_OPEN) or \
                 (req.direction == Direction_SHORT and req.action == Action_CLOSE)
        
        price = req.price
        
        # 构建 SimOrder 对象
        order = SimOrder(req, order_id, self.clock.now())
        
        # --- 1. Taker Logic (穿价撮合) ---
        # 如果买单价 >= 卖一价，或者 卖单价 <= 买一价，立即与当前 Book 撮合
        active_match = False
        
        if is_buy:
            # 买单：检查 Asks (从低到高)
            if self.book_asks:
                best_ask = min(self.book_asks.keys())
                if price >= best_ask:
                    active_match = True
                    self._match_taker(order, self.book_asks, is_buy=True)
        else:
            # 卖单：检查 Bids (从高到低)
            if self.book_bids:
                best_bid = max(self.book_bids.keys())
                if price <= best_bid:
                    active_match = True
                    self._match_taker(order, self.book_bids, is_buy=False)
        
        # 如果 Taker 逻辑后订单已完全成交，则无需入队
        if not order.active:
            # 即使全成，也需要推送一个 SUBMITTED 状态作为开始（或者是直接 FILLED）
            # 为了状态机完整性，通常先 SUBMITTED 再 FILLED
            # 但这里我们简化，在 _match_taker 内部已经推送了 UPDATE
            return

        # --- 2. Maker Logic (挂单排队) ---
        # 如果还有剩余量，进入队列
        order.is_maker = True
        
        # 计算初始排队位置 (Queue Ahead)
        # 即：下单瞬间，该价格档位已经存在的量
        queue_vol = 0.0
        if is_buy:
            queue_vol = self.book_bids.get(price, 0.0)
            if price not in self.bids: self.bids[price] = []
            order.queue_ahead = queue_vol
            order.initial_queue_vol = queue_vol
            self.bids[price].append(order)
        else:
            queue_vol = self.book_asks.get(price, 0.0)
            if price not in self.asks: self.asks[price] = []
            order.queue_ahead = queue_vol
            order.initial_queue_vol = queue_vol
            self.asks[price].append(order)
            
        # 推送订单确认 (Submitted)
        o_data = OrderData(
            req.symbol, order_id, req.direction, req.action, 
            price, req.volume, order.filled, 
            Status_SUBMITTED, self.clock.now()
        )
        self.event_engine.put(Event(EVENT_ORDER_UPDATE, o_data))

    def _match_taker(self, order, book_side, is_buy):
        """
        Taker 撮合核心：与 Snapshot 直接交易
        """
        # 排序对手盘价格
        # 买单吃卖盘(Asks): 价格从低到高
        # 卖单吃买盘(Bids): 价格从高到低
        if is_buy:
            sorted_prices = sorted(book_side.keys())
        else:
            sorted_prices = sorted(book_side.keys(), reverse=True)
            
        limit_price = order.req.price
        
        for p in sorted_prices:
            # 价格保护检查
            if is_buy and p > limit_price: break
            if not is_buy and p < limit_price: break
            
            available_vol = book_side[p]
            need_vol = order.req.volume - order.filled
            
            # 成交量
            fill_qty = min(available_vol, need_vol)
            
            if fill_qty > 0:
                # 执行成交
                self._exec_fill(order, fill_qty, p) # Taker按对手价成交
                
                # 扣减 Snapshot 中的量 (模拟吃单对盘口的冲击，仅对本次撮合有效，
                # 因为下一个 on_market_depth 会重置 book)
                book_side[p] -= fill_qty
                if book_side[p] <= 1e-9:
                    del book_side[p]
                
            if not order.active:
                break

    def _apply_cancel_decay(self, order_dict, old_book, new_book, prob):
        """
        应用撤单衰减：
        如果盘口量减少了，且不是因为成交（Trade事件单独处理），
        则认为是撤单，按概率减少排队量。
        """
        for price, orders in order_dict.items():
            if not orders: continue
            
            old_vol = old_book.get(price, 0)
            new_vol = new_book.get(price, 0)
            
            # 如果量减少了
            if new_vol < old_vol:
                delta = old_vol - new_vol
                
                # 对该价格下的所有活跃订单应用衰减
                for order in orders:
                    if order.active and order.queue_ahead > 0:
                        # 计算衰减量
                        decay = delta * prob
                        # 更新排队位置
                        order.queue_ahead = max(0.0, order.queue_ahead - decay)

    def _process_trade_side(self, order_dict, trade_price, trade_qty, is_buy):
        """
        处理 AggTrade 对 Maker 队列的消耗
        """
        # 筛选受影响的价格层
        # 对于买单队列 (Bids): 只有 TradePrice <= OrderPrice 才能成交 (卖方砸到了我的价位)
        # 对于卖单队列 (Asks): 只有 TradePrice >= OrderPrice 才能成交 (买方吃到了我的价位)
        relevant_prices = []
        
        if is_buy: # Bids
            relevant_prices = [p for p in order_dict.keys() if p >= trade_price]
            # 价格优先：高价买单先成交
            relevant_prices.sort(reverse=True)
        else: # Asks
            relevant_prices = [p for p in order_dict.keys() if p <= trade_price]
            # 价格优先：低价卖单先成交
            relevant_prices.sort()

        # 注意：这里我们假设 Trade 是横扫 (Sweep) 了这些价格层
        # 实际上 trade_qty 是总成交量。我们需要分配这个量。
        # 简化模型：每一笔 Trade 事件，对于由于价格匹配而处于 "成交区" 的订单，
        # 都将其 queue_ahead 减去 trade_qty。
        
        for p in relevant_prices:
            self._consume_queue(order_dict[p], trade_qty, self.clock.now())

    def _consume_queue(self, orders, trade_qty, dt):
        """
        消耗特定价格层级的队列
        """
        for order in orders:
            if not order.active: continue
            
            prev_queue = order.queue_ahead
            # 核心机制：排队量减少
            order.queue_ahead -= trade_qty
            
            # 如果排队量变成负数，说明轮到我了
            if order.queue_ahead < 0:
                # 计算可成交量
                # 情况1: 之前还要排队(prev>=0)，现在穿透了。成交量 = 穿透部分 abs(new_queue)
                # 情况2: 之前就已经穿透(prev<0)，现在继续穿透。成交量 = trade_qty (全部吃掉)
                
                covered_vol = 0.0
                if prev_queue >= 0:
                    covered_vol = abs(order.queue_ahead)
                else:
                    covered_vol = trade_qty
                
                need_vol = order.req.volume - order.filled
                fill_qty = min(covered_vol, need_vol)
                
                if fill_qty > 0:
                    self._exec_fill(order, fill_qty, order.req.price) # Maker按委托价成交

    def _exec_fill(self, order, amount, price):
        """
        执行成交动作，生成事件
        """
        order.filled += amount
        self.trade_cnt += 1
        
        # 1. Trade Event
        t = TradeData(
            symbol=order.req.symbol, 
            order_id=order.order_id, 
            trade_id=f"SIM{self.trade_cnt}", 
            direction=order.req.direction, 
            action=order.req.action, 
            price=price, 
            volume=amount, 
            datetime=self.clock.now()
        )
        self.event_engine.put(Event(EVENT_TRADE_UPDATE, t))
        
        # 2. Order Status Update
        status = Status_PARTTRADED
        if order.filled >= order.req.volume - 1e-8:
            status = Status_ALLTRADED
            order.active = False # 标记为非活跃，移出撮合循环
            
        o = OrderData(
            symbol=order.req.symbol, 
            order_id=order.order_id, 
            direction=order.req.direction, 
            action=order.req.action,
            price=order.req.price, 
            volume=order.req.volume, 
            traded=order.filled, 
            status=status, 
            datetime=self.clock.now()
        )
        self.event_engine.put(Event(EVENT_ORDER_UPDATE, o))

    def on_cancel_arrival(self, req: "CancelRequest"):
        """
        撤单指令到达交易所
        """
        order_found = None
        target_list = None
        
        # 1. 在 Bids 和 Asks 队列中寻找该订单
        # 这是一个 O(N) 操作，但 HFT 挂单通常不多，可以接受
        
        # 搜索 Bids
        for price, orders in self.bids.items():
            for o in orders:
                if o.order_id == req.order_id and o.active:
                    order_found = o
                    target_list = orders
                    break
            if order_found: break
            
        # 搜索 Asks (如果在 Bids 没找到)
        if not order_found:
            for price, orders in self.asks.items():
                for o in orders:
                    if o.order_id == req.order_id and o.active:
                        order_found = o
                        target_list = orders
                        break
                if order_found: break
        
        # 2. 处理撤单逻辑
        if order_found:
            # 标记为非活跃 (防止后续被撮合)
            order_found.active = False
            
            # 从队列中移除 (可选，为了性能最好移除，或者下次撮合时 lazy remove)
            # 这里简单处理：不做物理移除，依赖 active=False 标志位
            
            # 推送 CANCELLED 状态
            o_data = OrderData(
                req.symbol, req.order_id, 
                order_found.req.direction, order_found.req.action,
                order_found.req.price, order_found.req.volume, 
                order_found.filled, 
                Status_CANCELLED, 
                self.clock.now()
            )
            self.event_engine.put(Event(EVENT_ORDER_UPDATE, o_data))
        else:
            # 订单未找到，或者已经完全成交/已撤销
            # 真实交易所会返回 "Unknown Order" 或 "Order Filled"，回测中暂忽略
            pass