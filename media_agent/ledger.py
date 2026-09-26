"""出处账本：按 **infohash** 记下每个种子**是什么**——在我们知道得最清楚的那一刻记。

**为什么要它。** 一个种子是哪一集、是哪个版本，只在两个时刻知道得最清楚：
- 抓取那一刻（`_op_grab_episode`）：番组页上的发布标题、按番组页 + 播出日期定下的集位、偏好打分、
  发布日期、落选了几个——以前只进了 Finding 的 evidence，审计里只剩 `args` 与摘要（state.md H1）；
- AutoBangumi 登记它那一刻：AB 库的 `torrent` 表记着番组页标题，URL 里就是 infohash（state.md H2）。

此后每条规则都在从文件名、种子显示名**重新猜**：
- 2026-08-31 AB 把 `[Fyy Raws] … 3rd Season - 08` 改名成 `S01E08`，判重按文件名把 2016 年真正的第 8 集
  清进了隔离区——发布方明写的"第三季"，到判重那一刻已经没人记得；
- LoliHouse 的内部名只写 `ASSx2`，「简繁内封字幕」只在番组页标题里；尼古喵喵的「邪竜解放版」同样只在标题里
  （`builtin.meets_requirements` 的注释自己承认）——探测不可用时复核与择优只能按内部名判错；
- 钉着 `ma:S01E58`、还叫着发布名 `- 08` 的已下完文件，`have` 按名字算成第 8 集，第 58 集于是被再抓一遍
  （critic N14：29 集被抓了不止一次）。

**账本记事实，不记结论。** 一行 = 一个 infohash：谁加的（`source`）、番组页标题与链接、发布日期、落在哪部番、
**集位**（抓取器定的；补录的是按标题算的）、发布方声明的季号与原始集号、版本词（偏好关键词命中）、偏好评分、
为什么选它、何时抓、AB 的番组 id、备注。补录的集位只是补录那一刻按当时的 `season_offsets` 算的——规则要用时
按**此刻的**换算关系从标题重新算（`builtin.ledger_slot`），人后来补上的 `season_offsets` 立刻生效。

**写的人**：抓取（`record_grab`，定论，覆盖之前的补录）；补录（`upsert_backfill`，只填没有的，永不覆盖）；
回退抓取（`retract`，只标"不再作保"，不删——它仍然是那个种子）。

**坏了不拦路**：账本打不开、坏了、结构版本比代码新 → `LedgerUnavailable`，文件原样不动（不重建：坏账本要人看）；
读的一方用 `load_rows`，永不抛，给一句原因——调用方按没有账本的老办法走，健康报告说出来。

存储：`state/ledger.sqlite`，WAL（`run` 与人手的 `ledger backfill` 可以同时开着），`busy_timeout`，
结构版本记在 `PRAGMA user_version`。放在 `state/` 而不是 sidecar：sidecar 是按番的、会整份重写，
而账本按种子、只增不改（state.md H5）。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

LEDGER_NAME = "ledger.sqlite"
SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000

MEDIA_AGENT, AUTOBANGUMI, MANUAL, UNKNOWN = "media-agent", "autobangumi", "manual", "unknown"
SOURCES = (MEDIA_AGENT, AUTOBANGUMI, MANUAL, UNKNOWN)
ACTIVE, RETRACTED = "active", "retracted"

# 补录时番组页里找不到的种子，隔多久再找一次（`lookup_miss`）：每轮 `run` 开头的自动补录只找新的，
# 不为同一批没出处的种子每 6 小时拉一遍番组页
MISS_TTL = 7 * 86400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS provenance (
  infohash TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  mikan_title TEXT NOT NULL DEFAULT '',
  mikan_url TEXT NOT NULL DEFAULT '',
  pub_date TEXT NOT NULL DEFAULT '',
  show_dir TEXT NOT NULL DEFAULT '',
  season INTEGER,
  episode INTEGER,
  declared_season INTEGER,
  raw_episode INTEGER,
  versions TEXT NOT NULL DEFAULT '[]',
  verdict TEXT NOT NULL DEFAULT '{}',
  chosen_reason TEXT NOT NULL DEFAULT '',
  grabbed_at TEXT NOT NULL DEFAULT '',
  ab_bangumi_id INTEGER,
  notes TEXT NOT NULL DEFAULT '[]',
  evidence TEXT NOT NULL DEFAULT '{}',
  run_id TEXT NOT NULL DEFAULT '',
  recorded_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lookup_miss (
  infohash TEXT PRIMARY KEY,
  checked_at REAL NOT NULL,
  why TEXT NOT NULL DEFAULT ''
);
"""

