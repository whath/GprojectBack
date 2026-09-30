# 后台修复与运维接口（2026-09-30）

## 本轮实现

1. **美股历史源冲突**：新增 `rebuild-us-source`。先备份，检查目标日期、旧日期覆盖和重叠 OHLC 价格差异（不超过 0.5%），再在事务中归档旧记录并整段替换。云端 XLB、XLC 各 42 条历史已重建为新浪源，最大 OHLC 相对差分别约 0.0010% 和 0.00009%。后续沿用同一来源。已解析的美股标识可从本地数据库复用，新标的才依赖在线标识查询。
2. **完整性和原因分类**：新增每日采集范围快照；日线覆盖与采集任务完成状态分别返回。延迟日线、延迟披露、网络异常、源冲突、不支持的成分、预算内未完成分别标记，不能用少量成功样本代表全市场完成。
3. **有限补采和恢复**：每个调度窗口初次执行后最多补采 3 次，间隔 20 分钟；重试跳过成功与不支持的任务。进程锁防止多 worker、多采集器并行写入；中断后恢复持久化任务。单轮 900 秒请求启动预算，达到后不再开始后续任务，正在执行的调用仍受自身超时限制。A 股三个固定窗口仍为 15:10、17:10、19:10。
4. **后台告警及健康**：告警持久化、按键去重、保留恢复时间；返回 worker 心跳、备份时间、磁盘空间和数据库状态。仅后台列表，不发送外部通知。
5. **自动备份**：每日创建 SQLite 一致备份，检查完整性并保存 SHA-256 清单；保留最近 14 个自动备份。只有新备份成功后才清理旧自动备份，手动备份不参与轮换。已修复备份连接未关闭导致 Windows 清理失败的问题。自动异地备份尚未配置，本轮验收副本另下载本机。
6. **范围扩展**：云端配置为 90 个同花顺行业、15 个重点概念、5 只 A 股、2 只境内 ETF。美股为 11 个行业 ETF、12 个 AI 标的和 SPY。此范围仍不是全部 A 股和 ETF；实时完成情况以 coverage 接口为准。
7. **首页聚合**：CN/US 使用各自交易日，聚合行业/概念涨幅、龙虎榜、美股行业 ETF、AI 监控及覆盖状态。

## 新增 API

| 路径 | 作用 | 鉴权 |
| --- | --- | --- |
| `/readyz` | 就绪状态，不暴露详细信息；异常返回 503 | 无 |
| `/v1/health` | 心跳、备份、磁盘、数据库健康详情 | Bearer |
| `/v1/alerts` | 后台告警；`include_resolved=true` 包括已恢复项 | Bearer |
| `/v1/coverage?market=CN&trade_date=2026-09-30` | 配置范围的实际日线覆盖、缺失项、任务原因 | Bearer |
| `/v1/overview` | 分市场交易日的首页聚合；可指定 `cn_date`、`us_date` | Bearer |

`/healthz` 仍表示 API 进程能响应；`/readyz` 表示运维就绪；行情是否齐全必须读取 `/v1/coverage`。后台数据接口已具备，独立可视化管理页面不在本轮数据/API 阶段内。

## 域名准备

已按 `ericcself.top` 准备 `deploy/ericcself.top.nginx.conf`：HTTPS 反代、请求限流、请求体限制、关闭公网文档入口和保留 ACME 校验目录。默认不允许跨域，后续将实际前端来源加入 `cors_origins` 白名单并重启 API；同源部署无需 CORS。

用户完成控制台登录后，发现域名原有解析记录为 0。17:14 已添加并核验根域名 A 记录 `39.96.22.254`，TTL 600 秒；权威 DNS、Cloudflare DNS、阿里 DNS 均已返回该地址。nginx 1.30.4 和独立虚拟环境中的 Certbot 5.8.0 已安装。17:18 成功签发 Let's Encrypt 证书，有效期至 2026-12-29，并启用 HTTPS 反代。

公网 HTTPS 首次 10 项运维接口检查通过：健康 200、未授权 401、授权业务正常；没有关闭证书验证。报告为 `data/deploy/https-acceptance.json`。API 本体仍仅监听 127.0.0.1:8000，公网由 nginx 的 443 入口代理。

**尚未完成公网链路最终验收：** 证书续期 dry-run 收到阿里云 `Server: Beaver`、403、`Non-compliance ICP Filing` 页面，HTTP ACME 校验被备案拦截。用户最终澄清“目前尚未备案，需要等待三天”。当前按待备案处理，不把三天视为必定审核通过的承诺。后台已记录 `tls_renewal_validation_failed` error，现有 SSH 隧道可继续使用。

