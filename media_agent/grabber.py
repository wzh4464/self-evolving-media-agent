"""抓取加种之后的两步：等元数据、在**下载过程中**就把正片改成规范名。

**为什么必须在下载中改，而不是下完再改**：从加种子到下载完成这段时间里，
文件在磁盘上叫的是发布名。这段时间内——

- 刮削器扫到它，认不出是哪一集，留下空条目；
- `duplicate-episode` 按集位分桶，发布名解析不出集号就不进桶，
  同一集的另一个版本进来时看不见它，于是重复判定失效；
- AutoBangumi 读的是**种子显示名**而不是文件名，显示名不改，
  它下一轮会按自己的理解再改一次，两边打架。

所以正确时机不是"加种子时"，也不是"下完时"，而是**元数据一到手**：
`torrents/files` 有内容的那一刻就能 `renameFile`，此时文件可能一个字节
都还没下。qBittorrent 会把后续分片直接写进新名字。

**加种子本身不在这里。** 唯一的 HTTP 入口是 `QBitClient.add_torrent`
（2026-09-15 把 `actions.py` 里两处手写的 `torrents/add` 收了进去），调用方只有
`Executor._op_grab_episode` 与回退的 `readd_torrent`。这里原先还有一个
"加种 → 等元数据 → 改名 → 改显示名"一条龙的 `add_and_name`，模块文档称它是
"加种子的唯一入口"——实际零调用方（本地两份克隆、tests / tools / deploy、生产机
全部 `*.py` / `*.sh` 都查过），与在用的路径还各有一套等待时长与改显示名的规则。
2026-09-26 删除：两条路径并存，闸门只接一条等于没接。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .claims import ClaimCheck, ClaimIndex, ClaimsUnknown
from .naming import VIDEO_EXTS


def wait_metadata(qbit, torrent_hash: str, timeout: float = 30.0,
                  interval: float = 1.0) -> list[dict]:
    """等到 `torrents/files` 有内容为止，返回参与下载的文件列表。

    磁力链刚加进来时处于 `metaDL`——还在从 DHT/peer 拉元数据，此时
    `torrents/files` 是空的，任何 `renameFile` 都会失败。拉不到元数据的
    常见原因是连不上任何 peer（无端口转发时尤其常见），那可能要几分钟，
    所以超时返回空列表是正常结果，不是错误：交给后续的 `unrenamed-file`
    规则兜底即可。
    """
    deadline = time.time() + timeout
    while True:
        try:
            files = [f for f in (qbit.files(torrent_hash) or [])
                     if f.get("priority", 1) != 0]
        except Exception:
            files = []
        if files:
            return files
        if time.time() >= deadline:
            return []
        time.sleep(interval)


@dataclass
class RenameOutcome:
    """`rename_single_video` 的结果。以前返回 `str | None`，把"改了""不是单文件"
    "已是目标名""集位被占"四种结局混成一个 None，被占的原因从来到不了审计。"""
    renamed: str = ""                   # 改成的新条目名（种子内相对路径）；空 = 没改
    skipped: str = ""                   # 没改的原因
    check: ClaimCheck | None = None     # 占用查询结果（被占 / 看不全时才有）

    @property
    def blocked(self) -> bool:
        """是被占用闸门拦下的（被占，或看不全）。"""
        return self.check is not None and not self.check.free

    def audit(self) -> dict:
        """写进抓取审计记录的形状：`{"renamed": 新条目名|None, "skipped"?, "claims"?}`。"""
        d: dict = {"renamed": self.renamed or None}
        if self.skipped:
            d["skipped"] = self.skipped
        if self.blocked:
            d["claims"] = self.check.audit()
        return d


def rename_single_video(qbit, torrent_hash: str, target_stem: str,
                        files: list[dict] | None = None, *,
                        claims: ClaimIndex | None = None) -> RenameOutcome:
    """把种子里**唯一**的正片文件改成 `target_stem + 原扩展名`（保留它所在的文件夹）。

    只处理"恰好一个视频文件"的种子。合集、带特典的多文件种子在这里不猜——
    哪个文件对应哪一集需要逐个判断，那是 `unrenamed-file` 规则的职责。

    **目标名被占就不改**（`claims`，critic N6）。新种子刚拿到元数据、一个字节都没下，
    libtorrent 只改映射：目标名此刻若被别的种子声明着、或盘上有别人的 `X` / `X.!qB`，
    改过去就是两个种子无声地宣称同一路径——2026-09-06 尼古喵喵 S01E08 丢片的形态。
    被占时留在发布名上；调用方打的 `ma:` 钉子让判重认得出它，下完后由
    duplicate-episode 封存、清走占位的（op 5），unrenamed-file 同一轮改名（op 6）。
    看不全（qBittorrent 读失败）同样不改。

    **保留文件夹层**（critic N15）：条目是 `文件夹/x.mkv` 时目标是 `文件夹/<stem>.mkv`，
    与 `_op_rename` 一致。以前直接改成 `<stem>.mkv`：409 撞上一个已有的 Original 布局
    种子时，文件被挪到 save_path 根下。

    `claims` 是调用方这一批次的占用索引；不给就现建一个。改名之后作废它。
    """
    if claims is None:
        claims = ClaimIndex(qbit)
    if files is None:
        files = [f for f in (qbit.files(torrent_hash) or [])
                 if f.get("priority", 1) != 0]
    vids = [f for f in files if Path(f["name"]).suffix.lower() in VIDEO_EXTS]
    if len(vids) != 1:
        return RenameOutcome(skipped=f"不是单一正片（{len(vids)} 个视频文件），交给 unrenamed-file")
    cur = vids[0]["name"]
    want_name = target_stem + Path(cur).suffix.lower()
    want = str(Path(cur).parent / want_name) if "/" in cur else want_name
    if cur == want:
        return RenameOutcome(skipped="已是目标名")
    # save_path 问 qBittorrent 要，不用调用方算的：409（种子已存在）时它可能在别处。
    try:
        t = claims.torrent(torrent_hash)
    except ClaimsUnknown as e:
        return RenameOutcome(skipped="无法确认目标路径的占用情况",
                             check=ClaimCheck(want, unknown=str(e)))
    if t is None:
        return RenameOutcome(skipped="种子不在 qBittorrent 的列表里")
    sp = Path((t.get("save_path") or "").rstrip("/") or "/")
    chk = claims.check(sp / want, own_hash=torrent_hash, own_path=sp / cur)
    if not chk.free:
        return RenameOutcome(
            skipped=("无法确认目标路径的占用情况" if chk.unknown else "集位被占"), check=chk)
    qbit.rename_file(torrent_hash, cur, want)
    claims.invalidate()
    return RenameOutcome(renamed=want)
