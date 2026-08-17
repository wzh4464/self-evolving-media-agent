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
    def __init__(self, ctx: Context, dry_run: bool = True):
        self.ctx = ctx
        self.cfg = ctx.config
        self.dry_run = dry_run
        self.report = ExecReport()
        self._deleted_count = 0
        self._deleted_bytes = 0

    # ---------------- 审计 ----------------
    def _audit(self, status: str, finding: Finding, action: Action, extra: dict | None = None):
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "status": status,
            "dry_run": self.dry_run,
            "rule": finding.rule,
            "kind": finding.kind,
            "op": action.op,
            "args": action.args,
            "summary": finding.summary,
            **(extra or {}),
        }
        with self.cfg.audit_log.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
        bucket = {"applied": self.report.applied,
                  "skipped": self.report.skipped,
                  "failed": self.report.failed}[status]
        bucket.append(rec)

    # ---------------- 入口 ----------------
    def apply(self, findings: list[Finding]) -> ExecReport:
        for f in findings:
            if not f.action:
                continue
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
        if h and self.ctx.qbit:
            # 有种子的走 qBittorrent API，否则会破坏做种路径映射
            old_rel = self._torrent_rel_path(h, path)
            if old_rel is None:
                self._audit("failed", f, a, {"error": "在种子文件列表里找不到该文件"})
                return
            new_rel = str(Path(old_rel).parent / new_name) if "/" in old_rel else new_name
            self.ctx.qbit.rename_file(h, old_rel, new_rel)
        else:
            path.rename(target)
        self._audit("applied", f, a, {"new_path": str(target)})

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
        self._audit("applied", f, a)

    def _op_recategorize(self, f: Finding, a: Action) -> None:
        if self.dry_run:
            self._audit("skipped", f, a, {"reason": "dry-run"})
            return
        self.ctx.qbit.set_category([a.args["torrent_hash"]], a.args["category"])
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

        hashes = [t["hash"] for t in self.ctx.qbit.torrents()
                  if (t.get("content_path") or "").startswith(str(old))]
        old.rename(new)

        # 种子的保存路径要跟着走，否则做种会报文件丢失
        if hashes and self.ctx.qbit:
            for h in hashes:
                try:
                    t = next(x for x in self.ctx.qbit.torrents() if x["hash"] == h)
                    loc = (t.get("save_path") or "").replace(str(old), str(new))
                    self.ctx.qbit.set_location([h], loc)
                except Exception:
                    continue

        bid = a.args.get("bangumi_id")
        if bid and self.ctx.abdb:
            rows = self.ctx.abdb.query("SELECT save_path FROM bangumi WHERE id=?", (bid,))
            if rows:
                newsp = (rows[0]["save_path"] or "").replace(str(old), str(new))
                self.ctx.abdb.write([
                    ("UPDATE bangumi SET save_path=? WHERE id=?", (newsp, bid))
                ])
        self._audit("applied", f, a, {"new_path": str(new), "torrents_moved": len(hashes)})

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

        self._audit("applied", f, a, {"trashed_to": moved, "freed_bytes": size})

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
