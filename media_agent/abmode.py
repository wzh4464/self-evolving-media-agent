"""AutoBangumi 的模式：`full`（AB 拉 RSS、下载、改名——一直以来的样子）或 `subscription`（AB 只当订阅的前端）。

| 模式 | AB 做什么 | 本项目怎么对待 AB |
|---|---|---|
| `full`（默认） | 每 15 分钟拉 RSS、下载，60 秒改一次名 | `Bangumi` 分类归 AB（判重让位）、AB 专用的规则照常 |
| `subscription` | 只在 WebUI / 接口订阅的那一刻把已发布的集补进 `Bangumi` 分类；不拉 RSS、不改名 | 抓取与改名全归本项目 |

为什么不整个关掉 AB：用户**通过 AB 订阅**（critic §4：`autobangumi-subscribe-verify` 流程，35 条订阅），订阅之后的一切
本项目都接住了（`ab-adoption` 建目录、登记订阅、迁偏移；30 分钟的 `media-agent grab`；新季自动登记）。关的只是 AB 的
两个后台线程（ab 调研 §3.1 / §3.2）：

- `rss_parser.enable` → RSS 线程（拉 RSS、下载、聚合 feed 建订阅、`eps_complete`）；
- `bangumi_manage.enable` → 改名线程（含把多文件种子改到 `BangumiCollection`）。

两个开关只在 AB 的 `Program.start()` 时生效（循环从不回头看开关）：`media-agent ab-mode subscription|full` 读整份配置
（`GET /api/v1/config/get`）→ 只改这两个布尔 → 发回**整份**（`PATCH /api/v1/config/update`；漏掉的段 AB 会退回默认值）→
`GET /api/v1/restart`（程序重启，WebUI 不停）→ 等它回来、读回开关核对 → 记下 `state/ab_mode.json`（含核对用的基线）。
经执行器做（动作 `set_ab_mode`）：有审计、逆操作是改回原来的开关，`media-agent rollback` 能退；最直接的回退是
`media-agent ab-mode full`。

**本项目认的模式从哪来（优先级从高到低）**：`state/ab_mode.json`（`ab-mode` 切换成功时写）> `.env` 的 `AB_MODE` > `full`。
`.env` 代码不写（部署按 git tag，`.env` 是人的）；状态文件坏了 `load_config` 大声失败——静默当成哪一种都可能错：当成
`full` 会让已经停了改名的 AB 继续"拥有" `Bangumi` 分类（新订阅补的集永远没人改名），当成 `subscription` 会在 AB 还在改名
时抢它的文件。

切成 `subscription` 之后 AB 真停了没有，`/api/v1/status` 说不出来（`_tasks_started` 不管起了哪几个线程都置真）。
所以切换时记下每个 rssitem 的 `last_checked_at` 与已有的订阅 id，之后每轮的健康报告核对：还在拉 RSS、或在订阅动作之外
加了种子，都报出来（`health`，本模块的 `activity`）。
"""
from __future__ import annotations

import copy
import json
import os
import re
import time
from pathlib import Path

FULL, SUBSCRIPTION = "full", "subscription"
MODES = (FULL, SUBSCRIPTION)
STATE_NAME = "ab_mode.json"

# AB 配置里的两个开关（段名.键名）
FLAG_KEYS = ("rss_parser.enable", "bangumi_manage.enable")

# 重启：请求本身等多久（`Program.start()` 先等下载器，最多 10 × 30 秒，请求多半会超时——超时不等于没重启），
# 之后按 `POLL_S` 问 `status` 最多等多久。AB 3.2.6 等下载器的上限是 300 秒，多留一分钟
RESTART_REQUEST_TIMEOUT_S = 30.0
RESTART_TIMEOUT_S = 360.0
POLL_S = 3.0


def parse(value: str, where: str) -> str:
    """`full` / `subscription`；写错了大声失败（理由同 `config._evolve_mode`）。"""
    mode = str(value or "").strip().lower()
    if mode not in MODES:
        raise ValueError(f"{where} 只能是 {' / '.join(MODES)}，收到 {value!r}")
    return mode


