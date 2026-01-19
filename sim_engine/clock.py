from datetime import datetime

class EventClock:
    def __init__(self):
        self.current_time = datetime.min

    def update(self, dt: datetime):
        # 确保时间单调递增
        if dt >= self.current_time:
            self.current_time = dt
        else:
            # 允许极其微小的时间回退(数据乱序)，但通常应报错或忽略
            pass

    def now(self):
        return self.current_time