已安装 `market-certbot-renew.timer`，每日两次检查，随机延迟 30 分钟。失败通过 `market-certbot-alert.service` 写入后台告警；续期成功钩子重载 nginx 并解除续期服务失败告警。等待备案期间已将 **nginx 和续期 timer 停止并禁用自启动**，保留 DNS、证书、反代配置及 unit 文件。API/worker 仍 active，服务器不再监听 80/443。**定时器安装不等于续期验证通过**。

备案通过并成功接入阿里云后，先 `systemctl enable --now nginx`，确认公网 HTTP 的 ACME 校验文件不再被拦截，再运行 `/opt/personal-market-data/certbot-venv/bin/certbot renew --dry-run --no-random-sleep-on-renew --run-deploy-hooks`。通过后重新启用 `market-certbot-renew.timer`、复验公网鉴权/接口、解除 `tls_renewal_validation_failed`，再交付手机直连入口。

阿里云官方排查文档：https://help.aliyun.com/zh/icp-filing/basic-icp-service/support/web-site-suddenly-appeared-for-the-record-to-block-or-hang-and-so-on-and-so-forth 。

## 验收与回滚

本地运行 `python -m pytest -q`，另通过真实 SSH 隧道调用部署后的接口。云端升级前已保存应用压缩包和数据库一致备份；`series_rebuilds` 另保留源切换前的记录。

复验命令：

```powershell
./.venv/Scripts/python.exe -m scripts.check_hardening_api --env-file data/deploy/server-client.env --cn-date 2026-09-30 --us-date 2026-09-29 --output data/deploy/hardening-acceptance.json
```

恢复旧版必须同时恢复旧代码与升级前数据库，因为旧 worker 的调度表插入方式不兼容新版列结构。先停止 API/worker，保存当前一致备份和配置，将待恢复备份在临时目录中通过 SQLite `integrity_check` 后替换数据库，并一并处理停机后残留 WAL/SHM，再恢复对应代码/配置和服务。不能让新旧 worker 同时运行。

## 仍需明确的边界

- 同花顺完整板块成分目前无经验证的完整来源；明确标记 `unsupported_membership`，不拼接另一分类体系充数。
- 数据源未返回当日行情时，保存已有历史并报告 `awaiting_bar`，不会将昨日数据改成今日。
- 境内 ETF、板块成交量的原始单位尚未全部交叉验证，字段继续标记 `source_unit_unverified`，不提供误导性的跨源成交量比较。
- 还没有完成全 A 股、全 ETF 的历史初始化和长期负载验收。后续扩大这些范围须结合 API 覆盖率、耗时和失败比例逐步实施。

## 云端验收结果（约 17:01）

- 本地 54 项测试通过。真实运维接口检查 10 项通过；OpenAPI 0.2.0，共 16 个路径。
- 通过 SSH 隧道执行 210 项真实 HTTP 检查，136 个序列及披露 API 的记录与下载的一致备份逐行相同。报告：`data/deploy/hardening-row-comparison.json`。
- 国内已有 112 个序列、4,605 条历史日线；其中 9 月 30 日日线为个股 2/5、ETF 0/2、行业 0/90、概念 2/15，剩余 108 项为 `awaiting_bar`。这次扩容完成了历史采集，但当日日线未齐。
- 9 月 30 日龙虎榜 42 条、机构统计 29 条、买卖席位 420 条均已入库并通过逐行 API 核对。
- 美股 24 个序列、1,008 条历史日线，9 月 29 日全部齐全；目录及 24 项日线任务均 complete。
- 已修复手动补采成功后的旧告警/待重试窗口不立即解除问题。当前未解决告警为 108 条延迟日线 warning、105 条不支持成分 info；没有遗留美股元数据故障告警。
- 自动备份下载副本的 SHA-256 与清单一致，SQLite 完整性检查通过；升级前另有应用和数据库回滚副本。
- API 和 worker 均 active、无自动重启；扩容采集过程 worker 内存峰值约 137 MiB。此单轮结果不能替代长期性能验收。
- 当日 17:10、19:10 后续验收计划已同步到新的 112 个国内序列配置，服务长期调度继续运行。

## 17:10 窗口复验（17:21 采集结束）

窗口运行结束为 partial，85 项 complete、210 项 pending。当天个股已补齐 5/5，概念 2/15；ETF 0/2、行业 0/90。共 7/112 个序列有当日日线，另有 105 项等待上游，以及 105 项不支持完整成分。

当天龙虎榜更新为 49 条、机构统计 33 条、买卖席位 490 条。220 项真实 API 检查与本轮一致备份逐条相符；报告 `data/deploy/cn-20260930-1710-api.json`，备份 `data/deploy/cn-20260930-1710.sqlite3`，日志为同名前缀 `.log`。本窗口首次补采计划在 17:41 左右执行，19:10 固定窗口保留。
