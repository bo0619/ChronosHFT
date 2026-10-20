# ChronosHFT

加密货币永续合约的做市研究与交易框架（Python）。

唯一的实盘路径是：**在 GRVT 上做市，用币安的价格定价**（`scripts/run_grvt_maker.py`）。程序作为 systemd 服务运行在东京的云服务器上，本地电脑关机也不影响。

- 第一阶段是 200 USDT 的试运行，只做 XRP 和 ADA。
- 币安只用作行情来源和纸面撮合，币安实盘下单和 Lighter 都已删除。

> **回放和纸面盘的成交都是模型假设。** 它们根据公开数据推断我们的挂单会不会成交，看不到真实的排队位置，也不知道别人用不用 post-only。回放的盈亏只用来比较和筛选，真实收益要靠实盘验证。

## 现状一览

| 部分 | 入口 | 状态 |
| --- | --- | --- |
| **GRVT 做市（实盘）** | `deploy/live/grvt-maker/start.sh`（systemd 服务，内部运行 `scripts/run_grvt_maker.py`） | 程序、风控和一键启停已就绪，影子模式跑通；等填好密钥后开始 200 USDT 试运行 |
| 账户检查 | `scripts/check_grvt_account.py` | 只读：登录、余额、持仓、挂单、下单规则 |
| 行情录制 | `scripts/record_public_book.py`（币安）、`scripts/record_grvt_book.py`（GRVT） | 两轮录制已完成（research2 72 小时、research3 11 小时） |
| 延迟测量 | `scripts/measure_latency.py` | 录制期间每 6 小时测一次，共 11 次 |
| 跨交易所回放 | `scripts/replay_cross_venue.py` | 主要的筛选工具，和实盘共用同一套报价规则 |
| 毒性因子检验 | `scripts/evaluate_maker_toxicity.py` | 周末数据上 5 个因子有效，工作日数据还没复核 |
| 纸面交易框架 | `main.py`，配置 `config.json`（币安）和 `config.grvt.json`（GRVT） | 只能跑纸面模式，配置写了 `"mode": "live"` 会拒绝启动 |

## 研究结论

数据：research2 共 60 小时（周六到周一，含美股时段，15 个币）；research3 共 11 小时（周一，16 个新币）。回放按 GRVT 真实费率（maker −0.01 bps）计算，下单延迟从 11 次实测中随机抽样。

### 两种成交假设，差别在哪里

- **乐观（`--cross skip`）和悲观（`--cross fill`）只在一种情况上不同：** 币安一动，GRVT 上别的做市商改价比我们撤单快，盘口越过了我们的挂单（下文称"穿过"）。
- **以 XRP 为例：** "穿过"每小时约 110 次。真正有人按我们价格成交的那部分，每笔赚约 0.5 bps；"穿过"如果算作成交，每笔亏约 0.9 bps。
- **到底算不算成交，取决于对方用不用 post-only。** 用 post-only，我们就不会被成交，接近乐观；不用，我们会以旧价格被成交，接近悲观。这件事公开数据回答不了，**只有实盘能回答**。

### 核心币：60 小时，每笔 100 USDT，±0.25 bps

| 币种 | 成交/小时 | 每笔优势 bps（t 值） | 乐观 USDT/小时 | 四个时段（周六 / 周日 / 周一白天 / 美股时段） | 悲观 USDT/小时 |
| --- | --- | --- | --- | --- | --- |
| PUMP | 16.6 | +1.49 (6.5) | +0.25 | 全部为正 | −2.46 |
| XRP | 52.8 | +0.44 (9.1) | +0.24 | 全部为正 | −0.91 |
| SUI | 13.8 | +0.62 (5.6) | +0.09 | 全部为正 | −1.57 |
| DOGE | 7.6 | +0.71 (4.1) | +0.05 | 全部为正 | −0.90 |

其余 11 个币接近 0。美股时段"穿过"最多，悲观假设下的亏损是平时的 4–6 倍。

### 新币：11 小时，挂得远一点更稳

| 币种（挂单距离） | 乐观 USDT/小时（t 值） | 悲观 USDT/小时 |
| --- | --- | --- |
| **ADA（±2 bps）** | +0.17 (6.3) | +0.08 |
| KAITO（±1 bps） | +0.12 (2.5) | +0.20 |
| JUP（±1 bps） | +0.16 (3.9) | −0.04 |
| FARTCOIN（±0.25 bps） | +0.12 (4.1) | +0.07 |
| BERA（±2 bps） | +0.07 (3.7) | +0.09 |

