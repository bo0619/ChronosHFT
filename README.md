# ChronosHFT

加密货币永续合约的做市研究与交易框架（Python）。

唯一的实盘路径是：**在 GRVT 上做市，用币安的价格定价**（`scripts/run_grvt_maker.py`）。程序作为 systemd 服务运行在东京的云服务器上，本地电脑关机也不影响。

- **第一次实盘**（XRP + ADA，4 小时）发现回放把成交次数高估了几十倍。原因是 GRVT 盘口有大量看不见的挂单，排在我们前面。
- **回放已经修正。** 修正后只剩 ADA、PUMP 两个币还明显为正，现在用它们跑第二次实盘，同时录制盘口用来校准。
- 币安只用作行情来源和纸面撮合，币安实盘下单和 Lighter 都已删除。

> **回放的成交是模型推断，不是事实。** 第一次实盘证明，回放对排队位置的假设会让结论整个翻转。任何新结论都要先用实盘数据校准，再用来做决定。

## 现状一览

| 部分 | 入口 | 状态 |
| --- | --- | --- |
| **GRVT 做市（实盘）** | `deploy/live/grvt-maker/start.sh`（systemd 服务） | 第二次试运行：ADA + PUMP，每笔 250 USDT，计划跑 48 小时 |
| 账户检查 | `scripts/check_grvt_account.py` | 只读：登录、余额、持仓、挂单、下单规则 |
| 行情录制 | `scripts/record_public_book.py`（币安）、`scripts/record_grvt_book.py`（GRVT） | 在录 `storage/research4/`：ADA、PUMP，和实盘同时进行 |
| 延迟测量 | `scripts/measure_latency.py` | 之前测过 11 次，结果见下表 |
| 跨交易所回放 | `scripts/replay_cross_venue.py` | 已修正排队模型；ADA 的成交频率和实盘吻合，XRP 仍比实盘多约 5 倍 |
| 毒性因子检验 | `scripts/evaluate_maker_toxicity.py` | 基于旧回放的结论，需要重新验证 |
| 纸面交易框架 | `main.py`，配置 `config.json`（币安）和 `config.grvt.json`（GRVT） | 只能跑纸面模式 |

## 研究结论

### 第一次实盘告诉我们什么（2026-10-20，XRP + ADA，每笔 250 USDT，4 小时）

| | 旧回放的预期 | 实盘 |
| --- | --- | --- |
| XRP 成交/小时 | 51 | **1.5** |
| ADA 成交/小时 | 5.5 | **1.0** |
| 成交质量 | 每笔 +0.4 到 +3 bps | 10 笔里 8 笔成交时价格已经对我们不利，平均 −2.8 bps |
| 被"穿过"时会不会成交 | 不知道（乐观 / 悲观两种假设） | **不会**：只出现 post-only 拒单，别人跟价也用 post-only |
| 手续费、风控、断线处理 | — | 都正常：maker 返佣 −0.01 bps，没有吃单成交 |

结果是亏 0.73 USDT，然后停掉。

### 回放错在哪里，怎么修的

- **GRVT 的盘口只显示一部分挂单。** 用录制数据检验：最优价上的成交里，XRP 有 45%、ADA 有 54% 的成交量超过了显示的挂单量（中位数分别是 3.1 倍和 6.9 倍）。而且这一档价位 200 ms 后几乎都还在，说明成交的主要是隐藏挂单，它们排在我们前面。
- **旧回放的错误：** 假设每笔成交都先消耗我们前面显示的挂单，剩下的就轮到我们。
- **修正方法（`--queue-depletion visible`，现在是默认）：** 一笔成交能推进我们排队位置的量，只按那一档显示的挂单实际减少了多少来算。旧算法保留为 `--queue-depletion trades`，用来对比。
- **"被穿过"的处理：** 实盘证明穿过不会成交，所以回放用 `--cross skip`。

### 修正后重新筛选（31 个币，每笔 250 USDT）

| 币种（挂单距离） | 成交/小时 | 每笔优势 bps（t 值） | 每天 USDT |
| --- | --- | --- | --- |
| **ADA（±2 bps）** | 1.5 | +6.0 (5.8) | **+3.9** |
| **PUMP（±1 bps）** | 3.0 | +1.7 (2.5) | **+2.1** |
| FARTCOIN（±0.25 bps） | 0.4 | +6.1 (2.0) | +1.4 |
| KAITO（±2 bps） | 0.6 | +2.6 (1.0) | +1.2 |
| 其余 27 个，包括 XRP、SUI、DOGE | — | — | 0.5 以下或为负 |

- **每笔的优势还在，但成交很少，** 整体只能赚到很少的钱。
- **之前"每天 15–30 USDT"的估计，** 是被旧回放的错误放大出来的。

### 换单太频繁吗？

- 实盘 XRP 每分钟换单 22 次，其中 69% 是价格不利时的"后撤"，31% 是"追价"。
- 在修正后的回放里测了 9 种少换单的规则，包括少追价，以及小幅不利时先不撤（`adverse_bps`）。**没有一种比现在的规则好。**
- 原因：少追价，挂单会落在后面，成交就没了；小幅不利时先不撤，多出来的成交恰恰是价格已经过时、被别人吃掉的那些。

