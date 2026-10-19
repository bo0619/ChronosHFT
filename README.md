# ChronosHFT

加密货币永续合约的做市研究与交易框架（Python）。

现在只有一条实盘路径：**在 GRVT 上做市，用币安的价格定价**（`scripts/run_grvt_maker.py`）。币安只用作行情来源，以及纸面撮合的对象。币安实盘下单和 Lighter 都已经从仓库里删掉了。

> **回放和纸面盘的成交都是模型假设。** 它们根据公开成交推断我们的挂单会不会成交，看不到真实的排队位置。回放里的盈亏只能用来比较和筛选，真实收益要靠小资金实盘验证。

## 现状一览

| 部分 | 入口 | 状态 |
| --- | --- | --- |
| **GRVT 跨交易所做市** | `scripts/run_grvt_maker.py` | 影子模式已跑通；实盘需要两道开关加上密钥文件，还没下过真实订单 |
| 行情录制 | `scripts/record_public_book.py`（币安）、`scripts/record_grvt_book.py`（GRVT） | 在用，一个币安币种一个进程 |
| 网络和交易所延迟 | `scripts/measure_latency.py` | 在用，录制期间每 6 小时测一次 |
| 跨交易所回放 | `scripts/replay_cross_venue.py` | 主要的筛选工具，和实盘共用同一套报价逻辑 |
| 毒性因子检验 | `scripts/evaluate_maker_toxicity.py` | 有 5 个因子有效，待工作日数据确认 |
| 纸面交易框架 | `main.py`，配置 `config.json`（币安）和 `config.grvt.json`（GRVT） | 只能跑纸面模式；配置写了 `"mode": "live"` 会直接拒绝启动 |

## 目前的研究结论

| 方向 | 结论 |
| --- | --- |
| 在币安上被动做市（包括 RPI、高频因子、DeepLOB、扫单后的反转） | **亏钱**：不算手续费每笔就亏约 1 bps。信息在源头市场，我们排在队尾，又比别人慢 |
| **在 GRVT 上做市、用币安定价** | **为正**：周末 43.5 小时里，XRP、PUMP、SUI、DOGE 在 6 段数据中段段为正，样本外的 14 小时也通过了。LDO 的成交在盘口上大多看不到对应挂单，回放结果可能偏乐观，暂不实盘 |
| GRVT 返佣 | 0 级用户只有 **0.01 bps**（2026-03-23 起的费率），不是 1 bps。利润几乎全部来自价差和用币安定价。做市商计划 Basic 档有 0.3 bps，可以去申请 |
| 挂单距离 | 固定 ±0.25 bps 最好。按波动率加宽、或者多档报价，都会让利润下降 |
| 毒性因子 | GRVT 落后于公允价的程度、其他币的同步变动、本侧排队厚度最有用。适合按信号调整挂单量（样本外 +20–40%），不适合直接撤单 |
| 延迟（东京） | 币安下单往返约 2.2 ms，盘口推送中位数约 2.7 ms；GRVT 下单往返中位数约 11.6 ms，偶尔会有 90–190 ms 的尖峰 |

## 快速开始

