import errno
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from insnet.db import Database
from insnet.entrypoint import _check_archive_access
from insnet.engine import GalleryDL, GalleryError, normalize_messages, redact_output
from insnet.sync import Coordinator, SyncService
from insnet.web import valid_cookie_file


def post(code, username="creator", date="2026-09-01", media_count=1):
    return {
        "shortcode": code,
        "username": username,
        "caption": "caption " + code,
        "published_at": date,
        "source_url": f"https://www.instagram.com/p/{code}/",
        "display_name": "Display " + username,
        "avatar_url": "https://cdn.example/avatar.jpg",
        "profile_id": "1234",
        "items": [
            {"media_id": f"{code}-{index}", "position": index,
             "kind": "image", "extension": "jpg"}
            for index in range(1, media_count + 1)
        ],
    }


MESSAGES = [
    [2, {"post_shortcode": "ABC123", "username": "creator", "post_date": "2026-09-01",
         "description": "a carousel", "user": {"username": "creator", "full_name": "Creator Name",
         "profile_pic_url": "https://cdn.example/avatar.jpg", "pk": "1234"}}],
    [3, "https://cdn.example/a.jpg", {"media_id": "101", "extension": "jpg"}],
    [3, "https://cdn.example/b.mp4", {"media_id": "102", "extension": "mp4"}],
]


class FakeGallery:
    def __init__(self, posts=None):
        self.calls = 0
        self.partial = False
        self.posts_data = posts or [post("ABC123", media_count=2)]
        self.post_requests = []
        self.scan_requests = []
        self.history_responses = {}
        self.download_requests = []

    def posts(self, cookie, url, max_posts):
        self.post_requests.append((url, max_posts))
        items = list(self.posts_data)
        return items if max_posts is None else items[:max_posts]

    def scan_posts(self, cookie, url, max_posts=None, cursor=None):
        self.scan_requests.append((url, max_posts, cursor))
        if max_posts == 20:
            return list(self.posts_data[:20]), None
        return self.history_responses.get(cursor, ([], None))

    def download(self, cookie, item, staging, media_ids=None):
        self.calls += 1
        selected = set(media_ids or [media["media_id"] for media in item["items"]])
        self.download_requests.append(selected)
        path = Path(staging)
        for media in item["items"]:
            if media["media_id"] not in selected:
                continue
            if self.partial and media["position"] > 1:
                continue
            (path / f"{media['media_id']}.jpg").write_bytes(b"image:" + media["media_id"].encode())
        return {p.stem: p for p in path.iterdir()}


