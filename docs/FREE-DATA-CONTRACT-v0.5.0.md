# 免费来源扩展契约 v0.5.0

本版不要求新增供应商账号、积分、密钥或付费。新增采集均通过 CLI 人工触发；不改变 worker、巡检、补采及备份的停用状态。接口只读 SQLite，不在用户请求中抓取上游。所有 `/v1/` 路径沿用 Bearer 鉴权。

| 接口 | 内容与边界 |
| --- | --- |
| `/v1/indices` | 5 个国内指数、日经225、韩国KOSPI；独立 ID、来源、实际日期、点位和时效，不以 ETF 代替指数 |
| `/v1/indices/{id}/bars?end=YYYY-MM-DD&limit=2000` | 最新 limit 条日期升序日线，最多 2000；`unit=point`。CN 使用新浪指数，JP/KR 使用 AKShare 新浪环球指数；按各自交易日历和收盘时间判定目标日 |
| `/v1/cn/stocks/{id}/minutes?trade_date=YYYY-MM-DD` | 近期 1 分钟、不复权；时间带 `+08:00`，成交量为手、金额为元；源站旧日开盘值 0 转 null；不补造缺失分钟，不声称实时行情或完整历史窗口 |
| `/v1/cn/stocks/{id}/metrics?end=YYYY-MM-DD&limit=1` | 日期明确的总/流通市值、股本、PE TTM/静态、PB 等，金额为 CNY 元；负 PE 保留；最多返回 2000 条 |
| `/v1/cn/limit-up-ladder?trade_date=YYYY-MM-DD` | 东方财富披露的近期涨停池、连板数及分组；`basis=provider_reported`，`independently_verified=false`，不等同于已核验 ST/IPO/停牌等规则的全市场梯队 |
| `/v1/scheduled-events?start=YYYY-MM-DD&end=YYYY-MM-DD` | 美联储官方 FOMC 会议日期；`time_precision=date`，确切时刻、源站发布时间为 null，观察时间独立记录；尚不覆盖 CPI、非农、日韩及能源日历 |

指数历史异常日期会隔离，`validation_report` 提供日期和原因；原始响应保留于明确指定的私有目录。发布的是通过校验的观测记录，历史连续性尚未认证。JP/KR 分别采用 XTKS/XKRX 日历及本地收盘后 15 分钟的查询目标；这段延迟不保证上游届时发布。

仅采集指数时，各市场分别判断日历，不因中国休市而阻断日韩。中国指数仍遵守北京时间 15:10 后采集的限制；中国休市期间其目标日为上一已结束交易日。

换手率仅在同日已验证股单位成交量、同日流通股本及两源收盘价吻合时计算 `volume / float_shares * 100`，并返回 `turnover_basis`。分母为流通股本，不是自由流通股本；缺证据返回 null。

日历保存观察版本；源站某日程消失会标 `removed_from_latest_source`，不据此猜测取消或延期。`include_removed=true` 可查询这类记录。无源站发布时间时不以采集时间替代。该日历独立于现有新闻 `/v1/events`。

龙虎榜 `/v1/cn/lhb` 新增稳定分页 `total/snapshot_id/refresh`，续页携带首屏 `snapshot_id` 和同一 `trade_date`。409 时整次扫描失败，重新刷新；不同原因、买卖方向及单日/多日榜不合并金额。席位路径沿用 `/v1/cn/lhb/{symbol}/seats`。

## 手工采集与证据

```powershell
python -m marketdata collect-supplemental --date 2026-10-15 --feature indices --feature minutes --feature valuation --feature limit-up --raw-dir data/free-raw
python -m marketdata collect-scheduled-events --year 2026 --raw-dir data/free-raw
python -m marketdata collect --market CN --date 2026-10-15 --refresh --same-source-close --raw-dir data/cn-close-raw
python -m marketdata collect-history-batch --date 2026-10-15 --window 250 --scope configured --max-items 20 --budget 120 --raw-dir data/history-raw
```

国内采集仍须在北京时间 15:10 后执行。历史批次只运行一次，按实际交易日、上市日期及独立停牌证据核验；不会因为已有目标日一根行情而跳过短历史。`partial` 和退出码 2 表示还未完成，可显式再次执行，不会自行续跑。

`--same-source-close` 是显式可选的同源补充：新浪 ETF 档案缺当日线时，核对来源时间和上一交易日收盘；同花顺核对同源近期历史重叠价格，再校验当日收盘。这两个 HTTP 适配器属于本项目，区别于 AKShare 原始函数；原始证据记录开关及合并结果，未开启时行为不变。更早历史不覆盖；当天修订由 `bar_revisions` 保存旧值和新值。

同花顺的当日蜡烛可能单列在 `today.js`；年档已有当天行也可能仍是盘中缓存。适配器复用锁定 AKShare 的公开行情请求格式，核验板块代码、响应交易日、来源收盘后时间、非未来时间、上一交易日历史及 OHLC；已有当天年档时还核对开盘价锚点。校验通过后新增或修订当天记录，取不到或校验不通过时保留档案，由目标日状态标明是否待核验，不修改日期。

开启同源收盘核验后，当前日 THS 收盘检查失败会将任务标为 pending，不以“年档已有同日行”改判成功；原有库存作为历史观测保留。

无库存返回 503；THS 完整成员及合格逐日板块资金保持 501。未接入任何积分/付费数据；完整历史成分、长期资金窗口、严格连板规则、完整事件日历和金油主连仍需单独统计。
