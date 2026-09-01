# 部署脚本

跑在媒体库宿主机上，不属于 media-agent 包本身，但依赖它的
`QBitClient` / `load_config()`。

| 文件 | 作用 |
|---|---|
| `com.zihan.media-agent.plist` | launchd 定时任务，每 6 小时跑一轮 `run` |
| `vpn-watchdog.sh` | 每 6 小时检查 gluetun 健康，不健康就重建容器 |
| `rescue.py` | BT 救援模式：把 qBittorrent 临时切到自有 VPS 的 WireGuard 隧道 |

## 配置从哪来

这三个脚本**不含任何基础设施地址**，全部从环境读：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MEDIA_AGENT_HOME` | `~/media-agent` | media-agent 仓库位置 |
| `DOCKER_BIN` | `/usr/local/bin/docker` | docker 可执行文件 |
| `~/gluetun/.env.vps` 里的 `WIREGUARD_ENDPOINT_IP` | — | 救援隧道对端，`rescue.py` 用来核对出口 IP |

`.env` / `.env.vps` 由 `.gitignore` 挡在仓库外——它们装着 WireGuard 密钥和
服务器地址，不该进版本控制。

## rescue.py 的分享率策略

平时（ExpressVPN）**不限制**：带宽不值钱，随便做种。
救援中（自有 VPS）**上限 1.0，达标即暂停**：VPS 流量要花钱。

退出救援时要叫醒的是「现在停着、但进入救援前没停」的种子——这样既覆盖
`start` 时我们主动暂停的那批，也覆盖救援期间因达到 1.0 被 qBittorrent
自己暂停的那批，而用户手动停的不动。只认前者会让后一批永远停着没人管。