在全新的 Ubuntu 主机上：

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl git
git clone https://github.com/bo0619/ChronosHFT.git && cd ChronosHFT
bash scripts/bootstrap_aws.sh
```

`bootstrap_aws.sh` 会在仓库内安装固定版本的 `uv` 和 CPython，按 `uv.lock` 还原依赖，不动系统自带的 Python。

## GRVT 做市

### 影子模式（不需要密钥，不下单）

```bash
cp deploy/live/grvt-maker/grvt_maker.example.json config/local/grvt_maker.json
.venv/bin/python scripts/run_grvt_maker.py --symbol XRPUSDT --minutes 30
```

行情是真实的，引擎会照常计算报价，把每一笔"本来要下的单"写进 `storage/live/grvt_maker/<币种>_<日期>.jsonl`。影子订单不会成交。

### 实盘

1. 开一个**专用的 GRVT 子账户**，里面只放可以承受亏损的钱。创建一个有交易权限的 API 密钥。
2. 把 `deploy/live/grvt-maker/grvt_secrets.example.json` 复制成 `config/local/grvt_secrets.json`，在服务器上自己填入 API 密钥、子账户 ID 和签名私钥，然后执行 `chmod 600`。**不要提交，也不要发到任何地方。**
3. 在 `config/local/grvt_maker.json` 里设置 `"live_enabled": true`，并调整挂单量和风控限额。
4. 启动时加上 `--live`。

| 情况 | 引擎的处理 |
| --- | --- |
| 币安或 GRVT 行情过期、币安行情延迟超过 250 ms | 撤掉全部挂单，恢复后自动继续 |
| 持仓达到上限（默认 500 USDT） | 不再挂加仓那一侧 |
| 当天亏损超过 `daily_loss_reduce_usd` | 只挂减仓方向的单，第二天恢复 |
| 当天亏损超过 `daily_loss_halt_usd`、出现吃单成交、maker 费率异常、连续接口出错 | 全部撤单并停止，需要人工检查 |
| 进程崩溃或断网 | GRVT 的断线自动撤单（cancel-on-disconnect）在 5 秒内撤掉所有挂单 |
| 需要紧急停止 | `touch storage/live/KILL`，所有币种一起停 |

一个进程只做一个币种。详细说明见 [deploy/live/grvt-maker/README.md](deploy/live/grvt-maker/README.md)。**不要在中国大陆运行实盘。**

## 研究工具

```bash
# 录制：币安一个币种一个进程（多个热门币挤在同一个连接上会严重滞后）
.venv/bin/python scripts/record_public_book.py --symbols XRPUSDT --hours 72 --out storage/research2/binance --ticker-price-changes-only
.venv/bin/python scripts/record_grvt_book.py --symbols XRPUSDT,PUMPUSDT --hours 72 --out storage/research2/grvt

# 跨交易所回放：按 GRVT 的真实费率（maker −0.01 bps）
.venv/bin/python scripts/replay_cross_venue.py --grvt-dir storage/research2/grvt --binance-dir storage/research2/binance \
    --symbols XRPUSDT --start 2026-10-17T03:46 --end 2026-10-18T23:10 --slice-hours 6 --notional 100 \
    --anchor binance --half-spread-bps 0.25 --improve --skew-bps-per-lot 1 --max-inventory-lots 5 \
    --cross skip --latency-samples storage/research2/latency/latency_20261018_220001.csv.gz --maker-fee-bps -0.01

# 毒性因子：前一段训练、后一段检验
.venv/bin/python scripts/evaluate_maker_toxicity.py --grvt-dir storage/research2/grvt --binance-dir storage/research2/binance \
    --symbols XRPUSDT,PUMPUSDT --start 2026-10-17T03:46 --end 2026-10-18T23:10 --split 2026-10-18T09:10 \
    --latency-samples storage/research2/latency/latency_20261018_220001.csv.gz
```

- **录制中的文件不要直接读。** 先在 HDF5 读锁下拷一份再分析，录制程序在被锁时会推迟写入。
- 服务器内存只有约 4 GB，分析脚本用 `choom -n 1000` 运行，并按 `--slice-hours` 分段读取，避免录制进程被系统杀掉。
- 回放的两种成交模型：`--cross skip` 偏乐观，`--cross fill` 偏悲观，真实情况在两者之间。目前的证据更接近 `skip`。

## 纸面交易框架

```bash
.venv/bin/python main.py --check-config
.venv/bin/python main.py                          # 币安纸面盘，读取 config.json
.venv/bin/python main.py --config config.grvt.json
```

- 模拟账户 10,000 USDT，不需要 API 密钥，不调用任何私有接口。默认策略是 GLFT 被动做市。
- 停止时只按一次 `Ctrl+C`，程序会撤单、平仓、确认空仓后再退出。
- 看板只监听 `127.0.0.1:8765`，通过 SSH 隧道访问：`ssh -N -L 8765:127.0.0.1:8765 ubuntu@EC2_PUBLIC_IP`，然后打开 `http://127.0.0.1:8765/`。
- 作为系统服务运行：`sudo bash scripts/install_systemd_service.sh --start`（详见 [docs/operations.md](docs/operations.md)）。