- **淘汰：** ENA、UNI、LTC、LINK、TSLA、META、INTC，主要是悲观假设下亏得多，美股时段更严重。
- **为什么新币挂远一点更稳：** 这些币 GRVT 价差宽，我们挂 ±1–2 bps 仍然是最优价，照样拿得到成交，"穿过"却很少碰到我们。

### 其他结论

| 方向 | 结论 |
| --- | --- |
| 在币安上被动做市（包括 RPI、高频因子、DeepLOB、扫单后的反转） | **亏钱**：不算手续费每笔就亏约 1 bps |
| 挂单距离 | 要按币种设：核心币 ±0.25 bps，价差宽的新币 ±1–2 bps。按波动率加宽、多档报价都会降低利润 |
| 延迟的影响 | 乐观假设下多 25 ms 几乎没影响；悲观假设下 XRP 从 −0.91 变成 −2.34 USDT/小时。速度很重要，而且只在不确定的那一边重要 |
| GRVT 跟随币安的速度 | 盘口每 50 ms 批量推送一次。按推送时间算的中位数：XRP、PUMP、ENA 约 34–42 ms，ADA、JUP、KAITO 约 90–190 ms。跟得慢的币留给我们的空间更大 |
| GRVT 返佣 | 0 级用户只有 **0.01 bps**（2026-03-23 起的费率），利润几乎全部来自价差和定价。做市商计划 Basic 档有 0.3 bps，值得申请 |
| 毒性因子 | GRVT 落后于公允价的程度、其他币的同步变动、本侧排队厚度最有用。适合按信号调整挂单量（样本外 +20–40%），不适合直接撤单 |

### 延迟（东京服务器，11 次实测合并）

| 延迟（ms） | p5 | p25 | p50 | p75 | p95 | p99 |
| --- | --- | --- | --- | --- | --- | --- |
| 币安行情推送 | 1.8 | 2.2 | 2.9 | 10.4 | 44.3 | 83.9 |
| GRVT 行情推送 | 6.1 | 7.3 | 8.8 | 12.1 | 28.4 | 42.3 |
| GRVT 下单往返 | 9.4 | 10.8 | 12.9 | 17.6 | 27.3 | 187 |

美股时段币安推送的中位数会升到约 9 ms，GRVT 下单往返升到约 15 ms。

## GRVT 做市

### 试运行配置（200 USDT）

| 币种 | 挂单距离 | 每笔 | 单币持仓上限 | 当天亏损：只减仓 / 停止 |
| --- | --- | --- | --- | --- |
| XRP | ±0.25 bps | 50 USDT | 150 USDT | 3 / 5 USDT |
| ADA | ±2 bps | 50 USDT | 150 USDT | 3 / 5 USDT |

- **为什么选这两个：** XRP 成交最多、统计最可靠，能最快回答"穿过时会不会成交"；ADA 在两种假设下都为正。
- **最大敞口：** 两个币合计最多 300 USDT（1.5 倍）。
- **回放预期（每天）：** 乐观约 +4.7 USDT，最差约 −4.5 USDT（XRP 会被单日止损截住）。
- **GRVT 网页上的杠杆不要调得太低。** 挂单也占保证金，建议保持默认或 5 倍以上；真正的风险由程序的持仓上限控制。

配置文件是 `config/local/grvt_maker.json`（不进 git），模板是 `deploy/live/grvt-maker/grvt_maker.example.json`。每个币种可以单独覆盖默认参数。

### 启动、查看、停止

1. 在 GRVT 开一个**专用的交易子账户**，只放可以承受亏损的钱。在这个子账户上创建 API 密钥：开交易权限，不开提币，IP 白名单填服务器的公网 IP。
2. 在服务器上打开 `config/local/grvt_secrets.json`，自己填入 `api_key`、`sub_account_id`、`private_key`。**不要提交，也不要发到任何地方。**
3. 运行 `bash deploy/live/grvt-maker/start.sh`，或者在 VS Code 里点"终端 → 运行任务 → 做市：启动（实盘）"。脚本会依次：
   - 设置密钥文件的权限；
   - 做只读账户检查；
   - 列出每个币种的规模和止损；
   - 等你输入 `yes` 确认；
   - 打开实盘开关，为每个币种启动一个 systemd 服务。