class ArchiveTests(unittest.TestCase):
    def setup_service(self, root, fake=None):
        (root / "tmp").mkdir(exist_ok=True)
        archive = root / "archive"
        archive.mkdir(exist_ok=True)
        db = Database(root)
        account_id = db.add_account("owner", root / "cookies.txt")
        return db, db.account(account_id), fake or FakeGallery(), archive

    def test_archive_access_check_preserves_host_folder_owner_and_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary)
            before = archive.stat()
            _check_archive_access(archive)
            after = archive.stat()
            self.assertEqual((before.st_uid, before.st_gid, before.st_mode),
                             (after.st_uid, after.st_gid, after.st_mode))
            self.assertFalse(any(path.name.startswith(".insnet-write-test-") for path in archive.iterdir()))

    def test_gallery_messages_keep_carousel_and_creator_profile(self):
        posts = normalize_messages(MESSAGES)
        self.assertEqual([x["kind"] for x in posts[0]["items"]], ["image", "video"])
        self.assertEqual(posts[0]["shortcode"], "ABC123")
        self.assertEqual(posts[0]["display_name"], "Creator Name")
        self.assertEqual(posts[0]["avatar_url"], "https://cdn.example/avatar.jpg")
        self.assertEqual(posts[0]["profile_id"], "1234")
        self.assertIn("/p/", posts[0]["source_url"])

    def test_scan_posts_extracts_gallery_dl_cursor_and_profile(self):
        gallery = GalleryDL()
        stderr = "[instagram][debug] Cursor: QVFDLTIwMjY="
        with patch.object(gallery, "_run", return_value=(json.dumps(MESSAGES), stderr)) as fetch:
            items, cursor = gallery.scan_posts("cookies.txt", "https://www.instagram.com/creator/posts/",
                                               max_posts=31, cursor="old")
        self.assertEqual(cursor, "QVFDLTIwMjY=")
        self.assertEqual(items[0]["display_name"], "Creator Name")
        args, kwargs = fetch.call_args
        self.assertEqual(args[0], "cookies.txt")
        self.assertEqual(args[1][-1], "https://www.instagram.com/creator/posts/")
        self.assertIn("-j", args[1])
        self.assertIn("--verbose", args[1])
        self.assertIn("extractor.instagram.max-posts=31", args[1])
        self.assertIn("extractor.instagram.cursor=old", args[1])
        self.assertTrue(kwargs["with_stderr"])

    def test_regular_posts_unpacks_json_messages(self):
        gallery = GalleryDL()
        with patch.object(gallery, "_run", return_value=(json.dumps(MESSAGES), "")):
            items = gallery.posts("cookies.txt", "https://www.instagram.com/owner/posts/")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["shortcode"], "ABC123")

    def test_partial_download_retries_then_skips_completed_creator_post(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = FakeGallery()
            fake.partial = True
            db, account, fake, archive = self.setup_service(root, fake)
            service = SyncService(db, root, fake, archive_root=archive)
            db.add_creator(account["id"], "creator")
            counts, error = service.run(account, "creator", username="creator")
            self.assertEqual(counts["failed"], 1)
            self.assertEqual(db.posts(account["id"])[0]["status"], "partial")
            fake.partial = False
            counts, error = service.run(account, "creator", username="creator")
            self.assertEqual(counts["downloaded"], 1)
            archived = db.posts(account["id"])[0]
            self.assertEqual(archived["status"], "complete")
            self.assertEqual(archived["sources"], ["creator:creator:posts"])
            self.assertTrue(all((archive / item["relative_path"]).is_file() for item in archived["media"]))
            self.assertEqual(fake.calls, 2)
            self.assertTrue(all("/reels/" not in url for url, _, _ in fake.scan_requests))
            counts, error = service.run(account, "creator", username="creator")
            self.assertEqual(counts["skipped"], 1)
            self.assertEqual(fake.calls, 2)

    def test_creator_download_cap_is_per_cycle_and_completed_posts_are_skipped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            items = [post(f"P{i}", date=f"2026-09-{i:02d}") for i in range(1, 5)]
            db, account, fake, archive = self.setup_service(root, FakeGallery(items))
            db.add_creator(account["id"], "creator")
            db.save_admin_config({"creator_interval":360,"creator_max":2,"scheduler_enabled":1,"log_days":30})
            service = SyncService(db, root, fake, archive_root=archive)
            first, _ = service.run(account, "creator", username="creator")
            self.assertEqual(first["downloaded"], 2)
            self.assertEqual(len(db.posts(account["id"])), 4)
            second, _ = service.run(account, "creator", username="creator")
            self.assertEqual(second["downloaded"], 2)
            self.assertEqual(second["skipped"], 2)
            self.assertEqual(fake.calls, 4)
            self.assertEqual(sum(item["status"] == "complete" for item in db.posts(account["id"])), 4)

    def test_scanned_post_with_a_missing_media_file_is_repaired(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db, account, fake, archive = self.setup_service(root)
            db.add_creator(account["id"], "creator")
            service = SyncService(db, root, fake, archive_root=archive)
            initial, _ = service.run(account, "creator", username="creator")
            self.assertEqual(initial["downloaded"], 1)
            records = db.posts(account["id"])[0]
            missing = archive / records["media"][0]["relative_path"]
            missing.unlink()
            repaired, _ = service.run(account, "creator", username="creator")
            self.assertEqual(repaired["downloaded"], 1)
            self.assertTrue(missing.is_file())
            self.assertEqual(fake.calls, 2)

    def test_recent20_continues_already_queued_posts_before_new_feed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = [post(f"OLD{i:02d}", date=f"2026-08-{(i % 28) + 1:02d}") for i in range(20)]
            db, account, fake, archive = self.setup_service(root, FakeGallery(old))
            db.add_creator(account["id"], "creator")
            db.save_admin_config({"creator_interval":360,"creator_max":1,"scheduler_enabled":1,"log_days":30})
            service = SyncService(db, root, fake, archive_root=archive)
            first, _ = service.run(account, "creator", username="creator")
            self.assertEqual(first["downloaded"], 1)

            fake.posts_data = [post(f"NEW{i:02d}", date=f"2026-09-{(i % 28) + 1:02d}") for i in range(20)]
            second, _ = service.run(account, "creator", username="creator")
            self.assertEqual(second["downloaded"], 1)
            self.assertTrue(next(iter(fake.download_requests[-1])).startswith("OLD"))
            self.assertEqual(len(db.posts(account["id"])), 40)
            self.assertEqual(sum(row["status"] == "pending" for row in db.posts(account["id"])), 38)

    def test_truncated_archive_is_redownloaded_using_manifest_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db, account, fake, archive = self.setup_service(root)
            db.add_creator(account["id"], "creator")
            service = SyncService(db, root, fake, archive_root=archive)
            first, _ = service.run(account, "creator", username="creator")
            self.assertEqual(first["downloaded"], 1)
            media = db.posts(account["id"])[0]["media"][0]
            target = archive / media["relative_path"]
            expected = target.stat().st_size
            target.write_bytes(b"x")
            self.assertNotEqual(target.stat().st_size, expected)

            repaired, _ = service.run(account, "creator", username="creator")
            self.assertEqual(repaired["downloaded"], 1)
            self.assertEqual(target.stat().st_size, expected)
            self.assertEqual(fake.download_requests[-1], {media["media_id"]})

    def test_integrity_audit_repairs_archived_post_outside_recent_feed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            older = post("OLDARCHIVE", date="2024-01-01")
            db, account, fake, archive = self.setup_service(root, FakeGallery([post("NEWFEED")]))
            db.add_creator(account["id"], "creator")
            post_id = db.upsert_post(account["id"], older, "creator:creator:posts")
            target = archive / "old" / "image.jpg"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"complete-file")
            db.set_media_file(post_id, older["items"][0]["media_id"], "old/image.jpg", target.stat().st_size, "jpg")
            db.set_post_status(post_id, "complete")
            target.unlink()

            service = SyncService(db, root, fake, archive_root=archive)
            counts, _ = service.run(account, "creator", username="creator")
            record = next(p for p in db.posts(account["id"]) if p["shortcode"] == "OLDARCHIVE")
            self.assertEqual(counts["downloaded"], 2)
            self.assertEqual(record["status"], "complete")
            self.assertTrue((archive / record["media"][0]["relative_path"]).is_file())

    def test_media_download_filter_selects_only_missing_carousel_item(self):
        gallery = GalleryDL()
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary)
            with patch.object(gallery, "_run", return_value="") as run:
                gallery.download("cookies.txt", post("CAROUSEL"), staging, media_ids=["CAROUSEL-2"])
            args = run.call_args.args[1]
            self.assertEqual(args[0:2], ["--filter", "media_id in ('CAROUSEL-2',)"])
            self.assertIn("https://www.instagram.com/p/CAROUSEL/", args)

    def test_all_history_cursor_advances_in_bounded_batches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            head = [post("NEW1", date="2026-09-20")]
            fake = FakeGallery(head)
            fake.history_responses[None] = ([post("OLD1", date="2024-01-01")], "cursor-B")
            fake.history_responses["cursor-B"] = ([post("OLD2", date="2023-01-01")], None)
            db, account, fake, archive = self.setup_service(root, fake)
            db.add_creator(account["id"], "creator")
            db.set_creator(account["id"], "creator", sync_mode="all")
            db.save_admin_config({"creator_interval":360,"creator_max":1,"scheduler_enabled":1,"log_days":30})
            service = SyncService(db, root, fake, archive_root=archive)
            first, _ = service.run(account, "creator", username="creator")
            self.assertEqual(first["downloaded"], 1)
            self.assertEqual(db.creator(account["id"], "creator")["scan_cursor"], "cursor-B")
            second, _ = service.run(account, "creator", username="creator")
            creator = db.creator(account["id"], "creator")
            self.assertEqual(creator["scan_cursor"], None)
            self.assertEqual(creator["history_complete"], 1)
            history_requests = [(size, cursor) for _, size, cursor in fake.scan_requests if size == 31]
            self.assertEqual(history_requests, [(31, None), (31, "cursor-B")])
            self.assertEqual(len(db.posts(account["id"])), 3)
            self.assertLessEqual(first["downloaded"] + second["downloaded"], 2)
            self.assertTrue(any(item["status"] == "pending" for item in db.posts(account["id"])))

    def test_only_manual_creator_is_scheduled_and_post_feed_has_no_reels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db, account, fake, archive = self.setup_service(root)
            db.add_creator(account["id"], "selected")
            db.add_creator(account["id"], "paused")
            db.set_creator(account["id"], "selected", enabled=False)
            db.set_creator(account["id"], "paused", enabled=False)
            due = db.due_creators(account["id"])
            self.assertIsNone(due)
            db.set_creator(account["id"], "selected", enabled=True)
            due = db.due_creators(account["id"])
            self.assertEqual(due["username"], "selected")
            fake.posts_data = [post("SEL", username="selected")]
            service = SyncService(db, root, fake, archive_root=archive)
            service.run(account, "creator", username="selected")
            self.assertEqual(len(fake.scan_requests), 1)
            self.assertTrue(fake.scan_requests[0][0].endswith("/selected/posts/"))
            self.assertFalse(any("reels" in item[0] for item in fake.scan_requests))

    def test_archive_copies_across_mounts_before_atomic_replace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db, account, fake, archive = self.setup_service(root)
            service = SyncService(db, root, fake, archive_root=archive)
            real_replace = os.replace
            def fail_on_cross_mount(src, dst):
                if Path(src).parent.resolve() != Path(dst).parent.resolve():
                    raise OSError(errno.EXDEV, "Invalid cross-device link")
                return real_replace(src, dst)
            with patch("insnet.sync.os.replace", side_effect=fail_on_cross_mount):
                db.add_creator(account["id"], "creator")
                counts, error = service.run(account, "creator", username="creator")
            self.assertEqual(counts["downloaded"], 1)
            self.assertEqual(counts["failed"], 0)
            self.assertEqual(error, "")
            media = db.posts(account["id"])[0]["media"]
            self.assertTrue(all((archive / row["relative_path"]).is_file() for row in media))

    def test_creator_delete_hides_records_but_keeps_media_files_and_dedupe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db, account, _, _ = self.setup_service(root)
            account_id = account["id"]
            db.add_creator(account_id, "alice")
            original = post("KEEPFILE", username="alice")
            post_id = db.upsert_post(account_id, original, "creator:alice:posts")
            marker = root / "archive" / "alice" / "video.jpg"
            marker.parent.mkdir(parents=True)
            marker.write_bytes(b"archive")
            db.set_media_file(post_id, original["items"][0]["media_id"], "alice/video.jpg", 7, "jpg")
            db.set_post_status(post_id, "complete")
            result = db.delete_creator(account_id, "alice", delete_archive=True)
            self.assertEqual(result["removed_posts"], 1)
            self.assertEqual(result["files"], [])
            self.assertTrue(marker.is_file())
            self.assertIsNotNone(db.post(post_id))
            self.assertTrue(db.post(post_id)["deleted_at"])
            self.assertEqual(db.records({"account": account_id, "deleted": "0"})["total"], 0)
            self.assertEqual(db.records({"account": account_id, "deleted": "1"})["total"], 1)
            # Re-seeing the post leaves the tombstone in place so it is not downloaded again.
            db.upsert_post(account_id, original, "saved")
            self.assertTrue(db.post(post_id)["deleted_at"])

    def test_deleted_posts_are_reported_as_dedupe_skips_not_complete_archives(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = post("DELETED", username="creator")
            db, account, fake, archive = self.setup_service(root, FakeGallery([original]))
            db.add_creator(account["id"], "creator")
            post_id = db.upsert_post(account["id"], original, "creator:creator:posts")
            marker = archive / "creator" / "deleted.jpg"
            marker.parent.mkdir(parents=True)
            marker.write_bytes(b"archive")
            db.set_media_file(post_id, original["items"][0]["media_id"], "creator/deleted.jpg", 7, "jpg")
            db.set_post_status(post_id, "complete")
            db.hide_records(account["id"], username="creator")

            logs = []
            service = SyncService(db, root, fake, archive_root=archive)
            counts, error = service.run(account, "creator", log=lambda *entry: logs.append(entry), username="creator")

            self.assertEqual((counts["downloaded"], counts["skipped"], counts["failed"]), (0, 1, 0))
            self.assertEqual(error, "")
            summary = next(message for _, _, message in logs if message.startswith("跳过 1 条："))
            self.assertIn("隐藏记录的去重保护 1 条（媒体未校验）", summary)
            self.assertIn("已验证文件完整 0 条（其中本轮扫描到 0 条）", summary)

    def test_removing_creator_from_list_keeps_posts_and_file_relations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db, account, _, _ = self.setup_service(root)
            db.add_creator(account["id"], "creator")
            data = post("KEEP")
            post_id = db.upsert_post(account["id"], data, "creator:creator:posts")
            result = db.delete_creator(account["id"], "creator", delete_archive=False)
            self.assertEqual(result["files"], [])
            self.assertEqual(result["removed_posts"], 0)
            self.assertIsNotNone(db.post(post_id))
            self.assertEqual(db.posts(account["id"])[0]["sources"], ["creator:creator:posts"])
            db.add_creator(account["id"], "creator")
            self.assertEqual(db.creators(account["id"])[0]["username"], "creator")

    def test_old_database_migrates_once_and_clears_imported_creators(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "insnet.sqlite3"
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE accounts(id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                    cookie_path TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE creators(account_id TEXT NOT NULL, username TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, full_sync INTEGER NOT NULL DEFAULT 0,
                    manual INTEGER NOT NULL DEFAULT 1, last_sync TEXT,
                    PRIMARY KEY(account_id,username));
                INSERT INTO accounts(id,username,cookie_path) VALUES('a','owner','cookies.txt');
                INSERT INTO creators(account_id,username,enabled,full_sync,manual)
                    VALUES('a','keep',1,1,1),('a','imported',1,0,0);
            """)
            conn.commit(); conn.close()
            db = Database(temporary)
            self.assertEqual(db.creator("a", "keep")["sync_mode"], "all")
            self.assertEqual(db.creator("a", "imported")["manual"], 2)
            self.assertNotIn("imported", [c["username"] for c in db.creators("a")])
            db.set_creator("a", "keep", sync_mode="recent20")
            reopened = Database(temporary)
            self.assertEqual(reopened.creator("a", "keep")["sync_mode"], "recent20")

    def test_429_backoff_uses_global_creator_interval(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Database(temporary)
            account_id = db.add_account("owner", Path(temporary) / "cookies.txt")
            db.add_creator(account_id, "creator")
            db.save_admin_config({"creator_interval":30,"creator_max":20,"scheduler_enabled":1,"log_days":30})
            db.mark_creator_sync(account_id, "creator", "HTTP 429", rate_limited=True)
            creator = db.creator(account_id, "creator")
            self.assertEqual(creator["failures"], 1)
            self.assertEqual(creator["last_error"], "HTTP 429")
            self.assertFalse(db.due_creators(account_id))
            self.assertEqual(db.admin_config()["creator_interval"], 30)

    def test_failure_log_contains_diagnostics_and_redacts_cookie(self):
        with tempfile.TemporaryDirectory() as temporary:
            cookie = Path(temporary) / "cookies.txt"
            cookie.write_text(".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tsuper_secret_token\n")
            with patch("insnet.engine.subprocess.run", return_value=SimpleNamespace(
                    returncode=1, stderr="HTTP 401 super_secret_token", stdout="")):
                with self.assertRaises(GalleryError) as caught:
                    GalleryDL()._run(cookie, ["https://www.instagram.com/"])
            self.assertIn("HTTP 401", str(caught.exception))
            self.assertNotIn("super_secret_token", str(caught.exception))

    def test_redaction_preserves_urls_when_cookie_values_are_short(self):
        with tempfile.TemporaryDirectory() as temporary:
            cookie = Path(temporary) / "cookies.txt"
            cookie.write_text(".instagram.com\tTRUE\t/\tTRUE\t0\tlocale\ten\n"
                              ".instagram.com\tTRUE\t/\tTRUE\t0\tds_user_id\t123456789\n")
            message = "https://www.instagram.com/api/v1/users/web_profile_info/?username=666&locale=en 123456789"
            cleaned = redact_output(message, cookie)
            self.assertIn("/api/v1/users/web_profile_info/?username=666", cleaned)
            self.assertIn("en", cleaned)
            self.assertNotIn("123456789", cleaned)

    def test_run_logs_are_persisted_in_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Database(temporary)
            account_id = db.add_account("owner", Path(temporary) / "cookies.txt")
            run_id = db.create_run(account_id, "creator:@author")
            db.add_run_log(run_id, "info", "creator:author:posts", "start")
            db.add_run_log(run_id, "error", "creator:author:posts", "HTTP 401")
            self.assertEqual([line["message"] for line in db.run_logs(run_id)], ["start", "HTTP 401"])
            self.assertEqual(db.runs()[0]["log_count"], 2)

    def test_cookie_requires_instagram_session(self):
        self.assertTrue(valid_cookie_file(".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tsecret\n"
                                          ".instagram.com\tTRUE\t/\tTRUE\t0\tcsrftoken\tcsrf\n"))
        self.assertFalse(valid_cookie_file(".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tsecret\n"))
        self.assertFalse(valid_cookie_file("evil.com\tTRUE\t/\tTRUE\t0\tsessionid\tsecret\n"))


if __name__ == "__main__":
    unittest.main()
