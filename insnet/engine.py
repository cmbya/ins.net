"""Small, replaceable boundary around gallery-dl's Instagram extractor."""

import hashlib
import json
import re
import subprocess
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlparse


USERNAME = re.compile(r"^[A-Za-z0-9._]{1,30}$")
SHORTCODE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
EXTENSION = re.compile(r"^[a-zA-Z0-9]{1,8}$")


class GalleryError(Exception):
    pass


def normalize_messages(messages):
    """Group URL messages by Instagram shortcode; retain carousel item order."""
    groups = OrderedDict()
    directory = {}
    for message in messages:
        if not isinstance(message, list) or len(message) < 2:
            continue
        if message[0] == 2:
            # DataJob (-j) serializes directory messages as [2, metadata].
            directory = message[-1] if isinstance(message[-1], dict) else {}
            continue
        if message[0] != 3 or len(message) < 3 or not isinstance(message[2], dict):
            continue
        meta = {**directory, **message[2]}
        shortcode = str(meta.get("post_shortcode") or meta.get("shortcode") or "")
        username = str(meta.get("username") or meta.get("owner_username") or "")
        if not SHORTCODE.fullmatch(shortcode) or not USERNAME.fullmatch(username):
            continue
        media_url = str(message[1])
        if not media_url.startswith(("https://", "ytdl:")):
            continue
        media_id = str(meta.get("media_id") or hashlib.sha256(media_url.encode()).hexdigest()[:20])
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", media_id):
            media_id = hashlib.sha256(media_id.encode()).hexdigest()[:20]
        extension = str(meta.get("extension") or ("mp4" if meta.get("video_url") else "jpg")).lower()
        if not EXTENSION.fullmatch(extension):
            extension = "mp4" if meta.get("video_url") else "jpg"
        kind = "video" if extension in ("mp4", "mov", "webm", "mkv") else "image"
        if shortcode not in groups:
            date = meta.get("post_date") or meta.get("date") or ""
            groups[shortcode] = {
                "shortcode": shortcode, "username": username,
                "caption": str(meta.get("description") or meta.get("post_caption") or "")[:5000],
                "published_at": str(date)[:32] if date else None,
                "source_url": (meta.get("post_url") if str(meta.get("post_url", "")).startswith(
                    ("https://www.instagram.com/p/", "https://www.instagram.com/reel/"))
                    else f"https://www.instagram.com/p/{shortcode}/"),
                "items": [],
            }
        group = groups[shortcode]
        if any(x["media_id"] == media_id for x in group["items"]):
            continue
        group["items"].append({
            "media_id": media_id, "position": len(group["items"]) + 1,
            "kind": kind, "extension": extension,
        })
    return list(groups.values())


def following_from_messages(messages):
    names = set()
    for msg in messages:
        if not isinstance(msg, list) or len(msg) < 2 or msg[0] != 6:
            continue
        url = str(msg[1])
        path = urlparse(url).path.strip("/").split("/")
        if urlparse(url).hostname in ("instagram.com", "www.instagram.com") and len(path) == 1 and USERNAME.fullmatch(path[0]):
            names.add(path[0].lower())
    return sorted(names)


class GalleryDL:
    def __init__(self, executable="gallery-dl", timeout=1800):
        self.executable, self.timeout = executable, timeout

    def _run(self, cookie_path, args):
        command = [self.executable, "--config-ignore", "--no-input", "--cookies",
                   str(cookie_path), *args]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GalleryError(f"gallery-dl 无法运行或超时：{type(exc).__name__}") from exc
        if result.returncode:
            # Errors can include private URLs or cookies. Never surface raw subprocess output.
            raise GalleryError(f"gallery-dl 退出码 {result.returncode}；请检查登录 Cookie、网络与访问权限")
        return result.stdout

    def _json(self, cookie_path, url, max_posts=None):
        args = ["-j"]
        if max_posts is not None:
            args += ["-o", f"extractor.instagram.max-posts={int(max_posts)}"]
        raw = self._run(cookie_path, [*args, url])
        try:
            messages = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise GalleryError("gallery-dl 返回了无效元数据") from exc
        if not isinstance(messages, list) or any(
            isinstance(msg, list) and msg and msg[0] == -1 for msg in messages
        ):
            raise GalleryError("gallery-dl 无法读取元数据；请检查 Cookie 和访问权限")
        return messages

    def following(self, cookie_path, username):
        return following_from_messages(self._json(cookie_path, f"https://www.instagram.com/{username}/following/"))

    def posts(self, cookie_path, url, max_posts=None):
        return normalize_messages(self._json(cookie_path, url, max_posts))

    def download(self, cookie_path, post, staging):
        staging = Path(staging)
        staging.mkdir(parents=True, exist_ok=True)
        self._run(cookie_path, ["--no-mtime", "-D", str(staging), "-f",
                                 "{media_id}.{extension}", post["source_url"]])
        return {p.stem: p for p in staging.iterdir() if p.is_file() and p.stat().st_size > 0}