| 想做的事 | 改哪里 |
| --- | --- |
| 换交易标的 | 币安：`config/symbols.json`；GRVT：`config/grvt/symbols.json` |
| 调整下单规模和风控 | `config/strategy/capital_scaling.json` 的 `target_order_notional`、`target_total_risk_notional`、`target_daily_loss` |
| 改模拟手续费 | `config/paper_trade.json`，或交易所子目录下的同名文件（maker 为负数表示返佣） |
| 切换策略模型 | `config/strategy/core.json` 的 `primary_model`：`glft` 或 `avellaneda_stoikov` |

查询成交：`.venv/bin/python scripts/query_paper_trades.py --summary`。数据库表结构见 [docs/paper-trading.md](docs/paper-trading.md)。

## 数据放在哪里

| 路径 | 内容 |
| --- | --- |
| `storage/research2/` | 最近一轮录制：币安、GRVT 行情和延迟测试 |
| `storage/live/grvt_maker/` | GRVT 做市的事件日志（影子模式和实盘都写这里） |
| `storage/paper/` | 纸面交易数据库和 OMS 审计日志（审计日志是权威记录，不要单独删） |
| `config/local/` | 本地配置和密钥文件，不进 git |
| `logs/` | 纸面框架的运行日志 |

## 代码结构

| 目录 | 内容 |
| --- | --- |
| `strategy/grvt_cross_maker.py` | GRVT 做市的报价规则（公允价、挂单价、库存偏移、改价规则），**回放和实盘共用** |
| `strategy/grvt_maker_engine.py` | GRVT 做市实盘引擎：订单状态、持仓、盈亏、风控 |
| `gateway/grvt/` | GRVT：订单签名、REST 下单、私有推送、盘口重建，以及纸面网关 |
| `gateway/binance/` | 币安：bookTicker 行情接入、公开行情、纸面撮合（不再有实盘下单） |
| `scripts/` | 录制、回放、因子检验、延迟测量、GRVT 做市运行程序、纸面查询和标定 |
| `oms/`、`risk/`、`alpha/`、`infrastructure/`、`ui/`、`web/` | 纸面交易框架：订单管理、风控、公允价与预测、启动编排、本地看板 |
| `config/`、`deploy/live/grvt-maker/` | 纸面配置片段；GRVT 做市的配置模板 |

## 开发与测试

```bash
.tools/uv sync --frozen --dev
.venv/bin/python -m pytest -q
```

- 依赖只在 `pyproject.toml` 里声明，`uv.lock` 锁定完整依赖。
- `tests/` 在 `.gitignore` 里是**白名单**：新建测试文件后，要在 `.gitignore` 里加一行 `!tests/<文件名>`，否则不会被提交。
- 已知问题：`tests/test_governance_dependencies.py::test_production_dependency_graph_is_acyclic` 会扫描到 `.tools/` 里 uv 缓存的第三方包，导致失败，和项目代码无关。

## 文档

| 文档 | 内容 |
| --- | --- |
| [deploy/live/grvt-maker/README.md](deploy/live/grvt-maker/README.md) | GRVT 做市的上线步骤和风控 |
| [docs/configuration.md](docs/configuration.md) | 纸面框架的配置清单、资金缩放、组件边界 |
| [docs/paper-trading.md](docs/paper-trading.md) | 纸面模式的边界、数据库表结构、查询和标定工具 |
| [docs/operations.md](docs/operations.md) | systemd 服务、资源上限、性能基准 |
| [docs/strategy-models.md](docs/strategy-models.md) | GLFT、A-S 模型和自适应控制 |
| [config/README.md](config/README.md) | 配置文件格式（v3） |

## 路线图

- [x] 证明在币安上被动做市没有优势
- [x] 跨交易所回放：GRVT 用币安定价，按真实费率计算
- [x] GRVT 做市实盘程序（影子模式已跑通）
- [x] 删除币安实盘下单和 Lighter
- [ ] 用工作日数据确认 XRP、PUMP、SUI、DOGE 的优势和毒性因子
- [ ] 小资金实盘：每次挂 100 USDT，验证成交、延迟和费率，然后再放大
- [ ] 按毒性信号调整挂单量；扩大 GRVT 币种（加密货币、美股、商品）；录制 BTC、ETH 作为领先信号
