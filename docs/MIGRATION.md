# 更换电脑或服务器

代码不绑定当前电脑，也不绑定某个公网 IP。更换机器时，源码、运行配置、数据和调度需要分别处理。GitHub 只保存源码、依赖、测试、部署脚本及不含密钥的配置示例；不保存数据库、访问令牌、SSH 私钥和本机自动化任务。

## 只更换开发电脑

现有阿里云 API、worker、定时采集和云端每日备份继续运行，不需要重新部署服务器。新电脑需要重新建立 Python 环境、SSH 隧道和本机异地备份调度。

```powershell
git clone https://github.com/whath/GprojectBack.git
cd GprojectBack
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
./.venv/Scripts/python.exe -m pytest -q
```

使用 Python 3.11 或 3.12。原生云端运行于 3.11，本地测试环境及 Docker 为 3.12；虚拟环境不要跨机器复制，应重新安装依赖。

连接原服务器：

1. 安全迁移 SSH 私钥，或为新电脑生成密钥并将公钥加入服务器；不要把私钥提交 GitHub。
2. 创建 `data/deploy`，把 `config/cloud-connection.example.json` 复制为 `data/deploy/cloud-connection.json`，填写实际服务器及新电脑上的私钥、令牌文件路径。`client_env` 建议填写新电脑绝对路径。
3. 通过安全渠道迁移 API 令牌至 `data/deploy/server-client.env`，格式 `API_TOKEN=实际令牌`。不要在聊天、截图或 Git 中公开令牌。
4. 核对服务器 SSH 指纹后建立隧道：

```powershell
ssh -i '<新电脑私钥路径>' -N -L 127.0.0.1:18000:127.0.0.1:8000 -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes '<SSH用户>@<服务器地址>'
```

5. 项目根目录执行 `./.venv/Scripts/python.exe scripts/pull_cloud_backup.py`，确认哈希与恢复校验通过。
6. 在新电脑重新配置本机巡检/备份自动任务，更新工作目录；旧电脑任务停用，避免两边重复执行。当前 Codex 自动任务不属于 Git 仓库，不会随 clone 自动安装；需要电脑在线且 Codex 运行。

只连接云端时不必在新电脑启动另一个 worker。`data/offsite-backups` 是本机副本，可安全迁移已有备份，也可从服务器重新下载。

## 更换云服务器

核心代码可以继续使用，但需要迁移数据库与环境，并重新核验网络及数据源访问。部署脚本默认使用 `/opt/personal-market-data/app` 和 `/var/lib/personal-market-data`；若改路径，应同步调整 systemd、环境变量、备份连接配置及部署脚本。

1. 在旧服务器创建最终 SQLite 一致备份。不要仅复制正在写入的 `market.sqlite3`，因为 WAL 中可能还有未合并数据。
2. 部署同一 Git 提交和依赖版本。原生 Linux 安装可 clone 到 `/opt/personal-market-data/app` 后执行 `bash deploy/install-native.sh`；该脚本创建 API 服务，worker 的启动应安排在迁移验收之后。Docker 路径参见 README。
3. **迁移旧服务器实际 `config/universe.json`**，不要直接拿仓库默认配置替代。仓库 `config/production.example.json` 是当前阶段的无密钥生产配置参考，包含全目录快照与后台历史队列；默认 `config/universe.json` 的采集范围、历史长度与云端可能不同。
4. 安全迁移 `/etc/personal-market-data.env`，或重新生成令牌并同步更新客户端。若沿用原生部署位置，环境应指向 `/var/lib/personal-market-data` 和 `/opt/personal-market-data/app/config/universe.json`。权限保持仅管理员可读；systemd 会为服务加载环境。
5. 停止新服务器 API/worker 后，将一致备份恢复为新数据目录中的 `market.sqlite3`，确保新目录没有其他数据库遗留的 `-wal` / `-shm` 文件，设置属主 `marketdata:marketdata`。先验证 `PRAGMA integrity_check`，再启动 API 检查数据和鉴权。
6. 切换前停止旧服务器 worker，再启动并启用新服务器 worker，确保只有一个采集调度器运行。服务启用命令为 `systemctl enable --now personal-market-worker`。不要同时采集到两份独立数据库后尝试直接合并。
7. 更新 SSH 隧道、备份连接信息和定时任务。若公网 IP 变化，更新 DNS、防火墙以及域名/证书相关部署配置；迁移完成后重新验收公网入口与证书续期。当前项目公网入口仍处于等待 ICP 备案状态，不应直接运行 HTTPS 启用脚本。
8. 验收 `/healthz`、带鉴权的 `/v1/health`、`/v1/coverage`、`/v1/cn/quotes`、`/v1/cn/history-coverage`，并验证数据备份和恢复。

`deploy/upgrade-*`、`deploy/finalize-*` 和带具体日期的文档记录了当前服务器的历史升级步骤，不是新服务器的通用安装入口；不要在新环境无检查地顺序执行。原始数据源的网络可达性可能随地域/IP变化，应进行真实小批次采集核验。

## 迁移边界

- 只 clone 代码：不会获得已采集数据、实际访问令牌或自动任务。
- 数据库包含行情、采集任务、历史补采进度；迁移一致备份可保留这些状态。
- 数据库不包含 systemd 单元、域名证书、SSH 凭据和本机 Codex 自动任务，需要单独迁移或重建。
- 当前全目录历史初始化和同花顺完整板块成分仍未完成；更换主机不会自动补齐这些缺口。
