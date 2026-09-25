"""Scheduled and on-demand Instagram archiving."""

import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

from .engine import GalleryDL, GalleryError


def safe_part(value):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(value))[:80].strip(".") or "unknown"


def replace_from_staging(candidate, target):
    """Copy across mounts, then atomically replace from a temp file beside target."""
    candidate, target = Path(candidate), Path(target)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".part", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(candidate, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


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

    def _restore_legacy_media(self, post_id, row, log=None):
        """Copy files from the original /data/media volume into the configured archive mount."""
        if not row["relative_path"]:
            return
        relative = Path(row["relative_path"])
        legacy_root = (self.root / "media").resolve()
        try:
            if relative.is_absolute() or ".." in relative.parts:
                return
            archived = (self.archive_root / relative).resolve()
            archived.relative_to(self.archive_root.resolve())
            if self._file_is_complete(archived):
                return
            legacy = (legacy_root / relative).resolve()
            legacy.relative_to(legacy_root)
            if not legacy.is_file():
                return
            subdir = self.db.setting("media_subdir", "Instagram").strip("/")
            prefix = Path(subdir) if subdir else Path()
            target_relative = relative if not prefix.parts or relative.parts[:len(prefix.parts)] == prefix.parts \
                              else prefix / relative
            target = (self.archive_root / target_relative).resolve()
            target.relative_to(self.archive_root.resolve())
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy, target)
            self.db.set_media_file(post_id, row["media_id"], target_relative.as_posix(),
                                   target.stat().st_size, row["extension"])
            self._log(log, "INFO", "archive", f"旧归档已复制到设置的保存目录：{target_relative}")
        except (OSError, ValueError):
            # Keep the old record visible via the media endpoint's legacy-root fallback.
            return

    def _archive(self, account, post, source, log=None):
        post_id = self.db.upsert_post(account["id"], post, source)
        if self.db.post(post_id).get("deleted_at"):
            self._log(log, "INFO", source, f"跳过 {post['shortcode']}：记录已删除，保留去重标记")
            return "skipped"
        rows = self.db.post_media(post_id)
        for row in rows:
            self._restore_legacy_media(post_id, row, log)
        rows = self.db.post_media(post_id)
        missing = [row for row in rows if not row["relative_path"] or not
                   self._file_is_complete(self.archive_root / row["relative_path"])]
        if not missing:
            self.db.set_post_status(post_id, "complete")
            self._log(log, "INFO", source, f"跳过 @{post['username']}/{post['shortcode']}：文件已经完整")
            return "skipped"
        date = re.sub(r"\D", "", post["published_at"] or "")[:8] or "undated"
        folder = (account.get("creator_subdir") or self.db.setting("media_subdir", "Instagram")).strip("/")
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
                    target = (self.archive_root / relative / filename).resolve()
                    target.relative_to(self.archive_root.resolve())
                    target.parent.mkdir(parents=True, exist_ok=True)
                    size = candidate.stat().st_size
                    replace_from_staging(candidate, target)
                    self.db.set_media_file(post_id, row["media_id"], str(relative / filename), size, extension)
            remaining = [r for r in self.db.post_media(post_id) if not r["relative_path"] or not
                         self._file_is_complete(self.archive_root / r["relative_path"])]
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

    @staticmethod
    def _file_is_complete(path):
        try:
            return Path(path).is_file() and Path(path).stat().st_size > 0
        except OSError:
            return False

    def _scan(self, account, url, max_posts, cursor=None):
        scanner = getattr(self.gallery, "scan_posts", None)
        if scanner:
            return scanner(account["cookie_path"], url, max_posts=max_posts, cursor=cursor)
        return self.gallery.posts(account["cookie_path"], url, max_posts), None

    def _persist_scan(self, account, posts, source, creator=None):
        for post in posts:
            post_id = self.db.upsert_post(account["id"], post, source)
            post["id"] = post_id
            post["account_id"] = account["id"]
            post["status"] = self.db.post(post_id)["status"]
            post["scanned_this_run"] = True
            if creator:
                self.db.update_creator_profile(account["id"], creator,
                                               post.get("display_name", ""),
                                               post.get("avatar_url", ""),
                                               post.get("profile_id", ""))

    def _locally_complete(self, post, source, log=None):
        post_id = post.get("id") or self.db.upsert_post(post["account_id"], post, source)
        rows = self.db.post_media(post_id)
        for row in rows:
            self._restore_legacy_media(post_id, row, log)
        rows = self.db.post_media(post_id)
        return bool(rows) and all(row["relative_path"] and
                                  self._file_is_complete(self.archive_root / row["relative_path"])
                                  for row in rows)

    def _download_batch(self, account, source, posts, max_per_run=None, log=None):
        counts = {"downloaded": 0, "skipped": 0, "failed": 0}
        attempts = 0
        scanned_skips = 0
        deleted_skips = 0
        for post in posts:
            stored = self.db.post(post['id'])
            if stored and stored.get('deleted_at'):
                counts['skipped'] += 1
                deleted_skips += 1
                self._log(log, 'INFO', source, f"跳过 {post['shortcode']}：已删除记录（文件保留，禁止重复下载）")
                continue
            if post.get("status") == "complete" and not post.get("scanned_this_run"):
                counts["skipped"] += 1
                continue
            if self._locally_complete(post, source, log):
                self.db.set_post_status(post["id"], "complete")
                counts["skipped"] += 1
                scanned_skips += bool(post.get("scanned_this_run"))
                continue
            if max_per_run is not None and attempts >= max_per_run:
                continue
            attempts += 1
            result = self._archive(account, post, source, log)
            counts[result] += 1
        if counts["skipped"]:
            archived_skips = counts["skipped"] - deleted_skips
            self._log(log, "INFO", source,
                      f"跳过 {counts['skipped']} 条：已删除记录的去重保护 {deleted_skips} 条，"
                      f"文件已完整 {archived_skips} 条（其中本轮扫描到 {scanned_skips} 条）")
        queued = max(0, len(posts) - attempts - counts["skipped"])
        if queued:
            self._log(log, "INFO", source,
                      f"达到本轮下载上限；还有 {queued} 条待下载，将在后续同步继续")
        return counts

    def _run_creator(self, account, creator, log=None):
        account_id, name = account["id"], creator["username"]
        source = f"creator:{name}:posts"
        url = f"https://www.instagram.com/{name}/posts/"
        mode = creator["sync_mode"]
        max_per_run = int(creator["max_per_run"])
        self._log(log, "INFO", source,
                  f"开始同步 @{name}：{'最新 20 条' if mode == 'recent20' else '全部历史分批扫描'}，本轮最多下载 {max_per_run} 条")
        recent, _ = self._scan(account, url, 20)
        self._persist_scan(account, recent, source, name)
        self._log(log, "INFO", source, f"最新内容读取完成：{len(recent)} 条帖子")

        scanned = list(recent)
        next_cursor = creator.get("scan_cursor")
        history_complete = bool(creator.get("history_complete"))
        if mode == "all" and not history_complete:
            previous_cursor = next_cursor
            history, next_cursor = self._scan(account, url, 31, cursor=previous_cursor)
            self._persist_scan(account, history, source, name)
            scanned.extend(history)
            history_complete = next_cursor is None
            if next_cursor == previous_cursor and previous_cursor is not None:
                raise GalleryError("历史扫描游标没有前进，已暂停本轮，避免重复扫描")
            self.db.update_creator_scan(account_id, name, next_cursor, history_complete)
            state = "历史已经扫描完毕" if history_complete else "历史扫描进度已保存，下一轮从此处继续"
            self._log(log, "INFO", source, f"本轮读取 {len(history)} 条历史帖子；{state}")

        # Current page and persisted backlog are merged by shortcode. Complete
        # records are cheap skips; pending items precede partial failures so one
        # repeatedly failing post cannot block an entire history archive.
        backlog = self.db.creator_pending_posts(account_id, name) if mode == "all" else []
        by_shortcode = {post["shortcode"]: post for post in backlog}
        for post in scanned:
            by_shortcode[post["shortcode"]] = post
        candidates = list(by_shortcode.values())
        candidates.sort(key=lambda p: p.get("published_at") or "", reverse=True)
        # Partial failures follow pending items; an old failure cannot starve
        # the remaining history backlog.
        candidates.sort(key=lambda p: p.get("status") == "partial")
        counts = self._download_batch(account, source, candidates, max_per_run, log)
        return counts

    def run(self, account, kind, log=None, username=None):
        counts = {"downloaded": 0, "skipped": 0, "failed": 0}
        error = ""
        rate_limited = False
        if kind == "creator":
            creator = self.db.creator(account["id"], username or "")
            if not creator:
                raise ValueError("博主不存在")
            source = f"creator:{creator['username']}:posts"
            try:
                counts = self._run_creator(account, creator, log)
                if counts["failed"]:
                    errors = self.db.source_errors(account["id"], source)
                    error = errors[0] if errors else "有帖子下载失败，请展开运行日志查看详情"
                    rate_limited = bool(re.search(r"\b429\b|too many requests|rate.?limit", error, re.I))
            except Exception as exc:
                counts["failed"] += 1
                error = str(exc)[:1800]
                rate_limited = bool(re.search(r"\b429\b|too many requests|rate.?limit", error, re.I))
                self._log(log, "ERROR", source, f"同步博主失败：{exc}")
            finally:
                self.db.mark_creator_sync(account["id"], creator["username"], error, rate_limited)
        else:
            raise ValueError("未知同步任务")
        self._log(log, "INFO", kind,
                  f"本任务结束：下载 {counts['downloaded']}，跳过 {counts['skipped']}，失败 {counts['failed']}")
        return counts, error


