#!/usr/bin/env bash
# 停止 GRVT 做市：每个进程先撤掉自己的全部挂单再退出，并取消开机自启、关掉实盘开关。
# 持仓不会自动平掉，脚本最后会列出来。
#   bash deploy/live/grvt-maker/stop.sh
set -uo pipefail
cd "$(dirname "$0")/../../.."

CONFIG=config/local/grvt_maker.json
ENVFILE=config/local/grvt_maker.env
PY=.venv/bin/python

units=$( { systemctl list-units --all --plain --no-legend 'chronoshft-grvt-maker@*' | awk '{print $1}';
           ls /etc/systemd/system/multi-user.target.wants/ 2>/dev/null | grep '^chronoshft-grvt-maker@'; } | sort -u)
if [ -z "$units" ]; then
  echo "没有做市服务在运行。"
fi
for u in $units; do
  sudo systemctl disable --now "$u" && echo "已停止 $u"
done

echo "GRVT_MAKER_MODE=" > "$ENVFILE"
$PY - "$CONFIG" <<'EOF'
import json, sys
path = sys.argv[1]
config = json.load(open(path))
config["live_enabled"] = False
open(path, "w").write(json.dumps(config, indent=2) + "\n")
EOF
echo "实盘开关已关闭。"

if [ -s config/local/grvt_secrets.json ]; then
  echo
  echo "== 账户现状（只读）=="
  $PY scripts/check_grvt_account.py --config "$CONFIG" || true
  echo
  echo "如果还有持仓，可以在 GRVT 网页上手动平仓，或者下次启动时由程序继续管理。"
fi