| 操作 | 命令（VS Code 任务里也有） |
| --- | --- |
| 查看状态（只读） | `bash deploy/live/grvt-maker/status.sh`：服务状态、持仓、今日盈亏、成交、告警 |
| 账户检查（只读） | `.venv/bin/python scripts/check_grvt_account.py` |
| 停止 | `bash deploy/live/grvt-maker/stop.sh`：每个进程先撤掉全部挂单再退出，然后关掉实盘开关和开机自启。持仓不会自动平掉，脚本最后会列出来 |
| 紧急停止 | `touch storage/live/KILL`：所有币种一起停，删掉这个文件后才能再启动 |

服务由 systemd 托管（`deploy/live/grvt-maker/chronoshft-grvt-maker@.service`，一个币种一个实例）：

- **关掉 VS Code、断开 SSH、本地电脑关机都不影响**，服务器重启后会自动恢复；
- 进程意外崩溃，15 秒后自动重启，每小时最多 3 次；
- 因风控停下（退出码 3）不会自动重启，等人检查。

### 引擎的风控

| 情况 | 处理 |
| --- | --- |
| 连接断开：几秒内既没有行情也没有心跳回应（每 2 秒 ping 一次） | 撤掉全部挂单，恢复后自动继续。行情只是清淡、价格没变时，挂单保留 |
| 币安行情延迟超过 250 ms | 撤掉全部挂单，恢复后继续 |
| 持仓达到上限（`max_position_usd`） | 不再挂加仓那一侧 |
| 当天亏损超过 `daily_loss_reduce_usd` | 只挂减仓方向的单，UTC 第二天恢复 |
| 当天亏损超过 `daily_loss_halt_usd`、出现吃单成交、maker 费率异常、连续接口出错 | 全部撤单并停止（退出码 3），需要人工检查 |
| 进程崩溃或断网 | GRVT 的断线自动撤单（cancel-on-disconnect）在 5 秒内撤掉所有挂单 |

### 影子模式（不需要密钥，不下单）

```bash
.venv/bin/python scripts/run_grvt_maker.py --symbol XRPUSDT --minutes 30
```

用真实行情照常计算报价，每一笔"本来要下的单"都写进 `storage/live/grvt_maker/<币种>_<日期>.jsonl`，影子订单不会成交。

更多细节见 [deploy/live/grvt-maker/README.md](deploy/live/grvt-maker/README.md)。**不要在中国大陆运行实盘。**

## 研究工具

```bash
# 录制币安：只录最优价、一个币种一个连接，一个进程可以录很多币
.venv/bin/python scripts/record_public_book.py --symbols JUPUSDT,ADAUSDT --hours 12 --out storage/research3/binance \
    --ticker-only --connection-per-symbol --ticker-price-changes-only
# 录制 GRVT：盘口和成交
.venv/bin/python scripts/record_grvt_book.py --symbols JUPUSDT,ADAUSDT --hours 12 --out storage/research3/grvt

# 跨交易所回放：真实费率，下单延迟从实测样本抽取（--extra-latency-ms 可以加压）
.venv/bin/python scripts/replay_cross_venue.py --grvt-dir storage/research2/grvt --binance-dir storage/research2/binance \
    --symbols XRPUSDT --start 2026-10-18T00:00 --end 2026-10-19T00:00 --slice-hours 6 --notional 100 \
    --anchor binance --half-spread-bps 0.25 --improve --skew-bps-per-lot 1 --max-inventory-lots 5 \
    --cross skip --latency-samples storage/research2/latency/latency_20261019_040002.csv.gz --maker-fee-bps -0.01

# 毒性因子：前一段训练、后一段检验
.venv/bin/python scripts/evaluate_maker_toxicity.py --grvt-dir storage/research2/grvt --binance-dir storage/research2/binance \
    --symbols XRPUSDT,PUMPUSDT --start 2026-10-17T03:46 --end 2026-10-18T23:10 --split 2026-10-18T09:10 \
    --latency-samples storage/research2/latency/latency_20261018_220001.csv.gz
```

