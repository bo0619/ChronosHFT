# file: alpha/gate.py

import math

class AlphaGate:
    """
    Alpha 信号守门员
    职责：
    1. 限制信号幅度 (Clipping)
    2. 库存感知压制 (Inventory Awareness) - 仓位越重，Alpha 越弱
    3. 信号平滑 (Smoothing) - 防止信号跳动过快
    """
    def __init__(self, max_bps=2.0, decay_factor=0.9, inventory_dampening=0.1):
        self.max_bps = max_bps       # 最大允许偏移 (bps)
        self.decay = decay_factor    # 平滑因子 (EMA)
        self.inv_k = inventory_dampening # 库存压制系数
        
        self.smoothed_alpha = 0.0

    def process(self, raw_pred_bps, q_norm):
        """
        处理原始信号
        q_norm: 归一化持仓 (pos / order_vol)
        """
        # 1. 硬截断 (Hard Clip)
        clipped = max(-self.max_bps, min(self.max_bps, raw_pred_bps))
        
        # 2. 信号平滑 (EMA Smoothing)
        # 防止信号在 5.0 和 -5.0 之间剧烈跳动
        self.smoothed_alpha = self.smoothed_alpha * self.decay + clipped * (1 - self.decay)
        
        # 3. 库存压制 (Inventory Awareness) - 核心逻辑
        # 逻辑：当持仓很重时，我们只相信 GLFT 的库存回归，不相信 Alpha 的方向预测
        # 尤其是当 Alpha 预测方向与持仓方向一致时（比如持有多单，Alpha还看涨），这是最危险的
        # 这里采用双边压制：只要有持仓，Alpha 权重就线性下降
        
        # 归一化持仓绝对值
        abs_q = abs(q_norm)
        
        # 压制系数 (0.0 ~ 1.0)
        # 当 abs_q = 0, dampener = 1.0 (全信)
        # 当 abs_q = 10 (比如持有10手), dampener = 1 / (1 + 0.1 * 10) = 0.5 (信一半)
        # 当 abs_q = 100, dampener = 1 / 11 = 0.09 (基本不信)
        dampener = 1.0 / (1.0 + self.inv_k * abs_q)
        
        final_alpha = self.smoothed_alpha * dampener
        
        return final_alpha