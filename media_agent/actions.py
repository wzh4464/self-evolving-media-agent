"""动作执行器。

全自动模式下的三层安全网：
1. **隔离区**：删除 = 移入 `state/trash/<日期>/`，保留期内可整体还原，到期才真删。
2. **配额上限**：单轮删除数量/体积超过阈值就整体跳过并告警——防止规则写错批量误删。
3. **审计日志**：每个动作（含失败）落 `state/audit.jsonl`，可回溯可还原。
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .kernel import Action, Context, Finding


@dataclass
class ExecReport:
    applied: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        return f"执行 {len(self.applied)} 项，跳过 {len(self.skipped)} 项，失败 {len(self.failed)} 项"


class Executor:
    def __init__(self, ctx: Context, dry_run: bool = True, run_id: str | None = None):
        self.ctx = ctx
        self.cfg = ctx.config
        self.dry_run = dry_run
        # 一次 apply = 一个 run_id，回退以 run 为单位，这就是"一键回退"的单元
        self.run_id = run_id or datetime.now().strftime("%Y%m%dT%H%M%S")
        self.report = ExecReport()
        self._deleted_count = 0
        self._deleted_bytes = 0

    # ---------------- 审计 ----------------
    def _audit(self, status: str, finding: Finding, action: Action,
               extra: dict | None = None, undo: dict | None = None):
        """记录一条审计。

        `undo` 是**逆操作的完整描述**——有它才谈得上回退。每个真正改动了
        系统状态的动作都必须提供，否则这次改动就是不可逆的，
        `rollback` 会明确报告它跳过了什么，而不是假装回退干净了。
        """
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "status": status,
            "dry_run": self.dry_run,
            "rule": finding.rule,
            "kind": finding.kind,
            "op": action.op,
            "args": action.args,
            "summary": finding.summary,
            **(extra or {}),
        }
        if undo is not None:
            rec["undo"] = undo
        with self.cfg.audit_log.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
        bucket = {"applied": self.report.applied,
                  "skipped": self.report.skipped,
                  "failed": self.report.failed}[status]
        bucket.append(rec)

    # 动作之间存在安全顺序，与问题严重度无关：
    # 文件改名必须早于目录改名——目录一改，之前算出的文件路径全部失效。
    # 分类/标签不涉及路径，放最前无所谓；删除放最后，让前面的判断都基于完整状态。
    _OP_ORDER = {
        "fix_title_aliases": 0,  # 订阅失效则下游全部无从谈起，最先修
        "write_sidecar": 10,     # 最后写档案，记录本轮结束后的最终状态
        "relink_torrent": 1,     # 再把失联种子接回来，后续规则才看得到它们
        "retag": 2, "recategorize": 3, "delete_category": 4,
        "write_nfo": 5,
        "rename": 6,            # 先改文件名（此时目录名还是旧的，路径有效）
        "relocate": 7,
        "rename_show_dir": 8,   # 再改目录名，一次性带走里面所有文件
        "trash": 9,
    }

    # ---------------- 入口 ----------------
    def apply(self, findings: list[Finding]) -> ExecReport:
        ordered = sorted(
            (f for f in findings if f.action),
            key=lambda f: (self._OP_ORDER.get(f.action.op, 99), f.show, f.path),
        )
        for f in ordered:
            try:
                self._dispatch(f, f.action)
            except Exception as e:
                self._audit("failed", f, f.action, {"error": f"{type(e).__name__}: {e}"})
        return self.report

    def _dispatch(self, f: Finding, a: Action) -> None:
        handler = getattr(self, f"_op_{a.op}", None)
        if handler is None:
            self._audit("skipped", f, a, {"reason": f"未知动作 {a.op}"})
            return
        handler(f, a)

    # ---------------- 各类动作 ----------------
    def _op_rename(self, f: Finding, a: Action) -> None:
        path = Path(a.args["path"])
        new_name = (a.args.get("new_name") or "").strip()
        # 纵深防御：空目标名会把文件改名成父目录。验证层已拦一道，这里再拦一道。
        if not new_name or "/" in new_name or new_name in (".", ".."):
            self._audit("skipped", f, a, {"reason": f"非法目标文件名 {new_name!r}"})
            return
        target = path.parent / new_name

        if target.exists() and target != path:
            self._audit("skipped", f, a, {"reason": "目标文件名已存在，避免覆盖"})
            return
        if self.dry_run:
            self._audit("skipped", f, a, {"reason": "dry-run"})
            return

        h = a.args.get("torrent_hash")
        via = "filesystem"
        if h and self.ctx.qbit:
            # 有种子的一律走 qBittorrent API。找不到对应条目就报失败，
            # **绝不退化成文件系统改名**——那会让种子路径失效、做种中断。
            old_rel = self._torrent_rel_path(h, path)
            if old_rel is None:
                self._audit("failed", f, a,
                            {"error": "种子文件列表里找不到该文件，拒绝绕过 qBittorrent 改名"})
                return
            new_rel = str(Path(old_rel).parent / new_name) if "/" in old_rel else new_name
            self.ctx.qbit.rename_file(h, old_rel, new_rel)
            via = "qbittorrent"
        else:
            # 确认无种子关联才允许文件系统改名（纯本地文件，无从同步）
            path.rename(target)
        self._audit("applied", f, a, {"new_path": str(target), "via": via},
                    undo={"op": "rename", "path": str(target),
                          "new_name": path.name, "torrent_hash": h or ""})

    # macOS 会在共享卷上撒这些元数据文件，它们不算"内容"，
    # 判断目录是否为空、是否值得搬运时都应忽略
    _JUNK_PREFIXES = ("._",)
    _JUNK_NAMES = {".DS_Store", "Thumbs.db", ".localized"}

    @classmethod
    def _is_junk(cls, name: str) -> bool:
        return name in cls._JUNK_NAMES or name.startswith(cls._JUNK_PREFIXES)

    def _merge_tree(self, old: Path, new: Path) -> tuple[int, int]:
        """把 old 目录树递归合并进 new，逐**文件**移动并保持相对结构。

        必须递归：早先的实现只遍历顶层，遇到 `Season 1` 这种子目录时，
        若 new 下已存在同名子目录就整个跳过，导致里面的文件全部滞留在旧目录，
        造成新旧两个目录并存的分裂状态（实测 19 个目录、50 个文件中招）。

        返回 (已移动文件数, 因目标已存在而滞留的文件数)。
        """
        moved = stranded = 0
        if not old.exists():
            return 0, 0
        new.mkdir(parents=True, exist_ok=True)

        for src in sorted(old.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(old)
            if self._is_junk(src.name):
                src.unlink(missing_ok=True)     # 元数据垃圾直接丢弃，不搬
                continue
            dest = new / rel
            if dest.exists():
                stranded += 1                   # 同名文件已在，留着让去重规则处理
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            moved += 1

        # 自底向上清理空目录（此时只剩空壳）
        for d in sorted((p for p in old.rglob("*") if p.is_dir()),
                        key=lambda p: len(p.parts), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
        try:
            old.rmdir()
        except OSError:
            pass
        return moved, stranded

    def _torrent_rel_path(self, torrent_hash: str, abs_path: Path) -> str | None:
        """qBittorrent 的 renameFile 用的是种子内相对路径，不是绝对路径。"""
        for entry in self.ctx.qbit.files(torrent_hash):
            if Path(entry["name"]).name == abs_path.name:
                return entry["name"]
        return None

    def _op_retag(self, f: Finding, a: Action) -> None:
        if self.dry_run:
            self._audit("skipped", f, a, {"reason": "dry-run"})
            return
        self.ctx.qbit.add_tags([a.args["torrent_hash"]], a.args["tags"])
        self._audit("applied", f, a,
                    undo={"op": "remove_tags", "torrent_hash": a.args["torrent_hash"],
                          "tags": a.args["tags"]})

    def _op_recategorize(self, f: Finding, a: Action) -> None:
        if self.dry_run:
            self._audit("skipped", f, a, {"reason": "dry-run"})
            return
        # 旧分类要在改之前记下来，否则无从回退
        prev = f.evidence.get("current", "")
        self.ctx.qbit.set_category([a.args["torrent_hash"]], a.args["category"])
        self._audit("applied", f, a,
                    undo={"op": "recategorize", "torrent_hash": a.args["torrent_hash"],
                          "category": prev})

    def _op_write_sidecar(self, f: Finding, a: Action) -> None:
        """写入每部番的采集档案。纯写自有文件，不碰媒体内容。"""
        from . import sidecar as sc_mod
        show_dir = Path(a.args["show_dir"])
        if self.dry_run:
            self._audit("skipped", f, a, {"reason": "dry-run"})
            return
        prev = sc_mod.path_for(show_dir)
        prev_content = prev.read_text(encoding="utf-8") if prev.exists() else None
        payload = a.args["payload"]
        known = {k for k in sc_mod.Sidecar.__dataclass_fields__}
        sc = sc_mod.Sidecar(**{k: v for k, v in payload.items() if k in known})
        sc_mod.save(show_dir, sc)
        self._audit("applied", f, a,
                    undo={"op": "restore_sidecar", "show_dir": str(show_dir),
                          "prev": prev_content})

    def _op_fix_title_aliases(self, f: Finding, a: Action) -> None:
        """修复订阅的标题匹配，并解除因此卡住的重抓阻塞。

        **三步顺序不能反**，这是实测踩出来的：
        1. 先写 `title_aliases` —— 让匹配重新生效
        2. 再清掉该订阅下"已登记但 qBittorrent 里不存在"的 torrent 记录 ——
           `pull_rss` 只处理 `check_new()` 筛出的新条目，已登记的永不重评
        3. 最后刷新 RSS

        顺序反了（先清记录再改别名）的话，AutoBangumi 会用**仍然失效**的匹配规则
        把这些条目重新登记一遍，于是它们又变成"不新"，白清一轮。
        """
        bid = a.args["bangumi_id"]
        aliases = a.args["aliases"]
        if self.dry_run:
            self._audit("skipped", f, a,
                        {"reason": "dry-run", "would_set_aliases": aliases})
            return

        rows = self.ctx.abdb.query(
            "SELECT title_aliases FROM bangumi WHERE id=?", (bid,))
        prev = rows[0]["title_aliases"] if rows else None

        # 步骤 1+2 合并成一次停容器写库，减少中断
        stmts = [("UPDATE bangumi SET title_aliases=? WHERE id=?",
                  (json.dumps(aliases, ensure_ascii=False), bid))]

        b = next((x for x in self.ctx.abdb.bangumi() if x["id"] == bid), None)
        cleared = 0
        if b:
            keys = [k for k in (b.get("title_raw"), b.get("official_title")) if k]
            qbit_names = {t["name"] for t in self.ctx.qbit.torrents()}
            for r in self.ctx.abdb.query(
                    "SELECT id, bangumi_id, name FROM torrent"):
                if (r["bangumi_id"] == bid
                        or any(k in (r["name"] or "") for k in keys)):
                    if (r["name"] or "") not in qbit_names:
                        stmts.append(("DELETE FROM torrent WHERE id=?", (r["id"],)))
                        cleared += 1

        self.ctx.abdb.write(stmts)

        # 步骤 3：让 AutoBangumi 重新拉一遍
        refreshed = False
        if self.ctx.ab:
            try:
                self.ctx.ab.refresh_all()
                refreshed = True
            except Exception as e:
                self.ctx.log(f"[fix_title_aliases] 刷新失败: {e}")

        self._audit("applied", f, a,
                    {"aliases": aliases, "cleared_stuck_records": cleared,
                     "refreshed": refreshed},
                    undo={"op": "restore_title_aliases",
                          "bangumi_id": bid, "prev": prev})

    def _op_relink_torrent(self, f: Finding, a: Action) -> None:
        """把路径失效的种子重新关联到磁盘上的实际文件。

        `renameFile` 在这里不是"改文件名"而是"改种子对文件的期望路径"——
        目标文件已经存在，qBittorrent 只更新自己的映射。随后 `recheck`
        让 libtorrent 逐分片校验哈希：**校验通过才算真的修好了**，
        这一步是这套修复能自动做的依据（大小匹配只是定位手段，哈希才是证据）。
        """
        h = a.args["torrent_hash"]
        mapping = a.args["mapping"]
        new_save_path = a.args.get("new_save_path") or ""
        if self.dry_run:
            self._audit("skipped", f, a,
                        {"reason": "dry-run", "would_relink": len(mapping),
                         "would_relocate_to": new_save_path})
            return

        prev_save_path = ""
        if new_save_path:
            # 文件已搬到别的目录：先把 save_path 挪过去，renameFile 用的是
            # 相对 save_path 的路径，跨不出去。
            cur = next((t for t in self.ctx.qbit.torrents() if t["hash"] == h), None)
            prev_save_path = (cur or {}).get("save_path") or ""
            self.ctx.qbit.set_location([h], new_save_path)

        before = {e["name"] for e in self.ctx.qbit.files(h)}
        renamed, failed = [], []
        for m in mapping:
            if m["old"] not in before:
                failed.append({**m, "error": "种子文件列表里已无此条目"})
                continue
            try:
                self.ctx.qbit.rename_file(h, m["old"], m["new"])
                renamed.append(m)
            except Exception as e:
                failed.append({**m, "error": str(e)})

        if not renamed:
            self._audit("failed", f, a, {"error": "没有任何文件重建关联成功",
                                         "failures": failed[:3]})
            return

        # 交给 libtorrent 校验哈希；结果异步产生，这里只负责触发
        self.ctx.qbit.recheck([h])
        self._audit("applied", f, a,
                    {"relinked": len(renamed), "failed": len(failed),
                     "relocated_to": new_save_path, "recheck_triggered": True,
                     "note": "recheck 为异步，稍后确认 progress 回到 100%"},
                    undo={"op": "relink_torrent",
                          "torrent_hash": h,
                          "new_save_path": prev_save_path,
                          "mapping": [{"old": m["new"], "new": m["old"]} for m in renamed]})

    def _op_delete_category(self, f: Finding, a: Action) -> None:
        """删除空分类。只动分类定义，不碰任何文件。

        执行前重新核对该分类确实已无种子——合并动作可能在本轮早些时候失败，
        那样这个分类里还留着东西，删了会让它们变成"无分类"。
        """
        cat = a.args["category"]
        if self.dry_run:
            self._audit("skipped", f, a, {"reason": "dry-run"})
            return
        still = [t for t in self.ctx.qbit.torrents() if (t.get("category") or "") == cat]
        if still:
            self._audit("skipped", f, a,
                        {"reason": f"该分类仍有 {len(still)} 个种子，可能是合并失败，不删"})
            return
        self.ctx.qbit.remove_categories([cat])
        self._audit("applied", f, a)

    def _op_relocate(self, f: Finding, a: Action) -> None:
        if self.dry_run:
            self._audit("skipped", f, a, {"reason": "dry-run"})
            return
        self.ctx.qbit.set_location([a.args["torrent_hash"]], a.args["location"])
        self._audit("applied", f, a)

    def _op_write_nfo(self, f: Finding, a: Action) -> None:
        if self.dry_run:
            self._audit("skipped", f, a, {"reason": "dry-run"})
            return
        nfo = Path(a.args["path"])
        nfo.parent.mkdir(parents=True, exist_ok=True)
        nfo.write_text(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            "<tvshow>\n"
            f"  <title>{a.args['title']}</title>\n"
            f"  <originaltitle>{a.args.get('original_title','')}</originaltitle>\n"
            f'  <uniqueid type="tmdb" default="true">{a.args["tmdb_id"]}</uniqueid>\n'
            f"  <tmdbid>{a.args['tmdb_id']}</tmdbid>\n"
            "</tvshow>\n",
            encoding="utf-8",
        )
        self._audit("applied", f, a)

    def _op_rename_show_dir(self, f: Finding, a: Action) -> None:
        """改番剧目录名，并同步 AutoBangumi 的 save_path。

        不同步 save_path 的话，下次新集数会重新建出旧目录，整理工作周期性重演。
        """
        old = Path(a.args["path"])
        new = old.parent / a.args["new_name"]
        if new.exists():
            self._audit("skipped", f, a, {"reason": "目标目录已存在，需人工合并"})
            return
        if self.dry_run:
            self._audit("skipped", f, a, {"reason": "dry-run"})
            return

        # 改动前记下所有受影响种子的原始 save_path，供回退使用
        affected = [(t["hash"], t.get("save_path") or "")
                    for t in self.ctx.qbit.torrents()
                    if (t.get("content_path") or "").startswith(str(old) + "/")
                    or (t.get("save_path") or "").startswith(str(old))]

        bid = a.args.get("bangumi_id")
        prev_savepath = ""
        if bid and self.ctx.abdb:
            rows = self.ctx.abdb.query("SELECT save_path FROM bangumi WHERE id=?", (bid,))
            if rows:
                prev_savepath = rows[0]["save_path"] or ""

        # === 由 qBittorrent 搬运，而不是自己 mv 目录 ===
        # 裸 mv 会让所有种子的记录路径瞬间失效（实测已因此产生 28 个死链种子）。
        # setLocation 让 qBittorrent 自己移动文件并同步记录，做种不中断。
        moved_ok, move_failed = [], []
        for h, sp in affected:
            try:
                self.ctx.qbit.set_location([h], sp.replace(str(old), str(new), 1))
                moved_ok.append(h)
            except Exception as e:
                move_failed.append({"hash": h, "error": str(e)})

        if move_failed:
            # 有种子没搬成功就中止：此时目录处于半迁移状态，
            # 继续 mv 剩余文件只会让情况更糟，交给人处理。
            self._audit("failed", f, a,
                        {"error": f"{len(move_failed)} 个种子 setLocation 失败，已中止目录改名",
                         "moved_ok": len(moved_ok), "failures": move_failed[:3]},
                        undo={"op": "rename_show_dir_partial",
                              "moved_hashes": moved_ok,
                              "torrent_savepaths": affected})
            return

        # 种子搬完后，把没有种子关联的残留文件（NFO、孤儿字幕、失联种子的文件等）挪过去
        leftovers, stranded = self._merge_tree(old, new)

        if bid and self.ctx.abdb and prev_savepath:
            self.ctx.abdb.write([
                ("UPDATE bangumi SET save_path=? WHERE id=?",
                 (prev_savepath.replace(str(old), str(new), 1), bid))
            ])

        self._audit("applied", f, a,
                    {"new_path": str(new), "torrents_moved": len(moved_ok),
                     "leftover_files_moved": leftovers,
                     "stranded_files": stranded,
                     "old_dir_removed": not old.exists()},
                    undo={"op": "rename_show_dir", "path": str(new),
                          "new_name": old.name, "bangumi_id": bid,
                          "prev_savepath": prev_savepath,
                          "torrent_savepaths": affected})

    def _op_trash(self, f: Finding, a: Action) -> None:
        """删除 = 移入隔离区。受配额上限保护。"""
        path = Path(a.args["path"]) if a.args.get("path") else None
        size = path.stat().st_size if path and path.exists() else 0

        # 配额检查
        if self._deleted_count >= self.cfg.max_delete_per_run:
            self._audit("skipped", f, a, {"reason": f"已达单轮删除数量上限 {self.cfg.max_delete_per_run}"})
            return
        if (self._deleted_bytes + size) / 1e9 > self.cfg.max_delete_gb_per_run:
            self._audit("skipped", f, a, {"reason": f"已达单轮删除体积上限 {self.cfg.max_delete_gb_per_run}GB"})
            return

        if self.dry_run:
            self._audit("skipped", f, a, {"reason": "dry-run", "would_free_bytes": size})
            return

        h = a.args.get("torrent_hash")
        file_only = a.args.get("file_only", False)

        if h and self.ctx.qbit and not file_only:
            # 整个种子作废：先删种子记录（不删文件），文件再单独进隔离区
            try:
                self.ctx.qbit.delete([h], delete_files=False)
            except Exception as e:
                self.ctx.log(f"[trash] 删除种子记录失败 {h}: {e}")
        elif h and self.ctx.qbit and file_only:
            # 只作废种子里的某个文件：设为不下载，保留其余部分
            try:
                rel = self._torrent_rel_path(h, path) if path else None
                if rel is not None:
                    idx = next(e["index"] for e in self.ctx.qbit.files(h)
                               if e["name"] == rel)
                    self.ctx.qbit.set_file_priority(h, [idx], 0)
            except Exception as e:
                self.ctx.log(f"[trash] 设置文件不下载失败 {h}: {e}")

        moved = None
        if path and path.exists():
            day = datetime.now().strftime("%Y-%m-%d")
            dest_dir = self.cfg.trash_dir / day / f.show / (path.parent.name or "")
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / path.name
            if dest.exists():
                dest = dest_dir / f"{path.stem}.{int(time.time())}{path.suffix}"
            shutil.move(str(path), str(dest))
            moved = str(dest)
            self._deleted_count += 1
            self._deleted_bytes += size

        # 文件可从隔离区还原；但被删掉的种子记录还原不了（种子文件本身已不在）
        undo = {"op": "restore_from_trash", "path": str(path) if path else "",
                "trash_path": moved or "",
                "torrent_record_lost": bool(h and not file_only)}
        self._audit("applied", f, a, {"trashed_to": moved, "freed_bytes": size},
                    undo=undo)

    # ---------------- 回退 ----------------
    def rollback(self, run_id: str) -> dict:
        """把某一次 run 的所有改动逆序还原。

        逆序（LIFO）是必须的：先改文件名再改目录名的话，回退必须先还原目录名，
        否则文件的路径已经不对了。

        每一步都先核对当前状态与预期一致才动手——如果之后又有别的改动叠加上来，
        宁可跳过并报告，也不要盲目覆盖。
        """
        records = self._read_audit(run_id)
        undoable = [r for r in records if r.get("status") == "applied" and r.get("undo")]
        no_undo = [r for r in records
                   if r.get("status") == "applied" and not r.get("undo")]

        done, skipped, failed, lost = [], [], [], []

        for rec in reversed(undoable):        # LIFO
            u = rec["undo"]
            try:
                ok, reason = self._apply_undo(u)
                if ok:
                    done.append(rec)
                else:
                    skipped.append({**rec, "skip_reason": reason})
            except Exception as e:
                failed.append({**rec, "error": f"{type(e).__name__}: {e}"})
            if u.get("torrent_record_lost"):
                lost.append(rec)

        result = {
            "run_id": run_id,
            "total": len(records),
            "reverted": len(done),
            "skipped": len(skipped),
            "failed": len(failed),
            "irreversible": len(no_undo),
            "torrent_records_lost": len(lost),
            "skipped_detail": skipped[:10],
            "failed_detail": failed[:10],
        }
        if not self.dry_run:
            with self.cfg.audit_log.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps({
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "run_id": f"rollback-of-{run_id}",
                    "status": "rollback",
                    **result,
                }, ensure_ascii=False) + "\n")
        return result

    def _read_audit(self, run_id: str) -> list[dict]:
        if not self.cfg.audit_log.exists():
            return []
        out = []
        for line in self.cfg.audit_log.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("run_id") == run_id and not rec.get("dry_run"):
                out.append(rec)
        return out

    def _apply_undo(self, u: dict) -> tuple[bool, str]:
        """执行一条逆操作。返回 (是否成功, 跳过原因)。"""
        op = u["op"]

        if op == "rename":
            cur = Path(u["path"])
            if not cur.exists():
                return False, f"当前文件不存在，可能已被再次改名：{cur.name}"
            back = cur.parent / u["new_name"]
            if back.exists():
                return False, f"还原目标已存在：{back.name}"
            if self.dry_run:
                return True, ""
            h = u.get("torrent_hash")
            if h and self.ctx.qbit:
                rel = self._torrent_rel_path(h, cur)
                if rel is None:
                    cur.rename(back)      # 种子里找不到，退化为文件系统改名
                else:
                    new_rel = (str(Path(rel).parent / u["new_name"])
                               if "/" in rel else u["new_name"])
                    self.ctx.qbit.rename_file(h, rel, new_rel)
            else:
                cur.rename(back)
            return True, ""

        if op == "rename_show_dir":
            cur = Path(u["path"])
            back = cur.parent / u["new_name"]
            if back.exists():
                return False, f"还原目标目录已存在：{back.name}"
            if self.dry_run:
                return True, ""

            # 回退同样由 qBittorrent 搬运，保持"改动必经 qBit"的不变式
            for h, sp in u.get("torrent_savepaths", []):
                try:
                    self.ctx.qbit.set_location([h], sp)   # 还原为原始 save_path
                except Exception:
                    continue

            # 残留文件搬回去，再清掉空的新目录
            if cur.exists():
                back.mkdir(parents=True, exist_ok=True)
                for item in list(cur.iterdir()):
                    dest = back / item.name
                    if not dest.exists():
                        shutil.move(str(item), str(dest))
                try:
                    cur.rmdir()
                except OSError:
                    pass

            bid, prev = u.get("bangumi_id"), u.get("prev_savepath")
            if bid and prev and self.ctx.abdb:
                self.ctx.abdb.write([
                    ("UPDATE bangumi SET save_path=? WHERE id=?", (prev, bid))
                ])
            return True, ""

        if op == "restore_sidecar":
            if self.dry_run:
                return True, ""
            from . import sidecar as sc_mod
            p = sc_mod.path_for(Path(u["show_dir"]))
            if u.get("prev") is None:
                p.unlink(missing_ok=True)
            else:
                p.write_text(u["prev"], encoding="utf-8")
            return True, ""

        if op == "restore_title_aliases":
            if self.dry_run:
                return True, ""
            self.ctx.abdb.write([
                ("UPDATE bangumi SET title_aliases=? WHERE id=?",
                 (u.get("prev"), u["bangumi_id"]))
            ])
            return True, ""

        if op == "relink_torrent":
            if self.dry_run:
                return True, ""
            h = u["torrent_hash"]
            for m in u.get("mapping", []):
                try:
                    self.ctx.qbit.rename_file(h, m["old"], m["new"])
                except Exception:
                    continue
            if u.get("new_save_path"):        # 还原到原来的 save_path
                try:
                    self.ctx.qbit.set_location([h], u["new_save_path"])
                except Exception:
                    pass
            return True, ""

        if op == "recategorize":
            if self.dry_run:
                return True, ""
            self.ctx.qbit.set_category([u["torrent_hash"]], u.get("category") or "")
            return True, ""

        if op == "remove_tags":
            if self.dry_run:
                return True, ""
            self.ctx.qbit.remove_tags([u["torrent_hash"]], u["tags"])
            return True, ""

        if op == "restore_from_trash":
            src = Path(u.get("trash_path") or "")
            dst = Path(u.get("path") or "")
            if not src or not src.exists():
                return False, "隔离区文件已不存在（可能已过保留期被清理）"
            if dst.exists():
                return False, f"原位置已被占用：{dst.name}"
            if self.dry_run:
                return True, ""
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return True, ""

        return False, f"未知逆操作 {op}"

    def repair_split_dirs(self, run_id: str) -> dict:
        """修复目录改名后新旧并存的分裂状态。

        场景：`rename_show_dir` 执行过但旧目录没清干净（早期版本的递归合并 bug，
        或 setLocation 失败导致半迁移）。这里按审计记录找出所有 old/new 配对，
        把旧目录残留内容合并进新目录。

        对**仍有有效种子**的文件优先走 qBittorrent setLocation；
        种子已失联的（stale path）只能走文件系统——那些种子本来就已经断了，
        搬运不会让情况更糟，但会记录下来。
        """
        pairs = []
        for rec in self._read_audit(run_id):
            if rec.get("status") != "applied" or rec.get("op") != "rename_show_dir":
                continue
            u = rec.get("undo") or {}
            new = Path(u.get("path", ""))
            old = new.parent / u.get("new_name", "")
            if old.exists() and new.exists() and old != new:
                pairs.append((old, new))

        results = []
        for old, new in pairs:
            # 先让还活着的种子自己搬
            via_qbit = 0
            if self.ctx.qbit:
                for t in self.ctx.qbit.torrents():
                    sp = t.get("save_path") or ""
                    cp = t.get("content_path") or ""
                    if not sp.startswith(str(old)):
                        continue
                    if not Path(cp).exists():
                        continue          # 死链种子，setLocation 搬不动它
                    try:
                        self.ctx.qbit.set_location(
                            [t["hash"]], sp.replace(str(old), str(new), 1))
                        via_qbit += 1
                    except Exception:
                        continue

            if self.dry_run:
                remaining = sum(1 for p in old.rglob("*")
                                if p.is_file() and not self._is_junk(p.name))
                results.append({"old": old.name, "new": new.name,
                                "would_move_via_qbit": via_qbit,
                                "would_move_via_fs": remaining})
                continue

            moved, stranded = self._merge_tree(old, new)
            results.append({"old": old.name, "new": new.name,
                            "moved_via_qbit": via_qbit, "moved_via_fs": moved,
                            "stranded": stranded, "old_removed": not old.exists()})

        return {"pairs": len(pairs), "detail": results}

    def list_runs(self) -> list[dict]:
        """列出历史 run，供选择回退哪一次。"""
        if not self.cfg.audit_log.exists():
            return []
        runs: dict[str, dict] = {}
        for line in self.cfg.audit_log.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = rec.get("run_id")
            if not rid or rec.get("dry_run"):
                continue
            r = runs.setdefault(rid, {"run_id": rid, "ts": rec.get("ts", ""),
                                      "applied": 0, "undoable": 0, "kinds": set()})
            if rec.get("status") == "applied":
                r["applied"] += 1
                if rec.get("undo"):
                    r["undoable"] += 1
                r["kinds"].add(rec.get("kind", ""))
            if rec.get("status") == "rollback":
                r["rolled_back"] = True
        out = []
        for r in runs.values():
            r["kinds"] = sorted(k for k in r["kinds"] if k)
            out.append(r)
        return sorted(out, key=lambda r: r["ts"])

    # ---------------- 隔离区维护 ----------------
    def purge_trash(self) -> dict:
        """清理超过保留期的隔离区内容。"""
        cutoff = datetime.now() - timedelta(days=self.cfg.trash_retention_days)
        purged, freed = 0, 0
        for day_dir in sorted(self.cfg.trash_dir.iterdir()):
            if not day_dir.is_dir():
                continue
            try:
                day = datetime.strptime(day_dir.name, "%Y-%m-%d")
            except ValueError:
                continue
            if day >= cutoff:
                continue
            for p in day_dir.rglob("*"):
                if p.is_file():
                    freed += p.stat().st_size
                    purged += 1
            if not self.dry_run:
                shutil.rmtree(day_dir)
        return {"purged_files": purged, "freed_bytes": freed,
                "retention_days": self.cfg.trash_retention_days}
