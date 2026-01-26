# file: data/cache.py

from event.type import OrderBook, MarkPriceData, AggTradeData

class LiveDataCache:
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(LiveDataCache, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "books"): return
        self.books = {}
        self.mark_prices = {}
        self.last_trades = {}

    def update_book(self, ob: OrderBook): self.books[ob.symbol] = ob
    def update_mark_price(self, mp: MarkPriceData): self.mark_prices[mp.symbol] = mp
    def update_trade(self, tr: AggTradeData): self.last_trades[tr.symbol] = tr

    # --- 查询接口 ---
    def get_mark_price(self, symbol):
        """获取标记价格，如果没有则回退到盘口中间价，再没有则为0"""
        mp = self.mark_prices.get(symbol)
        if mp: return mp.mark_price
        
        # Fallback
        ob = self.books.get(symbol)
        if ob:
            b, _ = ob.get_best_bid()
            a, _ = ob.get_best_ask()
            if b > 0 and a > 0: return (b + a) / 2
        return 0.0

data_cache = LiveDataCache()