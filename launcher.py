"""Paper-only process watchdog for the ChronosHFT runtime."""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from infrastructure.config_scaling import load_root_config
from infrastructure.paper_trade import is_paper_trade


WORKSPACE_DIR = Path(__file__).resolve().parent
TARGET_SCRIPT = WORKSPACE_DIR / "main.py"
CONFIG_PATH = WORKSPACE_DIR / "config.json"
RESTART_INTERVAL_SEC = 5.0
MAX_RESTARTS_PER_HOUR = 10
NONRETRYABLE_EXIT_CODES = frozenset({2})


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
    """Restart one Paper runtime with a bounded hourly retry budget."""

    def __init__(
        self,
        *,
        target_script=TARGET_SCRIPT,
        config_path=CONFIG_PATH,
        restart_interval_sec=RESTART_INTERVAL_SEC,
    ):
        self.target_script = Path(target_script).resolve()
        self.config_path = Path(config_path).resolve()
        self.restart_interval_sec = max(0.0, float(restart_interval_sec))
        self.restart_history = []

    def run(self):
        print(f"HFT Paper Launcher started: {self.target_script}")

        while True:
            self._cleanup_history()
            if len(self.restart_history) >= MAX_RESTARTS_PER_HOUR:
                print("Maximum restart rate reached; watchdog is stopping.")
                return 1

            print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting process...")
            process = None
            try:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(self.target_script),
                        "--config",
                        str(self.config_path),
                    ],
                    cwd=str(WORKSPACE_DIR),
                )
                exit_code = int(process.wait())
            except KeyboardInterrupt:
                print("\nLauncher stopped by user.")
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5.0)
                return 130
            except OSError as exc:
                print(f"Could not start Paper runtime: {type(exc).__name__}: {exc}")
                return 2

            print(f"Process exited with code: {exit_code}")
            if exit_code == 0:
                print("Process exited normally; watchdog is stopping.")
                return 0
            if exit_code in NONRETRYABLE_EXIT_CODES:
                print("Runtime reported a non-retryable startup error.")
                return exit_code

            self.restart_history.append(time.time())
            print(
                "Process failed; restarting in "
                f"{self.restart_interval_sec:g} seconds..."
            )
            time.sleep(self.restart_interval_sec)

    def _cleanup_history(self):
        now = time.time()
        self.restart_history = [
            timestamp
            for timestamp in self.restart_history
            if now - timestamp < 3600.0
        ]


def main() -> int:
    if not TARGET_SCRIPT.is_file():
        print(f"Error: runtime entrypoint not found: {TARGET_SCRIPT}")
        return 2

    allowed, reason = launcher_allows_runtime(CONFIG_PATH)
    if not allowed:
        print(f"Error: {reason}")
        return 2
    return ProcessWatchdog().run()


if __name__ == "__main__":
    raise SystemExit(main())