_HASH_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class LedgerUnavailable(Exception):
    """账本打不开 / 坏了 / 结构版本比代码新。调用方按没有账本处理，并把原因说出来。"""


def path_of(state_dir) -> Path:
    return Path(state_dir) / LEDGER_NAME


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm_hash(h: str) -> str:
    h = (h or "").strip().lower()
    if not _HASH_RE.fullmatch(h):
        raise ValueError(f"不是 infohash：{h!r}")
    return h


def _int_or_none(v) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def infohash_of_url(url: str) -> str | None:
    """种子链接里的 infohash：Mikan 的 `/Download/<日期>/<infohash>.torrent`（AB 的 `torrent.url` 同形）、
    磁力链接的 `btih:`。认不出返回 None（不从别的 40 位十六进制串里猜）。"""
    u = (url or "").lower()
    m = re.search(r"(?<![0-9a-f])([0-9a-f]{40})\.torrent\b", u) or re.search(
        r"btih:([0-9a-f]{40})(?![0-9a-f])", u)
    return m.group(1) if m else None


def release_facts(title: str) -> tuple[int | None, int | None]:
    """发布标题里**发布方自己写的**季号与原始集号 `(declared_season, raw_episode)`，与抓取同一套解析
    （`naming.declared_seasons` 按 ` / ` 分段各认一次；集号 `naming.parse_episode`）。声明了不止一个季号取最小的。"""
    from .naming import declared_seasons, parse_episode

    ds = declared_seasons(title or "")
    return (min(ds) if ds else None), parse_episode(title or "")[1]


def versions_of(title: str, rules: dict | None = None) -> set[str]:
    """标题里命中的版本词：偏好规则（`preferences`）里 prefer / avoid 的关键词，外加 BDRip。
    内部名常常一个都没有（LoliHouse 的 `ASSx2`、合并发布的 `【邪龙解放版】` 只在番组页标题里）。"""
    from . import preferences

    r = rules or preferences.load_rules()
    t = title or ""
    out = {w for group in ("prefer", "avoid") for rule in r.get(group, [])
           for w in rule.get("any", []) if w and w in t}
    if re.search(r"BDRip|Blu-?Ray|BDBOX", t, re.IGNORECASE):
        out.add("BDRip")
    return out


def verdict_of(title: str, require_any: list[str] | tuple = ()) -> dict:
    """按此刻的偏好规则（叠上这部番的 `require_any`）评估一个发布标题，存进账本的形状。"""
    from . import preferences

    rules = preferences.with_requirement(preferences.load_rules(), list(require_any or []),
                                         name="只保留" + "/".join(require_any or []))
    v = preferences.evaluate(title or "", rules)
    return {"acceptable": v.acceptable, "score": v.score, "passed": list(v.passed),
            "penalties": list(v.penalties), "blocked_by": v.blocked_by}


@dataclass(frozen=True)
class Row:
    """账本里的一行。`season` / `episode` 是库内集位（`ma:` 钉子编码的那个）；认不出为 None。"""
    infohash: str
    source: str
    status: str = ACTIVE
    mikan_title: str = ""
    mikan_url: str = ""
    pub_date: str = ""
    show_dir: str = ""
    season: int | None = None
    episode: int | None = None
    declared_season: int | None = None
    raw_episode: int | None = None
    versions: tuple = ()
    verdict: dict = field(default_factory=dict, compare=False, hash=False)
    chosen_reason: str = ""
    grabbed_at: str = ""
    ab_bangumi_id: int | None = None
    notes: tuple = ()
    evidence: dict = field(default_factory=dict, compare=False, hash=False)
    run_id: str = ""
    recorded_at: str = ""
    updated_at: str = ""

    @property
    def active(self) -> bool:
        return self.status == ACTIVE

    @property
    def slot(self) -> tuple[int, int] | None:
        if self.season is None or self.episode is None:
            return None
        return int(self.season), int(self.episode)

    @property
    def grabbed(self) -> bool:
        """抓取器定过集位（它的集位是定论，与 `ma:` 钉子同一个来源）。"""
        return bool(self.grabbed_at) and self.slot is not None

    def to_dict(self) -> dict:
        return {k: (list(v) if isinstance(v, tuple) else v)
                for k, v in self.__dict__.items()}

    @classmethod
    def _from_sql(cls, r: sqlite3.Row) -> "Row":
        def js(v, default):
            try:
                out = json.loads(v) if v else default
            except (TypeError, ValueError):
                return default
            return out if isinstance(out, type(default)) else default

        return cls(infohash=r["infohash"], source=r["source"], status=r["status"],
                   mikan_title=r["mikan_title"], mikan_url=r["mikan_url"], pub_date=r["pub_date"],
                   show_dir=r["show_dir"], season=r["season"], episode=r["episode"],
                   declared_season=r["declared_season"], raw_episode=r["raw_episode"],
                   versions=tuple(js(r["versions"], [])), verdict=js(r["verdict"], {}),
                   chosen_reason=r["chosen_reason"], grabbed_at=r["grabbed_at"],
                   ab_bangumi_id=r["ab_bangumi_id"], notes=tuple(js(r["notes"], [])),
                   evidence=js(r["evidence"], {}), run_id=r["run_id"],
                   recorded_at=r["recorded_at"], updated_at=r["updated_at"])


