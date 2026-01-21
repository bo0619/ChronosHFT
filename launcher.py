# file: launcher.py

import subprocess
import time
import sys
import os
from datetime import datetime

# 配置
TARGET_SCRIPT = "main.py"
RESTART_INTERVAL = 5 # 重启等待时间 (秒)
MAX_RESTARTS_PER_HOUR = 10 # 防止无限重启死循环

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
        watchdog = ProcessWatchdog()
        watchdog.run()