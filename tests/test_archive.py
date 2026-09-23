import tempfile
import unittest
from pathlib import Path

from insnet.db import Database
from insnet.engine import following_from_messages, normalize_messages
from insnet.sync import SyncService
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

    def posts(self, cookie, url, max_posts):
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
            service = SyncService(db, root, fake)
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
            self.assertTrue(all((root / "media" / x["relative_path"]).exists() for x in posts[0]["media"]))
            counts, error = service.run(account, "saved")
            self.assertEqual(counts["skipped"], 1)
            self.assertEqual(fake.calls, 2)

    def test_cookie_requires_instagram_session(self):
        self.assertTrue(valid_cookie_file(".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tsecret\n"))
        self.assertFalse(valid_cookie_file("evil.com\tTRUE\t/\tTRUE\t0\tsessionid\tsecret\n"))


if __name__ == "__main__":
    unittest.main()
