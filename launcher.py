# file: launcher.py

import subprocess
import time
import sys
import os
from datetime import datetime

from infrastructure.config_scaling import load_root_config
from infrastructure.paper_trade import is_paper_trade

# 配置
TARGET_SCRIPT = "main.py"
CONFIG_PATH = "config.json"
RESTART_INTERVAL = 5 # 重启等待时间 (秒)
MAX_RESTARTS_PER_HOUR = 10 # 防止无限重启死循环


def launcher_allows_runtime(config_path=CONFIG_PATH):
    try:
        config = load_root_config(config_path)
    except Exception as exc:
        return False, f"configuration rejected: {type(exc).__name__}: {exc}"
    if not config:
        return False, f"configuration unavailable: {config_path}"
    if not is_paper_trade(config):
        return (
            False,
            "launcher.py is Paper-only because forced process termination can "
            "bypass verified live shutdown; start Live through main.py",
        )
    return True, ""

class ProcessWatchdog:
    def __init__(self):
        self.restart_history = []

    def run(self):
        print(f"🔥 HFT Launcher Started. Monitoring: {TARGET_SCRIPT}")
        
        while True:
            # 1. 检查重启频率
            self._cleanup_history()
            if len(self.restart_history) >= MAX_RESTARTS_PER_HOUR:
                print("🚨 Max restarts reached. System is unstable. Stopping watchdog.")
                break
                
            # 2. 启动子进程
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting Process...")
            try:
                # 使用 sys.executable 确保使用当前相同的 Python 解释器
                process = subprocess.Popen([sys.executable, TARGET_SCRIPT])
                
                # 3. 阻塞等待进程结束
                exit_code = process.wait()
                
            except KeyboardInterrupt:
                print("\n🛑 Launcher stopped by user.")
                # 尝试优雅关闭子进程
                if process:
                    process.terminate()
                break
                
            # 4. 进程退出处理
            print(f"⚠️ Process exited with code: {exit_code}")
            
            if exit_code == 0:
                print("Process exited normally. Watchdog stopping.")
                break
            else:
                print(f"Process crashed! Restarting in {RESTART_INTERVAL} seconds...")
                self.restart_history.append(time.time())
                time.sleep(RESTART_INTERVAL)

    def _cleanup_history(self):
        """清除1小时前的重启记录"""
        now = time.time()
        self.restart_history = [t for t in self.restart_history if now - t < 3600]

if __name__ == "__main__":
    if not os.path.exists(TARGET_SCRIPT):
        print(f"Error: {TARGET_SCRIPT} not found!")
    else:
        allowed, reason = launcher_allows_runtime()
        if not allowed:
            print(f"Error: {reason}")
        else:
            watchdog = ProcessWatchdog()
            watchdog.run()
