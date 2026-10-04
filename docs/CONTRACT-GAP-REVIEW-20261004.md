# GprojectBack 后台需求缺口审查

审查日期：2026 年 10 月 4 日，北京时间。代码已克隆到 `D:\workProjects\gupiaoHou`，来源为 [whath/GprojectBack](https://github.com/whath/GprojectBack)，分支 `main`，审查提交 `c729fe47647cd854ffd7ff40b53905d7dd29bf3d`。

依据用户提供的 [后台接口与业务逻辑需求](C:/Users/Administrator/Desktop/GprojectApp_后台接口与业务逻辑_2026-10-04.md)，当前后台具备基础查询和采集框架，但尚不能满足前端全部 P0 需求。主要阻断项是两类资金接口缺失、同花顺同日完整成员不可用、目录与历史覆盖不足，以及若干数据校验和响应状态问题。重点事件接口也未实现，属于 P1。

本次完成源码审查、现有测试和临时数据库复现，没有修改业务实现、运行真实行情采集、连接生产数据库或变更部署。文档中的业务规则作为核对依据；其历史部署指令、自动任务说明和运行限制不作为本次执行授权。历史部署报告只说明 9 月 30 日当时状态。

## 接口与需求对应

| 需求 | 当前代码 | 结论 |
| --- | --- | --- |
| HTTPS 与 Bearer | 全部现有 `/v1` 路由依赖 `authorize`，无效令牌 401；有 nginx TLS 配置 | 鉴权已实现，公网 HTTPS 当前可用性待联调确认 |
| CN 与 US 独立交易日 | `calendars.py` 使用 XSHG/XNYS，处理时区、夏令时及收盘后就绪时间 | 基础已实现；上游逐根日线仍缺交易日校验 |
| `/v1/instruments` | `total`、500 条上限、offset、ID 排序 | 参数兼容；全目录落库、上市退市状态和跨页一致性有缺口 |
| `/v1/bars/{instrument_id}` | end 含当日、最多 2000 根、取最近 N 根后升序、`adjustment=none` | 查询兼容；存量历史长度和完整性不能由 limit 保证 |
| `/v1/cn/rankings` | 默认行业、THS/东方财富分类筛选、同源上一市场交易日比较 | 基本兼容；采集失败时仍可能 200 空结果 |
| `/v1/cn/boards/{id}/members` | 指定 observed_date 读库存，500 条分页、task_status | 路由存在；THS 完整来源明确不可用，历史缺日不能重建 |
| `/v1/cn/lhb` | 独立原因记录、净额元单位、统计单日/多日、任务状态 | 核心已实现；周期起止、明确无披露状态仍待完善和真实验收 |
| `/v1/us/ai` | 配置观察池、各项日期/来源/close/change_1d_pct、stale/missing 状态 | 核心已实现；只覆盖配置池，不代表全美股或模型分析 |
| 板块资金 flows | 无路由和采集适配 | P0 缺失，实测 404 |
| 个股主力资金 flows | 无路由和采集适配 | P0 缺失，实测 404 |
| `/v1/events` | 无路由和事件采集、版本管理 | P1 缺失，实测 404 |
| 日韩和 A 股指数、黄金原油、分钟线、估值、连板梯队 | 未发现对应专用 API 和采集流程 | P2 双端开发范围，不能算作现有前端接口故障 |
| 国内模型分析 | 没有对应模型网关或分析调用 | P3 后续范围，`/v1/us/ai` 是行情观察池 |

## P0 缺口与代码证据

### 两类真实资金数据没有实现

需求第 4.1、4.2 节要求板块资金与个股主力资金。现有 [API 路由](D:/workProjects/gupiaoHou/marketdata/api.py:208) 只提供板块成员等查询，[上游白名单](D:/workProjects/gupiaoHou/marketdata/provider.py:12) 没有对应资金接口，也没有资金校验、日历窗口及独立采集任务。

隔离 HTTP 复现中，下列已授权请求均返回 404：

- `/v1/cn/boards/CN.industry.THS_881121/flows?limit=20&classification_source=ths`
- `/v1/cn/stocks/CN.stock.000001/flows?end=2026-09-30&limit=2000`

建议先建立真实供应商适配、独立持久化和失败状态，再实现查询契约。板块 `trade_dates` 必须来自市场日历，保留缺采集日期；两类资金须校验 CNY/yuan、main/all、来源一致性、日期唯一性和净额容差。个股仅接受 CN 股票和 main 口径。2000 条上限与 end 的包含性需要实际验收，成交额不能代替资金流。

### 同花顺完整同日成员被明确标记为不可用

[成员采集逻辑](D:/workProjects/gupiaoHou/marketdata/pipeline.py:241) 仅在目标日等于当前 CN 日期时采集，THS 分支直接抛出 `PendingData("THS membership unavailable...")`。[状态分类](D:/workProjects/gupiaoHou/marketdata/operations.py:17) 将其标为 `unsupported_membership`，不允许自动重试。

这一实现正确避免了拿部分成分或东方财富分类冒充完整同花顺名单，但前端所需的完整反向行业归属索引无法建立。指定历史日期只会读取当日保存的记录，没有归档则不可用；返回 `trade_date=observed_date` 本身不证明已采集。

建议接入可核验的完整 THS 成分来源或原始导出，按观察日期保存版本、总数和完整性证据，完成去重和全页校验后才发布 complete。已有成员数据也会被 `put_dataset` 替换，因此应防止同一观察日期在分页期间切换版本。正好 500 条时继续请求空页的 SQL 行为已支持。

### 历史长度不足且扩大配置不能自动补齐旧历史

[默认配置](D:/workProjects/gupiaoHou/config/universe.json:2) 的 `history_days=760` 是自然日范围，通常无法初始化到 2000 根日线；[生产示例](D:/workProjects/gupiaoHou/config/production.example.json:2) 为 60 个自然日，无法支撑 250 根筛选和 125/130 根技术判断窗口。上市较晚或停牌股票允许真实短历史，但老股票的采集不足需要单独识别。

[历史起点逻辑](D:/workProjects/gupiaoHou/marketdata/pipeline.py:112) 只查询最大已存日期，已有数据时将起点收紧到 `max(目标日-history_days, 最新已存日-10天)`。因此，即使把 history_days 从 60 改成 4000，已有最新短历史仍只请求近期修订。

复现：库存只有 2026-09-29 一根，将 history_days 改成 4000，目标日 2026-09-30，实际发给供应商的 start_date 仍为 `20260919`。

[补采队列](D:/workProjects/gupiaoHou/marketdata/backfill.py:35) 对已有目标日线或 complete 任务跳过处理，不检查最早日期、250/2000 根长度或内部缺口。[覆盖报告](D:/workProjects/gupiaoHou/marketdata/backfill.py:59) 明确只证明目标日行情或停牌，不证明历史连续性。

建议把近期修订、历史向前扩展和内部缺日回补分成独立任务。新增按股票的最早日期、有效根数、缺口、停牌和上市日期审计，优先满足老股票 250 根扫描，再覆盖至多 2000 根详情。完整性结论必须说明核对范围。

### 目录落库依赖日线成功并缺少上市退市状态

目录上游成功后，[CN 遍历](D:/workProjects/gupiaoHou/marketdata/pipeline.py:189) 保存期望 ID 到 universe_snapshots，但没有独立将整份目录写入 instruments。[实际落库](D:/workProjects/gupiaoHou/marketdata/pipeline.py:154) 位于 history 中、上游日线请求之后。因此新标的日线请求失败或任务预算耗尽时，它可能不出现在 `/v1/instruments` 中，前端连请求该股票的机会都没有。

[完整收盘快照](D:/workProjects/gupiaoHou/marketdata/quotes.py:76) 独立存 datasets，同样不能直接保证 instruments 目录完整。[目录表结构](D:/workProjects/gupiaoHou/marketdata/db.py:35) 没有上市日、退市日或有效状态；目录更新也不会将上游消失标的标为退市。因此接口的 total 是已落库行数，并非经过全交易所核验的全部上市股票数。

建议独立、原子发布经校验的全目录，不让日线失败影响标的登记；记录上市退市状态，保留退市历史。校验六位代码、ID 对应、重复代码和交易所覆盖。已有 coverage 接口可复用，但应与实际对外目录做差异核对。

### 跨页目录没有稳定快照

[目录 SQL](D:/workProjects/gupiaoHou/marketdata/api.py:123) 使用实时表上的 count 和 `ORDER BY id LIMIT/OFFSET`。ID 排序稳定，但采集期间新增 ID 会改变 total 及页边界。读取操作也没有显式开启覆盖多页的数据库快照。

隔离复现：第一页面取得 total=2、ID=`CN.stock.000001`，期间增加排序更靠前的标的；第二页 offset=1 得到 total=3，并再次返回 `CN.stock.000001`。前端会拒绝将这次扫描标记为完整。

建议在保持现有 limit/offset 请求兼容的前提下发布固定目录版本，并明确刷新或扫描期间的版本策略。只使用原子切表可以避免单次响应撕裂，但仍需处理跨页版本切换。板块成员按日期重新替换时也有相同风险。

### 采集失败仍可能以成功空列表响应

[日线查询](D:/workProjects/gupiaoHou/marketdata/api.py:139) 只检查 instrument 是否存在，不读取任务状态。隔离库中将对应历史任务设置为 failed 且不存日线，响应仍为 HTTP 200、`items=[]`，没有失败原因或完整性说明。

[行业排行](D:/workProjects/gupiaoHou/marketdata/api.py:153) 和 [披露查询](D:/workProjects/gupiaoHou/marketdata/api.py:185) 会附带任务覆盖或任务状态，但也没有根据已知采集失败调整 HTTP 状态。披露的 `empty_means` 提示是已有保护；它不能单独满足需求中“不能用 200 空列表掩盖采集失败”的约束。

建议区分真实无数据、上市历史不足、停牌、等待披露和明确失败。明确上游失败时返回适当非 2xx；已验证历史快照仍可按契约返回，并标明日期及采集状态。不要把所有空列表机械地改为错误，也不要仅增加 200 中的业务错误码。

### 金额和成交量单位的验证不完整

[日线标准化](D:/workProjects/gupiaoHou/marketdata/normalize.py:23) 已校验正有限 OHLC、上下界、重复日期和负成交量，非有限数转 null。但成交额只经过 number 转换，没有非负检查；volume_unit 从输入直接保留，没有检查同一序列单位一致。

隔离复现中，amount=-100 成功标准化、入库，并由日线 API 返回；连续两天分别为 share 与 lot_100_shares 也通过标准化。THS/ETF 标记 `source_unit_unverified` 是诚实表达尚未核验，但这类数据不能满足依赖完整成交量单位的技术筛选要求。

建议拒绝负成交额，在同一系列内固定成交量单位或经验证转换，并将单位未知的数据明确排除出量比计算。正常采集流程已有来源固定检查，应保留该保护；本次没有将不同供应商间受控回退认定为模拟行情。

## P1 问题与后续完善

### 重点事件接口及采集链路缺失

需求第 4.3 节的 `/v1/events?limit=100` 实测 404，没有事件发布、市场关联、去重和版本管理。

建议实现 US/JP/KR/OIL/GOLD 多市场关联、stable id、修订 updated_at、带时区时间、发布时间与过期过滤、high/normal 排序及 HTTPS 原文来源。feed 的更新时间必须来自实际核验，不能以接口响应时间掩盖陈旧内容。事件获取失败与真实空列表要区分。

### 日线未逐根核验市场交易日且默认查询缺少未来截止

[日期标准化](D:/workProjects/gupiaoHou/marketdata/normalize.py:18) 只检查日期格式，[日线循环](D:/workProjects/gupiaoHou/marketdata/normalize.py:27) 仅排除晚于目标日的数据，不调用市场交易日历。隔离复现中，CN 的 2026-09-27 星期日数据被接受。这是对异常上游数据的校验缺口，不能据此断言生产已有周末假行情。

[未传 end 的查询](D:/workProjects/gupiaoHou/marketdata/api.py:148) 使用 date.max。正常采集会过滤晚于目标日的数据，但导入、旧库或其他写入入口产生错误未来行时，API 没有第二道保护。注入 2099-01-05 的临时记录后，省略 end 的响应返回了该记录；该复现只证明防御缺口。

建议逐根核验对应交易所交易日，并对省略 end 使用对应市场合理最新历史截止日。历史收益计算已有“上一市场交易日不存在则不找更早日”的保护，应继续保留。

### 龙虎榜核心可用但披露完整性仍需真实验收

[龙虎榜标准化](D:/workProjects/gupiaoHou/marketdata/normalize.py:49) 按股票、日期、原因保存独立 key，区分 single_day/multi_day，保留净买额和来源，符合不跨原因合并的核心要求。[发布时间探测](D:/workProjects/gupiaoHou/marketdata/provider.py:71) 将明确空上游报告标记为 pending，避免虚报完整。

仍缺明确统计区间起止、披露发布时间和供应商披露 ID；当前 key 是自行散列，不是上游披露 ID。需求将其中部分字段列为建议，因此这属于完善项，不等同核心路由完全缺失。真实无披露状态与尚未发布目前也无法最终区分。

后台已有 offset 分页及席位接口，超过 50 条和席位展示的主要缺口在前端，不能重复认定后台没有分页。联调仍需核对金额单位、多原因/周期和席位归属。

### 公司行动与数据版本只具备局部保护

来源固定和美国来源重建有实现；[数据库日线主键](D:/workProjects/gupiaoHou/marketdata/db.py:45) 目前是 `(instrument_id, trade_date, adjustment)`，没有需求所列的 source。普通 `put_bars` 也会覆盖 source，来源一致性主要由 Collector 保证。

[近期修订](D:/workProjects/gupiaoHou/marketdata/pipeline.py:116) 默认覆盖最近 10 个自然日，但没有通用公司行动采集或历史修订版本记录。[来源重建](D:/workProjects/gupiaoHou/marketdata/rebuild.py:12) 限于美国新浪，虽然存档旧序列，但不能代表全部市场的历史版本机制。

建议明确采用“一标的一条固定来源序列”还是多来源存储，并在数据库写入边界贯彻约束；增加供应商适配版本、分类版本、公司行动和历史修订审计。无需为了字段列表机械地改主键，关键是不能发生未经验证的来源或口径覆盖。

## 部署与性能待验证

TLS、限流和数据库索引并非完全没有实现。[nginx 配置](D:/workProjects/gupiaoHou/deploy/ericcself.top.nginx.conf:2) 提供 TLS、10r/s 限流和 429；API 在请求内读取 SQLite，不同步抓取上游；数据库已有 WAL、主键及索引，任务也记录状态、attempts 和失败原因。

[9 月 30 日扩容记录](D:/workProjects/gupiaoHou/docs/BACKEND-EXPANSION-20260930.md:32) 表示公网 nginx 和续期仍停用、等待 ICP 备案。这是历史阻断记录，本次没有验证当前证书、备案或公网可达性，不能直接宣布 10 月 4 日仍停用或已经恢复。需要手机与 PC 访问同一 HTTPS 地址，验证证书、令牌、非 2xx、超时和恢复。

[API 中间件](D:/workProjects/gupiaoHou/marketdata/api.py:46) 对响应设 no-store，未发现目录、同日成分和历史查询的应用缓存。[SQLite 锁等待](D:/workProjects/gupiaoHou/marketdata/db.py:16) 为 30 秒，nginx proxy_read_timeout 为 30 秒，而客户端请求超时为 20 秒，存在锁竞争时客户端先超时的风险。此风险尚未通过并发负载复现，不作为已测出的性能故障。

建议按前端每批 4 个历史请求、全目录分页及成员遍历的真实负载测延迟与锁等待，再决定缓存和超时配置。仍保持现有同步 GET 协议，不能要求前端改用异步任务轮询才能获得数据。

## 已有能力与范围边界

已有能力包括 Bearer 401、参数上限、日线升序与 end、CN/US 日历、同源不复权保护、排行目标日不静默回退、龙虎榜原因独立保存、美国观察池的 stale/missing 状态、采集隔离子进程及超时重试、独立任务失败、在线备份和来源重建。当前没有发现真实行情失败回退模拟行情的实现。

MA、MACD、RSI、周月聚合、观察池、已读、置顶、后台关闭后推送和账户同步不属于当前后台必须补齐的范围。P2 的指数、商品、分钟线、估值、连板梯队和批量归属，以及 P3 模型分析，应分别规划双端开发，不能承诺仅新增后台接口即可激活当前 UI。

## 本次验证与证据

- 环境：Windows，Python 3.14.5，仓库固定直接依赖安装于本地 `.venv`；间接依赖未由原仓库完全锁定，本次环境与历史云端 Python 3.11 不同。
- 现有测试：`.\.venv\Scripts\python.exe -m pytest -q`，结果 **65 passed，1 条 Starlette TestClient 弃用警告**。原有测试文件未改动。
- 依赖：`.\.venv\Scripts\python.exe -m pip check`，结果无冲突。
- 缺口复现：`.\.venv\Scripts\python.exe -m scripts.audit_contract_gaps`，成功运行，仅使用临时数据库和模拟供应商，没有网络行情调用。
- [复现脚本](D:/workProjects/gupiaoHou/scripts/audit_contract_gaps.py) 与 [JSON 证据](D:/workProjects/gupiaoHou/docs/CONTRACT-GAP-EVIDENCE-20261004.json) 已保存。脚本断言的是当前缺口，修复后需要调整对应断言，不能当作未来行为验收测试直接沿用。

JSON 还记录了 instrument 更新仅修改名称、来源代码和分组，不修正 currency 的行为；正常 CN=CNY、US=USD 创建路径正确。该复现是旧元数据修正的边界情况，尚无证据表明生产美股实际返回错误币种，不列作已确认主阻断项。

本次测试通过只说明当前测试覆盖内的行为，不能替代供应商真实数据、授权、完整覆盖或手机 PC 公网联调。

## 建议交付顺序

1. 确认 HTTPS 当前可用性；独立发布完整目录，补齐分页一致性和失败状态语义。
2. 补齐老股票至少 250 根历史和历史缺口审计，支持按需向前扩展至 2000 根；同步修复金额、单位和交易日校验。
3. 获取完整 THS 同日成员来源并保存可核验快照，同时接入两类真实资金采集及接口。这些工作可独立推进。
4. 接入重点事件；完成龙虎榜、美国观察池真实口径验收与并发负载验证。
5. 按需求优先级另行推进 P2 双端功能及 P3 模型分析。

每阶段以需求第 8 节的相关验收项和真实数据证据收口。已存在的 coverage 与任务接口继续作为审计工具，不将“有一根最新日线”“样本 complete”或“现有测试全通过”作为全市场交付完成的依据。