### 其他仍然成立的结论

| 方向 | 结论 |
| --- | --- |
| 在币安上被动做市 | 亏钱：不算手续费每笔就亏约 1 bps |
| GRVT 返佣 | 0 级用户只有 0.01 bps，利润只能来自价差和定价。做市商计划 Basic 档有 0.3 bps，值得申请 |
| GRVT 盘口推送 | 每 50 ms 批量推送一次 |
| 挂单距离 | 要按币种设：价差只有 1 档的币（XRP）很难排到前面，价差宽或波动大的币挂得远一点 |

### 延迟（东京服务器，11 次实测合并，单位 ms）

| 延迟 | p5 | p25 | p50 | p75 | p95 | p99 |
| --- | --- | --- | --- | --- | --- | --- |
| 币安行情推送 | 1.8 | 2.2 | 2.9 | 10.4 | 44.3 | 83.9 |
| GRVT 行情推送 | 6.1 | 7.3 | 8.8 | 12.1 | 28.4 | 42.3 |
| GRVT 下单往返 | 9.4 | 10.8 | 12.9 | 17.6 | 27.3 | 187 |

实盘程序现在会逐笔记录每张订单的等待、签名和往返耗时（事件日志里的 `ack` 和 `cancel_ack`）。

## GRVT 做市

### 当前配置：第二次试运行（200 USDT 账户）

| 币种 | 挂单距离 | 每笔 | 单币持仓上限 | 当天亏损：只减仓 / 停止 |
| --- | --- | --- | --- | --- |
| ADA | ±2 bps | 250 USDT | 250 USDT（1 笔） | 5 / 10 USDT |
| PUMP | ±1 bps | 250 USDT | 250 USDT（1 笔） | 5 / 10 USDT |

- **目的：** 用实盘成交和同时录制的盘口，校准修正后的回放。
- **回放预期：** 两个币合计每天约 +6 USDT。
- **风险：** 两个币合计最多持仓 500 USDT（本金的 2.5 倍），最坏的一天约 −20 USDT。
- **杠杆：** GRVT 上保持 10 倍，只用作保证金额度。

配置文件是 `config/local/grvt_maker.json`（不进 git），模板是 `deploy/live/grvt-maker/grvt_maker.example.json`。每个币种可以单独覆盖默认参数，包括 `adverse_bps`（默认 0）。

### 启动、查看、停止

1. 在 GRVT 开一个**专用的交易子账户**，只放可以承受亏损的钱。在这个子账户上创建 API 密钥：开交易权限，不开提币，IP 白名单填服务器的公网 IP。
2. 在服务器上打开 `config/local/grvt_secrets.json`，自己填入 `api_key`、`sub_account_id`、`private_key`。**不要提交，也不要发到任何地方。**
3. 运行 `bash deploy/live/grvt-maker/start.sh`，或者在 VS Code 里点"终端 → 运行任务 → 做市：启动（实盘）"。脚本会依次：
   - 做只读账户检查；
   - 列出每个币种的规模和止损；
   - 等你输入 `yes` 确认；
   - 为每个币种启动一个 systemd 服务。

| 操作 | 命令（VS Code 任务里也有） |
| --- | --- |
| 查看状态（只读） | `bash deploy/live/grvt-maker/status.sh` |
| 账户检查（只读） | `.venv/bin/python scripts/check_grvt_account.py` |
| 停止 | `bash deploy/live/grvt-maker/stop.sh`：每个进程先撤掉全部挂单再退出，然后关掉实盘开关和开机自启。持仓不会自动平掉，脚本最后会列出来 |
| 紧急停止 | `touch storage/live/KILL`：所有币种一起停，删掉这个文件后才能再启动 |

服务由 systemd 托管（`deploy/live/grvt-maker/chronoshft-grvt-maker@.service`，一个币种一个实例）：

- 关掉 VS Code、断开 SSH、本地电脑关机都不影响，服务器重启后会自动恢复；
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
.venv/bin/python scripts/run_grvt_maker.py --symbol ADAUSDT --minutes 30
```

更多细节见 [deploy/live/grvt-maker/README.md](deploy/live/grvt-maker/README.md)。**不要在中国大陆运行实盘。**

## 研究工具

```bash
# 录制币安：只录最优价、一个币种一个连接
.venv/bin/python scripts/record_public_book.py --symbols ADAUSDT,PUMPUSDT --hours 48 --out storage/research4/binance \
    --ticker-only --connection-per-symbol --ticker-price-changes-only
# 录制 GRVT：盘口和成交
.venv/bin/python scripts/record_grvt_book.py --symbols ADAUSDT,PUMPUSDT --hours 48 --out storage/research4/grvt

# 跨交易所回放：修正后的排队模型（默认）、穿过不成交、真实费率、实测延迟
.venv/bin/python scripts/replay_cross_venue.py --grvt-dir storage/research4/grvt --binance-dir storage/research4/binance \
    --symbols ADAUSDT --start 2026-10-21T12:30 --end 2026-10-22T12:30 --slice-hours 6 --notional 250 \
    --anchor binance --half-spread-bps 2 --improve --skew-bps-per-lot 2.5 --max-inventory-lots 1 \
    --cross skip --latency-ms 8 --maker-fee-bps -0.01
