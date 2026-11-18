"""某一个 tmdb_id 404 不该让这一轮其余的番都不再问 TMDB（`scan._tmdb_meta`）。

2026-10 起凭物语 327851、历物语 328198 被 TMDB 并进「物语系列」，每轮 404 都触发熔断；
排在后面、没有旧标题可兜底的番（学生会也有洞！283938）因此每轮被 tmdb-identity 挡住改名与抓取。
"""
import httpx

from media_agent import scan


class _Cache:
    def __init__(self):
        self.d = {}

    def get_tmdb(self, key, ttl=None):
        return self.d.get(key)

    def put_tmdb(self, key, value):
        self.d[key] = value


class _Tmdb:
    def __init__(self, fail):
        self.fail, self.asked = fail, []

    def tv_detail(self, tid):
        self.asked.append(tid)
        if tid in self.fail:
            req = httpx.Request("GET", f"https://api.themoviedb.org/3/tv/{tid}")
            raise httpx.HTTPStatusError("x", request=req, response=httpx.Response(self.fail[tid], request=req))
        return {"id": tid, "name": f"番{tid}", "original_name": f"番{tid}",
                "seasons": [{"season_number": 1, "episode_count": 12}]}


class _Ctx:
    def __init__(self, tmdb):
        self.tmdb, self.logs = tmdb, []

    def log(self, m):
        self.logs.append(m)


def _run(fail, ids):
    ctx, cache, net = _Ctx(_Tmdb(fail)), _Cache(), {"broken": None}
    out = [scan._tmdb_meta(ctx, cache, t, f"目录{t}", net) for t in ids]
    return ctx, net, out


def test_a_404_only_skips_that_show():
    ctx, net, out = _run({327851: 404}, [327851, 283938])
    assert out[0] is None
    assert out[1] and out[1]["title"] == "番283938"
    assert net["broken"] is None
    assert ctx.tmdb.asked == [327851, 283938]
    assert any("已不存在" in m and "327851" in m for m in ctx.logs)


def test_other_failures_still_stop_asking():
    ctx, net, out = _run({327851: 503}, [327851, 283938])
    assert out == [None, None]
    assert net["broken"]
    assert ctx.tmdb.asked == [327851]


def test_a_transport_error_still_stops_asking():
    class Down(_Tmdb):
        def tv_detail(self, tid):
            self.asked.append(tid)
            raise httpx.ConnectError("dns")
    ctx, cache, net = _Ctx(Down({})), _Cache(), {"broken": None}
    assert scan._tmdb_meta(ctx, cache, 1, "a", net) is None
    assert scan._tmdb_meta(ctx, cache, 2, "b", net) is None
    assert ctx.tmdb.asked == [1] and net["broken"]
