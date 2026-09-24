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
        self.assertIsNone(self.db.creator(self.account_id, "nasa"))

    def test_invalid_settings_do_not_change_archive_directory(self):
        code, before = self.request_json("/api/settings?account=" + self.account_id)
        with self.assertRaises(Exception):
            self.request_json("/api/settings", {"account": self.account_id,
                                "media_subdir": "new", "auto_saved": True,
                                "saved_interval_minutes": 2})
        self.assertEqual(self.db.setting("media_subdir", "Instagram"), before["media_subdir"])


if __name__ == "__main__":
    unittest.main()
