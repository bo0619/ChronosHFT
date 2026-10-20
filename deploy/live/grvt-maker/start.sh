#!/usr/bin/env bash
# 启动 GRVT 实盘做市：每个币种一个 systemd 服务，关掉 SSH / VS Code / 本地电脑后照常运行，
# 服务器重启后自动恢复。之前先做只读账户检查，并要求输入 yes 确认。
#   bash deploy/live/grvt-maker/start.sh
set -euo pipefail
cd "$(dirname "$0")/../../.."

SECRETS=config/local/grvt_secrets.json
CONFIG=config/local/grvt_maker.json
ENVFILE=config/local/grvt_maker.env
UNIT=deploy/live/grvt-maker/chronoshft-grvt-maker@.service
PY=.venv/bin/python

if [ ! -s "$SECRETS" ]; then
  echo "缺少密钥文件 $SECRETS。"
  echo "先执行：cp deploy/live/grvt-maker/grvt_secrets.example.json $SECRETS"
  echo "然后在 VS Code 里填入 api_key、sub_account_id、private_key 并保存。"
  exit 1
fi
chmod 600 "$SECRETS"
if [ -e storage/live/KILL ]; then
  echo "急停文件 storage/live/KILL 存在。确认情况后删除它（rm storage/live/KILL）再启动。"
  exit 1
fi
SYMBOLS=$($PY -c "import json; print(' '.join(json.load(open('$CONFIG'))['symbols']))")

echo "== 只读账户检查（不会下单）=="
if ! $PY scripts/check_grvt_account.py --config "$CONFIG"; then
  echo
  echo "检查没有通过，没有启动。按上面的提示处理后再运行。"
  exit 1
fi

echo
echo "== 将要启动实盘 =="
$PY - "$CONFIG" <<'EOF'
import json, sys
sys.path.insert(0, "scripts")
from run_grvt_maker import load_config
path = sys.argv[1]
for symbol in json.load(open(path))["symbols"]:
    c = load_config(path, symbol)
    q, r = c["quote"], c["risk"]
    print(f"  {symbol}: 每笔 {q.notional:g} USDT，离公允价 ±{q.half_spread_bps:g} bps，持仓上限 {r.max_position_usd:g} USDT，"
          f"当日亏 {r.daily_loss_reduce_usd:g} USDT 只减仓、亏 {r.daily_loss_halt_usd:g} USDT 停止")
EOF
echo
read -r -p "确认用真实资金开始做市？输入 yes 继续：" answer
if [ "$answer" != "yes" ]; then
  echo "已取消，没有启动。"
  exit 1
fi

$PY - "$CONFIG" <<'EOF'
import json, sys
path = sys.argv[1]
config = json.load(open(path))
config["live_enabled"] = True
open(path, "w").write(json.dumps(config, indent=2) + "\n")
EOF
echo "GRVT_MAKER_MODE=--live" > "$ENVFILE"
chmod 600 "$ENVFILE"

sudo install -m 644 "$UNIT" /etc/systemd/system/
sudo systemctl daemon-reload
for s in $SYMBOLS; do
  sudo systemctl reset-failed "chronoshft-grvt-maker@$s" 2>/dev/null || true
  sudo systemctl enable --now "chronoshft-grvt-maker@$s"
  echo "已启动 $s"
done

echo
echo "等 20 秒看第一批状态……"
sleep 20
bash deploy/live/grvt-maker/status.sh
echo
echo "之后随时查看：bash deploy/live/grvt-maker/status.sh"
echo "停止并撤掉所有挂单：bash deploy/live/grvt-maker/stop.sh"
