# ChronosHFT

加密货币永续合约做市框架（Python）。目前用交易所**真实的公开行情**做**本地模拟成交**的纸面交易，来验证策略到底有没有正的 edge；实盘只具备一个受签名许可和严格亏损上限约束的 RPI 校准框架。

> **纸面盘的成交是模型假设。** 本地撮合按公开成交推断我们的挂单是否成交，看不到真实排队位置，也看不到 RPI 的零售对手方。纸面盈亏只能用来迭代策略，不能当成实盘预期。

## 现状一览

| 启动配置 | 交易所行情 | 标的 | 单笔报价 | 手续费（maker / taker） |
| --- | --- | --- | --- | --- |
| `config.json`（默认） | Binance USDⓈ-M | `KORUUSDT` | 500 USDT | 0（含 RPI）/ 2.5 bps |
| `config.grvt.json` | GRVT | `SOXLUSDT` | 100 USDT | −1 bps（返佣）/ 4.5 bps |
| `config.lighter.json` | Lighter | `ETHUSDC` | 100 USDT | 0 / 0 |

- 三套配置都是**纸面模式**：模拟账户 10,000 USDT，不需要 API Key，也不会调用任何私有接口。
- 策略默认是 GLFT 做市，被动挂单；Binance 上的标的支持 RPI 时，用 RPI 单挂。
- 实盘模板在 `deploy/live/`，见下文[实盘](#实盘)。

## 快速开始

在全新的 Ubuntu 主机上执行：

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl git
git clone https://github.com/bo0619/ChronosHFT.git && cd ChronosHFT
bash scripts/bootstrap_aws.sh
```

`bootstrap_aws.sh` 会在仓库内安装固定版本的 `uv` 和 CPython，按 `uv.lock` 还原依赖，再跑一次离线配置检查。它不会改动系统自带的 Python。

先检查配置，再启动：

```bash
.venv/bin/python main.py --check-config
.venv/bin/python main.py
```

- 不带参数时，读取的是 `config.json`（Binance）。
- 要跑其他交易所，加 `--config config.grvt.json` 或 `--config config.lighter.json`。
- **停止时只按一次 `Ctrl+C`**。程序会撤掉所有挂单、平掉持仓、确认账户是空仓后再退出。不要直接关终端或者强杀进程。

### 看板

看板只监听本机 `127.0.0.1:8765`，**不要在安全组里开放 8765 端口**。在你自己的电脑上建一条 SSH 隧道（Amazon Linux 用 `ec2-user`）：

```bash
ssh -N -L 8765:127.0.0.1:8765 ubuntu@EC2_PUBLIC_IP
```

然后在浏览器打开 `http://127.0.0.1:8765/`。页面顶部必须显示 `PAPER · LIVE DATA`；如果显示的是 `LIVE · REAL MONEY`，立刻停掉进程，检查 `execution.mode`。

### 作为系统服务运行（可选）

```bash
sudo bash scripts/install_systemd_service.sh --start
sudo journalctl -u chronoshft -f
```

- 服务读取的也是 `config.json`。
- 安装脚本会设置**开机自启**。如果只想手动启动，执行 `sudo systemctl disable chronoshft`，以后用 `sudo systemctl start chronoshft` 手动启动。
- 服务和手动运行的 `main.py` 不要同时开：两者会抢同一个看板端口。
- 服务的资源上限、看门狗、各类队列的容量见 [docs/operations.md](docs/operations.md)。

## 看板怎么看

看板默认打开**交易**页；所有工程诊断（OMS、网关、事件队列、风控、RPI、时钟、日志等）都在**工程**页，一项没少。

| 交易页区块 | 用来回答 |
| --- | --- |
| 健康灯 | 现在能不能正常做市？不能的话，一句话说明原因，可以跳到工程页的「交易诊断」看详情 |
| 净盈亏 + PnL 曲线 | 从第一次运行到现在一共赚了还是亏了。重启不会清零，虚线标出每次重启 |
| 报价面板 | 每个标的在不在报价、库存多少、我们的买卖价比最优价落后几个 tick |
| 报价位置 | 本次运行中，报价时长占比、两边挂在最优价上的时间占比 |
| 成交质量（markout） | 成交后价格走势是对我们有利还是不利，结果是否在统计上显著 |
| 按标的盈亏 | 每个标的的成交额、净盈亏和净 bps |

**判断"能不能赚钱"，看这两个跟下单规模无关的指标：**

- **净盈亏 / 成交额（bps）**：每成交 1 万 USDT 赚或亏多少。仓位放大，USDT 盈亏会跟着放大，但这个 bps 不会变。
- **成交质量的 t 值**：最长周期（30 秒）的 |t| < 2，说明结果和 0 没有显著差异，还不能下结论；t ≤ −2 说明被逆向选择（成交后价格对我们不利）。至少攒到几百笔成交再看。

净盈亏的算法是：已实现盈亏 − 手续费（负数为返佣）+ 未平仓的浮动盈亏，不含资金费，因为纸面模拟不计算资金费。

## 常用操作

每次改完配置，都先跑一遍 `.venv/bin/python main.py --check-config`。

| 想做的事 | 改哪里 |
| --- | --- |
| 换交易标的 | Binance：`config/symbols.json`；GRVT / Lighter：`config/grvt/symbols.json`、`config/lighter/symbols.json`。选标的可以参考 `scripts/rank_tradfi_symbols.py` 和 `scripts/list_binance_rpi_contracts.py` |
| 调整下单规模和风控 | `config/strategy/capital_scaling.json` 里的 `target_order_notional`、`target_total_risk_notional`、`target_daily_loss`（都是相对 100 USDT 的参考值）。单笔、持仓、敞口、日亏损上限都由它们按比例推导，**不要**在别的文件里单独写这些上限 |
| 改模拟手续费 | `config/paper_trade.json`（或交易所子目录下的同名文件）里的 `maker_fee` 和 `taker_fee`。maker 为负数表示返佣 |
| 切换策略模型 | `config/strategy/core.json` 的 `primary_model`：`glft` 或 `avellaneda_stoikov`，改完重启 |
| 打开或关闭 alpha | `config/strategy/glft.json` 的 `alpha.enabled`。注意这份文件三个交易所共用 |
| 改成交质量的统计周期 | `config/strategy/glft.json` 的 `adaptive.markout.horizons_ms` |

每份配置文件管什么、规模是怎么推导出来的，见 [docs/configuration.md](docs/configuration.md)。

### 查询历史数据

```bash
.venv/bin/python scripts/query_paper_trades.py --summary              # 按运行、标的汇总成交
.venv/bin/python scripts/query_paper_trades.py --limit 100            # 最近成交
.venv/bin/python scripts/query_paper_trades.py --dataset markouts     # 成交后价格走势
.venv/bin/python scripts/calibrate_paper_models.py --database storage/paper/trades.sqlite3 --output storage/paper/model-calibration.json
```

查询工具以只读方式打开数据库，服务运行时也可以放心使用。各张表的字段含义见 [docs/paper-trading.md](docs/paper-trading.md)。

## 数据放在哪里

| 路径 | 内容 |
| --- | --- |
| `storage/paper/trades.sqlite3` | 历次运行、逐笔成交、订单事件、成交质量，以及账户、策略、行情的每秒采样。看板的累计盈亏就读这里 |
| `storage/paper/oms_journal.jsonl` | OMS 审计日志（权威记录）。数据库可以从它重建，**不要单独删它** |
| `storage/*.h5` | 原始盘口和成交录制（HDF5），供离线分析使用，例如 `scripts/evaluate_fair_value.py` |
| `logs/hft_trading.log` | 运行日志，按 32 MiB 轮转，保留 7 份 |

纸面模拟账户每次启动都从 10,000 USDT 重新开始。历史记录保存在上面这个数据库里，**删除 `storage/` 会清空全部历史盈亏**。

## 代码结构

| 目录 | 内容 |
| --- | --- |
| `gateway/` | 交易所接入：`binance/`（实盘和纸面撮合）、`grvt/`、`lighter/` |
| `oms/` | 订单管理：下单闸门、对账、审计日志、纸面数据库 |
| `risk/` | 风控、Kill Switch、交易所 DMS、独立风控子进程（sidecar） |
| `strategy/` | GLFT、Avellaneda-Stoikov、自适应报价、模型就绪检查 |
| `alpha/` | 公允价、预测器、markout 估计 |
| `infrastructure/` | 启动编排、配置加载与校验、时钟、事件引擎、实盘配置闸门 |
| `ui/`、`web/` | 本地看板（后端快照和单页前端） |
| `data/`、`event/` | 合约参考数据、本地订单簿、行情录制、事件类型 |
| `governance/` | 实盘审批签名、校准产物、部署与策略身份摘要、发布清单、依赖图约束 |
| `scripts/` | 部署、查询、标定、离线证据工具 |
| `config/`、`deploy/` | 纸面配置片段；实盘部署模板 |

模块之间的边界见 [docs/configuration.md](docs/configuration.md#runtime-component-boundaries)；时钟和策略数学见 [docs/strategy-models.md](docs/strategy-models.md)。

## 实盘

仓库里跟踪的配置只用于纸面交易。实盘必须单独建一份配置，并通过离线检查（`scripts/check_live_canary_readiness.py`）；第一阶段是 RPI 校准 canary，需要离线签名的许可，单笔和亏损都有严格上限。

- 模板：`deploy/live/rpi-200u/`（200 USDT 账户）、`deploy/live/koru-30u/`（30 USDT 账户交易 1 张 KORU）
- 规则和证据链：[docs/live-deployment.md](docs/live-deployment.md)
- 操作步骤：[docs/live-rpi-framework-validation-runbook.md](docs/live-rpi-framework-validation-runbook.md)

**不要在中国大陆、或任何不允许使用 Binance 的地区运行实盘进程。**

## 开发与测试

```bash
.tools/uv sync --frozen --dev
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest -q
```

- 依赖版本只在 `pyproject.toml` 里声明，`uv.lock` 锁定完整依赖。
- `tests/` 在 `.gitignore` 里采用**白名单**方式：新建测试文件后，要在 `.gitignore` 中加一行 `!tests/<文件名>`，否则文件不会被提交。

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/operations.md](docs/operations.md) | systemd 服务、内存、队列和磁盘上限，性能基准，开发工具链 |
| [docs/paper-trading.md](docs/paper-trading.md) | 纸面模式的边界、执行数据库的表结构、查询和标定工具 |
| [docs/configuration.md](docs/configuration.md) | 配置清单与片段、资金缩放、运行时组件边界、资源监控 |
| [config/README.md](config/README.md) | 配置文件的格式和版本约定（v3） |
| [docs/strategy-models.md](docs/strategy-models.md) | HFT 时钟、GLFT 和 A-S 模型、多资产 Riccati 扩展、自适应控制 |
| [docs/live-deployment.md](docs/live-deployment.md) | 实盘 canary 检查、RPI 校准许可、离线证据工具 |
| [docs/live-rpi-framework-validation-runbook.md](docs/live-rpi-framework-validation-runbook.md) | RPI 校准实盘的操作手册 |
| [docs/sidecar-ipc-protocol.md](docs/sidecar-ipc-protocol.md) | 独立风控子进程的通信协议 |

## 路线图

- [x] 用真实公开行情做纸面交易，完全不调用私有接口
- [x] 按原论文审计 GLFT 和 A-S 的实现
- [x] 签名许可、独立设亏损上限的 RPI 校准 canary
- [x] 看板重设计：交易页（跨重启累计盈亏、PnL 曲线、报价位置、成交质量）和工程页分开
- [ ] 攒到足够的纸面成交，用成交质量和净 bps 判断策略是否有正的 edge
- [ ] 部署并执行第一次真实的 RPI 校准，拿到真实的 `LIVE_BINANCE_RPI_ACK` 样本
- [ ] 放大资金之前补齐模型缺口：微观结构波动率、真实的 RPI 排队和延迟模型、按毒性分组的 markout、参数漂移治理、行情和事件风控、多档报价