class Coordinator:
    def __init__(self, db, service, interval_hours=6):
        self.db, self.service = db, service
        self.poll_interval = 15
        self.lock = threading.Lock()
        self.active = set()
        self.stopped = threading.Event()

    def start(self, account_id, kind, username=None):
        if kind not in ("creator", "check"):
            raise ValueError("未知同步任务")
        account = self.db.account(account_id)
        if not account:
            raise ValueError("账号不存在")
        if not account.get("cookie_path"):
            raise ValueError("账号尚未配置 Cookie，请先更新授权")
        if kind != "check" and not account.get("enabled", 1):
            raise ValueError("账号同步已暂停，请先启用")
        if kind == "creator":
            creator = self.db.creator(account_id, username or "")
            if not creator or not creator.get("manual"):
                raise ValueError("博主不存在或已移出监控列表")
        with self.lock:
            if account_id in self.active:
                raise ValueError("该账号有任务正在运行")
            self.active.add(account_id)
            run_kind = f"creator:@{username}" if kind == "creator" else "authorization-check"
            run_id = self.db.create_run(account_id, run_kind)
            self.db.add_run_log(run_id, "INFO", run_kind, "任务已创建，等待执行")
        threading.Thread(target=self._execute, args=(run_id, account, kind, username), daemon=True).start()
        return run_id

    def _execute(self, run_id, account, kind, username=None):
        counts = {"downloaded": 0, "skipped": 0, "failed": 0}
        log = lambda level, source, message: self.db.add_run_log(run_id, level, source, message)
        try:
            label = f"@{username}" if kind == "creator" else "授权检测"
            log("INFO", kind, f"任务开始：账号 @{account['username']}，来源 {label}")
            if kind == 'check':
                self.service.gallery.posts(account['cookie_path'], f"https://www.instagram.com/{account['username']}/posts/", 1)
                message = ''
                log('INFO', 'authorization', '已成功访问 Instagram 帖子接口；未下载媒体')
            else:
                counts, message = self.service.run(account, kind, log, username)
            status = "partial" if counts["failed"] else "complete"
            self.db.finish_run(run_id, status, counts, message)
            if kind == "check":
                with self.db.connect() as conn:
                    conn.execute("UPDATE accounts SET cookie_status='ok',cookie_checked_at=CURRENT_TIMESTAMP WHERE id=?", (account["id"],))
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            log("ERROR", kind, f"任务异常：{message}")
            if kind == "check":
                with self.db.connect() as c:
                    c.execute("UPDATE accounts SET cookie_status='error',cookie_checked_at=CURRENT_TIMESTAMP WHERE id=?",(account['id'],))
            self.db.finish_run(run_id, "failed", counts, message)
        finally:
            with self.lock:
                self.active.discard(account["id"])

    def schedule(self):
        def loop():
            while not self.stopped.is_set():
                self.db.purge_old_logs()
                if self.db.setting('scheduler_enabled','1') != '1':
                    self.stopped.wait(self.poll_interval)
                    continue
                for account in self.db.accounts():
                    account_id = account["id"]
                    if not account.get('enabled',1):
                        continue
                    with self.lock:
                        if account_id in self.active:
                            continue
                    try:
                        creator = self.db.due_creators(account_id)
                        if creator:
                            self.start(account_id, "creator", creator["username"])
                    except Exception:
                        pass
                self.stopped.wait(self.poll_interval)
        threading.Thread(target=loop, daemon=True).start()
