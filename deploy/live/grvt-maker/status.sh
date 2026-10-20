#!/usr/bin/env bash
# GRVT 做市的运行状态（只读，不会改动任何东西）。
#   bash deploy/live/grvt-maker/status.sh
cd "$(dirname "$0")/../../.." || exit 1

echo "== 服务 =="
units=$(systemctl list-units --all --plain --no-legend 'chronoshft-grvt-maker@*' | awk '{print $1}')
[ -z "$units" ] && echo "没有做市服务在运行"
for u in $units; do
  printf "  %-40s %-10s 启动于 %s\n" "$u" "$(systemctl is-active "$u")" "$(systemctl show -p ActiveEnterTimestamp --value "$u")"
done
mode=$(grep -s '^GRVT_MAKER_MODE=' config/local/grvt_maker.env | cut -d= -f2-)
echo "  模式：${mode:+实盘}${mode:-影子（不下单）}"
[ -e storage/live/KILL ] && echo "  注意：急停文件 storage/live/KILL 存在，所有做市进程都会停下"

echo
echo "== 各币种最新状态（来自事件日志）=="
.venv/bin/python - <<'EOF'
import glob, json, os, time
from datetime import datetime, timezone

config = json.load(open("config/local/grvt_maker.json"))
log_dir = config.get("log_dir", "storage/live/grvt_maker")
for symbol in config.get("symbols") or {}:
    files = sorted(glob.glob(f"{log_dir}/{symbol}_*.jsonl"))
    if not files:
        print(f"  {symbol}: 还没有日志")
        continue
    last, halt = None, None
    with open(files[-1]) as fh:
        for line in fh:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") in ("status", "stop"):
                last = event
            elif event.get("event") == "halt":
                halt = event
    if last is None:
        print(f"  {symbol}: 日志里还没有状态记录")
        continue
    age = time.time() - last["ts"]
    when = datetime.fromtimestamp(last["ts"], timezone.utc).strftime("%m-%d %H:%M:%S UTC")
    print(f"  {symbol}: {last['mode']}{'（已停止）' if last['event'] == 'stop' else ''}  记录时间 {when}（{age:.0f} 秒前）")
    print(f"     持仓 {last['position']:g}（{last['exposure_usd']} USDT）  今日盈亏 {last['pnl_today']} USDT  "
          f"成交 {last['fills']} 笔  成交额 {last.get('maker_volume_usd', 0)} USDT  手续费 {last.get('fees_usd', 0)} USDT")
    if last.get("reason"):
        print(f"     原因：{last['reason']}")
    if halt:
        print(f"     最近一次停止：{halt.get('reason')}")
EOF

echo
echo "== 最近 24 小时的告警 =="
journalctl -u 'chronoshft-grvt-maker@*' --since "-24h" --no-pager -o short-iso 2>/dev/null \
  | grep -E "halt|HALTED|venue_error|Traceback|Error|error" | tail -8
echo "（没有输出就是没有告警）"
