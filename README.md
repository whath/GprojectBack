# 个人收盘数据 API · v0.6.0

提供近 **30 个本地市场交易日**的数据查询，服务于个人手机和 Windows 客户端。正式 HTTPS：`https://ericcself.top`；数据接口使用 Bearer Token，`/healthz` 仅表示存活，`/readyz` 表示服务就绪。

2026-10-16 已完成本轮代码、真实采集、原始响应对账和云端部署。后端 166 项回归通过，当前电脑到正式 HTTPS 的 40 项接口校验通过。当前范围、来源和限制见 [v0.6.0 契约](docs/DAILY-DATA-CONTRACT-v0.6.0.md) 和 [交付记录](docs/REVISION-DELIVERY-20261016.md)。客户端独立仓库：[GprojectApp](https://github.com/whath/GprojectApp)。

**定时采集、自动补采、巡检和电脑自动备份继续关闭。** 本轮仅执行有限手工采集和一次一致性备份。已有历史存档保留，API 不再排队扩大历史范围；证书自动续期保持启用。

## 当前业务范围

- A 股/境内 ETF：日线、近 5 个交易日涨幅榜、龙虎榜及席位；已采集标的可查询资金和估值。
- 东方财富行业/概念：独立 `BK` 编码、近 30 日指数行情与主力净流量、完整当前成分观测。资金口径为来源定义的大单加超大单净额，非全部现金流。
- 美股：11 个标普行业 ETF 代理、12 个 AI 配置标的及 SPY，共 24 个序列；行业代理不等于全美细分行业。
- 宏观：新浪 XAU 美元日度参考、EIA 布伦特现货、美财政部 2/10/30 年期平价收益率。
- 华尔街见闻经济日历、国务院/人民银行/证监会/发改委/商务部的官方政策与发布列表。
- 已移除连板接口和采集选项；客户端已去掉连板、信号雷达、模型分析及长历史入口。

最近完整行情日期为 CN/US **2026-10-15**，板块成分为 **2026-10-16 当下观测**。5 日榜实际覆盖：104/5576 股票、2/1702 ETF、77/496 行业、2/508 概念，**并非全市场完成**。东方财富目录含来源自有层级，不与原同花顺 90 行业/15 概念作等价映射。

## 本地启动

需要 Python 3.11+；依赖固定在 `requirements.txt`，开发测试见 `requirements-dev.txt`。

```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
$env:API_TOKEN = & ./.venv/Scripts/python.exe -c 'import secrets; print(secrets.token_urlsafe(32))'
./.venv/Scripts/python.exe -m marketdata init
./.venv/Scripts/python.exe -m uvicorn marketdata.api:app --host 127.0.0.1 --port 8000 --workers 1
```

令牌至少 32 字符，保存在环境或受保护配置中，客户端需输入相同令牌。本地 `/docs` 和 `/openapi.json` 可访问；生产公网这两个路径返回 404，契约见仓库 [OpenAPI](docs/openapi.json)。API 仅查已落库数据，不在请求中采集。

## 有限手工采集

`config/universe.json` 是开发模板，生产使用单独配置；全目录开关不代表实际已采全。`history_sessions=30` 限定采集/查询窗口；`--refresh` 会重新验证整个 30 日窗口。采集当前 CN 交易日需在北京时间 15:10 后，已结束的历史交易日允许全天手工采集。

```powershell
# 先按所需股票/ETF和 BK 编码编辑配置；所有日期必须是已经结束的市场交易日。
./.venv/Scripts/python.exe -m marketdata collect --market CN --date 2026-10-15 --refresh --raw-dir data/raw-cn
./.venv/Scripts/python.exe -m marketdata collect --market US --date 2026-10-15 --refresh --raw-dir data/raw-us
# 前复权抓取单次完整快照，用于近 5 日榜；不会与不复权图表混用。
./.venv/Scripts/python.exe -m marketdata collect-context --feature gainers --scope available --max-items 120 --date 2026-10-15 --raw-dir data/raw-gainers
./.venv/Scripts/python.exe -m marketdata collect-context --feature boards --max-items 80 --date 2026-10-15 --raw-dir data/raw-boards
./.venv/Scripts/python.exe -m marketdata collect-context --feature macro --feature calendar --feature policy --raw-dir data/raw-context
```

命令存在互斥、有限请求超时和失败状态；退出码 2 表示部分完成，旧的成功快照会保留。`--max-items` 限制单批标的数；不要把成功的一个批次当作全目录验收。当前 `automatic_collection=false`，启动 `worker` 会明确拒绝；15:10/17:10/19:10 的调度代码保留但不执行。

## 验证与迁移

```powershell
./.venv/Scripts/python.exe -m pytest tests -q
./.venv/Scripts/python.exe -m pip check
./.venv/Scripts/python.exe -m marketdata backup data/manual-backup.sqlite3
```

本轮检查了真实来源 → 标准化 → SQLite → 正式 HTTPS → Compose 客户端；具体计数及异地备份哈希见交付记录。无账号/积分/付费源；一次访问成功不能保证公开源的长期可用性。宏观发布日历和延迟各异，经济日历会修订，政策列表不承诺穷尽所有历史文章。

更换电脑或服务器时，请按 [迁移说明](docs/MIGRATION.md) 搬迁数据库、配置和密钥。Git 仅包含代码、测试和无敏感信息的验收记录，不含行情库、私钥或生产令牌。较早交付文档均为历史快照，本版本范围以本页和 v0.6.0 契约为准。