```

| 回放参数 | 作用 |
| --- | --- |
| `--queue-depletion visible`（默认）/ `trades` | 成交按显示挂单的实际减少量推进排队 / 旧算法：按成交量推进 |
| `--cross skip` / `fill` | 对手盘越过我们的价格时，不成交（实盘证实）/ 成交 |
| `--keep-ticks`、`--min-rest-sec` | 落后多少档、挂满多久才追价 |
| `--adverse-bps` | 价格小幅不利时先不撤单的容忍度 |
| `--latency-samples`、`--extra-latency-ms` | 下单延迟：从实测样本里抽，或者额外加压 |

- **录制中的文件不要直接读。** 先在 HDF5 读锁下拷一份再分析，录制程序在被锁时会推迟写入。
- **录制、测延迟或跑实盘时，不要在同一台机器上跑重分析。** 服务器只有 2 核，回放占满 CPU 会拖慢延迟测试和录制的时间戳。
- **内存约 4 GB。** 分析脚本用 `choom -n 1000` 运行，并按 `--slice-hours` 分段读取。

## 纸面交易框架

```bash
.venv/bin/python main.py --check-config
.venv/bin/python main.py                          # 币安纸面盘，读取 config.json
.venv/bin/python main.py --config config.grvt.json
```

- 模拟账户 10,000 USDT，不需要 API 密钥。默认策略是 GLFT 被动做市。
- 停止时只按一次 `Ctrl+C`，程序会撤单、平仓、确认空仓后再退出。
- 看板只监听 `127.0.0.1:8765`，通过 SSH 隧道访问：`ssh -N -L 8765:127.0.0.1:8765 ubuntu@EC2_PUBLIC_IP`。
- 作为系统服务运行：`sudo bash scripts/install_systemd_service.sh --start`（详见 [docs/operations.md](docs/operations.md)）。
- 改标的、规模、手续费、策略模型，见 [docs/configuration.md](docs/configuration.md)。查询成交：`.venv/bin/python scripts/query_paper_trades.py --summary`。

## 数据放在哪里

| 路径 | 内容 |
| --- | --- |
| `storage/research4/` | 第二次实盘期间的录制：ADA、PUMP 的币安最优价和 GRVT 盘口、成交 |
| `storage/live/grvt_maker/` | 做市事件日志：下单、撤单、成交、确认耗时、状态，一个币种一天一个文件 |
| `storage/live/KILL` | 紧急停止文件（存在时所有做市进程停下） |
| `storage/paper/` | 纸面交易数据库和 OMS 审计日志 |
| `config/local/` | 本地配置、密钥文件和服务的模式开关（`grvt_maker.env`），都不进 git |

前两轮录制（research2：72 小时、15 个币；research3：11 小时、16 个新币）已经删除，结论保留在上文。

## 代码结构

| 目录 | 内容 |
| --- | --- |
| `strategy/grvt_cross_maker.py` | 报价规则（公允价、挂单价、库存偏移、换单规则），**回放和实盘共用** |
| `strategy/grvt_maker_engine.py` | 实盘引擎：订单状态、持仓、盈亏、风控、连接存活判断 |
| `gateway/grvt/` | GRVT：订单签名、REST 下单、私有推送、盘口重建，以及纸面网关 |
| `gateway/binance/` | 币安：bookTicker 行情（`/public` 路由）、公开行情、纸面撮合 |
| `scripts/` | 做市运行程序和账户检查、录制、回放、因子检验、延迟测量、纸面工具 |
| `deploy/live/grvt-maker/` | 做市的配置模板、systemd 服务模板、启动 / 停止 / 状态脚本 |
| `oms/`、`risk/`、`alpha/`、`infrastructure/`、`ui/`、`web/` | 纸面交易框架 |

## 开发与测试

```bash
.tools/uv sync --frozen --dev
.venv/bin/python -m pytest -q
```

- 依赖只在 `pyproject.toml` 里声明，`uv.lock` 锁定完整依赖。
- `tests/` 在 `.gitignore` 里是**白名单**：新建测试文件后，要在 `.gitignore` 里加一行 `!tests/<文件名>`。
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
- [x] 实盘程序：按币种配置、连接心跳、systemd 服务、一键启停
- [x] 第一次实盘（XRP + ADA）：找出回放高估成交的原因（隐藏挂单），修正排队模型
- [x] 实盘证实"被穿过不会成交"；测试少换单的规则（没有改进）
- [ ] **第二次实盘（ADA + PUMP，48 小时）**，同时录制盘口
- [ ] 用第二次实盘的成交和录制的盘口，逐笔校准回放；查清 XRP 剩下的约 5 倍偏差
- [ ] 校准后决定：继续优化 GRVT 做市，还是换到能保证排在队首的场景（价差多档宽的品种、做市商优先权）
- [ ] 申请 GRVT 做市商返佣档
