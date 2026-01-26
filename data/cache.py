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

    # --- 更新接口 ---
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
        """获取标记价格"""
        data = self.mark_prices.get(symbol)
        if data:
            return data.mark_price
        
        # Fallback: 如果没有标记价格，尝试用盘口中间价
        ob = self.books.get(symbol)
        if ob:
            b, _ = ob.get_best_bid()
            a, _ = ob.get_best_ask()
            if b > 0 and a > 0:
                return (b + a) / 2
        return 0.0

    def get_best_quote(self, symbol):
        """
        [修复] 获取 BBA (买一价, 卖一价)
        返回: (bid_price, ask_price)
        """
        ob = self.books.get(symbol)
        if not ob:
            return 0.0, 0.0
        
        bid = ob.get_best_bid()[0]
        ask = ob.get_best_ask()[0]
        return bid, ask

    def get_last_trade_price(self, symbol):
        tr = self.last_trades.get(symbol)
        return tr.price if tr else 0.0

# 全局单例
data_cache = LiveDataCache()