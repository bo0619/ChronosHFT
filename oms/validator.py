# file: oms/validator.py
#
# ============================================================
# REFACTOR CHANGELOG
#
# [RISK-1] 统一 max_pos_notional 字段名
#   原：OMS 读 max_pos_notional_oms（与 JSON 不一致）
#   改：统一读 max_pos_notional，消除双字段歧义
#
# [RISK-2] 前移 max_order_notional 到 OMS 主路径
#   原：只在 RiskManager.check_order()（Legacy路径）校验
#   改：在 OrderValidator.validate_params() 硬拦截
#
# [RISK-3] 前移 max_deviation_pct 到 OMS 主路径
#   原：只在 RiskManager 里做，策略主路径完全绕过
#   改：在 validate_params() 查 data_cache 的 mark_price 实时校验
#   注：mark_price 尚未到达时（返回0）跳过检查，不误杀冷启动阶段
#
# [RISK-4] 前移 max_order_count_per_sec 到 OMS 主路径
#   原：滑动窗口计数器只在 RiskManager 维护
#   改：在 OrderValidator 内维护独立的线程安全滑动窗口
#   原理：使用 collections.deque + threading.Lock，
#         每次 validate_params 调用时清除 >1s 的旧时间戳，
#         若当前窗口内计数 >= 阈值则拒绝，通过后追加时间戳
# ============================================================

import time
import threading
from collections import deque

from data.cache import data_cache
from data.ref_data import ref_data_manager
from event.type import OrderIntent


class OrderValidator:
    def __init__(self, config: dict):
        limits  = config.get("risk", {}).get("limits", {})
        sanity  = config.get("risk", {}).get("price_sanity", {})
        tech    = config.get("risk", {}).get("tech_health", {})

        # ── 既有检查 ─────────────────────────────────────────
        self.max_order_qty      = limits.get("max_order_qty",      1000.0)

        # ── [RISK-2] 新增：单笔名义价值上限 ──────────────────
        self.max_order_notional = limits.get("max_order_notional", 5000.0)

        # ── [RISK-3] 新增：价格偏离保护 ──────────────────────
        self.max_deviation_pct  = sanity.get("max_deviation_pct",  0.05)

        # ── [RISK-4] 新增：每秒发单频率限制 ──────────────────
        self.max_orders_per_sec = tech.get("max_order_count_per_sec", 20)
        self._order_timestamps: deque = deque()      # 存放通过校验的时间戳
        self._rate_lock = threading.Lock()           # 保证多线程安全

    # ----------------------------------------------------------
    # 主校验入口（OMS.submit_order 调用）
    # ----------------------------------------------------------

    def validate_params(self, intent: OrderIntent) -> tuple[bool, str]:
        """
        按顺序执行所有 Pre-Trade 硬校验。
        返回 (True, "") 表示通过；(False, reason) 表示拒绝。

        校验顺序（由轻到重，快速失败）：
          1. 基础合法性（价格/数量为正）
          2. 交易所最小名义价值
          3. 单笔数量上限          [既有]
          4. 单笔名义价值上限      [RISK-2 新增]
          5. 价格偏离保护          [RISK-3 新增]
          6. 发单频率限制          [RISK-4 新增]
        """

        # 1. 基础合法性
        if intent.price <= 0 or intent.volume <= 0:
            return False, "non_positive_price_or_volume"

        notional = intent.price * intent.volume

        # 2. 交易所最小名义价值（防止废单浪费 API 限额）
        info = ref_data_manager.get_info(intent.symbol)
        if info:
            if notional < max(info.min_notional, 5.0):
                return False, f"notional_below_min:{notional:.8f}"

        # 3. 单笔数量上限
        if intent.volume > self.max_order_qty:
            return False, f"qty_exceeded:{intent.volume}>{self.max_order_qty}"

        # 4. [RISK-2] 单笔名义价值上限
        if notional > self.max_order_notional:
            return (
                False,
                f"notional_exceeded:{notional:.2f}>{self.max_order_notional:.2f}",
            )

        # 5. [RISK-3] 价格偏离保护
        #    mark_price == 0 说明行情还未到达，跳过（冷启动保护）
        mark_price = data_cache.get_mark_price(intent.symbol)
        if mark_price > 0:
            deviation = abs(intent.price - mark_price) / mark_price
            if deviation > self.max_deviation_pct:
                return (
                    False,
                    f"price_deviation:{deviation*100:.3f}%>{self.max_deviation_pct*100:.1f}%"
                    f"(order={intent.price},mark={mark_price})",
                )

        # 6. [RISK-4] 发单频率限制（滑动窗口，线程安全）
        reject, reason = self._check_rate_limit()
        if reject:
            return False, reason

        return True, ""

    # ----------------------------------------------------------
    # 内部：频率限制滑动窗口
    # ----------------------------------------------------------

    def _check_rate_limit(self) -> tuple[bool, str]:
        """
        维护 1 秒滑动窗口。
        通过校验后才追加时间戳，保证计数精确对应"已允许发出"的单量。
        返回 (True, reason) 表示应拒绝；(False, "") 表示放行。
        """
        with self._rate_lock:
            now = time.monotonic()
            cutoff = now - 1.0

            # 清除 1 秒以外的旧时间戳（deque 左端为最旧）
            while self._order_timestamps and self._order_timestamps[0] < cutoff:
                self._order_timestamps.popleft()

            current_count = len(self._order_timestamps)
            if current_count >= self.max_orders_per_sec:
                return (
                    True,
                    f"rate_limit:{current_count}>={self.max_orders_per_sec}/s",
                )

            # 放行：记录本次时间戳
            self._order_timestamps.append(now)
            return False, ""
