# file: sim_engine/clock.py

from datetime import datetime

class EventClock:
    def __init__(self):
        self.current_time = datetime.min

    def update(self, dt: datetime):
        if dt >= self.current_time:
            self.current_time = dt

    def now(self):
        return self.current_time