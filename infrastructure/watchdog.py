import time

from event.type import Event, EVENT_SYSTEM_HEALTH
from infrastructure.logger import logger


def emit_market_data_stale_if_needed(event_engine, last_tick_time: float, triggered: bool, threshold_sec: float = 60.0, now: float = None) -> bool:
    if triggered or last_tick_time <= 0:
        return triggered

    now = time.time() if now is None else now
    silence_sec = now - last_tick_time
    if silence_sec <= threshold_sec:
        return triggered

    message = f"MARKET_DATA_STALE:{silence_sec:.1f}s>{threshold_sec:.1f}s"
    logger.critical(f"SYSTEM WATCHDOG: {message}")
    event_engine.put(Event(EVENT_SYSTEM_HEALTH, message))
    return True