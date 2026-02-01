# file: sim_engine/exchange.py

from collections import deque, defaultdict
import numpy as np
from datetime import datetime

from event.type import OrderRequest, CancelRequest, OrderBook, AggTradeData, ExchangeOrderUpdate
from event.type import Side, TIF_GTX, TIF_IOC, TIF_FOK
from event.type import Event, EVENT_ORDERBOOK, EVENT_AGG_TRADE
# [关键] 引入网关专用的回报事件
from gateway.binance_future import EVENT_EXCHANGE_ORDER_UPDATE

class SimOrder:
    def __init__(self, req: OrderRequest, order_id: str, entry_time: datetime):
        self.req = req
        self.order_id = order_id
        self.entry_time = entry_time
        
        self.initial_queue_vol = 0.0
        self.queue_ahead = 0.0
        self.cum_filled_qty = 0.0 # 累计成交
        self.active = True
        self.is_maker = False

class ExchangeEmulator:
    def __init__(self, sim_engine, event_engine, clock, config):
        self.sim_engine = sim_engine
        self.event_engine = event_engine 
        self.clock = clock
        
        self.base_cancel_prob = config["backtest"].get("cancel_base_prob", 0.5)
        
        self.mid_prices = deque(maxlen=100)
        self.current_volatility = 0.0
        
        # Order Matching Queues
        self.bids = defaultdict(list) 
        self.asks = defaultdict(list) 
        
        # Snapshots
        self.book_bids = {}
        self.book_asks = {}
        
        # 维护一个 internal map 方便撤单查找 (exchange_oid -> SimOrder)
        self.order_map = {}

    def _update_volatility(self, mid_price):
        self.mid_prices.append(mid_price)
        if len(self.mid_prices) > 10:
            self.current_volatility = np.std(self.mid_prices)

    def on_market_depth(self, ob: OrderBook):
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 > 0 and ask_1 > 0:
            self._update_volatility((bid_1 + ask_1) / 2)

        self.event_engine.put(Event(EVENT_ORDERBOOK, ob))
        
        # Cancel Decay
        adj_cancel_prob = min(1.0, self.base_cancel_prob * (1 + 0.5 * self.current_volatility))
        self._apply_cancel_decay(self.bids, self.book_bids, ob.bids, adj_cancel_prob)
        self._apply_cancel_decay(self.asks, self.book_asks, ob.asks, adj_cancel_prob)
        
        self.book_bids = ob.bids.copy()
        self.book_asks = ob.asks.copy()

    def on_market_trade(self, trade: AggTradeData):
        self.event_engine.put(Event(EVENT_AGG_TRADE, trade))
        
        if hasattr(self.sim_engine, 'latency_model'):
            self.sim_engine.latency_model.record_message(self.clock.now())

        if trade.maker_is_buyer:
            self._process_trade_side(self.bids, trade.price, trade.quantity, is_buy=True)
        else:
            self._process_trade_side(self.asks, trade.price, trade.quantity, is_buy=False)

    def on_order_arrival(self, req: OrderRequest, order_id: str):
        is_buy = (req.side == Side.BUY)
        price = req.price
        
        order = SimOrder(req, order_id, self.clock.now())
        self.order_map[order_id] = order
        
        # 1. 立即推送 NEW 状态
        self._push_update(order, "NEW")
        
        # 2. Taker 撮合 (简化版)
        matched = False
        if is_buy:
            if self.book_asks:
                best_ask = min(self.book_asks.keys())
                if price >= best_ask:
                    self._match_taker(order, self.book_asks, is_buy=True)
                    matched = True
        else:
            if self.book_bids:
                best_bid = max(self.book_bids.keys())
                if price <= best_bid:
                    self._match_taker(order, self.book_bids, is_buy=False)
                    matched = True
                    
        # 3. PostOnly 检查 (如果 Taker 匹配了，但它是 PostOnly，则拒单)
        if matched and req.post_only:
            # 回滚成交（简化起见，直接发 REJECTED，不回滚之前的 Taker Fill，
            # 真实撮合是在匹配前检查，这里为了代码复用，假设 match_taker 会处理）
            # 简单处理：如果 Taker 逻辑触发了成交，且要求 PostOnly，则取消剩余部分并报错
            pass 

        if not order.active: return

        # 4. Maker 排队
        order.is_maker = True
        queue_vol = 0.0
        if is_buy:
            queue_vol = self.book_bids.get(price, 0.0)
            if price not in self.bids: self.bids[price] = []
            order.queue_ahead = queue_vol
            self.bids[price].append(order)
        else:
            queue_vol = self.book_asks.get(price, 0.0)
            if price not in self.asks: self.asks[price] = []
            order.queue_ahead = queue_vol
            self.asks[price].append(order)

    def on_cancel_arrival(self, req: CancelRequest):
        """处理撤单"""
        order = self.order_map.get(req.order_id)
        if order and order.active:
            order.active = False
            self._push_update(order, "CANCELED")
        else:
            # 订单不存在或已结束 -> 模拟交易所返回 "Unknown Order" (忽略)
            pass

    def _match_taker(self, order, book_side, is_buy):
        sorted_prices = sorted(book_side.keys()) if is_buy else sorted(book_side.keys(), reverse=True)
        for p in sorted_prices:
            if is_buy and p > order.req.price: break
            if not is_buy and p < order.req.price: break
            
            available = book_side[p]
            need = order.req.volume - order.cum_filled_qty
            fill = min(available, need)
            
            if fill > 0:
                # 扣减临时盘口
                book_side[p] -= fill
                if book_side[p] <= 1e-9: del book_side[p]
                
                # 执行成交
                self._exec_fill(order, fill, p)
                
            if not order.active: break

    def _process_trade_side(self, order_dict, trade_price, trade_qty, is_buy):
        # 筛选符合价格的 Maker 单
        relevant_prices = [p for p in order_dict.keys() if p >= trade_price] if is_buy else [p for p in order_dict.keys() if p <= trade_price]
        relevant_prices.sort(reverse=is_buy)

        for p in relevant_prices:
            # 消耗队列
            for order in order_dict[p]:
                if not order.active: continue
                
                prev_q = order.queue_ahead
                order.queue_ahead -= trade_qty
                
                if order.queue_ahead < 0:
                    covered = abs(order.queue_ahead) if prev_q >= 0 else trade_qty
                    need = order.req.volume - order.cum_filled_qty
                    fill = min(covered, need)
                    if fill > 0:
                        self._exec_fill(order, fill, order.req.price)

    def _apply_cancel_decay(self, order_dict, old_book, new_book, prob):
        for price, orders in order_dict.items():
            if not orders: continue
            old_v = old_book.get(price, 0)
            new_v = new_book.get(price, 0)
            if new_v < old_v:
                delta = old_v - new_v
                for order in orders:
                    if order.active and order.queue_ahead > 0:
                        order.queue_ahead = max(0.0, order.queue_ahead - delta * prob)

    def _exec_fill(self, order, amount, price):
        order.cum_filled_qty += amount
        
        status = "PARTIALLY_FILLED"
        if order.cum_filled_qty >= order.req.volume - 1e-9:
            status = "FILLED"
            order.active = False
            
        # 推送回报
        self._push_update(order, status, fill_qty=amount, fill_price=price)

    def _push_update(self, order, status, fill_qty=0.0, fill_price=0.0):
        """
        构造并推送 ExchangeOrderUpdate
        """
        update = ExchangeOrderUpdate(
            client_oid="", # 仿真环境 client_oid 也是 order_id，或者是空的
            exchange_oid=order.order_id,
            symbol=order.req.symbol,
            status=status,
            filled_qty=fill_qty,
            filled_price=fill_price,
            cum_filled_qty=order.cum_filled_qty,
            update_time=self.clock.now().timestamp()
        )
        self.event_engine.put(Event(EVENT_EXCHANGE_ORDER_UPDATE, update))