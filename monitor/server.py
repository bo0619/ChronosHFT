# file: monitor/server.py

import uvicorn
import threading
import asyncio
import json
import time
from typing import List
from fastapi import FastAPI, WebSocket, Request, WebSocketDisconnect
from fastapi.templating import Jinja2Templates
from dataclasses import asdict

from event.type import EVENT_ACCOUNT_UPDATE, EVENT_LOG, AccountData

class ConnectionManager:
    """
    WebSocket 连接管理器
    负责维护所有活跃的前端连接，并广播消息
    """
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        # 遍历所有连接进行推送
        # 注意：如果有断开的连接，send_text可能会抛出异常，需要处理
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                # 发送失败通常意味着连接已断开，稍后由 disconnect 处理
                pass

class WebMonitor:
    def __init__(self, engine, config):
        self.engine = engine
        self.port = config.get("system", {}).get("web_port", 8000)
        
        self.app = FastAPI()
        self.templates = Jinja2Templates(directory="monitor/templates")
        self.manager = ConnectionManager()
        
        # 关键：保存异步事件循环的引用，以便从同步线程切入
        self.loop = None 

        # --- 路由定义 ---
        @self.app.on_event("startup")
        async def startup_event():
            # 在 Uvicorn 启动时捕获当前的 EventLoop
            self.loop = asyncio.get_running_loop()
            print(">>> Web Monitor Event Loop Captured.")

        @self.app.get("/")
        async def index(request: Request):
            return self.templates.TemplateResponse("index.html", {"request": request})

        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            await self.manager.connect(websocket)
            try:
                while True:
                    # 保持连接活跃，也可以接收前端发来的指令（如暂停策略）
                    # 这里暂时只做单向推送，所以挂起等待
                    await websocket.receive_text()
            except WebSocketDisconnect:
                self.manager.disconnect(websocket)
            except Exception:
                self.manager.disconnect(websocket)

        # --- 注册事件监听 ---
        # 我们只关心影响 PnL 的核心事件：账户更新
        self.engine.register(EVENT_ACCOUNT_UPDATE, self.on_account_update)
        # 可选：也推送日志
        # self.engine.register(EVENT_LOG, self.on_log)

        # --- 启动服务器线程 ---
        self.thread = threading.Thread(target=self._run_server, daemon=True)
        self.thread.start()

    def _run_server(self):
        # 禁用 access log 以免刷屏，只保留 error
        uvicorn.run(self.app, host="0.0.0.0", port=self.port, log_level="critical")

    def _push_async(self, data: dict):
        """
        跨线程桥接核心：
        从 EventEngine 线程 -> 调度到 FastAPI Loop -> WebSocket Broadcast
        """
        if self.loop and self.loop.is_running():
            msg = json.dumps(data)
            # 线程安全地调度协程
            asyncio.run_coroutine_threadsafe(self.manager.broadcast(msg), self.loop)

    def on_account_update(self, event):
        """
        当账户权益发生变化时，立即推送
        """
        acc: AccountData = event.data
        
        payload = {
            "type": "pnl",
            "data": {
                "timestamp": acc.datetime.timestamp(), # 秒级时间戳
                "equity": acc.equity,
                "balance": acc.balance,
                "margin": acc.used_margin,
                "available": acc.available
            }
        }
        self._push_async(payload)

    def on_log(self, event):
        payload = {
            "type": "log",
            "data": event.data
        }
        self._push_async(payload)