def ab_renames(cfg) -> bool:
    """AB 还在改名（`full`）：`Bangumi` 分类里的文件此刻叫什么只是"AB 认为的"，名字的最终裁量权在 AB。"""
    return getattr(cfg, "ab_mode", FULL) != SUBSCRIPTION


def ab_downloads(cfg) -> bool:
    """AB 还在拉 RSS、下载（`full`）。只为 AB 存在的规则（`orphan-torrent` / `missing-ab-tag` / `title-match-broken` /
    `source-abandoned`，ab 调研 §2.1）按它开关——订阅模式下它们修的东西 AB 不再用；`full` 照旧（回退的路）。"""
    return getattr(cfg, "ab_mode", FULL) != SUBSCRIPTION


# ---------------------------------------------------------------- 状态文件
def state_path(state_dir) -> Path:
    return Path(state_dir) / STATE_NAME


def read_state(state_dir) -> dict | None:
    """`state/ab_mode.json`；没有返回 None。读不了、不是 JSON 对象、模式不对：`ValueError`（`load_config` 大声失败）。"""
    p = state_path(state_dir)
    if not p.exists():
        return None
    fix = f"——删掉它回到 AB_MODE（默认 full），再用 media-agent ab-mode show 核对 AB 的开关"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"{p} 读不了（{type(e).__name__}: {e}）{fix}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{p} 不是 JSON 对象{fix}")
    data["mode"] = parse(data.get("mode", ""), f"{p} 的 mode")
    return data


def write_state(state_dir, record: dict) -> None:
    """原子写（先写临时文件再 `os.replace`）。出错抛 `OSError`——调用方要说出来：AB 切了、本项目却没记下。"""
    p = state_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def remove_state(state_dir) -> None:
    try:
        state_path(state_dir).unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- AB 的开关
def flags_for(mode: str) -> dict:
    on = parse(mode, "mode") == FULL
    return {k: on for k in FLAG_KEYS}


def flags_of(config) -> dict | None:
    """整份配置里的两个开关 `{"rss_parser.enable": bool, "bangumi_manage.enable": bool}`；形状不对（版本不同？）返回 None。"""
    if not isinstance(config, dict):
        return None
    out = {}
    for key in FLAG_KEYS:
        section, name = key.split(".")
        v = (config.get(section) or {}).get(name) if isinstance(config.get(section), dict) else None
        if not isinstance(v, bool):
            return None
        out[key] = v
    return out


def mode_of(flags: dict | None) -> str | None:
    """两个都开 = `full`，两个都关 = `subscription`，一开一关 / 读不到 = None。"""
    if not flags:
        return None
    vals = set(flags.values())
    return None if len(vals) != 1 else (FULL if vals == {True} else SUBSCRIPTION)


def describe_flags(flags: dict | None) -> str:
    if not flags:
        return "读不到"
    return "  ".join(f"{k}={'true' if v else 'false'}" for k, v in flags.items())


def with_flags(config: dict, flags: dict) -> dict:
    """整份配置的副本，只改两个开关。其余一个字节不动（打码的密码原样发回，AB 按现值还原）。"""
    new = copy.deepcopy(config)
    for key, v in flags.items():
        section, name = key.split(".")
        new[section][name] = bool(v)
    return new


