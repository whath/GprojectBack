# 个人收盘数据 API · v0.6.4

提供全 A 股近 **52 周日线**及兼容的近 **30 个本地市场交易日**查询，服务于个人手机和 Windows 客户端。正式 HTTPS：`https://ericcself.top`；数据接口使用 Bearer Token，`/healthz` 仅表示存活，`/readyz` 表示服务就绪。

2026-10-16 已上线中美重要会议与数据公布日程、经济日历高重要性过滤、独立自选收盘报价；客户端增加价格轴、成交量及 MA/MACD/KDJ/RSI/BOLL 和所选日形态解释。后端204项回归、客户端105项测试（含正式HTTPS、手机/桌面渲染）通过；公网21项及9次并发请求通过，荣耀ALI-AN00真机已验收。当前新增契约见 [v0.6.4](docs/CALENDAR-STUDIES-v0.6.4.md)，验收和来源边界见 [交付记录](docs/CALENDAR-STUDIES-ACCEPTANCE-20261016.md)；原榜单/22方向/资讯/分时见 [v0.6.3](docs/SEVEN-DAY-v0.6.3.md)。客户端：[GprojectApp](https://github.com/whath/GprojectApp)。

**定时采集、自动补采、巡检和电脑自动备份继续关闭。** 本轮仅执行有限手工采集和一次一致性备份。已有历史存档保留，API 不再排队扩大历史范围；证书自动续期保持启用。

## 当前业务范围

- A 股/境内 ETF：日线、近 7 个交易日涨幅榜、龙虎榜及席位；A 股支持收盘分时快照与 52 周日线，原 `period=5d` 保持兼容。
- 22 个指定方向：行业、概念分开；20 个东方财富方向提供行情、资金和当前成分；同花顺独立提供 AIDC、算力租赁行情，缺少同口径资金时不显示资金。稳定币暂不展示。
- 美股：11 个标普行业 ETF 代理、12 个 AI 配置标的及 SPY，共 24 个序列；行业代理不等于全美细分行业。
- 宏观：新浪 XAU 美元日度参考、EIA 布伦特现货、美财政部 2/10/30 年期平价收益率。
- 中美重要会议和数据公布日程：国家统计局、BEA、美联储、已核验中国会议公告；BLS直连受限，有限窗口用明确标注的高重要性经济日历补充，不保证穷尽。经济日历仅默认保留来源评级3—4级。
- 自选独立查询已存收盘价、实际日期、币种及日涨跌，不依赖涨幅榜；GET不触发采集。
- 华尔街见闻经济日历；中央/国家部委政策；美国财政部、美联储；高盛、摩根士丹利公开投研；英伟达官方新闻及博客，均限制原发布方与发布日期。
- 已移除连板接口和采集选项；客户端已去掉连板、信号雷达、模型分析及无界历史扩容入口。

最近行情日期为 CN/US **2026-10-15**，成分为 **2026-10-16 当下观测**。A 股目录 5577 只，52 周实际有日线 5576 只、1320797 条；7 日榜合格 5549，只展示前 50，28 个自然排除项（停牌22、上市不足8日5、目标日未上市1），未解释缺日为 0。境内 ETF 已遍历 1702 只，1684 只具备 8 个对齐收盘价，14 只上市时间不足，**4 只窗口仍未核实**，已排除且客户端提示“已验证样本榜”；不能称为无缺口全 ETF 榜。22 个方向行情全部可用，20 个方向有 7 日资金；A 股 5565 只收盘分时已与日线核对，另 12 只停牌或尚未上市。未自动采集 10 月 16 日行情，客户端明确显示快照日期。

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

`config/universe.json` 是开发模板，生产使用单独配置；全目录开关不代表实际已采全。`history_sessions=30` 保留原默认窗口；全 A 股 52 周有限采集使用 `scripts/collect_all_a.py --weeks 52`，查询使用 `weeks=52`；`--refresh` 会重新验证整个 30 日窗口。采集当前 CN 交易日需在北京时间 15:10 后，已结束的历史交易日允许全天手工采集。

```powershell
# 先按所需股票/ETF和 BK 编码编辑配置；所有日期必须是已经结束的市场交易日。
./.venv/Scripts/python.exe -m marketdata collect --market CN --date 2026-10-15 --refresh --raw-dir data/raw-cn
./.venv/Scripts/python.exe -m marketdata collect --market US --date 2026-10-15 --refresh --raw-dir data/raw-us
# 前复权抓取单次完整快照，用于近 7 日榜；不会与不复权图表混用。
./.venv/Scripts/python.exe -m marketdata collect-context --feature gainers --scope available --max-items 120 --date 2026-10-15 --raw-dir data/raw-gainers
./.venv/Scripts/python.exe -m marketdata collect-context --feature boards --max-items 80 --date 2026-10-15 --raw-dir data/raw-boards
./.venv/Scripts/python.exe -m marketdata collect-context --feature macro --feature calendar --feature important-calendar --feature policy --raw-dir data/raw-context
# 分级资讯先刷新国家部委列表，再采集美国原发布方。
./.venv/Scripts/python.exe -m marketdata collect-context --feature priority-news --raw-dir data/raw-priority
```

命令存在互斥、有限请求超时和失败状态；退出码 2 表示部分完成，旧的成功快照会保留。`--max-items` 限制单批标的数；不要把成功的一个批次当作全目录验收。当前 `automatic_collection=false`，启动 `worker` 会明确拒绝；15:10/17:10/19:10 的调度代码保留但不执行。

## 验证与迁移

```powershell
./.venv/Scripts/python.exe -m pytest tests -q
./.venv/Scripts/python.exe -m pip check
./.venv/Scripts/python.exe -m marketdata backup data/manual-backup.sqlite3
```

本轮检查了真实来源 → 标准化 → SQLite → 正式 HTTPS → Compose 客户端；具体计数及异地备份哈希见交付记录。无账号/积分/付费源；一次访问成功不能保证公开源的长期可用性。宏观发布日历和延迟各异，经济日历会修订，政策列表不承诺穷尽所有历史文章。

ETF 全目录、22 个方向、分钟快照的有限采集命令见 [v0.6.3 契约](docs/SEVEN-DAY-v0.6.3.md)。分页计算使用一致的只读事务，发布缓存时才短暂持有写锁，避免并发榜单互相阻塞。

更换电脑或服务器时，请按 [迁移说明](docs/MIGRATION.md) 搬迁数据库、配置和密钥。Git 仅包含代码、测试和无敏感信息的验收记录，不含行情库、私钥或生产令牌。较早交付文档均为历史快照，本版本范围以本页及 v0.6.4 / v0.6.3 契约为准。
