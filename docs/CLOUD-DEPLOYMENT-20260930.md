# 阿里云部署与联调记录

部署日期：2026-09-30，北京时间。服务器 `39.96.22.254`。采用 Python 虚拟环境 + systemd，API 与采集服务已启用开机自启。无域名阶段，通过 SSH 隧道访问，公网不开放 8000 端口。

**17:01 升级记录：** 后台 0.2.0 已上线，加入完整性、告警、健康、聚合 API、有限补采及自动备份；云端范围扩为 90 行业、15 概念、5 股、2 ETF。210 项 API 检查与一致备份逐行相同，美股 24 个标的完整，国内当天仍缺 108 个序列。用户确认 `ericcself.top` 审核通过，但 DNS 仍未解析，HTTPS 未开启。详见 [后台修复与验收记录](BACKEND-HARDENING-20260930.md)。下文初始 9 序列、未配置自动备份等描述为部署首轮历史状态，以此次更新为准。

**17:24 域名进展：** DNS A 记录已添加并生效，HTTPS 证书签发成功，公网 HTTPS 10 项首次接口检查通过，续期定时器已配置。但 HTTP 续期演练遭阿里云 ICP 备案 403 拦截；当前账号备案页显示“尚无备案信息”，与用户确认已备案的状态不一致，需继续核对备案号及接入状态。正式公网链路暂不宣布验收完成，详见后台修复记录。API/worker 与 SSH 隧道保持运行。

**最终澄清：** 用户确认目前尚未 ICP 备案，需等待三天。已暂时停止并禁用 nginx、证书续期 timer，保留其配置和证书；后台 API/worker 及隧道继续运行。备案与阿里云接入通过后再启用公网入口、完成续期 dry-run 和公网复验，不需重部署后台。

**17:10 窗口验收已完成：** 17:21 结束，5 只个股当天均补齐，2 个概念当天有数据；共 7/112 个序列有当日日线。105 个日线仍待上游，105 个完整成分为不支持。当天龙虎榜 49 条、机构 33 条、席位 490 条。220 项真实 API 检查与本轮一致备份逐行相符，报告 `data/deploy/cn-20260930-1710-api.json`。后台有限补采及 19:10 固定窗口继续运行。

## 实际环境

- Alibaba Cloud Linux 4.0.3，x86_64，Python 3.11.6。
- 系统可见内存约 1,674 MiB，40 GiB 系统盘；安装前剩余约 34 GiB；北京时间、NTP 已同步。
- 未安装 Docker。依赖按 requirements.txt 的主依赖版本安装；默认阿里云镜像缺少最新锁定版本，改为从官方 PyPI 获取 Linux/Python 3.11 安装包，经 SSH 上传后离线安装。
- `pip check` 通过。服务器完整已安装版本列表：`/opt/personal-market-data/requirements-installed.txt`。
- 运行用户为专用非登录用户 `marketdata`；API 内存限制 384 MiB，worker 1000 MiB。

## 路径与服务

| 项目 | 服务器路径或名称 |
|---|---|
| 应用 | `/opt/personal-market-data/app` |
| Python | `/opt/personal-market-data/venv/bin/python` |
| 配置 | `/opt/personal-market-data/app/config/universe.json` |
| 数据与原始样本 | `/var/lib/personal-market-data` |
| 令牌及环境变量 | `/etc/personal-market-data.env`，root 可读，权限 600 |
| API | `personal-market-api.service`，监听 `127.0.0.1:8000` |
| 定时采集 | `personal-market-worker.service` |
| 首次在线备份 | `/var/lib/personal-market-data/backups/deployment-20260930.sqlite3` |

服务使用只读系统目录、独立临时目录、禁止新增权限，允许写入数据目录。鉴权令牌在服务器随机生成，没有写入源代码或部署包。

## 真实云端验收

云端直接从上游采集，没有用本机样本库替代：

