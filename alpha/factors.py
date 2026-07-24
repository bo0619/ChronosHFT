# file: alpha/factors.py
# [FIX-SIGMA] GLFTCalibrator: sigma 时间尺度错误

import math
import time
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
        raw_config = config if isinstance(config, dict) else {}
        strategy_config = raw_config.get("strategy", {})
        if isinstance(strategy_config, dict) and strategy_config:
            cfg = strategy_config.get("calibrator", {})
        elif isinstance(raw_config.get("calibrator"), dict):
            cfg = raw_config["calibrator"]
        else:
            cfg = raw_config
        if not isinstance(cfg, dict):
            cfg = {}

        self.window = max(2, int(cfg.get("window", window) or window))

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
        self.min_samples: int = max(
            2,
            min(
                self.window,
                int(cfg.get("min_samples", 10) or 10),
            ),
        )

        # 运行时状态
        self.last_mid:       float = 0.0
        # `last_tick_time` remains public for compatibility, but now belongs
        # to `last_tick_source` instead of the host wall clock.
        self.last_tick_time: float = 0.0
        self.last_tick_source: str = ""
        self.last_tick_monotonic: float = 0.0
        self._has_tick_reference: bool = False
        self.is_warmed_up:   bool  = False
        # Public aggTrade events are not evidence of RPI-accessible retail
        # flow and must never update the live A/k estimator.
        self.public_trade_sample_count: int = 0
        self.intensity_sample_count: int = 0

    @property
    def volatility_sample_count(self) -> int:
        return len(self.norm_returns)

    # ----------------------------------------------------------

    @staticmethod
    def _valid_clock_value(value) -> float | None:
        try:
            clock_value = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(clock_value) or clock_value <= 0.0:
            return None
        return clock_value

    def _clock_sample(self, ob: OrderBook, now_monotonic: float) -> tuple[str, float]:
        received_monotonic = self._valid_clock_value(
            getattr(ob, "received_monotonic", None)
        )
        if received_monotonic is not None:
            return "received_monotonic", received_monotonic

        exchange_timestamp = self._valid_clock_value(
            getattr(ob, "exchange_timestamp", None)
        )
        if exchange_timestamp is not None:
            return "exchange_timestamp", exchange_timestamp

        return "monotonic", now_monotonic

    def _set_tick_reference(
        self,
        *,
        mid: float,
        clock_source: str,
        tick_time: float,
        now_monotonic: float,
    ) -> None:
        self.last_mid = mid
        self.last_tick_source = clock_source
        self.last_tick_time = tick_time
        self.last_tick_monotonic = now_monotonic
        self._has_tick_reference = True

    def on_orderbook(self, ob: OrderBook):
        bid, _ = ob.get_best_bid()
        ask, _ = ob.get_best_ask()
        if (
            not math.isfinite(bid)
            or not math.isfinite(ask)
            or bid <= 0.0
            or ask <= bid
        ):
            return

        mid = (bid + ask) / 2.0
        now_monotonic = time.perf_counter()
        clock_source, tick_time = self._clock_sample(ob, now_monotonic)

        if self.last_mid > 0 and self._has_tick_reference:
            if clock_source == self.last_tick_source:
                dt = tick_time - self.last_tick_time
            else:
                # Clock domains cannot be subtracted from one another.  A
                # source transition therefore uses local monotonic arrival
                # time for exactly this interval.
                dt = now_monotonic - self.last_tick_monotonic

            # Duplicate/out-of-order events must not advance the price or time
            # reference.  Falling back here would hide invalid event ordering.
            if not math.isfinite(dt) or dt <= 0.0:
                return

            # A reconnect/pause gap is not a volatility sample.  Rebase so the
            # next valid event starts a fresh interval.
            if dt > self.max_tick_gap:
                self._set_tick_reference(
                    mid=mid,
                    clock_source=clock_source,
                    tick_time=tick_time,
                    now_monotonic=now_monotonic,
                )
                return

            # [FIX-SIGMA] 时间归一化回报：ret_normalized 的方差 ≈ sigma²（每秒）
            # ret_bps 除以 sqrt(dt) 使不同 tick 间隔的样本具有可比性
            if dt > 1e-4:  # 防止 dt=0 时除零
                ret_bps = math.log(mid / self.last_mid) * 10_000.0
                ret_normalized = ret_bps / math.sqrt(dt)
                if math.isfinite(ret_normalized):
                    self.norm_returns.append(ret_normalized)

            # 收集足够样本后才开始估计 sigma
            if len(self.norm_returns) >= self.min_samples:
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

        self._set_tick_reference(
            mid=mid,
            clock_source=clock_source,
            tick_time=tick_time,
            now_monotonic=now_monotonic,
        )

    # ----------------------------------------------------------

    def on_market_trade(self, trade: AggTradeData, current_mid: float):
        """Observe public flow without treating it as RPI fill evidence."""
        if not self.is_warmed_up or current_mid <= 0:
            return

        delta_mkt = abs(trade.price / current_mid - 1.0) * 10000.0

        if delta_mkt > 100.0:
            return
        self.public_trade_sample_count += 1
