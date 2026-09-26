"""Small, replaceable boundary around gallery-dl's Instagram extractor."""

import hashlib
import json
import re
import subprocess
from collections import OrderedDict
from pathlib import Path


USERNAME = re.compile(r"^[A-Za-z0-9._]{1,30}$")
SHORTCODE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
EXTENSION = re.compile(r"^[a-zA-Z0-9]{1,8}$")


class GalleryError(Exception):
    pass


def redact_output(value, cookie_path):
    if isinstance(value, (list, tuple)):
        text = "\n".join(x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x)
                           for x in value if x)
    else:
        text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or "")
    try:
        for line in Path(cookie_path).read_text(encoding="utf-8", errors="replace").splitlines():
            fields = line.removeprefix("#HttpOnly_").split("\t")
            # Cookie jars also contain short values such as "1" and "en".
            # Replacing those substrings corrupts useful URLs and diagnostics.
            if len(fields) >= 7 and len(fields[6]) >= 8:
                text = text.replace(fields[6], "[COOKIE_REDACTED]")
    except OSError:
        pass
    return re.sub(r"\x1b\[[0-9;]*m", "", text).strip()[-5000:]


def normalize_messages(messages, category="posts"):
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
            user_meta = directory.get("user") if isinstance(directory.get("user"), dict) else directory
            display_name = str(user_meta.get("full_name") or user_meta.get("fullname") or
                               meta.get("full_name") or meta.get("fullname") or "")[:160]
            avatar_url = str(user_meta.get("profile_pic_url_hd") or user_meta.get("profile_pic_url") or
                             user_meta.get("avatar_url") or meta.get("profile_pic_url") or "")[:2000]
            profile_id = str(user_meta.get("id") or user_meta.get("pk") or meta.get("owner_id") or "")[:80]
            groups[shortcode] = {
                "shortcode": shortcode, "username": username,
                "caption": str(meta.get("description") or meta.get("post_caption") or "")[:5000],
                "published_at": str(date)[:32] if date else None,
                "source_url": (meta.get("post_url") if str(meta.get("post_url", "")).startswith(
                    ("https://www.instagram.com/p/", "https://www.instagram.com/reel/"))
                    else f"https://www.instagram.com/{'reel' if category == 'reels' else 'p'}/{shortcode}/"),
                "display_name": display_name, "avatar_url": avatar_url, "profile_id": profile_id,
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


class GalleryDL:
    def __init__(self, executable="gallery-dl", timeout=1800):
        self.executable, self.timeout = executable, timeout

    def _run(self, cookie_path, args, with_stderr=False):
        command = [self.executable, "--config-ignore", "--no-input", "--cookies",
                   str(cookie_path), *args]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            detail = redact_output((exc.stderr, exc.stdout), cookie_path)
            raise GalleryError(f"gallery-dl 执行超过 {self.timeout} 秒后超时。\n{detail}") from exc
        except OSError as exc:
            raise GalleryError(f"无法启动 gallery-dl：{exc}") from exc
        if result.returncode:
            detail = redact_output((result.stderr, result.stdout), cookie_path)
            raise GalleryError(f"gallery-dl 退出码 {result.returncode}。\n{detail or '程序未输出错误详情。'}")
        return (result.stdout, result.stderr) if with_stderr else result.stdout

    def _json(self, cookie_path, url, max_posts=None, cursor=None, verbose=False):
        args = ["-j"]
        if max_posts is not None:
            args += ["-o", f"extractor.instagram.max-posts={int(max_posts)}"]
        if cursor:
            args += ["-o", f"extractor.instagram.cursor={cursor}"]
        if verbose:
            args.append("--verbose")
        raw, stderr = self._run(cookie_path, [*args, url], with_stderr=True)
        try:
            messages = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise GalleryError("gallery-dl 返回了无效元数据") from exc
        if not isinstance(messages, list):
            raise GalleryError("gallery-dl 返回的元数据不是列表")
        errors = [msg[-1] for msg in messages if isinstance(msg, list) and msg and msg[0] == -1]
        if errors:
            details = "\n".join(str(item.get("message") or item) for item in errors if isinstance(item, dict))
            raise GalleryError("gallery-dl 提取失败。\n" + redact_output(details or errors, cookie_path))
        return messages, stderr

    def posts(self, cookie_path, url, max_posts=None):
        messages, _ = self._json(cookie_path, url, max_posts)
        category = "reels" if re.search(r"/reels/?$", url) else "posts"
        return normalize_messages(messages, category)

    def scan_posts(self, cookie_path, url, max_posts=None, cursor=None):
        """Fetch one bounded page and return gallery-dl's Instagram continuation cursor."""
        messages, stderr = self._json(cookie_path, url, max_posts, cursor=cursor, verbose=True)
        matches = re.findall(r"\bCursor:\s*([^\s]+)", stderr, flags=re.IGNORECASE)
        next_cursor = matches[-1] if matches else None
        category = "reels" if re.search(r"/reels/?$", url) else "posts"
        return normalize_messages(messages, category), next_cursor

    def download(self, cookie_path, post, staging, media_ids=None):
        staging = Path(staging)
        staging.mkdir(parents=True, exist_ok=True)
        args = ["--no-mtime", "-D", str(staging), "-f",
                "{media_id}.{extension}", post["source_url"]]
        if media_ids:
            # IDs have already been normalized to [A-Za-z0-9_-], and repr
            # makes the tuple a valid, safely quoted gallery-dl expression.
            args[0:0] = ["--filter", f"media_id in {tuple(media_ids)!r}"]
        self._run(cookie_path, args)
        return {p.stem: p for p in staging.iterdir() if p.is_file() and p.stat().st_size > 0}
