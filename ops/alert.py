# file: ops/alert.py

import time
import threading
import requests
from queue import Queue, Empty
from event.type import Event, EVENT_ALERT, EVENT_LOG, AlertData

class TelegramAlerter:
    def __init__(self, engine, config):
        self.engine = engine
        self.config = config.get("alert", {})
        self.active = self.config.get("active", False)
        self.token = self.config.get("telegram_token", "")
        self.chat_id = self.config.get("telegram_chat_id", "")
        
        self.queue = Queue()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        
        if self.active and self.token and self.chat_id:
            self.thread.start()
            self.engine.register(EVENT_ALERT, self.on_alert)
            # 也可以监听 LOG 中的 ERROR
            self.engine.register(EVENT_LOG, self.on_log)
            self.send_msg("🚀 HFT System Started & Alerting Connected.")

    def on_alert(self, event: Event):
        data: AlertData = event.data
        self.queue.put(f"[{data.level}] {data.msg}")

    def on_log(self, event: Event):
        # 自动将日志中的 ERROR/CRITICAL 转发为报警
        msg: str = event.data
        if "ERROR" in msg or "CRITICAL" in msg:
            self.queue.put(f"🚨 {msg}")

    def send_msg(self, text):
        """发送 HTTP 请求给 Telegram"""
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            data = {"chat_id": self.chat_id, "text": text}
            requests.post(url, data=data, timeout=5)
        except Exception as e:
            print(f"Telegram Send Error: {e}")

    def _run_loop(self):
        """
        后台发送循环 (防抖动/限流)
        """
        while self.active:
            try:
                msg = self.queue.get(timeout=1)
                self.send_msg(msg)
                time.sleep(0.5) # 简单限流，防止被 TG 封
            except Empty:
                pass