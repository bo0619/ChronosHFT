# file: strategy/hybrid_glft/selector.py
# ============================================================
# BUG FIX:
#
# [FIX-7] select_mode(): 双重残留 bug 导致全品种恒定 MOMENTUM_SELL
#
#   Bug 路径（修复 FIX-3 后暴露）：
#   1. FIX-3 删除 trained=True 后，未训练模型的 predict() 返回：
#      MLPrediction(p_trend=0.5, direction="NEUTRAL")
#   2. threshold=0.3，而 0.5 > 0.3 → 进入 ML 分支 ← 子问题 A
#   3. direction="NEUTRAL" 不等于"UP" → 走 else → MOMENTUM_SELL ← 子问题 B
#   4. 结果：所有品种恒定 MOMENTUM_SELL，无限积累空仓
#
#   修复方案（双重防御）：
#   A. 在 ML 分支开头增加 direction=="NEUTRAL" 守卫 → 直接走做市模式
#      这是语义修复：NEUTRAL 方向永远不应触发动量模式，无论置信度多高
#   B. 将 threshold 从 0.3 提升到 0.55
#      未训练模型 p_trend 恒为 0.5，0.5 < 0.55 → 不触发 ML 分支
#      正常训练后的强信号 p_trend 通常 > 0.6，仍能正常触发
#      (0.55 = 0.5 + 一个安全 margin，高于随机基线)
# ============================================================

import time
from dataclasses import dataclass
from typing import Optional
from .detector import TrendSignal
from .predictor import MLPrediction


@dataclass
class StrategyMode:
    mode: str  # "MARKET_MAKING", "MOMENTUM_BUY", "MOMENTUM_SELL"
    momentum_strength: float
    transition_time: float


class HybridModeSelector:
    def __init__(self, threshold: float = 0.55):
        # [FIX-7B] threshold: 0.3 → 0.55
        #   旧值 0.3 低于未训练模型的基线输出 0.5，导致冷启动期恒触发动量模式。
        #   新值 0.55 高于基线，只有模型真正有信心时才触发。
        self.threshold = threshold

    def select_mode(
        self,
        rule_sig: TrendSignal,
        ml_pred: Optional[MLPrediction]
    ) -> StrategyMode:

        # --- ML 信号分支 ---
        if ml_pred and ml_pred.p_trend > self.threshold:

            # [FIX-7A] NEUTRAL 方向守卫
            #   即使置信度超过 threshold，方向不明确时也不应该进入动量模式。
            #   NEUTRAL 表示模型"有信心市场不会大幅移动"，正确行为是做市。
            if ml_pred.direction == "NEUTRAL":
                return StrategyMode("MARKET_MAKING", 0.0, time.time())

            mode = "MOMENTUM_BUY" if ml_pred.direction == "UP" else "MOMENTUM_SELL"
            return StrategyMode(mode, ml_pred.momentum_strength, time.time())

        # --- 规则信号分支 ---
        if abs(rule_sig.strength) > self.threshold:
            if rule_sig.direction == "NEUTRAL":
                return StrategyMode("MARKET_MAKING", 0.0, time.time())

            mode = "MOMENTUM_BUY" if rule_sig.direction == "UP" else "MOMENTUM_SELL"
            return StrategyMode(mode, abs(rule_sig.strength), time.time())

        # --- 默认：做市模式 ---
        return StrategyMode("MARKET_MAKING", 0.0, time.time())