"""Scheduled and on-demand Instagram archiving."""

import logging
import os
import random
import re
import shutil
import tempfile
import threading
import time
import unicodedata
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from .engine import GalleryDL, GalleryError


def safe_part(value):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(value))[:80].strip(".") or "unknown"


def safe_title(value, fallback="untitled"):
    """Keep readable Unicode titles while removing unsafe path and filename characters."""
    value = unicodedata.normalize("NFC", str(value or ""))
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
    value = re.sub(r'[<>:"/\\|?*]+', "_", value)
    value = " ".join(value.split()).strip(" .")
    if not value:
        value = str(fallback or "untitled")
    # Leave room for dates, carousel indexes, extensions, and UTF-8 byte limits.
    while len(value.encode("utf-8")) > 150:
        value = value[:-1]
    value = value.rstrip(" .") or "untitled"
    if value.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                         *(f"LPT{i}" for i in range(1, 10))}:
        value = "_" + value
    return value


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

    def _archive_path(self, relative_path):
        """Resolve a stored relative path only inside the configured archive mount."""
        if not relative_path:
            return None
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            return None
        target = (self.archive_root / relative).resolve()
        try:
            target.relative_to(self.archive_root.resolve())
        except ValueError:
            return None
        return target

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
            if self._file_is_complete(archived, row.get("size", 0)):
                return
            legacy = (legacy_root / relative).resolve()
            legacy.relative_to(legacy_root)
            if not self._file_is_complete(legacy, row.get("size", 0)):
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

    def _archive(self, account, post, source, log=None, allow_legacy_restore=True):
        post_id = self.db.upsert_post(account["id"], post, source)
        rows = self.db.post_media(post_id)
        if allow_legacy_restore:
            for row in rows:
                self._restore_legacy_media(post_id, row, log)
        rows = self.db.post_media(post_id)
        missing = [row for row in rows if not self._archive_path(row["relative_path"]) or not
                   self._file_is_complete(self._archive_path(row["relative_path"]), row.get("size", 0))]
        if not missing:
            self.db.set_post_status(post_id, "complete")
            self._log(log, "INFO", source, f"跳过 @{post['username']}/{post['shortcode']}：文件已经完整")
            return "skipped"
        date = re.sub(r"\D", "", post["published_at"] or "")[:8] or "undated"
        folder = (account.get("creator_subdir") or self.db.setting("media_subdir", "Instagram")).strip("/")
        base = (Path(folder) if folder else Path()) / safe_part(account["username"]) \
               / safe_part(post["username"])
        title = safe_title(post.get("caption"), safe_part(post.get("shortcode", "")))
        post_folder = base / f"{date}{title}"

        # Keep legacy partial carousels together with their already verified files.
        legacy_row = next((row for row in rows
                           if row["relative_path"] and self._archive_path(row["relative_path"]) and
                           self._file_is_complete(self._archive_path(row["relative_path"]), row.get("size", 0)) and
                           Path(row["relative_path"]).stem ==
                           f"{int(row['position']):02d}-{safe_part(row['media_id'])}"), None)
        if legacy_row:
            relative = Path(legacy_row["relative_path"]).parent
            legacy_names = True
        else:
            prior_dirs = {Path(row["relative_path"]).parent for row in rows if row["relative_path"]}
            if post_folder not in prior_dirs and (
                    (self.archive_root / post_folder).exists() or
                    self.db.archive_folder_used(post_folder.as_posix(), post_id)):
                suffix = safe_part(post.get("shortcode", ""))
                relative = post_folder.with_name(f"{post_folder.name}_{suffix}")
                counter = 2
                while ((self.archive_root / relative).exists() or
                       self.db.archive_folder_used(relative.as_posix(), post_id)):
                    relative = post_folder.with_name(f"{post_folder.name}_{suffix}_{counter}")
                    counter += 1
            else:
                relative = post_folder
            legacy_names = False
        self._log(log, "INFO", source,
                  f"下载 @{post['username']}/{post['shortcode']}：缺少 {len(missing)} 个媒体项")
        try:
            with TemporaryDirectory(dir=self.root / "tmp") as temporary:
                media_ids = [row["media_id"] for row in missing]
                cookie_path = account.get("download_cookie_path")
                try:
                    files = self.gallery.download(None, post, temporary, media_ids=media_ids)
                    if not set(media_ids).issubset(files):
                        raise GalleryError("公开方式未取得全部需要的媒体文件")
                    self._log(log, "INFO", source,
                              f"@{post['username']}/{post['shortcode']}：公开方式下载成功")
                except GalleryError as public_error:
                    if not cookie_path or re.search(r"\b429\b|too many requests|rate.?limit",
                                                     str(public_error), re.I):
                        raise
                    self._log(log, "WARNING", source,
                              f"@{post['username']}/{post['shortcode']}：公开方式下载失败"
                              f"（{public_error}），尝试账号 Cookie")
                    files = self.gallery.download(cookie_path, post, temporary, media_ids=media_ids)
                for row in missing:
                    candidate = files.get(row["media_id"])
                    if not candidate:
                        continue
                    extension = safe_part(candidate.suffix.lstrip(".")).lower()
                    if extension not in ("jpg", "jpeg", "png", "webp", "gif", "mp4", "mov", "webm", "mkv"):
                        continue
                    if legacy_names:
                        stem = f"{int(row['position']):02d}-{safe_part(row['media_id'])}"
                    else:
                        stem = title + (str(int(row["position"])) if len(rows) > 1 else "")
                    filename = f"{stem}.{extension}"
                    target = (self.archive_root / relative / filename).resolve()
                    target.relative_to(self.archive_root.resolve())
                    target.parent.mkdir(parents=True, exist_ok=True)
                    size = candidate.stat().st_size
                    replace_from_staging(candidate, target)
                    self.db.set_media_file(post_id, row["media_id"], str(relative / filename), size, extension)
            remaining = [r for r in self.db.post_media(post_id) if not self._archive_path(r["relative_path"]) or not
                         self._file_is_complete(self._archive_path(r["relative_path"]), r.get("size", 0))]
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
    def _file_is_complete(path, expected_size=0):
        try:
            if not Path(path).is_file():
                return False
            actual_size = Path(path).stat().st_size
            return actual_size > 0 and (int(expected_size or 0) <= 0 or actual_size == int(expected_size))
        except OSError:
            return False

    def _scan(self, account, url, max_posts, cursor=None):
        scanner = getattr(self.gallery, "scan_posts", None)
        if scanner:
            return scanner(account["cookie_path"], url, max_posts=max_posts, cursor=cursor)
        return self.gallery.posts(account["cookie_path"], url, max_posts), None

    @staticmethod
    def _pause_history_scan():
        """Space out paged profile requests to reduce request bursts."""
        time.sleep(random.uniform(2, 9))

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

    def _locally_complete(self, post, source, log=None, allow_legacy_restore=True):
        post_id = post.get("id") or self.db.upsert_post(post["account_id"], post, source)
        rows = self.db.post_media(post_id)
        if allow_legacy_restore:
            for row in rows:
                self._restore_legacy_media(post_id, row, log)
        rows = self.db.post_media(post_id)
        return bool(rows) and all(self._archive_path(row["relative_path"]) and
                                  self._file_is_complete(self._archive_path(row["relative_path"]), row.get("size", 0))
                                  for row in rows)

    def _download_batch(self, account, source, posts, max_per_run=None, log=None,
                        shared_budget=None, processed=None):
        counts = {"downloaded": 0, "skipped": 0, "failed": 0}
        attempts = 0
        duplicates = 0
        scanned_skips = 0
        cleared_file_skips = 0
        category = source.rsplit(":", 1)[-1]
        content_label = "帖子网格" if category == "posts" else "Reels" if category == "reels" else category
        for post in posts:
            shortcode = post["shortcode"]
            if processed is not None and shortcode in processed:
                duplicates += 1
                prior = processed[shortcode]
                prior_category = prior.get("category", "") if isinstance(prior, dict) else ""
                prior_outcome = prior.get("outcome", "已处理") if isinstance(prior, dict) else "已处理"
                prior_label = "帖子网格" if prior_category == "posts" else "Reels" if prior_category == "reels" else prior_category
                self._log(log, "INFO", source,
                          f"跨类型去重 @{post['username']}/{shortcode}：同一作品已由{prior_label}处理（{prior_outcome}）；"
                          f"{content_label}关联到同一作品记录和归档文件，不重复下载")
                continue
            if processed is not None:
                processed[shortcode] = {"category": category, "outcome": "正在检查"}
            stored = self.db.post(post['id'])
            was_cleared = bool(stored and stored.get("deleted_at"))
            if not was_cleared and post.get("status") == "complete" and not post.get("scanned_this_run"):
                counts["skipped"] += 1
                if processed is not None:
                    processed[shortcode]["outcome"] = "记录已完整归档，跳过"
                continue
            if self._locally_complete(post, source, log, allow_legacy_restore=not was_cleared):
                self.db.set_post_status(post["id"], "complete")
                counts["skipped"] += 1
                scanned_skips += bool(post.get("scanned_this_run"))
                cleared_file_skips += bool(was_cleared)
                if processed is not None:
                    processed[shortcode]["outcome"] = "本地媒体完整，跳过"
                continue
            if not account["cookie_path"] and not account.get("download_cookie_path") and not all(
                    item.get("url") for item in post.get("items", [])):
                if processed is not None:
                    processed.pop(shortcode, None)
                self._log(log, "INFO", source,
                          f"暂缓 {shortcode}：匿名模式需要本轮扫描取得媒体地址；完整历史扫描可重新找到旧作品")
                continue
            if (shared_budget is not None and shared_budget["remaining"] <= 0) or (
                    max_per_run is not None and attempts >= max_per_run):
                if processed is not None:
                    processed[shortcode]["outcome"] = "共享下载上限已满，留待后续"
                continue
            if was_cleared:
                # A cleared record is only hidden while its files remain intact.
                # If files were removed, make the retry visible and archive them again.
                self.db.hide_records(account["id"], ids=[int(post["id"])], restore=True)
                self._log(log, "INFO", source,
                          f"恢复 {post['shortcode']} 的作品记录：本地媒体缺失，将重新下载缺少的项目")
            attempts += 1
            if shared_budget is not None:
                shared_budget["remaining"] -= 1
            result = self._archive(account, post, source, log, allow_legacy_restore=not was_cleared)
            counts[result] += 1
            if processed is not None:
                processed[shortcode]["outcome"] = {
                    "downloaded": "本轮已下载归档",
                    "skipped": "本地媒体完整，跳过",
                    "failed": "本轮下载失败",
                }[result]
            if result == "failed":
                error = (self.db.post(post["id"]) or {}).get("error") or ""
                if re.search(r"\b429\b|too many requests|rate.?limit", error, re.I):
                    counts["rate_limited"] = True
                    self._log(log, "WARNING", source,
                              "检测到 HTTP 429，本轮停止继续下载和翻页，等待调度退避")
                    break
        if counts["skipped"]:
            self._log(log, "INFO", source,
                      f"跳过 {counts['skipped']} 条：本轮扫描且归档文件完整 {scanned_skips} 条，"
                      f"其中清除记录后文件仍在 {cleared_file_skips} 条；未扫描的完整状态记录 "
                      f"{counts['skipped'] - scanned_skips} 条")
        queued = max(0, len(posts) - duplicates - attempts - counts["skipped"])
        if queued:
            self._log(log, "INFO", source,
                      f"达到本博主帖子网格与 Reels 共用的本轮下载上限；{content_label}还有 {queued} 条候选本轮未尝试，"
                      "保留在待处理队列中，将在后续同步继续")
        counts["cross_type_duplicates"] = duplicates
        counts["deferred"] = queued
        return counts

    def _run_creator_category(self, account, creator, category, shared_budget, processed, log=None):
        account_id, name = account["id"], creator["username"]
        source = f"creator:{name}:{category}"
        url = f"https://www.instagram.com/{name}/{'posts' if category == 'posts' else 'reels'}/"
        mode = creator["sync_mode"]
        content_label = "帖子网格" if category == "posts" else "Reels"
        verify_cursor_key = "verify_cursor" if category == "posts" else "reels_verify_cursor"
        all_history = mode == "all"
        self._log(log, "INFO", source,
                  f"开始同步 @{name} 的{content_label}："
                  f"{'最新 20 条' if not all_history else '全部历史逐页扫描（每轮从最新页重新检查）'}，"
                  f"本博主本轮下载上限 {shared_budget['limit']} 条（帖子网格与 Reels 合计）")
        # Snapshot the unfinished queue before the new metadata scan inserts its
        # discoveries. This lets the batch finish queued work before extending it.
        backlog = self.db.creator_pending_posts(account_id, name, category)
        backlog_statuses = Counter(post.get("status", "unknown") for post in backlog)
        self._log(log, "INFO", source,
                  f"本类别开始时已有待处理 {len(backlog)} 条：待下载 {backlog_statuses.get('pending', 0)} 条，"
                  f"失败待重试 {backlog_statuses.get('partial', 0)} 条")
        # Check a bounded page of complete records every run. This catches files
        # removed or truncated on disk, even when the post is outside recent20.
        verify_cursor = int(creator.get(verify_cursor_key) or 0)
        archived = self.db.creator_complete_posts(account_id, name, verify_cursor, limit=50,
                                                  category=category)
        if not archived and verify_cursor:
            verify_cursor = 0
            archived = self.db.creator_complete_posts(account_id, name, 0, limit=50,
                                                  category=category)
        repaired_candidates = 0
        repaired_posts = []
        for post in archived:
            if not self._locally_complete(post, source, log):
                self.db.set_post_status(post["id"], "partial", "归档文件缺失或大小与记录不一致")
                post["status"] = "partial"
                post["scanned_this_run"] = False
                post["queued_before_run"] = True
                repaired_posts.append(post)
                repaired_candidates += 1
        next_verify_cursor = archived[-1]["id"] if len(archived) == 50 else 0
        self.db.update_creator_verify_cursor(account_id, name, next_verify_cursor, category)
        self._log(log, "INFO", source,
                  f"归档完整性抽查 {len(archived)} 条；发现需补齐 {repaired_candidates} 条")

        # Merge retries and integrity repairs before newly discovered items.
        backlog_by_shortcode = {
            post["shortcode"]: {**post, "queued_before_run": True} for post in backlog
        }
        for post in repaired_posts:
            backlog_by_shortcode[post["shortcode"]] = {**post, "queued_before_run": True}

        result = {"downloaded": 0, "skipped": 0, "failed": 0,
                  "cross_type_duplicates": 0, "deferred": 0}

        def order_candidates(items):
            return sorted(items, key=lambda post: (
                0 if post.get("queued_before_run") and post.get("status") != "partial" else
                1 if post.get("queued_before_run") else 2,
                int(post.get("id") or 0),
            ))

        def process_batch(items):
            if not items:
                return
            batch = self._download_batch(account, source, order_candidates(items), log=log,
                                         shared_budget=shared_budget, processed=processed)
            for key in ("downloaded", "skipped", "failed", "cross_type_duplicates", "deferred"):
                result[key] += batch.get(key, 0)
            if batch.get("rate_limited"):
                result["error"] = "HTTP 429（本轮下载遇到限流）"

        scan_error = ""
        raw_scanned = 0
        recent_count = 0
        history_count = 0
        repeated_scan_rows = 0
        overlap_with_backlog = 0
        scanned_shortcodes = set()

        if not all_history:
            # Keep recent-only mode bounded to the requested 20 posts.
            recent, _ = self._scan(account, url, 20)
            self._persist_scan(account, recent, source, name)
            recent_count = len(recent)
            raw_scanned = len(recent)
            self._log(log, "INFO", source,
                      f"最新内容读取完成：{len(recent)} 条{content_label}作品")
            candidates = dict(backlog_by_shortcode) if (
                account["cookie_path"] or account.get("download_cookie_path")) else {}
            seen = set()
            for post in recent:
                shortcode = post["shortcode"]
                if shortcode in seen:
                    repeated_scan_rows += 1
                    continue
                seen.add(shortcode)
                scanned_shortcodes.add(shortcode)
                if shortcode in backlog_by_shortcode:
                    overlap_with_backlog += 1
                    candidates[shortcode] = {**post, "queued_before_run": True}
                    continue
                candidates[shortcode] = {**post, "queued_before_run": False}
            process_batch(list(candidates.values()))
        else:
            # Never trust a persisted "history complete" flag as a permanent
            # stop condition. Each all-history run restarts at the newest page,
            # follows the returned cursor, and uses shortcode/file checks to
            # skip archives already present on disk.
            self.db.update_creator_scan(account_id, name, None, False, category)
            self._log(log, "INFO", source,
                      "全历史扫描从最新页开始；逐页跟随 Instagram 游标，已归档作品按短码和本地文件跳过；"
                      "达到本轮下载上限后，下轮仍从最新页重新扫描")
            cursor = None
            requested_cursors = set()
            seen_in_walk = set()
            page_number = 0
            backlog_processed = False
            page_limit = 31
            if shared_budget["remaining"] <= 0:
                self._log(log, "INFO", source,
                          "本博主共享下载额度已由另一种内容类型用完；本轮不再请求此类别，"
                          "待后续任务从最新页继续检查")

            while shared_budget["remaining"] > 0:
                if cursor in requested_cursors:
                    scan_error = "Instagram 历史游标重复，已停止翻页，避免无限重复扫描"
                    break
                requested_cursors.add(cursor)
                try:
                    page, next_cursor = self._scan(account, url, page_limit, cursor=cursor)
                except Exception as exc:
                    scan_error = str(exc)
                    break

                page_number += 1
                self._persist_scan(account, page, source, name)
                raw_scanned += len(page)
                if page_number == 1:
                    recent_count = len(page)
                else:
                    history_count += len(page)

                page_candidates = {}
                if page_number == 1 and (account["cookie_path"] or account.get("download_cookie_path")):
                    page_candidates.update(backlog_by_shortcode)
                    backlog_processed = True
                new_on_page = 0
                for post in page:
                    shortcode = post["shortcode"]
                    if shortcode in seen_in_walk:
                        repeated_scan_rows += 1
                        continue
                    seen_in_walk.add(shortcode)
                    scanned_shortcodes.add(shortcode)
                    if shortcode in backlog_by_shortcode:
                        overlap_with_backlog += 1
                        page_candidates[shortcode] = {**post, "queued_before_run": True}
                        continue
                    page_candidates[shortcode] = {**post, "queued_before_run": False}
                    new_on_page += 1

                before_remaining = shared_budget["remaining"]
                before_downloaded = result["downloaded"]
                before_skipped = result["skipped"]
                before_failed = result["failed"]
                process_batch(list(page_candidates.values()))
                if result.get("error"):
                    break
                self._log(log, "INFO", source,
                          f"分页第 {page_number} 页读取 {len(page)} 条{content_label}作品，"
                          f"去重后新增候选 {new_on_page} 条；本页下载 "
                          f"{result['downloaded'] - before_downloaded}、跳过 "
                          f"{result['skipped'] - before_skipped}、失败 "
                          f"{result['failed'] - before_failed}；下载额度 "
                          f"{before_remaining}→{shared_budget['remaining']}；"
                          f"{'有下一页游标' if next_cursor else '未返回下一页游标'}")

                if not page:
                    self._log(log, "INFO", source,
                              f"第 {page_number} 页没有返回作品；本轮翻页结束，下轮仍从最新页复核")
                    break
                if next_cursor is None:
                    if len(page) >= page_limit:
                        scan_error = (f"第 {page_number} 页返回了 {len(page)} 条，但 Gallery-dl 没有提供下一页游标；"
                                      "无法确认历史已到末尾，本轮暂停翻页，下轮会从最新页重试")
                    else:
                        self._log(log, "INFO", source,
                                  f"第 {page_number} 页未返回下一页游标且少于 {page_limit} 条；"
                                  "本轮到达当前接口可读取末页；无法仅据此证明与 Instagram 主页展示总数完全一致，"
                                  "下轮仍会从最新页重新检查")
                    break
                if next_cursor == cursor or next_cursor in requested_cursors:
                    scan_error = "Instagram 历史游标没有前进，已停止翻页，避免无限重复扫描"
                    break
                if shared_budget["remaining"] <= 0:
                    self._log(log, "INFO", source,
                              f"达到本博主本轮下载上限；已保存第 {page_number} 页发现的作品，"
                              "本轮不再请求更旧页面，下轮从最新页重新扫描并跳过已归档作品")
                    break
                cursor = next_cursor
                self._pause_history_scan()

            if not backlog_processed and (account["cookie_path"] or account.get("download_cookie_path")):
                process_batch(list(backlog_by_shortcode.values()))
            if not account["cookie_path"] and not account.get("download_cookie_path"):
                unscanned = len(set(backlog_by_shortcode) - scanned_shortcodes)
                if unscanned:
                    self._log(log, "INFO", source,
                              f"匿名模式暂留 {unscanned} 条旧队列作品：本轮未扫描到媒体地址，后续完整历史扫描继续处理")
            if scan_error:
                self._log(log, "ERROR", source, f"全历史分页未能确认完成：{scan_error}")
                result["failed"] += 1
                result["error"] = scan_error

            unique_scanned = len(scanned_shortcodes)
            self._log(log, "INFO", source,
                      f"本轮全历史扫描明细：读取 {page_number} 页，最新页 {recent_count} 条、"
                      f"后续历史页 {history_count} 条；原始返回 {raw_scanned} 条，"
                      f"按作品短码去重后 {unique_scanned} 条；与开始前待处理队列重合 "
                      f"{overlap_with_backlog} 条，分页重复 {repeated_scan_rows} 条")

        if not all_history:
            unique_scanned = len(scanned_shortcodes)
            self._log(log, "INFO", source,
                      f"本轮扫描明细：最新内容 {recent_count} 条，历史内容 0 条；"
                      f"按作品短码合并后 {unique_scanned} 条，与开始前待处理队列重合 {overlap_with_backlog} 条，"
                      f"扫描页内部重复 {repeated_scan_rows} 条；不在旧队列中的扫描作品 "
                      f"{len(scanned_shortcodes - set(backlog_by_shortcode))} 条")

        pending_after = self.db.creator_pending_posts(account_id, name, category)
        pending_statuses = Counter(post.get("status", "unknown") for post in pending_after)
        result["pending_after"] = len(pending_after)
        result["pending_statuses"] = dict(pending_statuses)
        self._log(log, "INFO", source,
                  f"{content_label}本轮统计：下载 {result['downloaded']} 条作品，已归档跳过 {result['skipped']} 条，"
                  f"失败 {result['failed']} 条，跨类型短码去重 {result['cross_type_duplicates']} 条，"
                  f"因共享上限留待后续 {result['deferred']} 条；本类别结束后待处理队列 "
                  f"{len(pending_after)} 条（待下载 {pending_statuses.get('pending', 0)} 条，"
                  f"失败待重试 {pending_statuses.get('partial', 0)} 条）；"
                  f"本博主共享下载额度剩余 {shared_budget['remaining']}/{shared_budget['limit']} 条")
        return result

    def _run_creator(self, account, creator, log=None):
        account_id, name = account["id"], creator["username"]
        categories = self.db._parse_sync_types(creator.get("sync_types", "posts"))
        turn = creator.get("sync_turn", "posts")
        if turn in categories:
            start_at = categories.index(turn)
            categories = categories[start_at:] + categories[:start_at]
        maximum = int(creator["max_per_run"])
        shared_budget = {"limit": maximum, "remaining": maximum}
        processed = {}
        counts = {"downloaded": 0, "skipped": 0, "failed": 0}
        category_errors = []
        category_results = {}
        for category in categories:
            try:
                result = self._run_creator_category(account, creator, category,
                                                    shared_budget, processed, log)
                category_results[category] = result
                for key in counts:
                    counts[key] += result[key]
                if result.get("error"):
                    category_errors.append((category, GalleryError(result["error"])))
                    if re.search(r"\b429\b|too many requests|rate.?limit", result["error"], re.I):
                        self._log(log, "WARNING", f"creator:{name}:{category}",
                                  "检测到限流，本轮不再请求另一种内容类型")
                        break
            except Exception as exc:
                category_errors.append((category, exc))
                counts["failed"] += 1
                category_results[category] = {
                    "downloaded": 0, "skipped": 0, "failed": 1,
                    "cross_type_duplicates": 0, "deferred": 0,
                    "pending_after": 0, "pending_statuses": {},
                }
                source = f"creator:{name}:{category}"
                label = "帖子网格" if category == "posts" else "Reels"
                self._log(log, "ERROR", source, f"{label}同步失败：{exc}")
                if re.search(r"\b429\b|too many requests|rate.?limit", str(exc), re.I):
                    self._log(log, "WARNING", source, "检测到限流，本轮不再请求另一种内容类型")
                    break
        category_summaries = []
        for category in ("posts", "reels"):
            label = "帖子网格" if category == "posts" else "Reels"
            if category not in categories:
                category_summaries.append(f"{label}未启用")
                continue
            result = category_results.get(category, {})
            category_summaries.append(
                f"{label}下载 {result.get('downloaded', 0)} 条、跳过 {result.get('skipped', 0)} 条、"
                f"失败 {result.get('failed', 0)} 条、跨类型去重 {result.get('cross_type_duplicates', 0)} 条、"
                f"待处理 {result.get('pending_after', 0)} 条")
        self._log(log, "INFO", f"creator:{name}",
                  "本博主本轮分类汇总：" + "；".join(category_summaries) +
                  f"；两类合计实际下载 {counts['downloaded']} 条唯一作品，"
                  f"共享下载上限 {shared_budget['limit']} 条，剩余 {shared_budget['remaining']} 条")
        if len(categories) > 1:
            self.db.set_creator_sync_turn(account_id, name, categories[1])
        creator["_sync_error"] = str(category_errors[0][1])[:1800] if category_errors else ""
        return counts

    def run(self, account, kind, log=None, username=None):
        counts = {"downloaded": 0, "skipped": 0, "failed": 0}
        error = ""
        rate_limited = False
        if kind == "creator":
            creator = self.db.creator(account["id"], username or "")
            if not creator or int(creator.get("manual", 0)) != 1:
                raise ValueError("博主不存在或已移出监控列表")
            sources = [f"creator:{creator['username']}:{category}"
                       for category in self.db._parse_sync_types(creator.get("sync_types", "posts"))]
            source = sources[0]
            try:
                access_mode = creator.get("access_mode", "cookie")
                cookie_allowed = bool(account.get("enabled", 1) and account.get("cookie_path"))
                scan_cookie = account["cookie_path"] if access_mode == "cookie" and cookie_allowed else None
                selected_account = {**account, "cookie_path": scan_cookie,
                                    "download_cookie_path": account["cookie_path"] if cookie_allowed else None}
                self._log(log, "INFO", source,
                          f"扫描方式：{'账号 Cookie' if scan_cookie else '公开访问（无 Cookie）'}；"
                          f"媒体下载：公开方式优先{'，失败后可用 Cookie 再试' if cookie_allowed else '，Cookie 不可用'}")
                if access_mode == "cookie" and not scan_cookie:
                    self._log(log, "WARNING", source,
                              "博主配置使用 Cookie 扫描，但授权未启用或未上传 Cookie；本轮改用公开扫描，"
                              "结果可能不完整")
                counts = self._run_creator(selected_account, creator, log)
                if counts["failed"]:
                    errors = [message for item_source in sources
                              for message in self.db.source_errors(account["id"], item_source)]
                    error = errors[0] if errors else (
                        creator.get("_sync_error", "") or
                        "有帖子或 Reels 下载失败，请展开运行日志查看详情")
                    rate_limited = bool(re.search(r"\b429\b|too many requests|rate.?limit",
                                                  error + " " + creator.get("_sync_error", ""), re.I))
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
        if kind == "check":
            if not account.get("enabled", 1):
                raise ValueError("当前禁止使用 Cookie，请先启用后再检测")
            if not account.get("cookie_path"):
                raise ValueError("尚未配置 Cookie，无法进行授权检测")
        if kind == "creator":
            creator = self.db.creator(account_id, username or "")
            if not creator or int(creator.get("manual", 0)) != 1:
                raise ValueError("博主不存在或已移出监控列表")
        with self.lock:
            if account_id in self.active:
                raise ValueError("该账号有任务正在运行")
            self.active.add(account_id)
        run_id = None
        run_kind = f"creator:@{username}" if kind == "creator" else "authorization-check"
        try:
            run_id = self.db.create_run(account_id, run_kind)
            self.db.add_run_log(run_id, "INFO", run_kind, "任务已创建，等待执行")
            worker = threading.Thread(target=self._execute,
                                      args=(run_id, account, kind, username), daemon=True)
            worker.start()
        except Exception as exc:
            with self.lock:
                self.active.discard(account_id)
            if run_id:
                try:
                    self.db.finish_run(run_id, "failed", {"downloaded": 0, "skipped": 0, "failed": 1},
                                       f"任务启动失败：{type(exc).__name__}: {exc}")
                    self.db.add_run_log(run_id, "ERROR", run_kind, f"任务启动失败：{exc}")
                except Exception:
                    logging.exception("failed to persist task startup error")
            raise
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
        def record_error(message):
            try:
                self.db.add_system_event("ERROR", "scheduler", message)
            except Exception:
                logging.exception("failed to persist scheduler error: %s", message)

        def loop():
            while not self.stopped.is_set():
                try:
                    self.db.purge_old_logs()
                    if self.db.setting('scheduler_enabled','1') == '1':
                        for account in self.db.accounts():
                            account_id = account["id"]
                            with self.lock:
                                if account_id in self.active:
                                    continue
                            try:
                                creator = self.db.due_creators(account_id)
                                if creator:
                                    self.start(account_id, "creator", creator["username"])
                            except Exception as exc:
                                record_error(f"账号 {account.get('username', account_id)} 的调度失败："
                                             f"{type(exc).__name__}: {exc}")
                except Exception as exc:
                    message = f"调度循环异常：{type(exc).__name__}: {exc}"
                    record_error(message)
                self.stopped.wait(self.poll_interval)
        self.scheduler_thread = threading.Thread(target=loop, daemon=True)
        self.scheduler_thread.start()
        return self.scheduler_thread