def read_config_file(path) -> dict | None:
    """只读 AB 的 config.json（`AB_CONFIG`，接口连不上时看一眼开关用）；读不了返回 None。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None


def read_ab_flags(ctx) -> tuple[dict | None, dict | None, str]:
    """AB 此刻的两个开关：`(开关, 整份配置, 从哪读的)`。先问接口；接口不可用再只读 config.json（`AB_CONFIG`），那时整份
    配置为 None（只读来的不拿去写）。都读不到：开关为 None，第三项说为什么。"""
    why = []
    if ctx.ab is not None:
        try:
            config = ctx.ab.get_config()
            flags = flags_of(config)
            if flags is not None:
                return flags, config, "接口"
            why.append("接口给的配置里没有 rss_parser.enable / bangumi_manage.enable（AB 版本不同？）")
        except Exception as e:                      # noqa: BLE001 —— 读不到就退回只读文件，原因照实说
            why.append(f"接口出错（{type(e).__name__}: {e}）")
    else:
        why.append(f"接口不可用（{(getattr(ctx, 'client_status', None) or {}).get('ab') or '未登录'}）")
    path = getattr(ctx.config, "ab_config", None)
    if path:
        flags = flags_of(read_config_file(path))
        if flags is not None:
            return flags, None, f"只读 {path}"
        why.append(f"{path} 读不了或认不出")
    else:
        why.append("没配 AB_CONFIG（AB 的 config.json 在宿主上的路径）")
    return None, None, "；".join(why)


def restart_and_wait(ab, want: dict, *, timeout: float | None = None, poll: float | None = None,
                     clock=time.monotonic, sleep=time.sleep) -> tuple[bool, str]:
    """`GET /api/v1/restart`，然后等 AB 回来（`status` 为真）并读回开关 == `want`。返回 `(核对上了, 说明)`。

    重启请求超时是常态（`Program.start()` 先等下载器），接着问；请求根本没发出去 / 被拒（连不上、5xx）就不等了：AB 也许没
    重启，config.json 里的新开关要到下一次重启才生效——说不清。"""
    import httpx

    timeout = RESTART_TIMEOUT_S if timeout is None else timeout
    poll = POLL_S if poll is None else poll
    note = ""
    try:
        ab.restart(timeout=RESTART_REQUEST_TIMEOUT_S)
    except httpx.TimeoutException:
        note = "重启请求超时（AB 在等下载器？），接着等它回来；"
    except Exception as e:                          # noqa: BLE001 —— 不知道重启了没有：交给调用方记 unknown
        return False, (f"重启请求出错（{type(e).__name__}: {e}）：AB 也许没有重启，config.json 里的新开关要到下一次重启"
                       f"才生效")
    deadline = clock() + timeout
    last = ""
    while True:
        try:
            if bool((ab.status() or {}).get("status")):
                seen = flags_of(ab.get_config())
                if seen == want:
                    return True, note + "AB 回来了，读回的开关对得上"
                return False, note + f"AB 回来了，但读回的开关是 {describe_flags(seen)}（有别人同时改了配置？）"
            last = "AB 程序还没起来（status=false）"
        except Exception as e:                      # noqa: BLE001 —— 还没起来 / 接口抖：到时限之前接着问
            last = f"问 AB 出错（{type(e).__name__}: {e}）"
        if clock() >= deadline:
            return False, note + f"等了 {timeout:g} 秒还没核对上：{last}"
        sleep(poll)


# ---------------------------------------------------------------- 核对用的基线
def baseline(abdb) -> tuple[dict | None, str]:
    """切到 `subscription` 那一刻 AB 库里的样子：每个 rssitem 的 `last_checked_at`（之后还动 = 还在拉 RSS）与已有的订阅 id
    （之后新出现的 = 人订的，它订阅那一刻补的集不算"AB 自己加的"）。读不了返回 `(None, 原因)`。"""
    if abdb is None:
        return None, "没有 AB 库（AB_DB）：健康报告核对不了 AB 之后还拉不拉 RSS"
    try:
        items = abdb.rss_items()
        rows = abdb.bangumi(include_deleted=True)
    except Exception as e:                          # noqa: BLE001 —— 基线是附加的核对，读不了照实说
        return None, f"读 AB 库出错（{type(e).__name__}: {e}）：健康报告核对不了 AB 之后还拉不拉 RSS"
    return ({"rss_last_checked": {str(r.get("id")): r.get("last_checked_at") for r in items},
             "bangumi_ids": sorted(int(r["id"]) for r in rows if r.get("id") is not None)}, "")


# ---------------------------------------------------------------- 之后的核对（健康报告、ab-mode show）
# 一次订阅（`/rss/subscribe`）当场把 feed 里已发布的集一口气加进 qBittorrent（ab 调研 §3.4）。这一批落在同一个保存路径、
# 几秒到几分钟之内；按新订阅的保存路径、从它第一个种子起这么久之内的算"订阅带来的"
SUBSCRIBE_WINDOW_S = 3600
AB_CATEGORIES = ("Bangumi", "BangumiCollection")
_AB_TAG = re.compile(r"(?:^|,)\s*ab:(\d+)\s*(?:,|$)")


def _rel(path, media_root) -> tuple:
    """`<媒体根目录名>/…` 之后的几段（AB 在容器里看到的挂载点可以与宿主不同，与 `abrow.show_dir_name` 同一个口径）。"""
    parts = Path(str(path or "")).parts
    root = Path(media_root).name
    if root not in parts:
        return ()
    return tuple(parts[parts.index(root) + 1:])


def activity(record: dict | None, *, rss_rows, bangumi_rows, torrents, media_root) -> dict:
    """切到 `subscription` 之后 AB 还做了什么（`record` 是 `state/ab_mode.json`）：

    - `polled`：`last_checked_at` 与切换时的基线不同的 rssitem（基线之后新加的、`last_checked_at` 有值的也算）——
      AB 的 `refresh_rss` 每拉一次就写它：RSS 线程还在跑（没重启 / 开关被改回），或有人点了刷新（`/rss/refresh`）；
    - `adds`：切换之后加进 qBittorrent、看得出是 AB 加的（`Bangumi` / `BangumiCollection` 分类、或 `ab:<id>` 标签；钉着
      `ma:` 的是本项目的）种子，分成 `subscribe`（落在切换之后新出现的订阅的保存路径下、在它那一批的
      `SUBSCRIBE_WINDOW_S` 之内）与 `outside`（其余：RSS 线程、刷新、对老订阅点了「收集」……）。

    这是**启发式**：交接（`category-consolidation` 把分类改成剧名）之后的订阅补集既不在 `Bangumi` 分类、也没有 `ab:`
    标签（新订阅那一刻还没有 id），就不再算进来——它本来就不是问题；带 `ab:` 标签的交接之后照样认得出。没有基线（模式
    来自 `AB_MODE`、没经 `ab-mode` 切过）什么都核对不了，`baseline` 为 False。"""
    from .naming import parse_pin

    base = (record or {}).get("baseline")
    out = {"baseline": bool(base), "since": (record or {}).get("since"), "polled": [],
           "adds": {"subscribe": [], "outside": []}}
    if not base:
        return out
    last = base.get("rss_last_checked") or {}
    # 基线里有 rssitem、这一轮一条都没读到：多半是 AB 库读不了（扫描只记一行日志）——没拉过与看不见分不开
    out["rss_unread"] = bool(last) and not rss_rows
    for r in rss_rows or []:
        rid, now = str(r.get("id")), r.get("last_checked_at")
        if not now:
            continue
        if rid in last and now == last[rid]:
            continue
        out["polled"].append({"id": r.get("id"), "name": str(r.get("name") or "")[:60],
                              "before": last.get(rid), "now": now})
    since = float((record or {}).get("switched_at_epoch") or 0)
    known = {int(x) for x in base.get("bangumi_ids") or []}
    new_subs = {}
    for row in bangumi_rows or []:
        if row.get("id") is None or int(row["id"]) in known:
            continue
        rel = _rel(row.get("save_path"), media_root)
        if rel:
            new_subs[rel] = int(row["id"])
    cands = []
    for t in torrents or []:
        tags = str(t.get("tags") or "")
        if parse_pin(tags) or float(t.get("added_on") or 0) <= since:
            continue
        m = _AB_TAG.search(tags)
        if (t.get("category") or "") not in AB_CATEGORIES and not m:
            continue
        rel = _rel(t.get("save_path"), media_root)
        bid = int(m.group(1)) if m else None
        sub = new_subs.get(rel) if bid is None else (bid if bid not in known else None)
        cands.append((t, sub))
    first: dict[int, float] = {}
    for t, sub in cands:
        if sub is not None:
            first[sub] = min(first.get(sub, float("inf")), float(t.get("added_on") or 0))
    for t, sub in sorted(cands, key=lambda x: float(x[0].get("added_on") or 0)):
        item = {"hash": str(t.get("hash") or "")[:8], "name": str(t.get("name") or "")[:80],
                "category": t.get("category") or "", "tags": str(t.get("tags") or ""),
                "added_on": t.get("added_on"), "subscription": sub}
        ok = sub is not None and float(t.get("added_on") or 0) - first[sub] <= SUBSCRIBE_WINDOW_S
        out["adds"]["subscribe" if ok else "outside"].append(item)
    return out


def activity_problems(act: dict) -> list[dict]:
    """`activity` 里要人看的，每条 `{code, text}`（健康报告的原因与 `ab-mode show` 共用）：

    - `ab_mode_unverified`：没有切换基线（模式来自 `AB_MODE`、没经命令切过），或这一轮读不到 AB 库的 rssitem；
    - `ab_still_polling`：切换之后 AB 拉过 RSS；
    - `ab_added_outside_subscribe`：AB 在订阅动作之外加了种子。"""
    out = []
    if not act.get("baseline"):
        out.append({"code": "ab_mode_unverified",
                    "text": "订阅模式没有切换基线（模式来自 AB_MODE、没经 media-agent ab-mode 切过）：AB 的开关关没关、之后还拉"
                            "不拉 RSS 都核对不了——用 media-agent ab-mode subscription 切一次（开关已关的只重启、核对、记基线）"})
        return out
    if act.get("rss_unread"):
        out.append({"code": "ab_mode_unverified",
                    "text": "这一轮读不到 AB 库的 rssitem（AB_DB 读不了？）：切到 subscription 之后 AB 还拉不拉 RSS 核对不了"})
    polled = act.get("polled") or []
    if polled:
        names = "、".join(f"{p['id']} {p['name']}".strip() for p in polled[:3])
        out.append({"code": "ab_still_polling",
                    "text": f"切到 subscription（{act.get('since')}）之后 AB 拉过 RSS：{len(polled)} 个 rssitem 的 "
                            f"last_checked_at 变了（{names}{' 等' if len(polled) > 3 else ''}）——RSS 线程还在跑（没重启 / "
                            f"开关被改回？）或有人点了刷新（刷新也会让它下载）；media-agent ab-mode show 核对"})
    outside = (act.get("adds") or {}).get("outside") or []
    if outside:
        names = "、".join(f"{x['hash']} {x['name'][:40]}" for x in outside[:3])
        out.append({"code": "ab_added_outside_subscribe",
                    "text": f"AB 在订阅动作之外加了 {len(outside)} 个种子（{names}{' 等' if len(outside) > 3 else ''}）："
                            f"RSS 线程 / 刷新 / 对老订阅点了「收集」——订阅模式下订阅之外它不该再下载"})
    return out


def activity_lines(act: dict) -> list[str]:
    """`ab-mode show` 的几行。"""
    problems = activity_problems(act)
    lines = [f"  ⚠️  {p['text']}" for p in problems]
    if act.get("baseline"):
        if not act.get("polled"):
            lines.append(f"  ✓ 切到 subscription（{act.get('since')}）之后 AB 没再拉过 RSS")
        sub = (act.get("adds") or {}).get("subscribe") or []
        if sub:
            lines.append(f"  · 订阅那一刻 AB 补的集：{len(sub)} 个（新订阅，正常）")
        if not (act.get("adds") or {}).get("outside"):
            lines.append("  ✓ 订阅动作之外 AB 没加过种子")
    return lines