- **录制中的文件不要直接读。** 先在 HDF5 读锁下拷一份再分析，录制程序在被锁时会推迟写入。
- **录制或测延迟时不要在同一台机器上跑重分析。** 服务器只有 2 核。之前回放占满 CPU，把那段时间的延迟测试拖慢了 10 倍以上，录到的时间戳也会偏晚。
- **内存约 4 GB。** 分析脚本用 `choom -n 1000` 运行并按 `--slice-hours` 分段读取，避免录制或做市进程被系统杀掉。服务器上另有 2 GB 交换文件（重启后不保留）。
- **两种成交假设要一起看。** `--cross skip` 偏乐观，`--cross fill` 偏悲观，真实情况在两者之间，见上文。

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
| `storage/research2/` | 第一轮录制（72 小时，15 个币）：币安、GRVT 行情和 11 次延迟测试 |
| `storage/research3/` | 第二轮录制（11 小时）：16 个新币，以及 BTC、ETH 的币安行情 |
| `storage/live/grvt_maker/` | GRVT 做市的事件日志，影子模式和实盘都写这里，一个币种一天一个文件 |
| `storage/live/KILL` | 紧急停止文件（存在时所有做市进程停下） |
| `storage/paper/` | 纸面交易数据库和 OMS 审计日志（审计日志是权威记录，不要单独删） |
| `config/local/` | 本地配置、密钥文件和服务的模式开关（`grvt_maker.env`），都不进 git |
| `logs/` | 纸面框架的运行日志 |

## 代码结构

| 目录 | 内容 |
| --- | --- |
| `strategy/grvt_cross_maker.py` | GRVT 做市的报价规则（公允价、挂单价、库存偏移、改价规则），**回放和实盘共用** |
| `strategy/grvt_maker_engine.py` | GRVT 做市实盘引擎：订单状态、持仓、盈亏、风控、连接存活判断 |
| `gateway/grvt/` | GRVT：订单签名、REST 下单、私有推送、盘口重建，以及纸面网关 |
| `gateway/binance/` | 币安：bookTicker 行情接入（`/public` 路由）、公开行情、纸面撮合（不再有实盘下单） |
| `scripts/` | 做市运行程序和账户检查、录制、回放、因子检验、延迟测量、纸面查询和标定 |
| `deploy/live/grvt-maker/` | 做市的配置模板、systemd 服务模板、启动 / 停止 / 状态脚本 |
| `oms/`、`risk/`、`alpha/`、`infrastructure/`、`ui/`、`web/` | 纸面交易框架：订单管理、风控、公允价与预测、启动编排、本地看板 |
| `config/` | 纸面框架的配置片段 |

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
| [deploy/live/grvt-maker/README.md](deploy/live/grvt-maker/README.md) | GRVT 做市的上线步骤、服务和风控 |
| [docs/configuration.md](docs/configuration.md) | 纸面框架的配置清单、资金缩放、组件边界 |
| [docs/paper-trading.md](docs/paper-trading.md) | 纸面模式的边界、数据库表结构、查询和标定工具 |
| [docs/operations.md](docs/operations.md) | 纸面框架的 systemd 服务、资源上限、性能基准 |
| [docs/strategy-models.md](docs/strategy-models.md) | GLFT、A-S 模型和自适应控制 |
| [config/README.md](config/README.md) | 配置文件格式（v3） |

## 路线图

- [x] 证明在币安上被动做市没有优势
- [x] 跨交易所回放：GRVT 用币安定价，按真实费率和实测延迟计算
- [x] 用工作日和美股时段数据确认核心币；筛出新币里的 ADA、KAITO、JUP、FARTCOIN、BERA
- [x] 实盘程序：按币种配置、连接心跳、systemd 服务、一键启停
- [ ] **200 USDT 试运行 XRP 和 ADA**，核对成交频率、每笔优势、延迟和手续费
- [ ] 用实盘回答"穿过时会不会成交"，决定核心币（PUMP、SUI、DOGE）能不能上
- [ ] 结果接近回放后再放大规模，加入 KAITO、JUP 等新币
- [ ] 按毒性信号调整挂单量（先用工作日数据复核因子）
- [ ] 查 GRVT 有没有比 50 ms 更快的盘口推送；申请做市商返佣档
