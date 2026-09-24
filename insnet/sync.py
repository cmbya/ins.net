"""Scheduled and on-demand Instagram archiving."""

import os
import re
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

from .engine import GalleryDL, GalleryError


def safe_part(value):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(value))[:80].strip(".") or "unknown"


class SyncService:
    def __init__(self, db, root, gallery=None, max_posts=20, archive_root=None):
        self.db, self.root = db, Path(root)
        self.archive_root = Path(archive_root or self.root / "media")
        self.gallery = gallery or GalleryDL()
        self.max_posts = max_posts

    @staticmethod
    def _log(log, level, source, message):
        if log:
            log(level, source, message)

    def import_following(self, account, log=None):
        self._log(log, "INFO", "following", f"正在读取 @{account['username']} 的关注列表")
        names = self.gallery.following(account["cookie_path"], account["username"])
        for username in names:
            self.db.upsert_creator(account["id"], username)
        self._log(log, "INFO", "following", f"读取完成：共 {len(names)} 位博主；新导入项默认不监控，请手动勾选")
        return len(names)

    def _archive(self, account, post, source, log=None):
        post_id = self.db.upsert_post(account["id"], post, source)
        rows = self.db.post_media(post_id)
        missing = [row for row in rows if not row["relative_path"] or not
                   (self.archive_root / row["relative_path"]).is_file()]
        if not missing:
            self.db.set_post_status(post_id, "complete")
            self._log(log, "INFO", source, f"跳过 @{post['username']}/{post['shortcode']}：文件已经完整")
            return "skipped"
        date = re.sub(r"\D", "", post["published_at"] or "")[:8] or "undated"
        folder = self.db.setting("media_subdir", "Instagram").strip("/")
        relative = (Path(folder) if folder else Path()) / safe_part(account["username"]) \
                   / safe_part(post["username"]) / f"{date}_{safe_part(post['shortcode'])}"
        self._log(log, "INFO", source,
                  f"下载 @{post['username']}/{post['shortcode']}：缺少 {len(missing)} 个媒体项")
        try:
            with TemporaryDirectory(dir=self.root / "tmp") as temporary:
                files = self.gallery.download(account["cookie_path"], post, temporary)
                for row in missing:
                    candidate = files.get(row["media_id"])
                    if not candidate:
                        continue
                    extension = safe_part(candidate.suffix.lstrip(".")).lower()
                    if extension not in ("jpg", "jpeg", "png", "webp", "gif", "mp4", "mov", "webm", "mkv"):
                        continue
                    filename = f"{row['position']:02d}-{safe_part(row['media_id'])}.{extension}"
                    target = self.archive_root / relative / filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    size = candidate.stat().st_size
                    os.replace(candidate, target)
                    self.db.set_media_file(post_id, row["media_id"], str(relative / filename), size, extension)
            remaining = [r for r in self.db.post_media(post_id) if not r["relative_path"] or not
                         (self.archive_root / r["relative_path"]).is_file()]
            if remaining:
                self.db.set_post_status(post_id, "partial", f"缺少 {len(remaining)} 个媒体文件")
                self._log(log, "ERROR", source,
                          f"@{post['username']}/{post['shortcode']} 下载不完整：仍缺少 {len(remaining)} 项")
                return "failed"
            self.db.set_post_status(post_id, "complete")
            self._log(log, "INFO", source,
                      f"完成 @{post['username']}/{post['shortcode']}：保存 {len(missing)} 个媒体项到 {relative}")
            return "downloaded"
        except (GalleryError, OSError) as exc:
            self.db.set_post_status(post_id, "partial", str(exc)[:500])
            self._log(log, "ERROR", source, f"@{post['username']}/{post['shortcode']} 下载失败：{exc}")
            return "failed"

    def run(self, account, kind, log=None):
        counts = {"downloaded": 0, "skipped": 0, "failed": 0}
        error = ""
        if kind == "following":
            self.import_following(account, log)
            return counts, ""
        feeds = []
        if kind in ("all", "creators"):
            for creator in self.db.creators(account["id"]):
                if not creator["enabled"]:
                    continue
                name = creator["username"]
                for feed in ("posts", "reels"):
                    feeds.append((f"https://www.instagram.com/{name}/{feed}/",
                                  f"creator:{name}:{feed}", None if creator["full_sync"] else self.max_posts))
        if kind in ("all", "saved"):
            feeds.append((f"https://www.instagram.com/{account['username']}/saved/", "saved", None))
        for url, source, limit in feeds:
            try:
                display_source = source.replace("creator:", "@")
                limit_text = f"最新 {limit} 条" if limit is not None else "全部历史"
                self._log(log, "INFO", source, f"开始扫描 {display_source}（{limit_text}）")
                posts = self.gallery.posts(account["cookie_path"], url, limit)
                self._log(log, "INFO", source, f"元数据读取完成：找到 {len(posts)} 条帖子/Reels")
                for post in posts:
                    result = self._archive(account, post, source, log)
                    counts[result] += 1
                if source.startswith("creator:"):
                    self.db.mark_creator_sync(account["id"], source.split(":")[1])
            except (GalleryError, OSError, ValueError) as exc:
                counts["failed"] += 1
                error = (error + "; " + f"{source}: {exc}")[-1800:]
                self._log(log, "ERROR", source, f"来源扫描失败：{exc}")
        self._log(log, "INFO", kind,
                  f"本任务结束：下载 {counts['downloaded']}，跳过 {counts['skipped']}，失败 {counts['failed']}")
        return counts, error.lstrip("; ")


class Coordinator:
    def __init__(self, db, service, interval_hours=6):
        self.db, self.service = db, service
        self.interval = max(1, interval_hours) * 3600
        self.lock = threading.Lock()
        self.active = set()
        self.stopped = threading.Event()

    def start(self, account_id, kind):
        if kind not in ("all", "creators", "saved", "following"):
            raise ValueError("未知同步任务")
        account = self.db.account(account_id)
        if not account:
            raise ValueError("账号不存在")
        with self.lock:
            if account_id in self.active:
                raise ValueError("该账号有任务正在运行")
            self.active.add(account_id)
            run_id = self.db.create_run(account_id, kind)
            self.db.add_run_log(run_id, "INFO", kind, "任务已创建，等待执行")
        threading.Thread(target=self._execute, args=(run_id, account, kind), daemon=True).start()
        return run_id

    def _execute(self, run_id, account, kind):
        counts = {"downloaded": 0, "skipped": 0, "failed": 0}
        log = lambda level, source, message: self.db.add_run_log(run_id, level, source, message)
        try:
            log("INFO", kind, f"任务开始：账号 @{account['username']}，类型 {kind}")
            counts, message = self.service.run(account, kind, log)
            status = "partial" if counts["failed"] else "complete"
            self.db.finish_run(run_id, status, counts, message)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            log("ERROR", kind, f"任务异常：{message}")
            self.db.finish_run(run_id, "failed", counts, message)
        finally:
            with self.lock:
                self.active.discard(account["id"])

    def schedule(self):
        def loop():
            # Run once on startup, then periodically. Existing accounts only.
            while not self.stopped.is_set():
                for account in self.db.accounts():
                    try:
                        self.start(account["id"], "all")
                    except ValueError:
                        pass
                self.stopped.wait(self.interval)
        threading.Thread(target=loop, daemon=True).start()
