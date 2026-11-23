# looklook 行情接口

现有 `marketdata worker` 按北京时间和 A 股交易日历自动采集。上午 09:26 至 11:26、下午 13:01 至 14:46，每 15 分钟一次。周末、休市日、午休及收盘后暂停。进程重启时只处理当前分钟的采集时点，错过的时点不补采。数据库记录已占用的时点，避免重启导致重复请求。采集使用现有的 A 股日历及 `calendar_overrides` 配置。

采集优先复用 `AKProvider` 调用 AKShare 的 `stock_zh_a_spot_em` 获取东方财富 A 股快照，并按照 `config/looklook-watchlist.json` 中的六位代码去重筛选。东方财富不可用时，复用腾讯行情解析器，每批最多请求 50 支自选标的。东方财富失败后会暂缓一小时再探测。服务仅将自选标的写入现有 SQLite `feed_snapshots`，供所有客户端读取。生产 worker 需要开启 `automatic_collection`，可在现有 `CONFIG_PATH` 文件中将 `looklook_quotes_enabled` 设为 `false` 来停止此采集任务。

`GET /v1/looklook/quotes` 使用与现有 API 相同的 `Authorization: Bearer <API_TOKEN>`。桌面端在交易日的刷新时段请求，API 只读取已落库的快照。

- `updatedAt` 为成功写入快照的 UTC 时间，首次采集成功前为 `null`。
- `tradeDate` 取腾讯源时间换算到上海时区的日期，每批快照只发布最近一个行情日的记录。腾讯源每支标的另有 `quotedAt`。东方财富的 AKShare 接口没有返回交易日期或逐股行情时间，`tradeDate` 为 `null`。
- `quotes` 以六位证券代码为键，`changePct` 以百分点计，`2.35` 即涨幅 `2.35%`。
- `partial` 说明名单中有缺失标的，例如停牌、上游漏报或行情字段缺失。
- `stale` 表示最近一次采集失败、缓存超过 16 分钟，或者从未取得快照。刷新失败时保留最后一次成功数据。非交易时段，源可能继续返回上一交易时段的数据。

使用生产 worker 相同的环境变量和项目目录运行 `python -m marketdata collect-looklook-quotes` 可手动采集一次。该诊断命令立即执行并输出采集状态和数量，自动采集时段由 worker 单独控制。
