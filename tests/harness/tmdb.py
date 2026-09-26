"""FakeTMDB：`TMDBClient` 被用到的那几个方法。

- `search_tv(q)` 只按登记过的查询词精确命中（目录名、标题、原名都会自动登记）。
  `_pick_tmdb` 在多候选时会去问 LLM，想测那条路径就对同一个词登记多个 id。
- 问到没登记的 id 会抛错（调用方都 catch），同时记 tripwire `tmdb_unknown`——
  否则 "季数据没配" 会被吞成 "这部番没有在播的季"，测试静悄悄地什么也没测。
"""
from __future__ import annotations

from datetime import date, timedelta


def weekly(n: int, first_days_ago: int, every: int = 7) -> list[tuple[int, str]]:
    """n 集周播，第 1 集在 `first_days_ago` 天前播出。返回 [(集号, 'YYYY-MM-DD')]。

    代码里直接调 `date.today()`（grab / subscription / sidecar_sync），
    测试数据一律用相对日期，不用冻结时钟。
    """
    start = date.today() - timedelta(days=first_days_ago)
    return [(i + 1, (start + timedelta(days=i * every)).isoformat()) for i in range(n)]


class TMDBNotFound(LookupError):
    pass


class FakeTMDB:
    def __init__(self, tripwire=None, enabled: bool = False):
        self.tripwire = tripwire
        self._enabled = enabled
        self._shows: dict[int, dict] = {}
        self._index: dict[str, list[int]] = {}
        self.calls: list[tuple] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, v: bool) -> None:
        self._enabled = bool(v)

    # ---- 登记 ----
    def add_show(self, tmdb_id: int, title: str, *, original: str = "",
                 seasons: dict[int, list[tuple[int, str]]] | None = None,
                 queries: list[str] | tuple = (), first_air_date: str = "") -> None:
        """登记一部番。`seasons` 形如 `{1: weekly(12, first_days_ago=60)}`。

        登记即启用（`enabled=True`）——既然测试给了 TMDB 数据，就是想让相关规则跑。
        """
        self._shows[int(tmdb_id)] = {
            "id": int(tmdb_id), "name": title, "original_name": original or title,
            "first_air_date": first_air_date, "overview": "",
            "seasons": {int(k): list(v) for k, v in (seasons or {}).items()},
        }
        for q in {title, original, *queries} - {""}:
            ids = self._index.setdefault(q, [])
            if int(tmdb_id) not in ids:
                ids.append(int(tmdb_id))
        self._enabled = True

    def _show(self, tv_id) -> dict:
        s = self._shows.get(int(tv_id)) if tv_id is not None else None
        if s is None:
            if self.tripwire is not None:
                self.tripwire.record("tmdb_unknown", f"id={tv_id}")
            raise TMDBNotFound(f"TMDB 404: tv/{tv_id}")
        return s

    # ---- TMDBClient 接口 ----
    def search_tv(self, query: str) -> list[dict]:
        self.calls.append(("search_tv", query))
        return [{k: self._shows[i][k] for k in
                 ("id", "name", "original_name", "first_air_date", "overview")}
                for i in self._index.get(query, [])]

    def tv_detail(self, tv_id: int) -> dict:
        self.calls.append(("tv_detail", tv_id))
        s = self._show(tv_id)
        return {"id": s["id"], "name": s["name"], "original_name": s["original_name"],
                "seasons": [{"season_number": n, "episode_count": len(eps),
                             "name": f"第 {n} 季"} for n, eps in sorted(s["seasons"].items())]}

    def official_title(self, tv_id: int) -> tuple[str, str]:
        d = self.tv_detail(tv_id)
        return (d["name"] or d["original_name"]), d["original_name"]

    def seasons(self, tv_id: int) -> list[dict]:
        return self.tv_detail(tv_id)["seasons"]

    def season_episodes(self, tv_id: int, season: int) -> list[dict]:
        self.calls.append(("season_episodes", tv_id, season))
        s = self._show(tv_id)
        eps = s["seasons"].get(int(season))
        if eps is None:
            raise TMDBNotFound(f"TMDB 404: tv/{tv_id}/season/{season}")
        return [{"episode_number": n, "air_date": d, "name": f"第{n}集"} for n, d in eps]

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if self.tripwire is not None:
            self.tripwire.record("unmodeled", f"FakeTMDB.{name}")
        raise AttributeError(f"FakeTMDB.{name} 未建模")
