# 重要日程、自选报价与技术指标 v0.6.4

## 范围与接口

- `GET /v1/important-calendar?start=YYYY-MM-DD&end=YYYY-MM-DD&country=CN|US&category=meeting|release`：最长180天，默认最多500条，可设1000条。只列已取得明确日期的重要会议、数据公布；`sources` 分别报告 current / retained / unavailable。`exhaustive=false`，不宣称日程穷尽。未公告日期保留在 `pending_meetings`，不估算进日历。
- `GET /v1/economic-calendar` 默认 `min_importance=3`（源评级0—4）；新客户端强制只展示3—4级。显式 `min_importance=0` 保留旧接口兼容性。日程时刻不是新闻发布时间。
- `GET /v1/watch-quotes?ids=CN.stock.000001,US.stock.NVDA&cn_date=YYYY-MM-DD&us_date=YYYY-MM-DD`：最多100个唯一标的。独立查询已存不复权收盘价，包含实际交易日、币种、来源、日涨跌口径；无需在前50涨幅榜中。`current/stale/unavailable/not_found` 逐个返回，不补零、不把旧收盘改为新日期。日涨跌优先采用来源日涨跌字段；缺少时仅与同来源前一市场交易日的原始收盘价比较，除权除息会影响该口径。缺少相邻日不计算跨日“日涨跌”。

所有业务接口仍需 Bearer 鉴权；GET 不采集上游、不创建采集任务。

## 日历来源与边界

- 中国：[国家统计局2026年发布日程](https://www.stats.gov.cn/xw/tjxw/tzgg/202512/t20251224_1962137.html)，精选国民经济运行（季度含GDP）、PMI、CPI、PPI，逐格解析月份、日期、时刻；支持合并单元格和同月多次公布。下一年度需更换 `nbs_schedule_url`，年份不符时失败保留上次日程，不照搬去年日期。
- 美国：[FOMC官方日程](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm)、[BEA发布日程](https://www.bea.gov/news/schedule)（GDP / 个人收入与支出，含PCE）、[BLS月度日程](https://www.bls.gov/schedule/2026/10_sched_list.htm)（非农、CPI、PPI、ECI、JOLTS）。BLS匿名请求若被403拒绝，不绕过；独立注明不可用，有限窗口内可用华尔街见闻高重要性就业/通胀事件作为补充，不能标为官方。
- 中国会议：仅白名单官方页面的明确公告；有已核验公告种子，发现商务部领导人活动列表中的新公告，支持配置 `important_meeting_urls` 补充官方原文。不宣称自动覆盖所有中央会议。公告发现失败仍保留已核验日期并标记更新未完成。
- 精确时刻统一存UTC，客户端显示北京时间；美国使用 `America/New_York` 自动处理夏令时。只有日期的会议保留当地起止日期，不制造“北京时间00:00”。日程会调整，保留来源链接供核对。
- 显式手动采集：`python -m marketdata collect-context --feature calendar --feature important-calendar --raw-dir <folder>`。采集失败保留最后核验快照；**不启用自动巡检、采集、补采或电脑备份**。

## 图表与指标口径

日线图增加价格轴、所选收盘水平线和OHLC；分时增加价格轴与分钟量。日线下依次为成交量、均线、MACD、KDJ、RSI、BOLL及所选日的形态说明。点选任一副图同步主图日期；缩放仅裁切可视范围，全部指标先基于完整已存序列计算，不重新启动预热，不读取未来日期。

- MA：5/10/20/60个已返回交易日的简单平均；主图叠加5/10/20。
- MACD(12,26,9)：EMA以SMA初始值启动；至少34根日线；柱值=2×(DIF−DEA)。初始值及有限52周窗口可能与其他软件略有差别。
- KDJ(9,3,3)：9日最高/最低的RSV，K/D初值50，逐次2/3平滑；最高=最低时RSV=50，J=3K−2D，不截断0—100。
- RSI14：前14个收盘变动的平均涨跌作为初值，后续Wilder平滑；至少15个收盘；全平为50，全涨为100，全跌为0。
- BOLL(20,2)：20日均价±2倍总体标准差。
- 成交量：显示实际来源原始量；单位未核实时不标为股或手，缺值留空。放/缩量对比此前20个同来源、同单位交易日，排除当天。分时量为来源累计计数的差分，开盘点可能包含集合竞价；不将成交量当价格技术指标。

全部日线指标沿用不复权价格，除权除息可能产生跳变；短历史/缺失日仅按已返回日线计算并显示窗口完整性提示。形态说明描述所选日期的已有行情，不输出确定买卖承诺。分时模式展示价量，日线指标切回日线查看，避免拿分钟价点伪造高低价或日线指标。

计算参考：[MACD](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/macd)、[Bollinger Bands](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/bollinger-bands)。KDJ初值、MACD柱倍率、RSI平滑、BOLL总体标准差以本项目明示公式为准。

## 验证记录

本轮执行记录在 `data/calendar-studies-20261016/`（含原始响应、测试、部署与真机验收材料，均忽略入Git）。后端204项通过，客户端105项通过、0失败、0跳过；云端14项、公网21项及9次并发读取通过。荣耀ALI-AN00 / Android13真机验证重要会议、数据公布、自选价格、价格轴、241→172日缩放、242点分时、全部指标及日期同步，原自选和亮屏设置已恢复。详情见 [本轮验收](CALENDAR-STUDIES-ACCEPTANCE-20261016.md)。BLS直连不可用单独披露，不作为完整覆盖。
