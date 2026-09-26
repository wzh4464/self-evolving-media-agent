"""补录出处账本：给 qBittorrent 里还没有出处的种子找回它是什么。

`media-agent ledger backfill [--dry-run]` 人手跑一遍全量；每轮 `run` 开头自动跑一次增量（只看还没有一行的、
以及来源还是 `unknown` 的种子）。账本开张时生产上 539 个种子一行都没有（state 调研 H4）。

来源按可信程度：

0. **本项目的抓取审计**（`audit.jsonl` 里的 `grab_episode`，applied / unknown）：抓取器定的集位——与 `ma:` 钉子
   同一个来源，照记为定论。URL 的文件名就是 infohash（新记录另有 `infohash` 字段）。
1. **AutoBangumi 库的 `torrent` 表**，只读打开（`AutoBangumiDB.query` 走 `mode=ro`，不停容器）：`name` 是番组页
   标题，`url` 里是 infohash（`qb_hash` 永远是 NULL），连带它的番组行（季、`episode_offset`）。
2. 还剩下的、所在的番 sidecar 里有 `mikan_id`（或订阅链接里带番组 id）的：拉那个番组页的 feed（有 1 小时缓存，
   与抓取共用），按 enclosure URL 的文件名认 infohash——不用下 .torrent。认不出的记进 `lookup_miss`，
   `ledger.MISS_TTL` 之内不再为它们拉番组页（每轮开头的自动补录不能每 6 小时把同一批番组页拉一遍）。
3. 最后只剩标签的：`ma:` 钉子（只有本项目的抓取打它，集位就是钉子）、`manual:`（人手加种时自己打的）。

集位按读账本的同一套算（`naming.title_slot`：放在种子所在的季目录里、按 sidecar 的 `season_offsets` 与抓取同一套
季内换算；集号偏移是 sidecar 的 `episode_offsets`，没登记时才用 AB 的 `episode_offset`、只对它订阅的那一季——
`builtin.episode_offset_for` 同一个口径）；钉着 `ma:` 的按钉子。换算不了（含声明了不止一个季号的）的
集位留空，声明的季号与原始集号照记。只补没有的：幂等。

**不拦路**：qBittorrent / AB 库 / 番组页 / 账本任何一样读不了，说一句（`problems`），能补的照补。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import ledger
from .kernel import under
from .naming import parse_pin, season_of_dir, title_slot


@dataclass
class BackfillReport:
    torrents: int = 0
    rows_before: int = 0
    inserted: dict[str, int] = field(default_factory=dict)       # 来源 → 补了几行（预演 = 会补几行），按最终的来源数
    upgraded: dict[str, int] = field(default_factory=dict)       # 早就有一行、来源是 unknown，这次认出了是谁加的
    covered: int = 0                                              # 补完之后有出处的种子数
    remaining: list[dict] = field(default_factory=list)           # 仍然没有出处的：{hash, name, show, added_on}
    problems: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def coverage(self) -> str:
        return f"{self.covered}/{self.torrents}"

    def summary(self) -> str:
        ins = "、".join(f"{k} {v}" for k, v in sorted(self.inserted.items())) or "0"
        up = "、".join(f"{k} {v}" for k, v in sorted(self.upgraded.items()))
        return (f"{'预演：会' if self.dry_run else ''}补录 {ins}" + (f"，认出来源 {up}" if up else "")
                + f"；有出处 {self.coverage}，"
                f"没有出处 {len(self.remaining)}"
                + (f"；{len(self.problems)} 处读不了" if self.problems else ""))

    def to_dict(self) -> dict:
        return {"torrents": self.torrents, "rows_before": self.rows_before,
                "inserted": dict(self.inserted), "upgraded": dict(self.upgraded), "covered": self.covered,
                "remaining": len(self.remaining), "problems": list(self.problems),
                "dry_run": self.dry_run}


def _where(t: dict, media_root: Path) -> tuple[Path | None, int | None]:
    """种子落在哪部番（媒体根下的第一层目录）、哪一季（`Season N` 目录）。不在媒体库里返回 (None, None)。"""
    sp = Path((t.get("save_path") or "").rstrip("/") or "/")
    if not under(sp, media_root) or sp == media_root:
        return None, None
    rel = sp.relative_to(media_root).parts
    show_dir = media_root / rel[0]
    return show_dir, (season_of_dir(rel[1]) if len(rel) > 1 else None)


class _Shows:
    """按需读每部番的 sidecar：`season_offsets`、`episode_offsets` 与番组页 id。坏档案按没有处理（说一句）。"""

    def __init__(self, problems: list[str]):
        self._cache: dict[Path, dict] = {}
        self.problems = problems

    def intent(self, show_dir: Path | None) -> dict:
        if show_dir is None:
            return {"offsets": {}, "episode_offsets": {}, "mikan": []}
        if show_dir not in self._cache:
            from . import sidecar as sc_mod
            sc, problem = sc_mod.load_checked(show_dir)
            if problem:
                self.problems.append(f"{show_dir.name} 的 sidecar 解析不了（{problem}），按没有换算关系算")
            mikan = [str(sc.mikan_id)] if (sc.mikan_id and not problem) else []
            for s in (sc.sources if not problem else []):
                m = re.search(r"bangumiId=(\d+)", (s or {}).get("rss_link") or "")
                if m and m.group(1) not in mikan:
                    mikan.append(m.group(1))
            from .plugins.builtin import _clean_offsets
            self._cache[show_dir] = {"offsets": dict(sc.season_offsets or {}) if not problem else {},
                                     "episode_offsets": _clean_offsets(sc.episode_offsets) if not problem else {},
                                     "mikan": mikan}
        return self._cache[show_dir]


def _audit_grabs(audit_log: Path) -> dict[str, dict]:
    """审计里的抓取：infohash → 最近的那条记录。

    不只认 applied：补录只针对**此刻就在 qBittorrent 里**的种子，它在，加种就发生过——2026-09-16 到 09-26 的
    12 次抓取种子加上了、之后 NameError，全记成了 failed（`tests/test_grab_bookkeeping.py` 的那次事故）；
    记录里的集位照样是抓取器定的。预演的记录不算。"""
    from . import audit as auditlog

    out: dict[str, dict] = {}
    for rec in auditlog.iter_records(audit_log):
        if rec.get("op") != "grab_episode" or rec.get("status") not in ("applied", "unknown", "failed"):
            continue
        if rec.get("dry_run"):
            continue
        args = rec.get("args") or {}
        h = (rec.get("infohash") or ledger.infohash_of_url(args.get("url") or "") or "").lower()
        if h:
            # 谁加的：只要有一次不是 409（`already_present` 不是 True；旧记录没有这个字段），就是本项目加的。
            # 全是 409 的——种子早就在，抓取器只是也选中了它：集位照记，来源不冒认
            added = out.get(h, {}).get("_added", False) or rec.get("already_present") is not True
            out[h] = {**rec, "_added": added}
    return out


def _ab_torrents(abdb) -> dict[str, dict]:
    """AB 库 `torrent` 表（只读）：infohash → 行（连带番组行的季、`episode_offset`）。"""
    rows = abdb.query(
        "SELECT t.id, t.bangumi_id, t.name, t.url, t.homepage, b.official_title, b.season,"
        " b.episode_offset FROM torrent t LEFT JOIN bangumi b ON b.id = t.bangumi_id")
    out: dict[str, dict] = {}
    for r in rows:
        h = ledger.infohash_of_url(r.get("url") or "") or ledger.infohash_of_url(r.get("homepage") or "")
        if h:
            out[h] = r
    return out


def backfill(ctx, *, dry_run: bool = False, torrents: list[dict] | None = None,
             network: bool = True) -> BackfillReport:
    """补录。`torrents` 给了就用它（`run` 传扫描时读到的那份，不再问一遍 qBittorrent）。"""
    cfg = ctx.config
    rep = BackfillReport(dry_run=dry_run)
    if torrents is None:
        if ctx.qbit is None:
            rep.problems.append("qBittorrent 不可用，没有种子可补")
            return rep
        try:
            torrents = ctx.qbit.torrents()
        except Exception as e:                        # noqa: BLE001 —— 读不到种子就没得补：说一句，交给调用方
            rep.problems.append(f"读 qBittorrent 的种子出错（{type(e).__name__}: {e}），这次不补")
            return rep
    rep.torrents = len(torrents)

    led = None
    if dry_run:
        rows, problem = ledger.load_rows(cfg.state_dir)
        if problem:
            rep.problems.append(f"出处账本{problem}")
            return rep
        misses: set[str] = set()
    else:
        try:
            led = ledger.Ledger.open(cfg.state_dir)
            rows, misses = led.rows(), led.misses()
        except ledger.LedgerUnavailable as e:
            rep.problems.append(f"出处账本{e}")
            ctx.log(f"[ledger] 出处账本打不开，这次不补录：{e}")
            return rep
    try:
        rep.rows_before = len(rows)
        _fill(ctx, rep, led, rows, misses, torrents, network)
    finally:
        if led is not None:
            led.close()
    return rep


def _fill(ctx, rep: BackfillReport, led, rows: dict, misses: set, torrents: list[dict],
          network: bool) -> None:
    cfg = ctx.config
    media_root = Path(cfg.media_root)
    shows = _Shows(rep.problems)
    todo = {t["hash"].lower(): t for t in torrents
            if t.get("hash") and (t["hash"].lower() not in rows
                                  or rows[t["hash"].lower()].source == ledger.UNKNOWN)}
    done: set[str] = set()                            # 补到了、而且知道是谁加的：后面的来源不用再看
    known: set[str] = set(rows)                       # 有一行（含来源 unknown 的）：知道它"是什么"

    mine_unknown: set[str] = set()                    # 这一次补录插进去的 unknown 行（后面的来源可能把它升级）

    def bump(d: dict, k: str, by: int) -> None:
        d[k] = d.get(k, 0) + by
        if not d[k]:
            del d[k]

    def put(h: str, **fields) -> None:
        source = fields["source"]
        existed = h in known
        if led is not None:
            if not led.upsert_backfill(infohash=h, **fields):
                return
        elif existed:                                 # 预演：只有 unknown 的行会被升级
            if source == ledger.UNKNOWN:
                return
        # 报告按行数：一行一个来源。这一次先插成 unknown、后面又被 AB 库认出来的，只算 autobangumi 那一次——以前两边
        # 各数一次，生产重放报「unknown 10」而账本里只有 8 行，各来源合计对不上覆盖率（2026-09-27 审查）；
        # 早就有的 unknown 行被认出来源，是"认出来源"，不是补了一行
        if h in mine_unknown:
            mine_unknown.discard(h)
            bump(rep.inserted, ledger.UNKNOWN, -1)
            bump(rep.inserted, source, 1)
        elif existed:
            bump(rep.upgraded, source, 1)
        else:
            bump(rep.inserted, source, 1)
            if source == ledger.UNKNOWN:
                mine_unknown.add(h)
        known.add(h)
        if source != ledger.UNKNOWN:
            done.add(h)                               # unknown 的留在待补里：AB 库认得出就补上是谁加的

    # 0. 本项目的抓取审计
    try:
        grabs = _audit_grabs(cfg.audit_log)
    except Exception as e:                            # noqa: BLE001 —— 审计读不了只少一个来源：说出来，其余照补
        grabs = {}
        rep.problems.append(f"读审计出错（{type(e).__name__}: {e}），抓取记录这次不补")
    for h, t in list(todo.items()):
        rec = grabs.get(h)
        if rec is None:
            continue
        a = rec.get("args") or {}
        try:
            season, episode = int(a["season"]), int(a["episode"])
        except (KeyError, TypeError, ValueError):
            season = episode = None
        show_dir, _ = _where(t, media_root)             # 此刻在哪部番（目录可能改过名），不在库里退回抓取时的
        put(h, source=ledger.MEDIA_AGENT if rec["_added"] else ledger.UNKNOWN,
            mikan_title=a.get("title") or "", mikan_url=a.get("url") or "",
            show_dir=str(show_dir or a.get("show_dir") or ""), season=season, episode=episode,
            ab_bangumi_id=a.get("bangumi_id"), chosen_reason=str(rec.get("summary") or ""),
            verdict=ledger.verdict_of(a.get("title") or ""), grabbed_at=str(rec.get("ts") or ""),
            run_id=str(rec.get("run_id") or ""), note="补录自抓取审计")
    todo = {h: t for h, t in todo.items() if h not in done}

    # 1. AutoBangumi 库（只读）
    ab: dict[str, dict] = {}
    if ctx.abdb is not None and todo:
        try:
            ab = _ab_torrents(ctx.abdb)
        except Exception as e:                        # noqa: BLE001 —— AB 库读不了只少一个来源：说出来，其余照补
            rep.problems.append(f"读 AutoBangumi 库的 torrent 表出错（{type(e).__name__}: {e}）")
    for h, t in list(todo.items()):
        r = ab.get(h)
        if r is None:
            continue
        show_dir, dir_season = _where(t, media_root)
        pin = parse_pin(t.get("tags") or "")
        title = r.get("name") or ""
        ab_season = int(r.get("season") or 1)
        target = dir_season if dir_season is not None else ab_season
        # 集号偏移与读账本（`builtin.episode_offset_for`）同一个口径：sidecar 登记了这一季就按它，没登记才用 AB 行的
        # （只对它订阅的那一季）
        intent = shows.intent(show_dir)
        shift = intent["episode_offsets"].get(str(target),
                                              int(r.get("episode_offset") or 0) if target == ab_season else 0)
        slot = pin or title_slot(title, target=target, offsets=intent["offsets"], episode_offset=shift)[0]
        put(h, source=ledger.MEDIA_AGENT if pin else ledger.AUTOBANGUMI, mikan_title=title,
            mikan_url=r.get("url") or "", show_dir=str(show_dir or ""),
            season=slot[0] if slot else None, episode=slot[1] if slot else None,
            ab_bangumi_id=r.get("bangumi_id"),
            chosen_reason="AutoBangumi 按订阅规则下载" if not pin else "本项目抓取（钉着 ma:，AB 也登记过）",
            verdict=ledger.verdict_of(title), note="补录自 AutoBangumi 库")
    todo = {h: t for h, t in todo.items() if h not in done}

    # 2. 这部番的番组页 feed——只为还完全不知道是什么的种子拉（已有一行、只差"谁加的"的，番组页也答不了）
    unseen = {h: t for h, t in todo.items() if h not in known}
    if network and unseen:
        _from_feeds(ctx, rep, led, shows, unseen, misses, put, media_root)
        todo = {h: t for h, t in todo.items() if h not in done}

    # 3. 只剩标签能说明来路的：`ma:` 钉子只由本项目的抓取打（集位就是钉子）；`manual:` 是人手加种时自己打的。
    #    番组页标题不知道，但"谁加的、是哪一集"知道——比"查不到出处"强。放在最后：前面的来源能给出标题。
    for h, t in list(todo.items()):
        tags = t.get("tags") or ""
        pin = parse_pin(tags)
        manual = next((x.strip() for x in tags.split(",") if x.strip().startswith("manual:")), "")
        if not (pin or manual):
            continue
        show_dir, _ = _where(t, media_root)
        put(h, source=ledger.MEDIA_AGENT if pin else ledger.MANUAL, show_dir=str(show_dir or ""),
            season=pin[0] if pin else None, episode=pin[1] if pin else None,
            chosen_reason=("只有 ma: 钉子（抓取审计、AB 库、番组页里都没有）" if pin
                           else f"人手加的（标签 {manual}）"),
            note=f"补录自种子标签；显示名 {str(t.get('name') or '')[:80]}")
    todo = {h: t for h, t in todo.items() if h not in done}

    covered = known | done                            # 来源 unknown 的行也知道它"是什么"
    for t in torrents:
        h = (t.get("hash") or "").lower()
        if h in covered:
            rep.covered += 1
            continue
        show_dir, _ = _where(t, media_root)
        rep.remaining.append({"hash": h, "name": t.get("name") or "",
                              "show": show_dir.name if show_dir else "",
                              "added_on": t.get("added_on") or 0})


def _from_feeds(ctx, rep: BackfillReport, led, shows: _Shows, todo: dict, misses: set, put,
                media_root: Path) -> None:
    from .cache import Cache
    from .plugins.grab import _feed_cached

    by_show: dict[Path, list[str]] = {}
    for h, t in todo.items():
        if h in misses:
            continue
        show_dir, _ = _where(t, media_root)
        if show_dir is not None and shows.intent(show_dir)["mikan"]:
            by_show.setdefault(show_dir, []).append(h)
    if not by_show:
        return
    cache = Cache(ctx.config.cache_db)
    missed: list[str] = []
    for show_dir, hashes in sorted(by_show.items()):
        items: dict[str, dict] = {}
        for mid in shows.intent(show_dir)["mikan"][:3]:
            try:
                for it in _feed_cached(mid, cache):
                    ih = ledger.infohash_of_url(it.get("url") or "")
                    if ih:
                        items.setdefault(ih, it)
            except Exception as e:                    # noqa: BLE001 —— 番组页拉不到只少这一部番：说出来，其余照补
                rep.problems.append(f"拉 {show_dir.name} 的番组页 {mid} 出错（{type(e).__name__}: {e}）")
                continue
        offsets = shows.intent(show_dir)["offsets"]
        shifts = shows.intent(show_dir)["episode_offsets"]
        for h in hashes:
            it = items.get(h)
            if it is None:
                missed.append(h)
                continue
            t = todo[h]
            _, dir_season = _where(t, media_root)
            pin = parse_pin(t.get("tags") or "")
            title = it.get("title") or ""
            target = dir_season if dir_season is not None else 1
            slot = pin or title_slot(title, target=target, offsets=offsets,
                                     episode_offset=shifts.get(str(target), 0))[0]
            put(h, source=ledger.MEDIA_AGENT if pin else ledger.UNKNOWN, mikan_title=title,
                mikan_url=it.get("url") or "", pub_date=it.get("pub") or "", show_dir=str(show_dir),
                season=slot[0] if slot else None, episode=slot[1] if slot else None,
                chosen_reason="番组页 feed 里按 infohash 认出" + ("（钉着 ma:）" if pin else ""),
                verdict=ledger.verdict_of(title), note="补录自番组页 feed")
    if missed and led is not None:
        led.note_miss(missed, "番组页 feed 里没有")
