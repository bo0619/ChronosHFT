# file: sim_engine/exchange.py

from collections import deque, defaultdict
import numpy as np
from datetime import datetime

from event.type import OrderRequest, OrderData, TradeData, OrderBook, AggTradeData
from event.type import Side_BUY, Side_SELL
from event.type import Status_SUBMITTED, Status_PARTTRADED, Status_ALLTRADED, Status_CANCELLED
from event.type import Event, EVENT_ORDERBOOK, EVENT_AGG_TRADE, EVENT_TRADE_UPDATE, EVENT_ORDER_UPDATE

class SimOrder:
    def __init__(self, req: OrderRequest, order_id: str, entry_time: datetime):
        self.req = req
        self.order_id = order_id
        self.entry_time = entry_time
        self.initial_queue_vol = 0.0
        self.queue_ahead = 0.0
        self.filled = 0.0
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
        
        self.bids = defaultdict(list) 
        self.asks = defaultdict(list) 
        self.book_bids = {}
        self.book_asks = {}
        self.trade_cnt = 0

    def _update_volatility(self, mid_price):
        self.mid_prices.append(mid_price)
        if len(self.mid_prices) > 10: self.current_volatility = np.std(self.mid_prices)

    def on_market_depth(self, ob: OrderBook):
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 > 0 and ask_1 > 0: self._update_volatility((bid_1 + ask_1) / 2)

        self.event_engine.put(Event(EVENT_ORDERBOOK, ob))
        
        adj_cancel_prob = min(1.0, self.base_cancel_prob * (1 + 0.5 * self.current_volatility))
        self._apply_cancel_decay(self.bids, self.book_bids, ob.bids, adj_cancel_prob)
        self._apply_cancel_decay(self.asks, self.book_asks, ob.asks, adj_cancel_prob)
        self.book_bids = ob.bids.copy()
        self.book_asks = ob.asks.copy()

    def on_market_trade(self, trade: AggTradeData):
        self.event_engine.put(Event(EVENT_AGG_TRADE, trade))
        if hasattr(self.sim_engine, 'latency_model'):
            self.sim_engine.latency_model.record_message(self.clock.now())

        if trade.maker_is_buyer: self._process_trade_side(self.bids, trade.price, trade.quantity, is_buy=True)
        else: self._process_trade_side(self.asks, trade.price, trade.quantity, is_buy=False)

    def on_order_arrival(self, req: OrderRequest, order_id: str):
        is_buy = (req.side == Side_BUY)
        price = req.price
        order = SimOrder(req, order_id, self.clock.now())
        
        # Taker Check
        if is_buy:
            if self.book_asks:
                best_ask = min(self.book_asks.keys())
                if price >= best_ask: self._match_taker(order, self.book_asks, is_buy=True)
        else:
            if self.book_bids:
                best_bid = max(self.book_bids.keys())
                if price <= best_bid: self._match_taker(order, self.book_bids, is_buy=False)
        
        if not order.active: return

        # Maker Queue
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
            
        o_data = OrderData(req.symbol, order_id, req.side, price, req.volume, 0, Status_SUBMITTED, self.clock.now())
        self.event_engine.put(Event(EVENT_ORDER_UPDATE, o_data))

    def _match_taker(self, order, book_side, is_buy):
        sorted_prices = sorted(book_side.keys()) if is_buy else sorted(book_side.keys(), reverse=True)
        for p in sorted_prices:
            if is_buy and p > order.req.price: break
            if not is_buy and p < order.req.price: break
            
            fill_qty = min(book_side[p], order.req.volume - order.filled)
            if fill_qty > 0:
                self._exec_fill(order, fill_qty, p)
                book_side[p] -= fill_qty
                if book_side[p] <= 1e-9: del book_side[p]
            if not order.active: break

    def _apply_cancel_decay(self, order_dict, old_book, new_book, prob):
        for price, orders in order_dict.items():
            if not orders: continue
            old_vol = old_book.get(price, 0)
            new_vol = new_book.get(price, 0)
            if new_vol < old_vol:
                delta = old_vol - new_vol
                for order in orders:
                    if order.active and order.queue_ahead > 0:
                        order.queue_ahead = max(0.0, order.queue_ahead - delta * prob)

    def _process_trade_side(self, order_dict, trade_price, trade_qty, is_buy):
        relevant_prices = [p for p in order_dict.keys() if p >= trade_price] if is_buy else [p for p in order_dict.keys() if p <= trade_price]
        relevant_prices.sort(reverse=is_buy) # Buy: 高价优先, Sell: 低价优先 (Match逻辑)
        
        # Fix sort: Buy side (Bids) should sort Descending (High->Low). Sell side (Asks) Ascending.
        # But wait, maker_is_buyer=True means Taker Sold into Bids. So we consume Bids >= trade_price.
        # Highest bid is matched first. Correct.
        # maker_is_buyer=False means Taker Bought from Asks. Consume Asks <= trade_price.
        # Lowest ask matched first. Correct.
        
        # 修正: relevant_prices.sort(reverse=is_buy) 是对的
        # Buy(Bids): reverse=True (Descending)
        # Sell(Asks): reverse=False (Ascending)

        for p in relevant_prices:
            self._consume_queue(order_dict[p], trade_qty)

    def _consume_queue(self, orders, trade_qty):
        for order in orders:
            if not order.active: continue
            prev_queue = order.queue_ahead
            order.queue_ahead -= trade_qty
            if order.queue_ahead < 0:
                covered_vol = abs(order.queue_ahead) if prev_queue >= 0 else trade_qty
                fill = min(covered_vol, order.req.volume - order.filled)
                if fill > 0: self._exec_fill(order, fill, order.req.price)

    def _exec_fill(self, order, amount, price):
        order.filled += amount
        self.trade_cnt += 1
        t = TradeData(order.req.symbol, order.order_id, f"SIM{self.trade_cnt}", 
                      order.req.side, price, amount, self.clock.now())
        self.event_engine.put(Event(EVENT_TRADE_UPDATE, t))
        
        status = Status_PARTTRADED
        if order.filled >= order.req.volume - 1e-8:
            status = Status_ALLTRADED
            order.active = False
        o = OrderData(order.req.symbol, order.order_id, order.req.side,
                      price, order.req.volume, order.filled, status, self.clock.now())
        self.event_engine.put(Event(EVENT_ORDER_UPDATE, o))