class Ledger:
    """`state/ledger.sqlite` 的一条连接。用完 `close()`（或 `with`）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.conn = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_MS / 1000)
        except sqlite3.Error as e:
            raise LedgerUnavailable(f"{self.path.name} 打不开（{type(e).__name__}: {e}）") from e
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            ver = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
            if ver > SCHEMA_VERSION:
                raise LedgerUnavailable(
                    f"{self.path.name} 的结构版本 {ver} 比这份代码认识的 {SCHEMA_VERSION} 新"
                    f"（部署回退过？）——不读也不写，按没有账本处理")
            self.conn.execute("PRAGMA journal_mode=WAL").fetchone()
            if ver < SCHEMA_VERSION:
                self.conn.executescript(_SCHEMA)
                self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self.conn.commit()
            self.conn.execute("SELECT count(*) FROM provenance").fetchone()
        except LedgerUnavailable:
            self.conn.close()
            raise
        except sqlite3.Error as e:
            self.conn.close()
            raise LedgerUnavailable(f"{self.path.name} 读不了（{type(e).__name__}: {e}）——"
                                    f"不覆盖、不重建，要人看") from e

    @classmethod
    def open(cls, state_dir) -> "Ledger":
        return cls(path_of(state_dir))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------------- 读
    def get(self, infohash: str) -> Row | None:
        try:
            h = _norm_hash(infohash)
        except ValueError:
            return None
        r = self.conn.execute("SELECT * FROM provenance WHERE infohash=?", (h,)).fetchone()
        return Row._from_sql(r) if r else None

    def rows(self) -> dict[str, Row]:
        return {r["infohash"]: Row._from_sql(r)
                for r in self.conn.execute("SELECT * FROM provenance")}

    def slot_of(self, infohash: str) -> tuple[int, int] | None:
        """这个种子的库内集位（撤销了的不算）；没有这一行 / 认不出返回 None。"""
        row = self.get(infohash)
        return row.slot if row is not None and row.active else None

    def title_of(self, infohash: str) -> str:
        """番组页上的发布标题（撤销了的也给：它仍然是那个发布）；没有返回空串。"""
        row = self.get(infohash)
        return row.mikan_title if row is not None else ""

    def misses(self, ttl: float = MISS_TTL) -> set[str]:
        """补录时找过、没找到、还没到再找的时候的种子。"""
        cutoff = time.time() - ttl
        return {r[0] for r in self.conn.execute(
            "SELECT infohash FROM lookup_miss WHERE checked_at >= ?", (cutoff,))}

    # ---------------------------------------------------------------- 写
    def record_grab(self, *, infohash: str, mikan_title: str, mikan_url: str = "", pub_date: str = "",
                    show_dir: str = "", season: int, episode: int, verdict: dict | None = None,
                    chosen_reason: str = "", ab_bangumi_id: int | None = None, run_id: str = "",
                    added: bool | None = True, evidence: dict | None = None,
                    grabbed_at: str = "", note: str = "") -> Row:
        """抓取器为这个种子定下的：它的集位是定论（与 `ma:` 钉子同一个来源），覆盖之前补录的。

        `added`：True = 这次加进 qBittorrent 的；False = 409，种子早就在（谁加的不改，记一笔）；
        None = 说不清（加种请求出错、核实种子在）。"""
        h = _norm_hash(infohash)
        now = _now()
        declared, raw = release_facts(mikan_title)
        prev = self.get(h)
        notes = list(prev.notes) if prev else []
        if prev is not None and added is not True and prev.source != MEDIA_AGENT:
            source = prev.source
            notes.append(f"{now} 抓取器也选中了它（种子已经在 qBittorrent 里，409），集位按抓取器的")
        else:
            source = MEDIA_AGENT
        if prev is not None and not prev.active:
            notes.append(f"{now} 又被抓取（{run_id}），恢复作保")
        if note:
            notes.append(f"{now} {note}")
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO provenance (infohash, source, status, mikan_title, mikan_url,"
                " pub_date, show_dir, season, episode, declared_season, raw_episode, versions, verdict,"
                " chosen_reason, grabbed_at, ab_bangumi_id, notes, evidence, run_id, recorded_at,"
                " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (h, source, ACTIVE, mikan_title or "", mikan_url or "", pub_date or "",
                 show_dir or "", int(season), int(episode), declared, raw,
                 json.dumps(sorted(versions_of(mikan_title)), ensure_ascii=False),
                 json.dumps(verdict or {}, ensure_ascii=False), chosen_reason or "",
                 grabbed_at or now,
                 _int_or_none(ab_bangumi_id) if _int_or_none(ab_bangumi_id) is not None
                 else (prev.ab_bangumi_id if prev else None),
                 json.dumps(notes, ensure_ascii=False),
                 json.dumps(evidence or {}, ensure_ascii=False, default=str), run_id or "",
                 prev.recorded_at if prev else now, now))
            self.conn.execute("DELETE FROM lookup_miss WHERE infohash=?", (h,))
        return self.get(h)                                    # type: ignore[return-value]

    def upsert_backfill(self, *, infohash: str, source: str, mikan_title: str = "",
                        mikan_url: str = "", pub_date: str = "", show_dir: str = "",
                        season: int | None = None, episode: int | None = None,
                        ab_bangumi_id: int | None = None, chosen_reason: str = "",
                        verdict: dict | None = None, grabbed_at: str = "", run_id: str = "",
                        note: str = "") -> bool:
        """补录：这个种子还没有一行时才写（抓取写的、之前补录的一律不动）。返回写了没有。"""
        h = _norm_hash(infohash)
        if source not in SOURCES:
            raise ValueError(f"来源只能是 {' / '.join(SOURCES)}，收到 {source!r}")
        now = _now()
        declared, raw = release_facts(mikan_title)
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO provenance (infohash, source, status, mikan_title, mikan_url,"
                " pub_date, show_dir, season, episode, declared_season, raw_episode, versions, verdict,"
                " chosen_reason, grabbed_at, ab_bangumi_id, notes, evidence, run_id, recorded_at,"
                " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (h, source, ACTIVE, mikan_title or "", mikan_url or "", pub_date or "", show_dir or "",
                 season, episode, declared, raw,
                 json.dumps(sorted(versions_of(mikan_title)), ensure_ascii=False),
                 json.dumps(verdict or {}, ensure_ascii=False), chosen_reason or "", grabbed_at or "",
                 _int_or_none(ab_bangumi_id), json.dumps([f"{now} {note}"] if note else [], ensure_ascii=False),
                 "{}", run_id or "", now, now))
            if cur.rowcount:
                self.conn.execute("DELETE FROM lookup_miss WHERE infohash=?", (h,))
        return bool(cur.rowcount)

    def retract(self, infohash: str, *, run_id: str, why: str) -> bool:
        """不再替这一行的集位作保（回退了抓取）。行留着：它仍然是那个种子。返回有没有这一行。"""
        row = self.get(infohash)
        if row is None:
            return False
        now = _now()
        notes = [*row.notes, f"{now} 撤销（{run_id}）：{why}"]
        with self.conn:
            self.conn.execute("UPDATE provenance SET status=?, notes=?, updated_at=? WHERE infohash=?",
                              (RETRACTED, json.dumps(notes, ensure_ascii=False), now, row.infohash))
        return True

    def note_miss(self, hashes, why: str) -> None:
        """补录时这些种子在番组页里没找到：`MISS_TTL` 之内不再为它们拉番组页。"""
        now = time.time()
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO lookup_miss VALUES (?,?,?)",
                                  [(_norm_hash(h), now, why) for h in hashes])


def load_rows(state_dir) -> tuple[dict[str, Row], str]:
    """全部行（给扫描挂到文件上）与一句问题；**永不抛**。还没有账本是 `({}, "")`，而且不凭空建库——
    只读的一方（`diagnose`、purge 的重扫）不该在 `state/` 里留下一个空库。"""
    p = path_of(state_dir)
    if not p.exists():
        return {}, ""
    try:
        with Ledger(p) as led:
            return led.rows(), ""
    except LedgerUnavailable as e:
        return {}, str(e)
    except Exception as e:                        # noqa: BLE001 —— 账本读不了不拦任何一轮：按没有账本走，原因交给调用方说
        return {}, f"{p.name} 读不了（{type(e).__name__}: {e}）"
