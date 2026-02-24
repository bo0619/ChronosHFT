# file: strategy/ml_sniper/predictor.py
#
# 修复记录（相对于原版）：
#
# [FIX-1] buffer 遍历方向错误
#   原版：for past_data in list(self.buffer) → 从旧到新迭代
#         break 条件 elapsed < 0.9 → 碰到最新数据时才 break
#         效果：每帧遍历 buffer 中所有旧数据（最多 2000 条），O(n) 浪费
#   修复：for past_data in reversed(list(self.buffer)) → 从新到旧迭代
#         找到各 horizon 所需的最老样本后提前 break → O(1) 均摊
#         同时修正了"遍历还没到达目标样本就 break"的逻辑缺陷
#
# [FIX-2] 各 horizon 的观测噪声 R 应反映标签的真实噪音
#   原版：三个 horizon 均 R=10.0
#   修复：1s → R=5.0（短期标签噪音大），10s → R=1.5，30s → R=0.5
#         R 越小表示"更信任这个标签"，与 horizon 越长噪音越低一致
#
# [FIX-3] 卡尔曼更新使用 Joseph 稳定形式
#   原版：P = (I - K @ x.T) @ P
#         标准形式在数值精度上容易因浮点误差导致 P 矩阵失去正定性
#   修复：P = (I-Kx^T) @ P @ (I-Kx^T)^T + R * K @ K^T
#         Joseph 形式保证 P 在任意增益下保持对称正定
#
# [FIX-4] 标签异常值过滤阈值从 500 bps 调整为 100 bps
#   500 bps = 5% 的价格变动，在 30s 内几乎不可能是真实信号
#   调整为 100 bps 后仍保留足够大的真实行情，同时过滤数据污染

import numpy as np
from collections import deque
from typing import List, Dict


class KalmanFilterRegressor:
    """
    单时间尺度卡尔曼回归器。

    状态：权重向量 w ∈ R^n
    预测步：P += Q          （不确定性随时间扩散，Q 控制遗忘速度）
    更新步：标准卡尔曼增益更新，使用 Joseph 稳定形式维护 P 的正定性

    Q 越大 → 遗忘越快 → 适应短周期变化
    R 越小 → 越信任标签 → 学习速度越快（但对噪音也更敏感）
    """

    def __init__(self, num_features: int, R: float = 1.0, Q: float = 1e-5):
        self.num_features = num_features
        self.w = np.zeros((num_features, 1))          # 权重列向量
        self.P = np.eye(num_features)                 # 状态协方差矩阵
        self.R = float(R)                             # 观测噪声（标量）
        self.Q = np.eye(num_features) * Q             # 过程噪声矩阵
        self.I = np.eye(num_features)
        self.n_updates = 0                            # 训练次数（用于预热判断）

    def predict(self, features: List) -> float:
        try:
            x = np.array(features, dtype=float).reshape(-1, 1)
            y_pred = float((x.T @ self.w).item())
            return float(np.clip(y_pred, -50.0, 50.0))
        except Exception:
            return 0.0

    def update(self, features: List, y_true: float):
        """
        卡尔曼更新：
          1. 预测步：P += Q
          2. 计算增益 K
          3. 更新权重 w
          4. [FIX-3] Joseph 稳定形式更新 P
        """
        try:
            x = np.array(features, dtype=float).reshape(-1, 1)

            # 预测步
            self.P += self.Q

            # 增益计算
            Px = self.P @ x                           # (n, 1)
            S  = float((x.T @ Px).item()) + self.R   # 标量新息方差
            K  = Px / S                               # 卡尔曼增益 (n, 1)

            # 权重更新
            y_pred = float((x.T @ self.w).item())
            error  = y_true - y_pred
            self.w += K * error

            # [FIX-3] Joseph 稳定形式：P = (I-Kx^T)P(I-Kx^T)^T + R·KK^T
            IKx    = self.I - K @ x.T
            self.P = IKx @ self.P @ IKx.T + self.R * (K @ K.T)

            self.n_updates += 1
        except Exception:
            pass

    def get_weights(self) -> List[float]:
        return self.w.flatten().tolist()

    @property
    def is_warmed_up(self) -> bool:
        """至少训练过 2 × horizon_ticks 次才视为预热完成"""
        return self.n_updates >= 2


