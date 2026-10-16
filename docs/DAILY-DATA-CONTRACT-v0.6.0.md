# v0.6.0 数据契约

本版按照真实数据结构提供页面，不发布空能力占位。所有 `/v1/` 请求鉴权，分页接口返回稳定 `snapshot_id`；下一页携带同一版本，不能在每页重新 `refresh=true`。完整请求定义见 [OpenAPI](openapi.json)。

| 功能 | 接口 | 口径 |
|---|---|---|
| 页面能力与日期 | `GET /v1/data-overview` | `history_sessions=30`，按市场列出已存日期和目标日期，不表示全市场齐全 |
| 行情 | `GET /v1/bars/{id}?end=YYYY-MM-DD&limit=30` | 不复权 OHLC；旧调用较大 limit 仍最多 30，返回窗口及覆盖；不提交扩容队列 |
| 涨幅榜 | `GET /v1/cn/rankings?period=5d&kind=stock/etf/industry/concept` | `close[T]/close[T-5]-1`；6 个精确市场交易日必须齐全，股票/ETF使用单次同源前复权，板块使用指数收盘 |
| 板块资金榜 | `GET /v1/cn/fund-rankings?kind=industry/concept&period=1d/5d` | 东方财富源定义主力净额，元；5d 为精确 5 个交易日净额合计 |
| 板块资金 | `GET /v1/cn/boards/{id}/flows?end=YYYY-MM-DD` | 东方财富 BK 编码，30 日；main=large+superlarge，未知总流入/流出为 null |
| 板块成分 | `GET /v1/cn/boards/{id}/members?observed_date=YYYY-MM-DD` | 完整当前观测，非历史关系；`has_history` 仅表示已有日线；最大 500/页 |
| 宏观 | `GET /v1/macro/assets`、`/{asset_id}/history` | gold/brent/us2y/us10y/us30y，最近 30 个来源观测，保留各自日期/延迟 |
| 经济日历 | `GET /v1/economic-calendar?start=...&end=...` | 华尔街见闻；按周请求拼接，历史 30 个 CN 交易日跨度加未来 7 自然日；实际/预期/前值/修订前值分开 |
| 政策 | `GET /v1/policy-news?start=...&end=...` | 5 个官方当前列表；原文网址、发布机构、精确日期和类别，不保证历史穷尽 |

股票与 ETF 涨幅榜中的 `close` 是当日**实际不复权收盘价**，`adjusted_close` 为收益计算使用的前复权收盘；`price_source` 与收益序列的 `source` 分列。禁止把复权价格当市场报价。`expected_count/eligible_count/excluded_count/exclusions` 明示覆盖，不能把“已采集池”宣传为全 A 股榜单。

板块 ID 例：`CN.industry.BK1036`（半导体）、`CN.industry.BK1283`（银行）、`CN.concept.BK0800`（人工智能）、`CN.concept.BK0574`（锂电池概念）。东方财富目录包含自有分类层级，不和同花顺按同名混接。旧 THS 成分/资金请求返回 404；旧 THS 日线存档不清除。成分 observed_date 与资金 trade_date 是不同事实。

黄金来自新浪 XAU 美元参考日线（页面标为 CFD），非 LBMA 基准或期货主连结算价；排除当前 UTC 日期日柱，不声称来源未披露的交易所时区/最终结算。布油采用 [EIA Europe Brent Spot FOB](https://www.eia.gov/dnav/pet/hist/RBRTED.htm)，USD/barrel。美债采用 [美国财政部 Daily Par Yield Curve](https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve) 的 2/10/30 Yr 字段，水平是百分比、变动是 bp：`(today - previous) × 100`。价格变化和收益率 bp 不混用。

日历来源：[华尔街见闻](https://wallstreetcn.com/calendar)。epoch 转换为 UTC，客户端显示北京时间；计划发生时刻不是文章发表时间，`published_at=null`。政策来源：[国务院](https://www.gov.cn/zhengce/)、[人民银行](https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html)、[证监会](https://www.csrc.gov.cn/)、[发改委](https://www.ndrc.gov.cn/xxgk/zcfb/tz/)、[商务部](https://www.mofcom.gov.cn/zcfb/)。只保留源中完整年月日，不从月日猜年份；未知生效时刻/发布时分为空。列表滚动消失不等于事件取消。

保留有数据的龙虎榜、买卖席位、美股行业/AI 观察、指数、配置股资金和估值接口。分钟、关键词旧新闻、Fed 官方会议等存量接口按已有快照查询，未作为新导航占位。`/v1/cn/limit-up-ladder` 已删除并返回 404；不提供长历史扩容、模型推理和信号扫描业务入口。

读取不发起上游抓取。失败保留最后成功快照并返回更新状态；完全无可用快照时返回 503，未知/移除入口返回 404，日期越界或不合规参数返回 422。自动任务继续关闭，数据不会因访问 API 而刷新。公共数据来源当前可用不等于有长期 SLA。

## 全 A 股覆盖解释（10-16 增量）

5 日榜新增 `missing_data_count`：有证据的 `not_listed_at_target`、`insufficient_listed_sessions`、`suspended_within_return_window` 不计为采集缺口。`complete` 仍表示目录每只股票均能进入榜单；存在正常排除项时可为 false，而 `missing_data_count=0`。覆盖接口分别返回 `available`（实际目标日日线）、`suspended_count`、`not_yet_listed_count`、`accounted` 和 `accounted_pct`，不填造停牌价格。详情见 [全 A 验收](FULL-A-ACCEPTANCE-20261016.md)。
