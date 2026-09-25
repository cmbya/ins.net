import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

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

        self.request_json("/api/creator", {"account": self.account_id, "username": "nasa",
                                            "enabled": False, "sync_mode": "all",
                                            "interval_minutes": 30, "max_per_run": 15})
        creator = self.db.creator(self.account_id, "nasa")
        self.assertEqual((creator["enabled"], creator["sync_mode"], creator["interval_minutes"],
                          creator["max_per_run"]), (0, "all", 30, 15))

        code, settings = self.request_json("/api/settings", {
            "account": self.account_id, "media_subdir": "Instagram/test",
            "auto_saved": False, "saved_interval_minutes": 90})
        self.assertEqual(code, 200)
        self.assertEqual(settings["media_path"], str(self.archive / "Instagram/test"))
        self.assertFalse(self.db.account(self.account_id)["auto_saved"])
        self.assertEqual(self.db.account(self.account_id)["saved_interval_minutes"], 90)

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
        self.assertEqual(logs["total"], 1)
        self.assertEqual(logs["items"][0]["run_id"], run_id)

    def test_system_config_validation_and_saved_defaults(self):
        config = {"creator_interval":30,"creator_max":12,"saved_interval":60,"saved_max":8,
                  "saved_recent":0,"saved_enabled":0,"scheduler_enabled":0,"log_days":14,"media_subdir":"Instagram/X"}
        code, result = self.request_json("/api/config",config)
        self.assertEqual(code,200)
        self.assertTrue(result["ok"])
        self.assertEqual(self.db.admin_config()["creator_interval"],30)
        account = self.db.account(self.account_id)
        self.assertEqual((account["saved_interval_minutes"],account["saved_max_per_run"],account["saved_recent_only"],account["auto_saved"]),(60,8,0,0))
        before = self.db.setting("media_subdir")
        with self.assertRaises(Exception):
            self.request_json("/api/config",{**config,"media_subdir":"../outside"})
        self.assertEqual(self.db.setting("media_subdir"),before)
        _, accounts = self.request_json("/api/admin/accounts")
        self.assertNotIn("cookie_path",accounts[0])

    def test_invalid_settings_do_not_change_archive_directory(self):
        code, before = self.request_json("/api/settings?account=" + self.account_id)
        with self.assertRaises(Exception):
            self.request_json("/api/settings", {"account": self.account_id,
                                "media_subdir": "new", "auto_saved": True,
                                "saved_interval_minutes": 2})
        self.assertEqual(self.db.setting("media_subdir", "Instagram"), before["media_subdir"])


if __name__ == "__main__":
    unittest.main()
