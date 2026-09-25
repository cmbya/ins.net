"""Password-protected web UI and JSON API; no application server dependency."""

import hashlib
import hmac
import json
from collections import deque
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .db import Database
from . import admin
from .engine import USERNAME
from .sync import Coordinator, SyncService


STATIC = Path(__file__).resolve().parent.parent / "static"
APP_VERSION = "0.7.0"


def valid_cookie_file(value):
    if not isinstance(value, str) or len(value.encode("utf-8")) > 256 * 1024:
        return False
    required = {"sessionid", "csrftoken"}
    found = set()
    for line in value.splitlines():
        fields = line.lstrip("#HttpOnly_").split("\t") if line.startswith("#HttpOnly_") else line.split("\t")
        if len(fields) >= 7 and fields[0].lstrip(".").lower() == "instagram.com" \
                and fields[5] in required and fields[6]:
            found.add(fields[5])
    return required <= found


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, db, coordinator, password, archive_root):
        self.db, self.coordinator = db, coordinator
        self.password = password
        self.archive_root = Path(archive_root).resolve()
        self.secret = secrets.token_bytes(32)
        self.cookie_secure = os.environ.get("INS_COOKIE_SECURE", "0") == "1"
        self._login_lock = threading.Lock()
        self._login_failures = {}
        super().__init__(address, Handler)

    def login_retry_after(self, client):
        now = time.monotonic()
        with self._login_lock:
            attempts = self._login_failures.get(client)
            if not attempts:
                return 0
            while attempts and now - attempts[0] >= 900:
                attempts.popleft()
            if not attempts:
                self._login_failures.pop(client, None)
                return 0
            return max(1, int(900 - (now - attempts[0]))) if len(attempts) >= 10 else 0

    def record_login_failure(self, client):
        now = time.monotonic()
        with self._login_lock:
            if client not in self._login_failures and len(self._login_failures) >= 2048:
                expired = [key for key, values in self._login_failures.items()
                           if not values or now - values[-1] >= 900]
                for key in expired:
                    self._login_failures.pop(key, None)
                if len(self._login_failures) >= 2048:
                    self._login_failures.pop(next(iter(self._login_failures)))
            attempts = self._login_failures.setdefault(client, deque())
            while attempts and now - attempts[0] >= 900:
                attempts.popleft()
            attempts.append(now)

    def clear_login_failures(self, client):
        with self._login_lock:
            self._login_failures.pop(client, None)