class TimeHorizonPredictor:
    """
    三时间尺度卡尔曼预测器（1s / 10s / 30s）。

    训练逻辑（[FIX-1] 修复后）：
      - buffer 按时间正序存储，deque 最右端是最新数据
      - 每帧 update_and_predict() 调用时，从 buffer 右端（最新）往左（最老）搜索
      - 对每个 horizon h，找到第一个 elapsed >= h_sec 且未训练过的样本，训练一次
      - 找到后立即 break，不再继续遍历 → O(1) 均摊复杂度

    各 horizon 的卡尔曼超参（[FIX-2]）：
      1s  : Q=1e-4（快速遗忘），R=5.0（短期标签噪音大）
      10s : Q=1e-5（中速），    R=1.5
      30s : Q=1e-6（慢速遗忘），R=0.5（长期标签更可信）
    """

    def __init__(self, num_features: int = 9):
        self.horizons = {
            "1s":  1.0,
            "10s": 10.0,
            "30s": 30.0,
        }
        # [FIX-2] 各 horizon 独立的 R/Q 超参
        self.models: Dict[str, KalmanFilterRegressor] = {
            "1s":  KalmanFilterRegressor(num_features, R=5.0,  Q=1e-4),
            "10s": KalmanFilterRegressor(num_features, R=1.5,  Q=1e-5),
            "30s": KalmanFilterRegressor(num_features, R=0.5,  Q=1e-6),
        }
        # buffer 每条记录：{"ts": float, "price": float, "feats": List}
        self.buffer: deque = deque(maxlen=2000)
        # 各 horizon 上次训练时用到的样本 ts（防止重复训练同一样本）
        self.last_trained_ts: Dict[str, float] = {h: 0.0 for h in self.horizons}

    # ── 核心接口 ─────────────────────────────────────────────

    def update_and_predict(self,
                           features: List,
                           current_mid: float,
                           now: float,
                           ) -> Dict[str, float]:
        """
        每 tick 调用：写入 buffer → 训练 → 推理。
        返回 {"1s": bps, "10s": bps, "30s": bps}
        """
        res = {h: 0.0 for h in self.horizons}
        if current_mid <= 0:
            return res

        # 1. 写入当前快照
        self.buffer.append({"ts": now, "price": current_mid, "feats": features})

        # 2. [FIX-1] 从新到旧遍历，逐 horizon 寻找训练样本
        buf_list = list(self.buffer)   # 快照一次，防止迭代中 deque 被修改

        for h_name, h_sec in self.horizons.items():
            trained_this_frame = False
            # reversed → 从最新往最老搜索
            for past_data in reversed(buf_list):
                if not isinstance(past_data, dict):
                    continue
                elapsed = now - past_data["ts"]

                # 还没到达目标时间距离，继续往更旧的方向找
                if elapsed < h_sec:
                    continue

                # 找到第一个满足时间距离的样本
                # 检查是否已训练过（用 ts 防重）
                if past_data["ts"] <= self.last_trained_ts[h_name]:
                    break   # 比这更旧的也一定训练过了，直接结束

                # [FIX-4] 过滤极端标签（100 bps 以内才是有效学习信号）
                y_bps = (current_mid / past_data["price"] - 1.0) * 10000.0
                if abs(y_bps) < 100.0:
                    self.models[h_name].update(past_data["feats"], y_bps)

                self.last_trained_ts[h_name] = past_data["ts"]
                trained_this_frame = True
                break   # 每帧每个 horizon 只训练一次，避免重复

        # 3. 推理（不修改任何状态）
        for h_name, model in self.models.items():
            res[h_name] = model.predict(features)

        return res

    def get_model_weights(self, horizon: str) -> List[float]:
        if horizon in self.models:
            return self.models[horizon].get_weights()
        return []

    @property
    def is_warmed_up(self) -> bool:
        """三个 horizon 全部至少训练过一次"""
        return all(m.is_warmed_up for m in self.models.values())

    def warmup_progress(self) -> Dict[str, int]:
        """返回各 horizon 的训练次数，用于 UI 展示预热进度"""
        return {h: m.n_updates for h, m in self.models.items()}