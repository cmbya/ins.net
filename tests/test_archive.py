import tempfile
import unittest
import sys
import types
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from insnet.db import Database
from insnet.engine import GalleryDL, GalleryError, following_from_messages, normalize_messages
from insnet.sync import Coordinator, SyncService
from insnet.web import valid_cookie_file


MESSAGES = [
    [2, {"post_shortcode": "ABC123", "username": "creator", "post_date": "2026-09-01", "description": "a carousel"}],
    [3, "https://cdn.example/a.jpg", {"media_id": "101", "extension": "jpg"}],
    [3, "https://cdn.example/b.mp4", {"media_id": "102", "extension": "mp4"}],
]


class FakeGallery:
    def __init__(self):
        self.calls = 0
        self.partial = True
        self.post_requests = []

    def posts(self, cookie, url, max_posts):
        self.post_requests.append((url, max_posts))
        return normalize_messages(MESSAGES)

    def download(self, cookie, post, staging):
        self.calls += 1
        path = Path(staging)
        (path / "101.jpg").write_bytes(b"image")
        if not self.partial:
            (path / "102.mp4").write_bytes(b"video")
        return {p.stem: p for p in path.iterdir()}


class ArchiveTests(unittest.TestCase):
    def test_gallery_messages_keep_carousel_and_following(self):
        posts = normalize_messages(MESSAGES)
        self.assertEqual([x["kind"] for x in posts[0]["items"]], ["image", "video"])
        self.assertEqual(posts[0]["shortcode"], "ABC123")
        self.assertEqual(following_from_messages([
            [6, "https://www.instagram.com/Author/", {}],
            [6, "https://www.instagram.com/another/", {}],
            [6, "https://evil.example.com/wrong/", {}],
        ]), ["another", "author"])

    def test_partial_download_retries_and_preserves_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = Database(root)
            (root / "tmp").mkdir()
            account_id = db.add_account("owner", root / "cookies.txt")
            account = db.account(account_id)
            fake = FakeGallery()
            service = SyncService(db, root, fake, archive_root=root / "archive")
            (root / "archive").mkdir()
            counts, error = service.run(account, "saved")
            self.assertEqual(counts["failed"], 1)
            posts = db.posts(account_id)
            first = posts[0]["media"][0]["relative_path"]
            self.assertEqual(posts[0]["status"], "partial")
            fake.partial = False
            db.upsert_creator(account_id, "creator", enabled=True)
            counts, error = service.run(account, "creators")
            self.assertEqual(counts["downloaded"], 1)
            posts = db.posts(account_id)
            self.assertEqual(posts[0]["status"], "complete")
            self.assertEqual(posts[0]["sources"], ["creator:creator:posts", "creator:creator:reels", "saved"])
            self.assertEqual(posts[0]["media"][0]["relative_path"], first)
            self.assertTrue(all((root / "archive" / x["relative_path"]).exists() for x in posts[0]["media"]))
            counts, error = service.run(account, "saved")
            self.assertEqual(counts["skipped"], 1)
            self.assertEqual(fake.calls, 2)

    def test_only_selected_creators_are_scanned_latest_20_or_all(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tmp").mkdir()
            (root / "archive").mkdir()
            db = Database(root)
            account_id = db.add_account("owner", root / "cookies.txt")
            account = db.account(account_id)
            db.upsert_creator(account_id, "selected", enabled=True)
            db.upsert_creator(account_id, "not_selected", enabled=False)
            fake = FakeGallery()
            service = SyncService(db, root, fake, archive_root=root / "archive")
            service.run(account, "creators")
            self.assertEqual(len(fake.post_requests), 2)
            self.assertTrue(all("selected" in url and "not_selected" not in url for url, _ in fake.post_requests))
            self.assertTrue(all(limit == 20 for _, limit in fake.post_requests))
            db.set_creator(account_id, "selected", full_sync=True)
            service.run(account, "creators")
            self.assertEqual([limit for _, limit in fake.post_requests[-2:]], [None, None])

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

    def test_following_import_uses_logged_in_profile_api(self):
        with tempfile.TemporaryDirectory() as temporary:
            cookie = Path(temporary) / "cookies.txt"
            cookie.write_text(
                "# Netscape HTTP Cookie File\n"
                ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tsession-secret\n"
                ".instagram.com\tTRUE\t/\tTRUE\t0\tcsrftoken\tcsrf-secret\n"
            )
            loaded = {}

            class FakeLoader:
                def __init__(self, **kwargs):
                    self.context = SimpleNamespace(load_session=lambda username, values:
                                                   loaded.update(username=username, cookies=values))

                def close(self):
                    pass

            class FakeProfile:
                @staticmethod
                def from_username(context, username):
                    return SimpleNamespace(get_followees=lambda: iter([
                        SimpleNamespace(username="Zed"), SimpleNamespace(username="alice")]))

            fake_module = types.ModuleType("instaloader")
            fake_module.Instaloader = FakeLoader
            fake_module.Profile = FakeProfile
            progress = []
            with patch.dict(sys.modules, {"instaloader": fake_module}):
                names = GalleryDL().following(cookie, "owner", progress.append)
            self.assertEqual(names, ["alice", "zed"])
            self.assertEqual(loaded["username"], "owner")
            self.assertEqual(loaded["cookies"]["sessionid"], "session-secret")
            self.assertEqual(progress, [2])

    def test_following_failure_is_saved_with_detail(self):
        class BrokenGallery:
            def following(self, cookie_path, username, progress=None):
                raise GalleryError("HTTP 403 checkpoint_required")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = Database(root)
            account_id = db.add_account("owner", root / "cookies.txt")
            service = SyncService(db, root, BrokenGallery(), archive_root=root / "archive")
            coordinator = Coordinator(db, service)
            run_id = db.create_run(account_id, "following")
            coordinator._execute(run_id, db.account(account_id), "following")
            run = db.runs()[0]
            self.assertEqual(run["status"], "failed")
            self.assertIn("HTTP 403 checkpoint_required", run["message"])
            self.assertIn("HTTP 403 checkpoint_required", db.run_logs(run_id)[-1]["message"])

    def test_legacy_media_is_copied_to_archive_mount(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_file = root / "media" / "owner" / "creator" / "post.jpg"
            old_file.parent.mkdir(parents=True)
            old_file.write_bytes(b"legacy image")
            archive = root / "archive"
            archive.mkdir()
            db = Database(root)
            account_id = db.add_account("owner", root / "cookies.txt")
            post = {"shortcode": "ABC123", "username": "creator", "caption": "",
                    "published_at": "2026-09-01", "source_url": "https://www.instagram.com/p/ABC123/",
                    "items": [{"media_id": "101", "position": 1, "kind": "image", "extension": "jpg"}]}
            post_id = db.upsert_post(account_id, post, "saved")
            db.set_media_file(post_id, "101", "owner/creator/post.jpg", old_file.stat().st_size, "jpg")
            service = SyncService(db, root, archive_root=archive)
            service._restore_legacy_media(post_id, db.post_media(post_id)[0])
            row = db.post_media(post_id)[0]
            self.assertEqual(row["relative_path"], "Instagram/owner/creator/post.jpg")
            self.assertEqual((archive / row["relative_path"]).read_bytes(), b"legacy image")
            self.assertTrue(old_file.exists())

    def test_run_logs_are_persisted_in_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Database(temporary)
            account_id = db.add_account("owner", Path(temporary) / "cookies.txt")
            run_id = db.create_run(account_id, "following")
            db.add_run_log(run_id, "info", "following", "start")
            db.add_run_log(run_id, "error", "following", "HTTP 401")
            self.assertEqual([line["message"] for line in db.run_logs(run_id)], ["start", "HTTP 401"])
            self.assertEqual(db.runs()[0]["log_count"], 2)

    def test_cookie_requires_instagram_session(self):
        self.assertTrue(valid_cookie_file(
            ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tsecret\n"
            ".instagram.com\tTRUE\t/\tTRUE\t0\tcsrftoken\tcsrf\n"))
        self.assertFalse(valid_cookie_file(".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tsecret\n"))
        self.assertFalse(valid_cookie_file("evil.com\tTRUE\t/\tTRUE\t0\tsessionid\tsecret\n"))


if __name__ == "__main__":
    unittest.main()
