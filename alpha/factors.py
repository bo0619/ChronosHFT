# file: alpha/factors.py
# [FIX-SIGMA] GLFTCalibrator: sigma 时间尺度错误

import math
import time
import json
import numpy as np
from collections import deque
from event.type import OrderBook, TradeData, AggTradeData


class FactorBase:
    def __init__(self, name):
        self.name  = name
        self.value = 0.0

    def on_orderbook(self, ob: OrderBook): pass
    def on_trade(self, trade: TradeData):  pass


class GLFTCalibrator:
    """
    GLFT 在线参数校准器

    核心改进：time-normalized sigma
      sigma 估计基于「每秒的价格变动标准差（bps）」，
      与 tick 到达速率无关，在网络抖动和重连场景下保持稳定。
    """

    def __init__(self, window: int = 1000, config: dict = None):
        cfg = (config or {}).get("strategy", {}).get("calibrator", {})

        self.window = window

        # 用于存储时间归一化回报的环形队列
        self.norm_returns: deque = deque(maxlen=self.window)

        # 初始参数（从 config 读取，允许调整）
        self.sigma_bps: float = cfg.get("initial_sigma_bps", 10.0)
        self.A:         float = cfg.get("initial_A",          10.0)
        self.k:         float = cfg.get("initial_k",           0.8)

        self.learning_rate: float = cfg.get("learning_rate",    0.005)
        self.sigma_max:     float = cfg.get("sigma_max_bps",  100.0)
        self.ema_alpha:     float = cfg.get("sigma_ema_alpha",   0.1)

        # [FIX-SIGMA] 异常 tick 过滤：超过此间隔视为断线重连，丢弃该 tick
        self.max_tick_gap: float = cfg.get("max_tick_gap_sec", 2.0)

        # 运行时状态
        self.last_mid:       float = 0.0
        self.last_tick_time: float = 0.0   # [FIX-SIGMA] 记录上一 tick 的时间戳
        self.is_warmed_up:   bool  = False

    # ----------------------------------------------------------

    def on_orderbook(self, ob: OrderBook):
        bid, _ = ob.get_best_bid()
        ask, _ = ob.get_best_ask()
        if bid == 0:
            return

        mid = (bid + ask) / 2.0
        now = time.time()

        if self.last_mid > 0 and self.last_tick_time > 0:
            dt = now - self.last_tick_time  # 实际经过的秒数

            # [FIX-SIGMA] 超过阈值视为断线/暂停，丢弃此 tick 避免污染
            if dt > self.max_tick_gap:
                # 不更新 sigma，只更新参考点
                self.last_mid       = mid
                self.last_tick_time = now
                return

            # [FIX-SIGMA] 时间归一化回报：ret_normalized 的方差 ≈ sigma²（每秒）
            # ret_bps 除以 sqrt(dt) 使不同 tick 间隔的样本具有可比性
            if dt > 1e-4:  # 防止 dt=0 时除零
                ret_bps        = (mid / self.last_mid - 1.0) * 10000.0
                ret_normalized = ret_bps / math.sqrt(dt)
                self.norm_returns.append(ret_normalized)

            # 收集足够样本后才开始估计 sigma
            if len(self.norm_returns) >= 10:
                # std(norm_returns) 的单位是 bps/sqrt(sec)
                # sigma_bps 表示 1 秒内的价格标准差（bps），直接等于 std
                raw_std = float(np.std(self.norm_returns))

                # EMA 平滑，防止突变
                self.sigma_bps = (
                    (1.0 - self.ema_alpha) * self.sigma_bps
                    + self.ema_alpha       * raw_std
                )
                self.sigma_bps = min(self.sigma_bps, self.sigma_max)
                self.sigma_bps = max(self.sigma_bps, 0.1)  # 下限保护

                self.is_warmed_up = True

        self.last_mid       = mid
        self.last_tick_time = now

    # ----------------------------------------------------------

    def on_market_trade(self, trade: AggTradeData, current_mid: float):
        """在线梯度下降更新订单流参数 A 和 k"""
        if not self.is_warmed_up or current_mid <= 0:
            return

        delta_mkt = abs(trade.price / current_mid - 1.0) * 10000.0

        # 过滤极端异常值（偏离超过 1%）
        if delta_mkt > 100.0:
            return

        prediction = self.A * math.exp(-self.k * delta_mkt)
        error      = 1.0 - prediction

        self.A += self.learning_rate * error * math.exp(-self.k * delta_mkt)
        grad_k  = error * (-delta_mkt) * prediction
        self.k -= self.learning_rate * grad_k

        # 参数约束
        self.A = max(0.1, min(200.0, self.A))
        self.k = max(0.1, min(10.0,  self.k))