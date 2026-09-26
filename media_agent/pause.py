"""维护暂停（critic N17）：`run` / `apply`（以后的抓取模式同样）在这些时候一开始就以 75 结束、什么都不做。

1. **VPN 救援进行中**：`deploy/rescue.py start` 写下的 `~/gluetun/.rescue-active`（`rescue.py` 的 `MARKER`；
   `vpn-watchdog.sh` 看的也是它），`stop` 时删掉。救援期间 qBittorrent 容器被重建过、做种被暂停、分享率上限改成
   1.0、流量走按量计费的 VPS——一轮 run 在这时候读 qBit、摘种子、抓新的，都不该发生。路径可用 `RESCUE_MARKER` 改。
2. **`state/PAUSE`**：人手动放的（`touch state/PAUSE`，里面可以写一句为什么），删掉就恢复。

`diagnose` / `scan` / `health` / `runs` 照常；人手动的 `rollback` / `repair` / `purge --apply` / `evolve` 也不拦——维护
期间正是人在操作。退出码与"运行锁被占"相同（75，EX_TEMPFAIL："被挡住了，下轮再来"）。暂停的 `run` 照样写健康报告
（warn），忘了删的 `state/PAUSE` 会让通知发一封、健康报告一直是 warn，而不是让 agent 悄悄停摆。

暂停**不拿运行锁**就返回：救援脚本重建容器时自己拿着那把锁（`deploy/rescue.py`、`deploy/vpn-watchdog.sh`）。
"""
from __future__ import annotations

import time
from pathlib import Path

PAUSE_NAME = "PAUSE"


def _age(p: Path) -> str:
    try:
        hours = (time.time() - p.stat().st_mtime) / 3600
    except OSError:
        return "时间未知"
    return f"已 {hours:.1f} 小时"


def _first_line(p: Path) -> str:
    try:
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if ln.strip():
                return ln.strip()[:200]
    except OSError:
        pass                                        # 读不了内容不影响"暂停"本身：文件在就暂停
    return ""


def reason(cfg) -> str:
    """此刻为什么该暂停；不该暂停返回空串。"""
    marker = getattr(cfg, "rescue_marker", None)
    if marker and Path(marker).exists():
        note = _first_line(Path(marker))
        return (f"VPN 救援模式进行中（{marker} 存在，{_age(Path(marker))}"
                + (f"：{note}" if note else "") + "）——`rescue.py stop` 之后自动恢复")
    p = Path(cfg.state_dir) / PAUSE_NAME
    if p.exists():
        note = _first_line(p)
        return (f"{p} 存在（{_age(p)}" + (f"：{note}" if note else "") + "）——删掉它就恢复")
    return ""