- 国内历史样本：5 只个股、2 只 ETF、行业和概念各 1 个，共 378 条日线；19 项 sample 任务 complete。
- 美股历史样本：XLB、XLC、NVDA、SPY，共 168 条日线；5 项 sample 任务 complete。XLB/XLC 来自东方财富，NVDA/SPY 回退新浪，来源逐条记录。
- 龙虎榜 72 条、机构统计 39 条、两只股票的买卖席位 30 条。
- 数据日期范围：2026-07-31 至 2026-09-29。
- 云端在线备份完整性检查通过；将备份下载到本机，通过 SSH 隧道对真实常驻 API 执行 23 项 HTTP 检查，546 条日线、141 条披露/席位与备份逐条一致。
- 健康检查 200；无鉴权业务请求 401；有鉴权请求 200。

证据：本机 `data/deploy/cloud-acceptance.json`、`data/deploy/cloud-market.sqlite3`；服务器 `raw/`、`collection-CN-2026-09-29.json`、`collection-US-2026-09-29.json`。这是样本与部署验收，不代表当日或全市场已齐。

## 采集范围与调度

云端初始配置 `cn_all_stocks/cn_all_etfs/cn_all_boards=false`，60 个自然日历史，国内仅上述 9 个序列。美股 worker 使用配置内 11 个行业 ETF、12 个 AI 标的及 SPY；其完整覆盖需看 configured 任务，而非四个样本的完成状态。

国内交易日北京时间 15:10、17:10、19:10 执行，启动时补跑最新已到窗口。美股依交易日历，在收盘后 1 小时、3 小时执行，按时区处理夏令时。服务日志与任务表保留 complete / pending / failed；完整同花顺成分尚不可用，仍会显示 pending。

本任务今天 17:10、19:10 的两轮后续验收已改为检查云端 worker 和云端 API；到期只结束验收计划，不停止服务器长期采集服务。

### 当日补采进展

16:26 启动 worker 后自动补跑当日已到窗口。约 16:29 检查时，9 月 30 日龙虎榜已入库 42 条，该披露任务 complete，API 返回相同条数；席位明细仍在持续采集。这是部署期间的进展快照，不代表本轮所有行情和披露任务已完成，17:10、19:10 将按计划继续核验。

## 本机访问

本轮已建立 SSH 隧道，本机地址：`http://127.0.0.1:18000`，接口文档：`http://127.0.0.1:18000/docs`。

令牌保存在本机受限文件 `D:/workSpace/Gproject/data/deploy/server-client.env` 的 `API_TOKEN` 项，仅当前 Windows 用户可访问。业务请求使用 `Authorization: Bearer <API_TOKEN>`。不要将该文件放进源码、分享包或截图。

连接参数保存在 `data/deploy/cloud-connection.json`，仅包含主机、用户和路径，不包含私钥内容或令牌。SSH 隧道进程号保存在 `data/deploy/tunnel.pid`。本机重启或 SSH 进程退出后需重新建立隧道，例如：

```powershell
ssh -i '<你的私钥路径>' -N -L 127.0.0.1:18000:127.0.0.1:8000 -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 root@39.96.22.254
```

本机隧道关闭不影响云端采集。手机尚未配置连接；需要在手机上建立自己的 SSH 隧道，或之后提供域名部署 HTTPS。不能在手机上直接使用电脑的 `127.0.0.1` 地址。

## 运维与复验

服务器检查：

```bash
systemctl status personal-market-api personal-market-worker
journalctl -u personal-market-worker --since today --no-pager
```

本机经隧道检查常驻 API：

```powershell
./.venv/Scripts/python.exe -m scripts.check_live_api --base http://127.0.0.1:18000 --env-file data/deploy/server-client.env --output data/deploy/cloud-latest.json
```

首轮备份已验证并下载本机；周期性异地备份尚未配置。已有备份对比只适用于备份时的数据快照，采集更新后应重新生成一致备份，避免将时间差误判为 API 错误。

当前没有完成全市场负载或长期稳定性验收，也没有开放公网 HTTPS。后续扩大配置池前先检查缺失任务、历史初始化耗时及内存峰值。
