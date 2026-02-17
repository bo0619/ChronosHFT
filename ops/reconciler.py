# file: ops/reconciler.py

import time
from infrastructure.logger import logger
from dashboard.models import SystemStatus

class AutoReconciler:
    """
    自动对账与自愈系统
    职责：
    1. 监控系统状态是否为 DIRTY
    2. 假如 DIRTY 持续超过阈值 (例如 5秒)，触发 OMS 强制同步
    3. 防止过于频繁的同步 (冷却时间)
    """
    def __init__(self, oms, aggregator, config):
        self.oms = oms
        self.aggregator = aggregator
        
        # 配置
        # 允许脏数据的持续时间 (秒)。太短会导致网络波动时频繁重置，太长会导致风险暴露
        self.dirty_threshold = 10.0 
        # 两次强制同步之间的最小间隔 (秒)，防止死循环同步
        self.cooldown = 10.0 
        
        # 状态
        self.first_dirty_time = 0.0
        self.last_sync_time = 0.0
        self.is_reconciling = False

    def check_and_fix(self):
        """
        在主循环中调用
        """
        # 1. 如果正在冷却，跳过
        now = time.time()
        if now - self.last_sync_time < self.cooldown:
            return

        # 2. 获取当前状态
        # 注意：这里直接读取 aggregator 的缓存状态
        state = self.aggregator.state
        
        if state.status == SystemStatus.DIRTY:
            if self.first_dirty_time == 0:
                self.first_dirty_time = now
            
            # 3. 检查持续时间
            duration = now - self.first_dirty_time
            if duration > self.dirty_threshold:
                self._trigger_force_sync(now)
        else:
            # 状态恢复正常，重置计时器
            self.first_dirty_time = 0

    def _trigger_force_sync(self, now):
        logger.warning("🚨 System is DIRTY for too long. Triggering Auto-Reconciliation...")
        
        # 1. 暂停策略发单 (可选，目前通过架构解耦，同步期间发单可能会被覆盖或报错，但不会崩)
        # 2. 执行同步
        try:
            self.oms.sync_with_exchange()
            
            # 3. 同步完立即强制刷新视图，以便 UI 变绿
            # (虽然 aggregator 下一帧也会更新，但这能立即重置 DIRTY 状态)
            self.aggregator.exch_view.refresh() 
            self.aggregator.update()
            
            logger.info("✅ Auto-Reconciliation Complete. System Status Reset.")
        except Exception as e:
            logger.error(f"❌ Auto-Reconciliation Failed: {e}")
        
        self.last_sync_time = now
        self.first_dirty_time = 0