# v0.6.3：7 日榜、指定方向、分级资讯与分时

## 确定的业务口径

- 仅供个人复盘，继续使用免费公开源；定时采集、补采、巡检、电脑自动备份保持关闭。
- 近 7 个交易日涨幅使用 **8 个同日历收盘价**。股票及 ETF 使用单次抓取的同源前复权收盘快照计算，列表价格与日线展示不复权行情。旧 `period=5d` 保留兼容。
- 个股展示前 50；境内全 ETF 目录中满足完整窗口的标的展示前 10，非固定十只 ETF 池。缺日不插值，不用净值代替成交价，不把停牌日补成一根 K 线。
- 行业/概念只展示 `marketdata/focus_boards.py` 中的 22 个指定方向，沿用各发布方独立分类。证券按用户“证卷”统一名称；军工对应东方财富“国防军工”，医药对应“医药生物”。稳定币经用户确认暂不展示。
- AIDC＝同花顺 `THS_308642`（发布名称“数据中心(AIDC)”）；算力租赁＝`THS_309068`。数据中心另外保留东方财富 `BK0922`，不能将两者视作同一指数。
- 20 个东方财富方向的资金为大单、超大单主力净额，人民币元，合计 7 个交易日；两个同花顺方向没有同口径已验证资金，不显示资金值和跨来源成分。

## 新版接口

全部 `/v1/` 接口要求 Bearer Token，GET 只读取数据库，不向行情网站发起采集。

| 用途 | 请求示例 |
|---|---|
| 个股前 50 | `/v1/cn/rankings?kind=stock&period=7d&top=50&limit=50&trade_date=2026-10-15` |
| ETF 前 10 | `/v1/cn/rankings?kind=etf&period=7d&top=10&limit=10&trade_date=2026-10-15` |
| 22 个方向 | `/v1/focus-boards?trade_date=2026-10-15` |
| 52 周日线 | `/v1/bars/CN.stock.000001?weeks=52&end=2026-10-15` |
| 收盘分时快照 | `/v1/intraday/CN.stock.000001?trade_date=2026-10-15` |
| 级别资讯 | `/v1/priority-news?region=US&level=research&limit=500` |

涨幅响应保留全目录 `expected_count`、有效样本 `eligible_count`、自然排除原因、`missing_data_count`；`top` 仅限制展示数量，不改变覆盖统计。分页快照包含周期与 top，不能混用 5 日和 7 日版本。`required_sessions` 逐项列出 8 个交易日。

资讯支持 `region=CN/US` 和 `level=central/ministry/government/central_bank/research/company`。日期范围最多 90 个自然日，默认沿用近 30 个中国交易日窗口；记录包含原文、发布方、日期精度，未给出精确时间时不推断小时分钟。

## 来源和边界

| 内容 | 来源及校验 |
|---|---|
| 全 ETF 目录 | 新浪 ETF 目录；上市日期与上交所 ETF 官网、深交所基金列表交叉核对 |
| ETF 行情 | 腾讯公开日线及 qfq；原有两条新浪日线保持原来源，不混接原始日线 |
| 指定板块 | 东方财富独立 BK 行业/概念指数、资金、当前成分；同花顺目录 → 网页 clid → 年度及 recent 数据交叉核对 |
| A 股分时 | 腾讯最近五交易日公开分钟接口；只保存本轮明确指定的已收盘日，末点 15:00 与当日日线收盘交叉核对 |
| 中国政策 | 中国政府网、人民银行、证监会、发改委、商务部；限定中央/部委政策路径与政策标题，排除地方局、分行和无关会见新闻 |
| 美国官方 | 美联储原始 RSS、美国财政部新闻发布列表 |
| 重要投研 | 高盛、摩根士丹利公开 Insights，独立核对文章发布日期；不绕过登录或付费墙 |
| 重要公司 | 英伟达官方 RSS，限定新闻站与官方博客域名 |

ETF 目录官方来源：深交所 `https://fund.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=1000_lf&TABKEY=tab1`；上交所 `https://etf.sse.com.cn/` 使用其公开接口 `https://query.sse.com.cn/commonQuery.do?sqlId=COMMON_JJZWZ_JJLB_L&isPagination=true&pageHelp.pageSize=10000&pageHelp.pageNo=1`。成立日不能替代上市日。

分时图是已采集交易日的分钟**价格线**，不是实时行情，也不是历史一年分钟 K 线。午间、盘后点不绘制；不插值。成交量单位未核实，因此客户端不画分时量柱或均价。上游累计成交额下修时保留累计观测，并将该分钟差额留空、标注修订。停牌/尚未上市返回 `not_traded`；真正缺失返回 503。

本轮图表交互作用于 A 股：分时与日线切换，日线最大近 52 周；双指缩放、横向拖动，桌面可使用放大/缩小和前后平移按钮。其他市场仍使用原有日线，不声称已有美股分时。

## 有限手工更新

生产机需以 `marketdata` 用户加载原有受保护环境文件后执行。`DATA_DIR`、`CONFIG_PATH` 必须指向现有数据及配置；不得使用 root 创建数据库 WAL。

```text
python scripts/collect_all_etfs.py --date 2026-10-15 --output /var/lib/personal-market-data/manual-etfs --budget 3600
python scripts/collect_focus.py --date 2026-10-15 --output /var/lib/personal-market-data/manual-focus
python scripts/collect_all_minutes.py --date 2026-10-15 --output /var/lib/personal-market-data/manual-minutes --budget 3600
python -m marketdata collect-context --feature priority-news --raw-dir /var/lib/personal-market-data/manual-news
```

这些命令互斥运行，均有限结束，不启用定时器。股票 52 周与 qfq 更新沿用 `scripts/collect_all_a.py`。先更新日线，再抓该日分时；分钟源只提供最近窗口，不能承诺随时回补过去任意日期。`collect_all_etfs` 保留原发布者，允许同来源部分序列补齐；API 请求不会自动执行这些命令。

公开网站可能延迟、更改结构或返回错误；一次成功不代表长期可用保证。资讯为可访问、已核日期原文的观测集合，并不穷尽全部机构/公司的发布。
