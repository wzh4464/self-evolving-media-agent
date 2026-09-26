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
# 重建之前先拿 media-agent 的运行锁（state/run.lock，与 media-agent、deploy.sh 同一把 flock），
# 最多等 RUNLOCK_WAIT 秒（默认 900；一轮 run 约 2 分钟）：qBittorrent 与 gluetun 共用网络命名空间，
# 一起被重建——在一轮 run 中途消失，扫描读到一半、改名改到一半（critic N17）。等不到就这一轮不重建、
# 退出码 75，下一轮（6 小时后）再来。拿锁的办法是在锁里把本脚本重新跑一遍（MA_RUNLOCK_HELD=1）：
# 等锁的那几分钟里隧道可能自己好了，重跑会先重新看健康状态。
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

DOCKER="${DOCKER_BIN:-/usr/local/bin/docker}"
DIG=/usr/bin/dig
DIR="${HOME}/gluetun"
RUNLOCK="${MEDIA_AGENT_HOME:-${HOME}/media-agent}/state/run.lock"
RUNLOCK_WAIT="${RUNLOCK_WAIT:-900}"
POLL="${WATCHDOG_POLL:-5}"          # 重建后查健康的间隔（秒）；测试里设 0
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

# with_runlock <锁文件> <最多等几秒> <命令...>：拿不到返回 75。与 media_agent/runlock.py、deploy.sh 的
# with_lock 是同一种锁（flock）；锁文件一律保留（删了它，下一个按路径打开的人会拿到另一把锁）。
with_runlock() {
    local file=$1 wait=$2
    shift 2
    if [ -x /usr/bin/lockf ]; then                          # macOS（生产）
        /usr/bin/lockf -k -s -t "${wait}" "${file}" "$@"
    elif command -v flock >/dev/null 2>&1; then             # Linux
        ( exec 9>>"${file}" || exit 75; flock -w "${wait}" 9 || exit 75; "$@" )
    else
        python3 -c '
import fcntl, os, subprocess, sys, time
path, wait, cmd = sys.argv[1], float(sys.argv[2]), sys.argv[3:]
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
end = time.time() + wait
while True:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except OSError:
        if time.time() >= end:
            sys.exit(75)
        time.sleep(0.5)
sys.exit(subprocess.call(cmd))
' "${file}" "${wait}" "$@"
    fi
}

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

# --- 重建会连 qBittorrent 一起拆掉：先拿 media-agent 的运行锁（见文件头）---
if [ -z "${MA_RUNLOCK_HELD:-}" ]; then
    if [ -d "$(dirname "${RUNLOCK}")" ]; then
        log "WAIT 先拿 media-agent 的运行锁（最多 ${RUNLOCK_WAIT}s），免得在一轮 run 中途重建 qBittorrent"
        MA_RUNLOCK_HELD=1 with_runlock "${RUNLOCK}" "${RUNLOCK_WAIT}" "${BASH:-/bin/bash}" "$0" "$@"
        rc=$?
        if [ "${rc}" -eq 75 ]; then
            log "SKIP media-agent 持有运行锁超过 ${RUNLOCK_WAIT}s（$(cat "${RUNLOCK}" 2>/dev/null)），这一轮不重建，下轮再来"
        fi
        exit "${rc}"
    fi
    log "WARN 找不到 media-agent 的 state 目录（${RUNLOCK} 所在），不拿锁直接重建"
fi

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
    sleep "${POLL}"
    if [ "$(health)" = "healthy" ]; then
        log "RECOVERED 隧道恢复，出口 IP=$(exit_ip)（耗时 $((i * POLL))s）"
        exit 0
    fi
done

log "FAILED 重建后 120s 仍未恢复，需人工介入"
exit 1
