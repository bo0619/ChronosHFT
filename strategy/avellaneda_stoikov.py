# file: strategy/avellaneda_stoikov.py

import time
import math
import numpy as np
from collections import defaultdict, deque
from .base import StrategyTemplate
from event.type import (
    EVENT_STRATEGY_UPDATE,
    Event,
    OrderBook,
    OrderIntent,
    OrderStateSnapshot,
    Side,
    StrategyData,
    TradeData,
)
from data.ref_data import ref_data_manager
from infrastructure.config_scaling import load_root_config

class AvellanedaStoikovStrategy(StrategyTemplate):
    """
    经典的 Avellaneda-Stoikov 做市策略
    适配 OMS 核心架构 (Step 11)
    """
    def __init__(self, engine, oms, strategy_config=None):
        # [修改] 适配新的基类构造函数
        super().__init__(engine, oms, "AvellanedaStoikov")
        
        self.config = (
            dict(strategy_config)
            if strategy_config is not None
            else self._load_strategy_config()
        )
        self.as_conf = self.config.get("as_parameters", {})
        self.use_rpi = bool(self.config.get("use_rpi", False)) and bool(
            self.as_conf.get(
                "use_rpi",
                self.config.get("use_rpi_for_avellaneda_stoikov", True),
            )
        )
        self.rpi_fallback_to_gtx = bool(
            self.as_conf.get(
                "rpi_fallback_to_gtx",
                self.config.get("rpi_fallback_to_gtx", True),
            )
        )
        
        # --- A-S 模型参数 ---
        self.gamma = self.as_conf.get("gamma", 0.05)
        self.k = self.as_conf.get("k", 1.5)
        self.vol_window = self.as_conf.get("vol_window", 60)
        self.interval = self.config.get("cycle_interval", 1.0)
        self.min_spread_ratio = self.as_conf.get("min_spread_ratio", 0.0002)
        
        self.lot_multiplier = self.config.get("lot_multiplier", 1.0)
        
        # --- 运行时状态 ---
        self.mid_prices = defaultdict(lambda: deque(maxlen=self.vol_window))
        self.last_recalc_time = defaultdict(float)
        self.current_sigma_sq = defaultdict(float)
        
        print(f"[{self.name}] A-S 模型已启动 (OMS驱动): Gamma={self.gamma}, K={self.k}, Cycle={self.interval}s")

    def _load_strategy_config(self):
        full_config = load_root_config("config.json")
        return full_config.get("strategy", {}) if full_config else {}

    def _calculate_volatility_sq(self, symbol):
        """计算短期回报率的方差 (sigma^2)"""
        prices_for_symbol = self.mid_prices[symbol]
        if len(prices_for_symbol) < 5: # 需要足够样本
            return 0.0
        
        prices = np.array(prices_for_symbol)
        log_returns = np.log(prices[1:] / prices[:-1])
        
        # 方差
        return float(np.var(log_returns))

    def _calculate_safe_vol(self, symbol, price):
        """计算符合交易所限制的下单量"""
        info = ref_data_manager.get_info(symbol)
        if not info:
            return 0.0
        safe_min = max(5.0, info.min_notional) * 1.1
        qty_val = safe_min / price
        target = max(info.min_qty, qty_val) * self.lot_multiplier
        return ref_data_manager.round_qty(symbol, target)

    def on_orderbook(self, ob: OrderBook):
        bid_1, _ = ob.get_best_bid()
        ask_1, _ = ob.get_best_ask()
        if bid_1 == 0:
            return
        
        mid_price = (bid_1 + ask_1) / 2.0
        self.mid_prices[ob.symbol].append(mid_price)
        
        # --- 1. 周期控制 ---
        now = time.time()
        if now - self.last_recalc_time[ob.symbol] < self.interval:
            return
        
        self.last_recalc_time[ob.symbol] = now

        # --- 2. 只撤本策略在该标的上的旧报价，并等待终态 ACK ---
        # 不能使用 symbol-wide mass cancel，否则会误撤同标的上的其他策略订单；
        # 也不能在撤单确认前立刻重挂，否则可能产生重叠报价。
        active_symbol_orders = [
            oid
            for oid, intent in self.active_orders.items()
            if intent.symbol == ob.symbol
        ]
        if active_symbol_orders:
            for oid in active_symbol_orders:
                self.cancel_order(oid)
            return

        # --- 3. A-S 核心计算 ---
        
        # A. 更新波动率
        self.current_sigma_sq[ob.symbol] = self._calculate_volatility_sq(ob.symbol)
        
        # B. 计算保留价格 (Reservation Price)
        # r = s - q * gamma * sigma^2 * T (T=1)
        # 这里的 q 是 self.pos (净持仓)
        strategy_positions = getattr(
            self.oms.exposure,
            "strategy_net_positions",
            {},
        )
        position = strategy_positions.get((self.name, ob.symbol))
        if position is None:
            position = self.oms.exposure.net_positions.get(ob.symbol, self.pos)
        inventory_risk_adjustment = (
            position * self.gamma * self.current_sigma_sq[ob.symbol]
        )
        reservation_price = mid_price - inventory_risk_adjustment
        
        # C. 计算最优价差 (Optimal Spread)
        # δ_a + δ_b = (2/gamma) * ln(1 + gamma/k)
        if self.k > 0:
            optimal_spread = (2.0 / self.gamma) * math.log(1.0 + self.gamma / self.k)
        else:
            optimal_spread = mid_price * 0.001 # Fallback
            
        # D. 结合波动率调整价差 (工程实践)
        # 波动越大，价差应该越宽，以保护自己
        # Spread = OptimalSpread + VolatilityAdjustment
        # 这里的 sigma 是收益率标准差，本身就是比例
        volatility_adjustment = (
            self.current_sigma_sq[ob.symbol] * self.gamma * mid_price
        )
        
        # 总价差
        total_spread = optimal_spread + volatility_adjustment
        
        # E. 应用最小价差保护
        min_spread_val = mid_price * self.min_spread_ratio
        passive_tif = self.resolve_passive_time_in_force(
            ob.symbol,
            use_rpi=self.use_rpi,
            fallback_to_gtx=self.rpi_fallback_to_gtx,
            route="avellaneda_stoikov_quote",
        )
        fee_spread_val = (
            mid_price
            * self.passive_round_trip_fee_bps(ob.symbol, passive_tif)
            / 10000.0
        )
        final_spread = max(total_spread, min_spread_val, fee_spread_val)

        # 4. 计算目标挂单价
        target_bid = reservation_price - final_spread / 2.0
        target_ask = reservation_price + final_spread / 2.0
        
        # 5. 规整化与安全检查
        # 策略层负责规整，OMS层负责最终校验
        target_bid = ref_data_manager.round_price(ob.symbol, target_bid)
        target_ask = ref_data_manager.round_price(ob.symbol, target_ask)
        
        # 6. 执行新挂单
        order_vol = self._calculate_safe_vol(ob.symbol, mid_price)
        if order_vol <= 0:
            return
        
        # 挂买单 (Bid)
        # 使用 PostOnly 确保我们是 Maker
        intent_buy = OrderIntent(
            strategy_id=self.name,
            symbol=ob.symbol,
            side=Side.BUY,
            price=target_bid,
            volume=order_vol,
            time_in_force=passive_tif,
            is_post_only=True,
        )
        bid_oid = self.send_intent(intent_buy)
        
        # 挂卖单 (Ask)
        intent_sell = OrderIntent(
            strategy_id=self.name,
            symbol=ob.symbol,
            side=Side.SELL,
            price=target_ask,
            volume=order_vol,
            time_in_force=passive_tif,
            is_post_only=True,
        )
        ask_oid = self.send_intent(intent_sell)

        quoted_spread = max(0.0, target_ask - target_bid)
        quoted_spread_bps = (
            quoted_spread / mid_price * 10000.0
            if mid_price > 0.0
            else 0.0
        )
        sigma_sq = float(self.current_sigma_sq[ob.symbol])
        sigma_bps = math.sqrt(max(0.0, sigma_sq)) * 10000.0
        alpha_bps = (
            (reservation_price - mid_price) / mid_price * 10000.0
            if mid_price > 0.0
            else 0.0
        )
        params = {
            "schema": "market_making.v1",
            "strategy": self.name,
            "state": "QUOTING",
            "mode": passive_tif,
            "time_in_force": passive_tif,
            "use_rpi": bool(self.use_rpi),
            "rpi_supported": ref_data_manager.supports_rpi(ob.symbol),
            "mid_price": mid_price,
            "best_bid": bid_1,
            "best_ask": ask_1,
            "market_spread_bps": (
                (ask_1 - bid_1) / mid_price * 10000.0
                if mid_price > 0.0
                else 0.0
            ),
            "fair_value": reservation_price,
            "alpha_bps": alpha_bps,
            "position_qty": float(position),
            "position_notional": float(position) * mid_price,
            "target_bid": target_bid,
            "target_ask": target_ask,
            "quote_spread_bps": quoted_spread_bps,
            "quote_qty": order_vol,
            "bid_order_id": bid_oid or "",
            "ask_order_id": ask_oid or "",
            "gamma": float(self.gamma),
            "k": float(self.k),
            "sigma_sq": sigma_sq,
            "sigma_bps": sigma_bps,
            "inventory_risk_adjustment": inventory_risk_adjustment,
            "reservation_price": reservation_price,
            "optimal_spread": optimal_spread,
            "volatility_adjustment": volatility_adjustment,
            "raw_spread": total_spread,
            "min_spread": min_spread_val,
            "fee_spread": fee_spread_val,
            "final_spread": final_spread,
            "final_spread_bps": (
                final_spread / mid_price * 10000.0
                if mid_price > 0.0
                else 0.0
            ),
            # Compatibility fields consumed by the existing TUI.
            "State": "QUOTING",
            "Mode": passive_tif,
            "Spread": f"{quoted_spread_bps:.1f}",
            "Sigma": f"{sigma_bps:.1f}",
            "Size": f"{order_vol:.8g}",
        }
        self.engine.put(
            Event(
                EVENT_STRATEGY_UPDATE,
                StrategyData(
                    symbol=ob.symbol,
                    fair_value=reservation_price,
                    alpha_bps=alpha_bps,
                    params=params,
                ),
            )
        )
        
        # 打印日志
        # self.log(f"Quoting Bid={target_bid} Ask={target_ask} | r={reservation_price:.2f} s={final_spread:.2f}")

    def on_trade(self, trade: TradeData):
        # A-S 模型不依赖 Trade 流，但可以打印日志
        # self.log(f"成交: {trade.side} @ {trade.price} Vol={trade.volume}")
        pass

    def on_order(self, snapshot: OrderStateSnapshot):
        # 调用基类处理 active_orders
        super().on_order(snapshot)
        # 可选：如果订单被 Reject，可以在这里加入重试或调整逻辑
