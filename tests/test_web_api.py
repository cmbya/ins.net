import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from insnet.db import Database
from insnet.sync import Coordinator, SyncService
from insnet.web import AppServer


class WebApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archive = self.root / "archive"
        self.archive.mkdir()
        self.db = Database(self.root / "data")
        self.account_id = self.db.add_account("owner", self.root / "cookies.txt")
        self.account = self.db.account(self.account_id)
        self.service = SyncService(self.db, self.root / "data", archive_root=self.archive)
        self.coordinator = Coordinator(self.db, self.service)
        self.server = AppServer(("127.0.0.1", 0), self.db, self.coordinator,
                                "test-management-password", self.archive)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        response = urlopen(Request(self.base + "/api/login", data=json.dumps(
            {"password": "test-management-password"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST"))
        self.cookie = response.headers["Set-Cookie"].split(";", 1)[0]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request_json(self, path, data=None):
        headers = {"Cookie": self.cookie}
        method = "GET"
        body = None
        if data is not None:
            method = "POST"
            body = json.dumps(data).encode()
            headers.update({"Content-Type": "application/json", "X-Insnet-Action": "1"})
        response = urlopen(Request(self.base + path, data=body, headers=headers, method=method))
        return response.status, json.loads(response.read())

    def test_creator_settings_lists_and_deletion_api(self):
        code, result = self.request_json("/api/creators", {
            "account": self.account_id, "username": "@NASA"})
        self.assertEqual(code, 201)
        self.assertTrue(result["ok"])
        code, creators = self.request_json("/api/creators?account=" + self.account_id)
        self.assertEqual(code, 200)
        self.assertEqual(creators[0]["username"], "nasa")
        self.assertEqual(creators[0]["sync_mode"], "recent20")
        self.assertNotIn("interval_minutes", creators[0])
        self.assertNotIn("max_per_run", creators[0])

        self.request_json("/api/creator", {"account": self.account_id, "username": "nasa",
                                            "enabled": False, "sync_mode": "all"})
        creator = self.db.creator(self.account_id, "nasa")
        self.assertEqual((creator["enabled"], creator["sync_mode"], creator["interval_minutes"],
                          creator["max_per_run"]), (0, "all", 360, 20))

        code, settings = self.request_json("/api/settings", {
            "media_subdir": "Instagram/test"})
        self.assertEqual(code, 200)
        self.assertEqual(settings["media_path"], str(self.archive / "Instagram/test"))

        self.request_json("/api/creator/delete", {
            "account": self.account_id, "username": "nasa", "mode": "list"})
        self.assertEqual(self.db.creator(self.account_id, "nasa")["manual"], 2)
        self.assertNotIn("nasa", [c["username"] for c in self.db.creators(self.account_id)])


    def test_dashboard_records_soft_delete_preserve_files_and_logs(self):
        self.db.add_creator(self.account_id, "nasa")
        post = {"shortcode":"TEST001", "username":"nasa", "caption":"A test post",
                "published_at":"2026-09-20T12:00:00", "source_url":"https://www.instagram.com/p/TEST001/",
                "items":[{"media_id":"101", "position":1, "kind":"image", "extension":"jpg"}]}
        post_id = self.db.upsert_post(self.account_id, post, "creator:nasa:posts")
        archived = self.archive / "Instagram" / "nasa.jpg"
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_bytes(b"photo")
        self.db.set_media_file(post_id, "101", "Instagram/nasa.jpg", 5, "jpg")
        self.db.set_post_status(post_id, "complete")

        code, dashboard = self.request_json("/api/dashboard?account=" + self.account_id)
        self.assertEqual(code, 200)
        self.assertEqual(dashboard["total"], 1)
        self.assertEqual(dashboard["authors"][0]["count"], 1)
        self.assertTrue(dashboard["authors"][0]["monitored"])

        _, records = self.request_json("/api/records?account=" + self.account_id + "&author=nasa")
        self.assertEqual(records["total"], 1)
        _, result = self.request_json("/api/records/delete", {"account":self.account_id,"username":"nasa"})
        self.assertEqual(result["count"], 1)
        self.assertTrue(archived.is_file())
        self.assertEqual(self.db.records({"account":self.account_id,"deleted":"0"})["total"], 0)
        self.assertEqual(self.db.records({"account":self.account_id,"deleted":"1"})["total"], 1)

        self.db.set_post_status(post_id, "complete")
        self.assertTrue(self.db.post(post_id)["deleted_at"])
        _, result = self.request_json("/api/records/restore", {"account":self.account_id,"ids":[post_id]})
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.db.records({"account":self.account_id,"deleted":"0"})["total"], 1)

        run_id = self.db.create_run(self.account_id, "creator:@nasa")
        self.db.add_run_log(run_id, "ERROR", "creator:nasa:posts", "test request denied")
        _, logs = self.request_json("/api/logs?account="+self.account_id+"&level=ERROR&q=denied")
        self.assertGreaterEqual(logs["total"], 1)
        self.assertEqual(logs["items"][0]["run_id"], run_id)

    def test_system_config_validation_and_creator_defaults(self):
        self.db.add_creator(self.account_id, "nasa")
        config = {"creator_interval":30,"creator_max":12,
                  "scheduler_enabled":0,"log_days":14,"media_subdir":"Instagram/X"}
        code, result = self.request_json("/api/config",config)
        self.assertEqual(code,200)
        self.assertTrue(result["ok"])
        self.assertEqual(self.db.admin_config()["creator_interval"],30)
        self.assertEqual(self.db.admin_config(), {"creator_interval":30,"creator_max":12,"scheduler_enabled":0,"log_days":14})
        creator = self.db.creator(self.account_id, "nasa")
        self.assertEqual((creator["interval_minutes"],creator["max_per_run"]),(30,12))
        before = self.db.setting("media_subdir")
        with self.assertRaises(Exception):
            self.request_json("/api/config",{**config,"media_subdir":"../outside"})
        self.assertEqual(self.db.setting("media_subdir"),before)
        _, accounts = self.request_json("/api/admin/accounts")
        self.assertNotIn("cookie_path",accounts[0])
        self.assertFalse(any("saved" in key for key in accounts[0]))

    def test_saved_sync_is_disabled_and_old_saved_only_records_are_hidden(self):
        post = {"shortcode":"OLD001", "username":"nasa", "caption":"Legacy saved post",
                "published_at":"2026-09-20T12:00:00", "source_url":"https://www.instagram.com/p/OLD001/",
                "items":[{"media_id":"old101", "position":1, "kind":"image", "extension":"jpg"}]}
        post_id = self.db.upsert_post(self.account_id, post, "saved")
        self.db.set_post_status(post_id, "complete")
        self.assertEqual(self.db.dashboard(self.account_id)["total"], 0)
        self.assertEqual(self.db.records({"account":self.account_id})["total"], 0)
        with self.assertRaises(ValueError):
            self.coordinator.start(self.account_id, "saved")
        self.assertEqual(self.db.post(post_id)["status"], "complete")

    def test_legacy_reel_only_rows_do_not_appear_in_post_statistics(self):
        self.db.add_creator(self.account_id, "nasa")
        reel = {"shortcode":"LEGREEL", "username":"nasa", "caption":"Legacy reel",
                "published_at":"2026-09-20T12:00:00", "source_url":"https://www.instagram.com/reel/LEGREEL/",
                "items":[{"media_id":"r101", "position":1, "kind":"video", "extension":"mp4"}]}
        post_id = self.db.upsert_post(self.account_id, reel, "creator:nasa:reels")
        self.db.set_post_status(post_id, "complete")
        self.assertEqual(self.db.dashboard(self.account_id)["total"], 0)
        self.assertEqual(self.db.records({"account":self.account_id})["total"], 0)
        self.assertEqual(self.db.creators(self.account_id)[0]["archived_count"], 0)

    def test_removed_creator_cannot_be_started_by_direct_sync_request(self):
        self.db.add_creator(self.account_id, "nasa")
        self.db.delete_creator(self.account_id, "nasa")
        with self.assertRaisesRegex(ValueError, "移出监控列表"):
            self.coordinator.start(self.account_id, "creator", "nasa")
        with self.assertRaisesRegex(ValueError, "移出监控列表"):
            self.service.run(self.account, "creator", username="nasa")

    def test_interrupted_runs_and_scheduler_errors_are_visible_in_system_logs(self):
        run_id = self.db.create_run(self.account_id, "creator:@nasa")
        self.db.add_run_log(run_id, "INFO", "creator:nasa:posts", "started")
        self.assertEqual(self.db.recover_interrupted_runs(), 1)
        self.assertEqual(self.db.runs()[0]["status"], "interrupted")
        self.coordinator.poll_interval = 0.01
        with patch.object(self.db, "purge_old_logs", side_effect=RuntimeError("simulated scheduler failure")):
            thread = self.coordinator.schedule()
            deadline = time.monotonic() + 2
            logs = {"total": 0}
            while time.monotonic() < deadline and not logs["total"]:
                logs = self.db.system_logs({"q":"simulated scheduler failure"})
                if not logs["total"]:
                    time.sleep(0.01)
            self.coordinator.stopped.set()
            thread.join(timeout=1)
        self.assertGreaterEqual(logs["total"], 1)
        self.assertEqual(logs["items"][0]["source"], "scheduler")
        self.assertIn("simulated scheduler failure", logs["items"][0]["message"])

    def test_coordinator_releases_active_flag_when_worker_cannot_start(self):
        self.db.add_creator(self.account_id, "nasa")
        with patch("insnet.sync.threading.Thread.start", side_effect=RuntimeError("thread unavailable")):
            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                self.coordinator.start(self.account_id, "creator", "nasa")
        self.assertNotIn(self.account_id, self.coordinator.active)
        self.assertEqual(self.db.runs()[0]["status"], "failed")

    def test_login_rate_limit_returns_retry_after(self):
        for _ in range(10):
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(self.base + "/api/login", data=json.dumps(
                    {"password": "wrong-password"}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST"))
            self.assertEqual(error.exception.code, 401)
            error.exception.close()
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.base + "/api/login", data=json.dumps(
                {"password": "wrong-password"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST"))
        self.assertEqual(error.exception.code, 429)
        self.assertGreater(int(error.exception.headers["Retry-After"]), 0)
        error.exception.close()

    def test_login_cookie_can_be_marked_secure_for_https_proxy(self):
        self.server.cookie_secure = True
        response = urlopen(Request(self.base + "/api/login", data=json.dumps(
            {"password": "test-management-password"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST"))
        self.assertIn("; Secure", response.headers["Set-Cookie"])

    def test_invalid_settings_do_not_change_archive_directory(self):
        code, before = self.request_json("/api/settings")
        with self.assertRaises(Exception):
            self.request_json("/api/settings", {"media_subdir": "../outside"})
        self.assertEqual(self.db.setting("media_subdir", "Instagram"), before["media_subdir"])


if __name__ == "__main__":
    unittest.main()
