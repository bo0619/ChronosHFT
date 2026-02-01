# file: monitor/server.py

import uvicorn
import threading
import asyncio
import json
import queue
from typing import List
from fastapi import FastAPI, WebSocket, Request, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates

from event.type import EVENT_ACCOUNT_UPDATE, EVENT_LOG, AccountData

class WebMonitor:
    def __init__(self, engine, config):
        self.engine = engine
        self.port = config.get("system", {}).get("web_port", 8000)
        
        self.app = FastAPI()
        self.templates = Jinja2Templates(directory="monitor/templates")
        
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

        self.data_queue = queue.Queue()
        self.active_connections: List[WebSocket] = []

        @self.app.get("/")
        async def index(request: Request):
            return self.templates.TemplateResponse("index.html", {"request": request})

        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            await websocket.accept()
            self.active_connections.append(websocket)
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                if websocket in self.active_connections:
                    self.active_connections.remove(websocket)

        @self.app.on_event("startup")
        async def startup_event():
            asyncio.create_task(self._broadcaster_task())

        self.engine.register(EVENT_ACCOUNT_UPDATE, self.on_account_update)
        self.engine.register(EVENT_LOG, self.on_log)

        self.thread = threading.Thread(target=self._run_server, daemon=True)
        self.thread.start()

    def _run_server(self):
        # 强制使用 0.0.0.0 和关闭多余日志
        uvicorn.run(self.app, host="0.0.0.0", port=self.port, log_level="error", access_log=False)

    async def _broadcaster_task(self):
        while True:
            try:
                while not self.data_queue.empty():
                    msg = self.data_queue.get_nowait()
                    if self.active_connections:
                        await asyncio.gather(
                            *[conn.send_text(msg) for conn in self.active_connections],
                            return_exceptions=True
                        )
                await asyncio.sleep(0.1)
            except Exception:
                await asyncio.sleep(1)

    def on_account_update(self, event):
        acc: AccountData = event.data
        payload = {
            "type": "pnl",
            "data": {
                "timestamp": acc.datetime.timestamp(),
                "equity": acc.equity,
                "balance": acc.balance,
                "margin": acc.used_margin,
                "available": acc.available
            }
        }
        self.data_queue.put(json.dumps(payload))

    def on_log(self, event):
        payload = {"type": "log", "data": event.data}
        self.data_queue.put(json.dumps(payload))