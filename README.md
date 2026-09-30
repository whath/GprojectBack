# 个人收盘数据 API · v0.3

源码仓库：[whath/GprojectBack](https://github.com/whath/GprojectBack)。更换开发电脑或云服务器前，请按 [迁移说明](docs/MIGRATION.md) 迁移配置、数据和调度；GitHub 不包含令牌、私钥及数据库。

最新扩容与验收说明：[全目录收盘快照、历史补采和异地备份](docs/BACKEND-EXPANSION-20260930.md)。云端使用独立批量快照与空闲时历史队列扩容；不要直接将数千只历史请求塞入单次定时窗口。全量历史仍在初始化，同花顺完整成分来源仍未解决。

范围：采集 → 标准化 → SQLite → 查询 API。没有前端，没有自动交易，没有消息推送。

## 已实现

- A 股行业/概念板块、股票、境内 ETF 的不复权日线；板块当前成分关系快照。
- 龙虎榜总表、机构买卖、个股买卖席位；不同原因独立保存，不合并单日和多日榜金额。
- 美国 11 个标普行业 ETF 代理排名、可编辑 AI 观察池与基准 SPY。
- 日线、排名、龙虎榜、席位、板块成分、任务状态查询接口，Bearer Token 鉴权。
- CN 在交易日北京时间 15:10、17:10、19:10 各采集一轮，15:10 前不允许请求。每轮重新校验已成功日线，并补齐龙虎榜；休市日不自动采集。US 使用 XNYS 日历常规收盘后 1 小时和 3 小时。
- 每个接口在隔离子进程中调用，整体超时、有限重试、采集互斥、任务断点恢复。
- SQLite WAL、短事务、30 秒锁等待、在线一致性备份。

## 首次运行（Windows PowerShell）

```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
$env:API_TOKEN = & ./.venv/Scripts/python.exe -c 'import secrets; print(secrets.token_urlsafe(32))'
./.venv/Scripts/python.exe -m marketdata init
./.venv/Scripts/python.exe -m uvicorn marketdata.api:app --host 127.0.0.1 --port 8000 --workers 1
```

保持令牌在本机安全配置中，客户端使用相同令牌。示例环境变量只对当前终端生效；不要把令牌提交到代码仓库或放进 URL。

接口说明：`http://127.0.0.1:8000/docs`，点击 Authorize 输入令牌。OpenAPI：`/openapi.json`。这两个页面仅公开接口结构；全部 `/v1/` 数据接口鉴权。`/healthz` 只报告进程存活，不代表上游采集完成。

另开终端运行样本采集：

```powershell
./.venv/Scripts/python.exe -m marketdata collect --market US --sample
# 以下命令只能在北京时间 15:10 起运行。
./.venv/Scripts/python.exe -m marketdata collect --market CN --sample
# 通过后采集配置中的完整范围；可以 --date YYYY-MM-DD 回补已结束交易日。
./.venv/Scripts/python.exe -m marketdata collect --market CN
./.venv/Scripts/python.exe -m marketdata collect --market US
# 重跑默认跳过已经成功的历史任务；需重新核对成功数据时使用 --refresh。
./.venv/Scripts/python.exe -m marketdata collect --market US --refresh
./.venv/Scripts/python.exe -m marketdata worker
```

不要同时启动多个 worker。采集进程互斥；worker 负责唯一调度，API 不在请求中抓取上游。退出码 0 表示该运行范围的任务完整，2 表示部分完成。`sample` 完成不代表全市场已完成。

初次全市场历史拉取可能持续数小时，具体取决于上游响应和限流。默认低并发，失败后可按同一日期继续；不要以进程退出或某个样本成功代替覆盖验收。初始化建议先将 `cn_all_stocks/cn_all_etfs/cn_all_boards` 设为 false，验证配置池后再扩大。

## 配置与口径

编辑 `config/universe.json`，更改后重启 worker。API 每次读取最新配置。

- 默认配置目标为全 A 股、ETF 和板块；`--sample` 使用配置中的少量 CN 样本及 2 个行业 ETF、1 个 AI 标的、SPY。
- 国内默认 `cn_sources={"stock":"tencent","etf":"sina","boards":"ths"}`：独立 A 股代码目录 + 腾讯沪深个股日线；北交所代码转入新浪个股日线；新浪 ETF 目录/日线；同花顺行业/概念目录与指数日线。各类可显式改回 `eastmoney`。这些是实际来源选择，不把不同分类或来源自动拼接到同一序列。
- 同花顺板块 ID 使用 `CN.industry.THS_881121`、`CN.concept.THS_302035` 等独立命名；`group_name=ths`。完整成分列表目前未取得，成分任务记 pending，不保存首页部分数据，也不套用东方财富成分。
- 国内排名默认 `metric=price_change_pct`，按相邻市场交易日不复权收盘价计算；`change_pct` 保留上游原报涨跌幅，缺失为 null。两者口径不同，分红/除权会影响收盘价变化。可显式请求 `metric=change_pct`，以及板块 `classification_source=ths/eastmoney`；来源、分类、日期不同的序列不会混排。
- 腾讯个股日线适配针对锁定的 AKShare 1.19.1 修正 `sz000` 股票的手/股转换；科创板原始值已为股，不再次乘 100。ETF/板块原始成交量继续标记未核验单位。升级 AKShare 时需重验这一适配。
- `us_ai` 是可编辑的初始工程观察池，不是已获用户确认的“核心”名单。
- 美国代码目录优先使用已配置映射，缺失部分采用东方财富定向元数据查询，避免只为查代码下载整个市场的实时行情。美国日线优先使用 AKShare `stock_us_hist`，失败时可通过 `us_history_fallback=sina` 使用 AKShare `stock_us_daily`。每条日线与监控结果标注实际来源。
- 新浪序列首次采集成功后继续使用同一来源；已有东方财富序列时不会自动混入新浪数据，而是保留旧数据并提示需单独重建。新浪接口没有成交额和直接涨跌幅字段时保留空值，监控收益按收盘价计算。
- `us_source_codes` 可以显式配置来源代码。程序不猜测唯一映射；未找到或多义映射记录 pending。元数据查询成功也不代表该 ETF 日线可用。
- `use_system_proxy=false` 仅影响采集子进程的代理环境，不修改 Windows 或服务器系统代理；需要系统代理时设 true。
- `history_days=760` 表示首次取约两年自然日范围；之后重取最近 10 个自然日用于修订。更早的修订需指定日期 `--refresh` 回补。
- 仅存不复权日线；API 明确标注。美国收益比较是价格变化，不是总回报；分红和拆股可能导致误导性跳变，提醒只供复盘线索。
- 行业排名是标普 500 行业 ETF 代理，不是全美细分行业指数。缺失/过期数据不参与排名。
- 量比为当前成交量 / 此前 20 个市场交易日的平均成交量，不含当天；中间缺失交易日时不计算。1/5/20 日收益也按市场交易日对齐。单位在每条日线上暴露；ETF/板块单位未经验证时标记 `source_unit_unverified`，不跨标的直接比较绝对量。
- 板块成分只在目标日期等于采集当地日期时保存，并标注 `observed_current_membership`，历史回补不伪造历史成分关系。
- 龙虎榜金额按元存储，保留单日/多日披露标记。总榜和买卖席位、不同上榜原因可能重叠，禁止直接加总。空数据记 pending，不自动认定当天无榜。
- 龙虎榜先检查东方财富披露服务的响应；只有明确 `code=9201, success=false, result=null` 的空报告记 pending。其他错误继续报 failed，不把任意 TypeError 当作尚未披露。
- 当目标日线缺失时保留已获得的历史数据并记 pending；停牌与上游延迟均需要进一步核验。
- 最新数据日期、采集时间和任务范围分别保留。不要把某个标的最新日期解释为整个市场已完成。

## API

所有数据接口请求头：`Authorization: Bearer <API_TOKEN>`。

| 方法与路径 | 内容 |
|---|---|
| GET `/v1/status` | 最近运行、市场最新已存日线日期 |
| GET `/v1/cn/quotes?kind=stock&limit=500&offset=0` | 全目录收盘快照，含有效行情、整日停牌与未解释缺失 |
| GET `/v1/cn/history-coverage` | 全目录历史初始化和目标日日线覆盖 |
| GET `/v1/health` | 心跳、磁盘、本地及异地备份验证状态 |
| GET `/v1/tasks?market=CN&trade_date=2026-09-29&scope=configured` | 分任务成功/失败/待核验，支持分页 |
| GET `/v1/instruments?market=US&kind=stock` | 已登记标的，支持搜索分页 |
| GET `/v1/bars/US.stock.NVDA?start=2026-01-01` | 不复权日线，最多 2000 条，日期升序 |
| GET `/v1/cn/rankings?kind=industry&trade_date=2026-09-29` | 当日已采集范围排行；同时返回任务覆盖信息 |
| GET `/v1/cn/lhb?trade_date=2026-09-29` | 当日龙虎榜，可按 symbol 筛选 |
| GET `/v1/cn/lhb?trade_date=2026-09-29&institutions=true` | 机构买卖统计 |
| GET `/v1/cn/lhb/000001/seats?trade_date=2026-09-29&direction=买入` | 买卖席位；买卖方向分别查询 |
| GET `/v1/cn/boards/CN.industry.BK1036/members?observed_date=2026-09-30` | 采集日成分快照，代码以实际目录为准 |
| GET `/v1/us/sectors?trade_date=2026-09-29` | 行业 ETF 排名、Top 5、覆盖率 |
| GET `/v1/us/ai?trade_date=2026-09-29` | AI 观察池、量价提示 |

省略日期时查询日历推算的最近可采集交易日，不静默回退为旧日期。数据为空时查 `/v1/tasks`。列表 `limit` 有上限；日线接口返回日期范围内最近的 limit 条，可通过 end 向前翻页。第一版 API 为只读，采集和配置从服务器 CLI 执行。

## 阿里云部署准备

**2026-09-30 更新：** 已在用户提供的服务器完成原生 Python + systemd 部署及 SSH 隧道样本验收，详见 [实际云端部署记录](docs/CLOUD-DEPLOYMENT-20260930.md)。下面的 Docker/Compose 流程保留为另一种部署方式，当前服务器实际未使用 Docker。

服务器：轻量应用服务器，2 核 / 2 GiB / 40 GiB，Alibaba Cloud Linux 4。最初截图仅提供规格；现已使用用户提供的 SSH 密钥完成连接及原生部署。

在已安装 Docker 和 Compose 的服务器上，把项目复制到应用目录。前置检查：系统架构、磁盘、时间同步、源站网络可达；首次镜像构建避免和采集同时进行。

```bash
cp .env.example .env
# 编辑 .env，把 API_TOKEN 替换为随机长令牌
docker compose build
docker compose up -d api
docker compose run --rm worker python -m marketdata collect --market US --sample
# 北京时间 15:10 起执行国内样本；检查 /v1/tasks 后再开启全量 worker
docker compose run --rm worker python -m marketdata collect --market CN --sample
docker compose up -d worker
docker compose logs --tail=100 worker
```

API 默认只映射服务器 `127.0.0.1:8000`，不会直接开放公网明文鉴权接口。通过 SSH 隧道可做个人联调；正式手机直连前，在反向代理配置 HTTPS，再按实际域名完成必要的接入准备。API、worker 共用本地 Docker 数据卷，不放网络文件系统。

Compose 内存限制为 API 384 MiB、worker 1100 MiB，是初始保护阈值，**不是已实测的容量保证**。出现 OOM 应先缩小批量、检查上游响应及日志，再调整资源。日志轮转已配置。数据库、WAL、镜像和备份共同占用 40 GiB 磁盘，需定期监测，禁止无限保留容器与备份。

## 备份与恢复

```bash
docker compose exec worker python -m marketdata backup /data/backup.sqlite3
docker compose cp worker:/data/backup.sqlite3 ./backup.sqlite3
```

该命令使用在线备份 API 并进行完整性检查。将备份另存到服务器之外。恢复时停止 API 和 worker，将现有数据库及对应 WAL/SHM 一起移动到独立归档目录，再以备份替换数据库；确保文件属主可供 UID 10001 读写，然后启动服务。不要用运行中的单一 `.db` 文件复制替代在线备份。

## 日历维护

使用 exchange-calendars 的 XSHG（国内股票市场统一时段）及 XNYS（美国常规股票时段）日历。2026 年国庆休市、周末调休、美股夏冬令时和提前收盘有测试。日历库不覆盖未来年份时会失败，**不会猜测普通工作日开市**。

`calendar_overrides` 可按市场/日期设置 false（休市）或当地收盘时间如 `"13:00"`（额外开市/提前收盘）；仅应根据交易所公告维护。每年核对新日历并升级锁定依赖。

## 验证

```powershell
./.venv/Scripts/python.exe -m pytest -q
./.venv/Scripts/python.exe -m pip check
```

测试使用隔离临时数据库和明确的模拟上游；不会把测试行情写入实际数据目录。真实采集状态记录在本地数据库，可经 API 查看。最新验证结果见 `VALIDATION.md`。

国内替代源真实样本与 API 验收见 [国内修复记录](docs/CN-REPAIR-20260930.md)。目录可取不代表全市场历史已采完；截至该次验收，9 月 29 日样本已通过，9 月 30 日样本日线与披露仍待源站补齐。

第一版尚不提供：复权/总回报序列、交易所直接核验、历史成分重建、自选写 API、盘中行情、新闻归因、外部推送、自动异地备份及云端性能验收。

## 后台运维升级

新增 `/readyz`、`/v1/health`、`/v1/alerts`、`/v1/coverage`、`/v1/overview`；业务和详细运维接口均需 Bearer。`/v1/status` 增加调度窗口、重试次数及下次补采时间。worker 每日自动备份并保留最近 14 份；补采最多 3 次，间隔 20 分钟，单轮请求启动预算 900 秒。

云端范围已扩展为 90 行业、15 重点概念、5 A 股、2 境内 ETF，美股 24 个配置标的；覆盖率只针对该范围。`ericcself.top` 已完成 DNS 和证书部署；因用户确认尚未完成 ICP 备案，公网入口和续期定时器暂时关闭，后台与 SSH 隧道保持运行。具体变更、接口语义、回滚和未完成边界见 [后台修复记录](docs/BACKEND-HARDENING-20260930.md)。
