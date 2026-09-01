#!/usr/bin/env python3
"""BT 救援模式：把 qBittorrent 临时切到自有 VPS 的 WireGuard 隧道。

为什么需要：ExpressVPN 不提供端口转发，qBittorrent 长期处于 firewalled 状态——
只能主动拨出、无法接听。而 swarm 里多数 peer 同样在 NAT 后面，两边都只能拨号
就永远握不上手。实测过 51 个做种者一个都连不上、种子卡在 8% 两天不动。
自有 VPS 上做了 6881 的 DNAT，补的正是"能接听"这一环。

为什么不常驻用 VPS：机房离 peer 集中的区域远，绕路不划算；而且 VPS 有月流量
配额，还要留给别的用途。所以平时走 ExpressVPN，只在有种子真的卡住时临时切过去。

**切换前必须暂停已完成的种子**。库里有约 249 GB 的上传积压（411 个完成种子里
七成分享率不到 0.1）。一旦可被连入又不暂停，这些会立刻涌向 VPS，
几小时就能烧穿配额——那才是真正的风险，不是被救的那几个种子的下载量。

用法:
    rescue.py status         看当前模式
    rescue.py start          进入救援模式（暂停做种 + 切到 VPS）
    rescue.py stop           退出，切回 ExpressVPN 并恢复做种
    rescue.py auto [小时]    start，等未完成种子下完（或超时）后自动 stop
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# media-agent 的位置：默认 ~/media-agent，可用 MEDIA_AGENT_HOME 覆盖
sys.path.insert(0, os.environ.get(
    "MEDIA_AGENT_HOME", str(Path.home() / "media-agent")))
from media_agent.clients import QBitClient          # noqa: E402
from media_agent.config import load_config          # noqa: E402

DIR = Path.home() / "gluetun"
DOCKER = os.environ.get("DOCKER_BIN", "/usr/local/bin/docker")
STATE = DIR / ".rescue-state.json"
# 看门狗靠这个文件避让。它每 6 小时跑一次，若在救援期间发现 gluetun 不健康，
# 会用**默认** compose 重建容器，等于把救援模式踢掉。
MARKER = DIR / ".rescue-active"
GLUETUN = "gluetun-expressvpn"


def expected_exit_ip() -> str:
    """救援隧道的对端 IP，从 `.env.vps` 的 WIREGUARD_ENDPOINT_IP 读。

    以前是写死在 print 里的。这个文件要进公开仓库，基础设施地址不该跟着走——
    它本来就已经在 `.env.vps` 里了（那是 gluetun 自己要用的），没必要抄第二份。
    """
    env = DIR / ".env.vps"
    if not env.exists():
        return ""
    for line in env.read_text(encoding="utf-8").splitlines():
        k, _, v = line.partition("=")
        if k.strip() == "WIREGUARD_ENDPOINT_IP":
            return v.strip()
    return ""


def sh(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=DIR, capture_output=True, text=True, **kw)


def health() -> str:
    r = sh([DOCKER, "inspect", "--format", "{{.State.Health.Status}}", GLUETUN])
    return r.stdout.strip() or "无容器"


def exit_ip() -> str:
    """从 gluetun 自己的日志读出口 IP。不发网络请求——隧道断时任何 exec 探测都会卡死。"""
    r = sh([DOCKER, "logs", "--tail", "400", GLUETUN])
    hits = [ln for ln in (r.stdout + r.stderr).splitlines() if "Public IP address is" in ln]
    return hits[-1].split("Public IP address is", 1)[1].split()[0] if hits else "?"


def compose(vps: bool) -> subprocess.CompletedProcess:
    cmd = [DOCKER, "compose", "--env-file", ".env"]
    if vps:
        cmd += ["--env-file", ".env.vps",
                "-f", "docker-compose.yml", "-f", "docker-compose.vps.yml"]
    cmd += ["up", "-d", "--force-recreate"]
    return sh(cmd, timeout=180)


def wait_healthy(limit: int = 150) -> bool:
    for _ in range(limit // 5):
        time.sleep(5)
        if health() == "healthy":
            return True
    return False


def qbit() -> QBitClient:
    cfg = load_config()
    return QBitClient(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass)


RATIO_KEYS = ("max_ratio_enabled", "max_ratio", "max_ratio_act")


def get_ratio_prefs(q) -> dict:
    p = q._get("app/preferences")
    return {k: p.get(k) for k in RATIO_KEYS}


def set_ratio_prefs(q, prefs: dict) -> None:
    q._post("app/setPreferences", {"json": json.dumps(prefs)})


def apply_rescue_ratio(q) -> dict:
    """救援期间限制分享率 1.0，返回原设置供退出时还原。

    出发点是 VPS 的流量是要花钱的，而 ExpressVPN 的带宽不值钱。
    平时不设上限、随便做种；一旦走自有 VPS，做到 1.0 就停，别把月流量喂光。

    `max_ratio_act=0` 是"达标即暂停"。被它暂停的种子不在 `stopped` 名单里
    （那是我们自己按下的），所以退出时要靠"现在停着但进入救援前没停"来兜底，
    否则这批种子会在救援结束后继续停着，没人叫醒。
    """
    prev = get_ratio_prefs(q)
    set_ratio_prefs(q, {"max_ratio_enabled": True, "max_ratio": 1.0,
                        "max_ratio_act": 0})
    print(f"    分享率上限 1.0 已启用（原设置 {prev} 已记下，退出时还原）")
    return prev


def stopped_hashes(q) -> set:
    return {t["hash"] for t in q.torrents()
            if t["state"].startswith(("paused", "stopped"))}


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"mode": "expressvpn", "stopped": []}


def save_state(st: dict) -> None:
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_status() -> int:
    st = load_state()
    q = qbit()
    info = q._get("transfer/info")
    ts = q.torrents()
    inc = [t for t in ts if t.get("progress", 0) < 1.0]
    print(f"模式        : {st['mode']}" + ("（救援中）" if st["mode"] == "vps" else ""))
    print(f"gluetun     : {health()}   出口 {exit_ip()}")
    print(f"qBit 连接   : {info.get('connection_status')}   DHT {info.get('dht_nodes')}")
    rp = get_ratio_prefs(q)
    pol = (f"上限 {rp['max_ratio']}（达标即暂停）" if rp.get("max_ratio_enabled")
           else "不限制")
    print(f"分享率策略  : {pol}")
    print(f"速度        : 下载 {info.get('dl_info_speed',0)/1024:.0f} KB/s  "
          f"上传 {info.get('up_info_speed',0)/1024:.0f} KB/s")
    print(f"未完成      : {len(inc)} 个")
    for t in sorted(inc, key=lambda x: x.get("progress", 0)):
        print(f"   {t['progress']*100:5.1f}%  {t['dlspeed']/1024:6.1f}KB/s  "
              f"seeds={t.get('num_seeds')}/{t.get('num_complete')}  {t['name'][:46]}")
    if st["stopped"]:
        print(f"已为救援暂停 : {len(st['stopped'])} 个做种种子")
    return 0


def cmd_start() -> int:
    st = load_state()
    if st["mode"] == "vps":
        print("已经在救援模式了，无需重复进入")
        return 0

    q = qbit()
    pre_stopped = stopped_hashes(q)      # 必须在我们动手暂停之前取
    # 只记录"当前确实在做种"的完成种子。已被用户或分享率上限暂停的不碰，
    # 否则退出救援时会把它们错误地叫醒。
    active = [t for t in q.torrents()
              if t.get("progress", 0) >= 1.0 and not t["state"].startswith(("paused", "stopped"))]
    hashes = [t["hash"] for t in active]
    print(f"1/4 暂停做种中的完成种子: {len(hashes)} 个（挡住约 249 GB 上传积压）")
    if hashes:
        for i in range(0, len(hashes), 100):        # 分批，避免超长 URL
            q._post("torrents/stop", {"hashes": "|".join(hashes[i:i + 100])})

    st = {"mode": "vps", "stopped": hashes, "started_at": int(time.time()),
          # 进入救援**前**就已停着的种子（用户自己停的）。退出时不碰它们，
          # 其余"现在停着"的都是救援期间新停的——我们按的或分享率规则按的——都要叫醒。
          "stopped_before": sorted(pre_stopped),
          "ratio_prefs": apply_rescue_ratio(q)}
    save_state(st)
    MARKER.write_text("rescue mode active — vpn-watchdog 请勿接管\n", encoding="utf-8")

    print("2/4 切换 gluetun -> WireGuard(VPS)")
    r = compose(vps=True)
    if r.returncode != 0:
        print("!! compose 失败，回滚"); print(r.stderr[-500:])
        return cmd_stop()

    print("3/4 等待隧道建立")
    if not wait_healthy():
        print("!! 隧道 150 秒未健康，回滚"); return cmd_stop()

    want = expected_exit_ip()
    print(f"4/4 就绪。出口 IP = {exit_ip()}" + (f"（应为 {want}）" if want else ""))
    time.sleep(10)
    info = qbit()._get("transfer/info")
    print(f"    qBit 连接状态 = {info.get('connection_status')}"
          f"（期望 connected，不再是 firewalled）")
    return 0


def cmd_stop() -> int:
    st = load_state()
    print("1/3 切回 ExpressVPN")
    r = compose(vps=False)
    if r.returncode != 0:
        print("!! compose 失败"); print(r.stderr[-500:]); return 1

    print("2/3 等待隧道建立")
    ok = wait_healthy()
    print(f"    {'健康' if ok else '⚠️ 未在 150 秒内健康，需人工检查'}，出口 {exit_ip()}")

    q = qbit()
    prev = st.get("ratio_prefs")
    if prev and all(v is not None for v in prev.values()):
        set_ratio_prefs(q, prev)
        print(f"3/4 还原分享率设置 -> {prev}")
    else:
        set_ratio_prefs(q, {"max_ratio_enabled": False})
        print("3/4 关闭分享率上限（未记到原设置，按常态=不限制处理）")

    # 该叫醒谁：现在停着、但进入救援前没停的。这样既包含我们按下的那批，
    # 也包含救援期间因达到分享率 1.0 被 qBittorrent 自己暂停的那批；
    # 用户自己停的（进入前就停着）不动。
    pre = set(st.get("stopped_before") or [])
    now_stopped = stopped_hashes(q)
    wake = sorted((now_stopped - pre) | (set(st.get("stopped") or []) - pre))
    print(f"4/4 恢复 {len(wake)} 个种子（我们暂停的 + 分享率达标被停的）")
    for i in range(0, len(wake), 100):
        q._post("torrents/start", {"hashes": "|".join(wake[i:i + 100])})

    save_state({"mode": "expressvpn", "stopped": []})
    MARKER.unlink(missing_ok=True)
    return 0


def cmd_auto(hours: float = 6.0) -> int:
    if cmd_start() != 0:
        return 1
    deadline = time.time() + hours * 3600
    print(f"\n进入等待，最长 {hours} 小时。每 60 秒检查一次。")
    while time.time() < deadline:
        time.sleep(60)
        q = qbit()
        inc = [t for t in q.torrents() if t.get("progress", 0) < 1.0]
        info = q._get("transfer/info")
        left = (deadline - time.time()) / 60
        print(f"  未完成 {len(inc)} 个  下载 {info.get('dl_info_speed',0)/1024:6.0f} KB/s  "
              f"剩余 {left:.0f} 分钟")
        if not inc:
            print("全部下载完成")
            break
    else:
        print("已达时限，无论是否下完都退出救援模式")
    return cmd_stop()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        sys.exit(cmd_status())
    elif cmd == "start":
        sys.exit(cmd_start())
    elif cmd == "stop":
        sys.exit(cmd_stop())
    elif cmd == "auto":
        sys.exit(cmd_auto(float(sys.argv[2]) if len(sys.argv) > 2 else 6.0))
    else:
        print(__doc__)
        sys.exit(2)