class Handler(BaseHTTPRequestHandler):
    server: AppServer

    def log_message(self, format, *args):
        # URLs can include user identifiers; avoid logging API payloads.
        pass

    def reply(self, code, data, headers=None):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, str(value))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 300000:
            raise ValueError("请求过大或为空")
        return json.loads(self.rfile.read(length))

    def authorized(self):
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
            value = jar["insnet_session"].value
            timestamp, signature = value.split(".", 1)
            if abs(time.time() - int(timestamp)) > 7 * 86400:
                return False
            expected = hmac.new(self.server.secret, timestamp.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, signature)
        except (KeyError, ValueError):
            return False

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/app.js", "/style.css"):
            name = "index.html" if path == "/" else path[1:]
            data = (STATIC / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/version":
            return self.reply(200, {"version": APP_VERSION})
        if not self.authorized():
            return self.reply(401, {"error": "请先登录"})
        query = parse_qs(urlparse(self.path).query)
        try:
            result = admin.get(self.server, path, query)
            if result is not None:
                return self.reply(200, result)
            if path == "/api/accounts":
                return self.reply(200, [{"id": a["id"], "username": a["username"]} for a in self.server.db.accounts()])
            if path == "/api/creators":
                return self.reply(200, self.server.db.creators(query.get("account", [""])[0]))
            if path == "/api/posts":
                return self.reply(200, self.server.db.posts(query.get("account", [""])[0]))
            if path == "/api/runs":
                return self.reply(200, self.server.db.runs())
            if path == "/api/runlogs":
                run_id = query.get("run", [""])[0]
                return self.reply(200, self.server.db.run_logs(run_id))
            if path == "/api/settings":
                subdir = self.server.db.setting("media_subdir", "Instagram")
                target = self.server.archive_root / subdir if subdir else self.server.archive_root
                return self.reply(200, {"media_subdir": subdir,
                                        "archive_root": str(self.server.archive_root),
                                        "media_path": str(target)})
            match = re.fullmatch(r"/media/(\d+)/([A-Za-z0-9_-]+)", path)
            if match:
                return self.serve_media(int(match[1]), match[2])
            self.reply(404, {"error": "未找到"})
        except (OSError, ValueError):
            self.reply(400, {"error": "请求无效"})

    def serve_media(self, post_id, media_id):
        post = self.server.db.post(post_id)
        row = next((r for r in self.server.db.post_media(post_id) if r["media_id"] == media_id), None) if post else None
        if not row or not row["relative_path"] or post.get("deleted_at"):
            return self.reply(404, {"error": "媒体不存在"})
        relative = Path(row["relative_path"])
        file = None
        for base in (self.server.archive_root, self.server.db.root / "media"):
            base = base.resolve()
            candidate = (base / relative).resolve()
            if candidate.is_relative_to(base) and candidate.is_file():
                file = candidate
                break
        if file is None:
            return self.reply(404, {"error": "媒体不存在"})
        size = file.stat().st_size
        start, end = 0, size - 1
        range_header = self.headers.get("Range", "")
        if range_header:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
            if not match:
                return self.reply(416, {"error": "不支持的区间"})
            start = int(match[1]); end = min(int(match[2]), end) if match[2] else end
            if start > end:
                return self.reply(416, {"error": "区间越界"})
        self.send_response(206 if range_header else 200)
        self.send_header("Content-Type", mimetypes.guess_type(file.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if range_header:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with file.open("rb") as stream:
            stream.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/login":
                client = self.client_address[0]
                retry_after = self.server.login_retry_after(client)
                if retry_after:
                    return self.reply(429, {"error": "登录尝试过多，请稍后再试"},
                                      {"Retry-After": retry_after})
                value = self.body()
                candidate = value.get("password", "") if isinstance(value, dict) else ""
                if not hmac.compare_digest(str(candidate), self.server.password):
                    self.server.record_login_failure(client)
                    return self.reply(401, {"error": "密码错误"})
                self.server.clear_login_failures(client)
                timestamp = str(int(time.time()))
                signature = hmac.new(self.server.secret, timestamp.encode(), hashlib.sha256).hexdigest()
                self.send_response(200)
                secure = "; Secure" if self.server.cookie_secure else ""
                self.send_header("Set-Cookie", f"insnet_session={timestamp}.{signature}; HttpOnly; SameSite=Strict; Path=/{secure}")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
                return
            if not self.authorized():
                return self.reply(401, {"error": "请先登录"})
            if self.headers.get("X-Insnet-Action") != "1" or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.reply(403, {"error": "请求来源无效"})
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                return self.reply(403, {"error": "请求来源无效"})
            value = self.body()
            result = admin.post(self.server, path, value)
            if result is not None:
                return self.reply(200, result)
            if path == "/api/settings":
                raw_subdir = str(value.get("media_subdir", "")).strip()
                if raw_subdir.startswith("/"):
                    raise ValueError("请填写 /archive 下的相对路径，例如 Instagram/备份")
                subdir = raw_subdir.rstrip("/")
                parts = subdir.split("/") if subdir else []
                if len(subdir) > 180 or "\\" in subdir or "\x00" in subdir or any(
                    part in ("", ".", "..") for part in parts
                ):
                    raise ValueError("保存目录只能填写 /archive 下的相对路径，不能包含 ..")
                target = self.server.archive_root / subdir if subdir else self.server.archive_root
                target.resolve().relative_to(self.server.archive_root)
                self.server.db.set_setting("media_subdir", subdir)
                return self.reply(200, {"media_subdir": subdir,
                                        "media_path": str(target)})
            account_id = str(value.get("account", ""))
            if path == "/api/accounts":
                name = str(value.get("username", "")).strip().lower().lstrip("@")
                cookie = value.get("cookies")
                if not USERNAME.fullmatch(name) or not valid_cookie_file(cookie):
                    raise ValueError("用户名或 Netscape Cookie 文件无效（需含 instagram.com 的 sessionid 和 csrftoken）")
                existing = self.server.db.account_by_username(name)
                account_id = existing["id"] if existing else self.server.db.add_account(name, "pending")
                target = self.server.db.root / "accounts" / account_id / "cookies.txt"
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(".tmp")
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(cookie)
                os.replace(temporary, target)
                label = str(value.get("label", "")).strip()[:60]
                with self.server.db.connect() as conn:
                    conn.execute("UPDATE accounts SET label=?,cookie_path=?,cookie_status='unverified',enabled=1 WHERE id=?",
                                 (label, str(target), account_id))
                return self.reply(201, {"id": account_id})
            if not self.server.db.account(account_id):
                raise ValueError("账号不存在")
            if path == "/api/creators":
                name = str(value.get("username", "")).strip().lower().lstrip("@")
                if not USERNAME.fullmatch(name):
                    raise ValueError("博主用户名无效")
                self.server.db.add_creator(account_id, name)
                return self.reply(201, {"ok": True})
            if path == "/api/creator":
                name = str(value.get("username", ""))
                if not USERNAME.fullmatch(name):
                    raise ValueError("博主参数无效")
                enabled = value.get("enabled")
                sync_mode = value.get("sync_mode")
                if enabled is not None and not isinstance(enabled, bool):
                    raise ValueError("自动同步开关无效")
                if sync_mode is not None and sync_mode not in ("recent20", "all"):
                    raise ValueError("同步范围无效")
                if not self.server.db.set_creator(account_id, name, enabled=enabled,
                                                 sync_mode=sync_mode):
                    raise ValueError("博主不存在")
                return self.reply(200, {"ok": True})
            if path == "/api/creator/delete":
                name = str(value.get("username", ""))
                mode = value.get("mode")
                if not USERNAME.fullmatch(name) or mode not in ("list", "archive"):
                    raise ValueError("删除参数无效")
                # Older clients may still send archive mode; it now removes records only.
                with admin.idle(self.server, account_id):
                    removed = self.server.db.hide_records(account_id, username=name) if mode == "archive" else 0
                    result = self.server.db.delete_creator(account_id, name, delete_archive=False)
                    if result is None:
                        raise ValueError("博主不存在")
                return self.reply(200, {"ok": True, "removed_posts": removed,
                                        "shared_posts": 0, "removed_files": 0, "file_errors": []})
            if path == "/api/sync":
                kind = value.get("kind")
                username = value.get("username")
                return self.reply(202, {"run_id": self.server.coordinator.start(account_id, kind, username)})
            self.reply(404, {"error": "未找到"})
        except (ValueError, TypeError, KeyError, sqlite3.IntegrityError) as exc:
            self.reply(400, {"error": str(exc)[:200]})


def main():
    password = os.environ.get("INS_PASSWORD", "")
    if len(password) < 12:
        raise SystemExit("请设置至少 12 位的 INS_PASSWORD")
    root = Path(os.environ.get("INS_DATA", "/data")).resolve()
    archive_root = Path(os.environ.get("INS_ARCHIVE_ROOT", "/archive")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "tmp").mkdir(exist_ok=True)
    archive_root.mkdir(parents=True, exist_ok=True)
    db = Database(root)
    db.recover_interrupted_runs()
    service = SyncService(db, root, archive_root=archive_root)
    coordinator = Coordinator(db, service)
    coordinator.schedule()
    server = AppServer(("0.0.0.0", int(os.environ.get("INS_PORT", "18080"))),
                       db, coordinator, password, archive_root)
    server.serve_forever()


if __name__ == "__main__":
    main()
