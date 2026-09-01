#!/bin/bash
# gluetun 隧道看门狗
#
# 背景：gluetun 的 custom OpenVPN 配置要求 remote 填 **IP 而非域名**，
# 而 ExpressVPN 会不定期轮换服务器 IP。一旦轮换，钉死的 IP 变成死地址，
# OpenVPN 无限重试 TLS 握手，隧道永远起不来——qBittorrent 随之变成
# firewalled / DHT 0 节点 / 全部种子 stalled，且**不会自愈**。
#
# 每次运行：判断隧道状态 -> 断了就重新解析域名 -> IP 变了就改配置 -> 重建容器 -> 复验。
# 隧道正常时什么都不做。
#
# ---------------------------------------------------------------------------
# 检测手段为什么不用 `docker exec ... wget`：
# 隧道断开时 gluetun 的 killswitch 会拦掉容器内所有出网流量，wget 卡死在
# **DNS 解析**上，而 `--timeout` 只管网络 I/O、管不到解析阶段。结果是检测
# 本身在它要检测的故障场景下永久挂起，看门狗一行日志都写不出来——实测踩过。
# 改用 `docker inspect` 读 gluetun 自带健康检查的结果：瞬时返回、不可能挂住，
# 而那个健康检查本来就是穿隧道探测的，可信度不低于自己再探一次。
# ---------------------------------------------------------------------------

export LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8
set -uo pipefail

DOCKER=/usr/local/bin/docker
DIG=/usr/bin/dig
DIR="${HOME}/gluetun"
OVPN="${DIR}/expressvpn.ovpn"
HOSTFILE="${DIR}/.vpn-hostname"      # 存原始域名，解析的唯一可靠来源
LOG="${DIR}/vpn-watchdog.log"
MAX_LOG=$((2 * 1024 * 1024))       # 日志硬上限 2MB，防止长年累积撑爆盘
GLUETUN=gluetun-expressvpn

log() { printf '%s %s\n' "$(/bin/date '+%Y-%m-%d %H:%M:%S')" "$*" >>"${LOG}"; }

# 超限就截断，保留后一半（近期记录比远古记录有用）
if [ -f "${LOG}" ] && [ "$(/usr/bin/stat -f%z "${LOG}" 2>/dev/null || echo 0)" -gt "${MAX_LOG}" ]; then
    /usr/bin/tail -c $((MAX_LOG / 2)) "${LOG}" >"${LOG}.tmp" 2>/dev/null && mv "${LOG}.tmp" "${LOG}"
    log "-- 日志超过 2MB，已截断 --"
fi

health() { ${DOCKER} inspect --format '{{.State.Health.Status}}' "${GLUETUN}" 2>/dev/null; }

# 出口 IP 从 gluetun 自己的日志里读，不发网络请求——同样是为了不可能卡住
exit_ip() {
    ${DOCKER} logs --tail 400 "${GLUETUN}" 2>&1 \
        | /usr/bin/grep -a 'Public IP address is' | /usr/bin/tail -1 \
        | /usr/bin/sed -E 's/.*Public IP address is ([0-9.]+).*/\1/'
}

# Docker 没起来（OrbStack 未运行）就退出，不反复折腾
if ! ${DOCKER} info >/dev/null 2>&1; then
    log "SKIP Docker 未运行（OrbStack 可能没启动）"
    exit 0
fi

# 救援模式期间必须避让。那时 gluetun 跑的是 WireGuard(VPS) 配置，
# 而本脚本的"修复"是用**默认** compose 重建容器——一旦触发就会把救援模式踢掉，
# 而且是在半夜无人值守时静默发生。
if [ -f "${DIR}/.rescue-active" ]; then
    log "SKIP 救援模式进行中（.rescue-active 存在），本轮不接管"
    exit 0
fi

H=$(health)

# 刚重建过，健康检查还在跑——不是故障，别插手
if [ "${H}" = "starting" ]; then
    log "SKIP 容器启动中（health=starting），本轮不处理"
    exit 0
fi

if [ "${H}" = "healthy" ]; then
    log "OK 隧道正常（出口 $(exit_ip)）"
    exit 0
fi

log "BROKEN 隧道不通 (health=${H:-无容器})，开始恢复"

# --- 重新解析域名，拿当前有效 IP ---
HOST=$(cat "${HOSTFILE}" 2>/dev/null || true)
if [ -z "${HOST}" ]; then
    log "WARN 缺少 ${HOSTFILE}，跳过 IP 更新，仅重建容器"
else
    NEW=$(${DIG} +short "${HOST}" A 2>/dev/null | head -1)
    CUR=$(/usr/bin/grep -m1 '^remote ' "${OVPN}" 2>/dev/null | /usr/bin/awk '{print $2}')
    if [ -z "${NEW}" ]; then
        log "WARN 解析 ${HOST} 失败，沿用旧 IP ${CUR}"
    elif [ "${NEW}" = "${CUR}" ]; then
        log "IP 未变 (${CUR})，判断为隧道本身故障，仅重建"
    else
        cp "${OVPN}" "${OVPN}.prev"
        /usr/bin/sed -i '' "s/^remote .* 1195/remote ${NEW} 1195/" "${OVPN}"
        log "IP 轮换 ${CUR} -> ${NEW}，配置已更新（旧文件存为 .prev）"
    fi
fi

# --- 重建。qbit-vpn 共用 gluetun 的 network namespace，必须一起重建；
#     只重建 gluetun 会让 qbit 永久失去网络。---
cd "${DIR}" || { log "ERROR 进不去 ${DIR}"; exit 1; }
if ! ${DOCKER} compose up -d --force-recreate >>"${LOG}" 2>&1; then
    log "ERROR compose 重建失败"
    exit 1
fi
log "容器已重建，等待隧道建立"

for i in $(seq 1 24); do
    sleep 5
    if [ "$(health)" = "healthy" ]; then
        log "RECOVERED 隧道恢复，出口 IP=$(exit_ip)（耗时 $((i * 5))s）"
        exit 0
    fi
done

log "FAILED 重建后 120s 仍未恢复，需人工介入"
exit 1
