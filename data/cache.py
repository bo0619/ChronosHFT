# file: data/cache.py

from event.type import OrderBook, MarkPriceData, AggTradeData

class LiveDataCache:
    """
    实时数据缓存 (替代 Redis)
    提供 O(1) 时间复杂度的最新数据查询
    """
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(LiveDataCache, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "books"): return
        self.books = {}       # Symbol -> OrderBook
        self.mark_prices = {} # Symbol -> MarkPriceData
        self.last_trades = {} # Symbol -> AggTradeData

    def update_book(self, ob: OrderBook):
        self.books[ob.symbol] = ob

    def update_mark_price(self, mp: MarkPriceData):
        self.mark_prices[mp.symbol] = mp

    def update_trade(self, tr: AggTradeData):
        self.last_trades[tr.symbol] = tr

    # --- 查询接口 ---
    def get_book(self, symbol):
        return self.books.get(symbol)

    def get_mark_price(self, symbol):
        data = self.mark_prices.get(symbol)
        return data.mark_price if data else 0.0

    def get_best_quote(self, symbol):
        """获取 BBA (买一/卖一)"""
        ob = self.books.get(symbol)
        if not ob: return 0.0, 0.0
        return ob.get_best_bid()[0], ob.get_best_ask()[0]

data_cache = LiveDataCache()