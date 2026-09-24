import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
    cookie_path TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS creators (
    account_id TEXT NOT NULL, username TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
    full_sync INTEGER NOT NULL DEFAULT 0, manual INTEGER NOT NULL DEFAULT 1,
    last_sync TEXT, display_name TEXT NOT NULL DEFAULT '', avatar_url TEXT NOT NULL DEFAULT '',
    profile_id TEXT NOT NULL DEFAULT '', sync_mode TEXT NOT NULL DEFAULT 'recent20',
    interval_minutes INTEGER NOT NULL DEFAULT 360, max_per_run INTEGER NOT NULL DEFAULT 20,
    scan_cursor TEXT, history_complete INTEGER NOT NULL DEFAULT 0,
    next_sync_at TEXT, last_error TEXT NOT NULL DEFAULT '', failures INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, username)
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, shortcode TEXT NOT NULL,
    username TEXT NOT NULL, caption TEXT NOT NULL DEFAULT '',
    published_at TEXT, source_url TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    error TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (account_id, shortcode)
);
CREATE TABLE IF NOT EXISTS post_sources (
    post_id INTEGER NOT NULL, source TEXT NOT NULL,
    PRIMARY KEY (post_id, source), FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS media (
    post_id INTEGER NOT NULL, media_id TEXT NOT NULL, position INTEGER NOT NULL,
    kind TEXT NOT NULL, extension TEXT NOT NULL, relative_path TEXT,
    size INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (post_id, media_id),
    FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running', downloaded INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS run_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'INFO', source TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_account_date ON posts(account_id, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_run_logs_id ON run_logs(run_id, id);
"""


class Database:
    CREATOR_COLUMNS = {
        "display_name": "TEXT NOT NULL DEFAULT ''",
        "avatar_url": "TEXT NOT NULL DEFAULT ''",
        "profile_id": "TEXT NOT NULL DEFAULT ''",
        "sync_mode": "TEXT NOT NULL DEFAULT 'recent20'",
        "interval_minutes": "INTEGER NOT NULL DEFAULT 360",
        "max_per_run": "INTEGER NOT NULL DEFAULT 20",
        "scan_cursor": "TEXT",
        "history_complete": "INTEGER NOT NULL DEFAULT 0",
        "next_sync_at": "TEXT",
        "last_error": "TEXT NOT NULL DEFAULT ''",
        "failures": "INTEGER NOT NULL DEFAULT 0",
    }
    ACCOUNT_COLUMNS = {
        "auto_saved": "INTEGER NOT NULL DEFAULT 1",
        "saved_interval_minutes": "INTEGER NOT NULL DEFAULT 360",
        "saved_next_sync_at": "TEXT",
        "saved_last_sync": "TEXT",
        "saved_failures": "INTEGER NOT NULL DEFAULT 0",
    }

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "insnet.sqlite3"
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            creator_added = self._ensure_columns(conn, "creators", self.CREATOR_COLUMNS)
            self._ensure_columns(conn, "accounts", self.ACCOUNT_COLUMNS)
            # Migrate the old full_sync checkbox exactly once. Do not overwrite
            # choices made in the new UI on every restart.
            if "sync_mode" in creator_added:
                conn.execute("UPDATE creators SET sync_mode=CASE WHEN full_sync=1 THEN 'all' ELSE 'recent20' END")
            # The user chose to keep existing archive files but remove imported follow-list entries.
            conn.execute("DELETE FROM creators WHERE manual=0")

    @staticmethod
    def _ensure_columns(conn, table, columns):
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        added = set()
        for name, definition in columns.items():
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                added.add(name)
        return added

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def accounts(self):
        with self.connect() as c:
            return [dict(x) for x in c.execute("SELECT * FROM accounts ORDER BY created_at")]

    def account(self, account_id):
        with self.connect() as c:
            row = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            return dict(row) if row else None

    def account_by_username(self, username):
        with self.connect() as c:
            row = c.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
            return dict(row) if row else None

    def add_account(self, username, cookie_path):
        account_id = uuid4().hex
        with self.connect() as c:
            c.execute("INSERT INTO accounts(id,username,cookie_path) VALUES(?,?,?)",
                      (account_id, username, str(cookie_path)))
        return account_id

    def creators(self, account_id):
        with self.connect() as c:
            rows = c.execute("""SELECT c.*,
                (SELECT COUNT(DISTINCT p.id) FROM posts p JOIN post_sources s ON s.post_id=p.id
                 WHERE p.account_id=c.account_id AND s.source IN
                   ('creator:'||c.username||':posts','creator:'||c.username||':reels')
                   AND p.status='complete') AS archived_count
                FROM creators c WHERE c.account_id=? ORDER BY c.enabled DESC,c.username""",
                              (account_id,)).fetchall()
            return [dict(x) for x in rows]

    def creator(self, account_id, username):
        with self.connect() as c:
            row = c.execute("SELECT * FROM creators WHERE account_id=? AND username=?",
                            (account_id, username)).fetchone()
            return dict(row) if row else None

    def add_creator(self, account_id, username):
        with self.connect() as c:
            c.execute("""INSERT INTO creators(account_id,username,manual,enabled,next_sync_at)
                         VALUES(?,?,1,1,CURRENT_TIMESTAMP)
                         ON CONFLICT(account_id,username) DO UPDATE SET manual=1""",
                      (account_id, username))

    def set_creator(self, account_id, username, *, enabled=None, sync_mode=None,
                    interval_minutes=None, max_per_run=None):
        fields, values = [], []
        for field, value in (("enabled", enabled), ("sync_mode", sync_mode),
                             ("interval_minutes", interval_minutes), ("max_per_run", max_per_run)):
            if value is not None:
                fields.append(f"{field}=?")
                values.append(int(value) if field == "enabled" else value)
        if enabled is True:
            fields.append("next_sync_at=CURRENT_TIMESTAMP")
        elif interval_minutes is not None:
            fields.append("next_sync_at=datetime('now','+'||?||' minutes')")
            values.append(int(interval_minutes))
        if not fields:
            return False
        with self.connect() as c:
            cur = c.execute(f"UPDATE creators SET {','.join(fields)} WHERE account_id=? AND username=? AND manual=1",
                            (*values, account_id, username))
            return cur.rowcount == 1

    def update_creator_profile(self, account_id, username, display_name="", avatar_url="", profile_id=""):
        with self.connect() as c:
            c.execute("""UPDATE creators SET
                         display_name=CASE WHEN ?<>'' THEN ? ELSE display_name END,
                         avatar_url=CASE WHEN ?<>'' THEN ? ELSE avatar_url END,
                         profile_id=CASE WHEN ?<>'' THEN ? ELSE profile_id END
                         WHERE account_id=? AND username=?""",
                      (display_name, display_name, avatar_url, avatar_url, profile_id, profile_id,
                       account_id, username))

    def update_creator_scan(self, account_id, username, cursor, history_complete):
        with self.connect() as c:
            c.execute("UPDATE creators SET scan_cursor=?,history_complete=? WHERE account_id=? AND username=?",
                      (cursor, int(history_complete), account_id, username))

    def mark_creator_sync(self, account_id, username, error="", rate_limited=False):
        with self.connect() as c:
            row = c.execute("SELECT interval_minutes,failures FROM creators WHERE account_id=? AND username=?",
                            (account_id, username)).fetchone()
            if not row:
                return
            failures = int(row["failures"]) + 1 if error else 0
            interval = int(row["interval_minutes"])
            delay = min(24 * 60, max(interval, 30) * (2 ** min(failures, 5))) if rate_limited else interval
            c.execute("""UPDATE creators SET last_sync=CURRENT_TIMESTAMP,last_error=?,failures=?,
                         next_sync_at=datetime('now','+'||?||' minutes') WHERE account_id=? AND username=?""",
                      (error[:1000], failures, delay, account_id, username))

    def due_creators(self, account_id):
        with self.connect() as c:
            row = c.execute("""SELECT * FROM creators WHERE account_id=? AND manual=1 AND enabled=1
                         AND (next_sync_at IS NULL OR next_sync_at<=CURRENT_TIMESTAMP)
                         ORDER BY COALESCE(next_sync_at,'1970-01-01'),username LIMIT 1""",
                              (account_id,)).fetchone()
            return dict(row) if row else None

    def creator_pending_posts(self, account_id, username):
        source = f"creator:{username}:posts"
        with self.connect() as c:
            posts = [dict(x) for x in c.execute("""SELECT p.* FROM posts p JOIN post_sources s ON s.post_id=p.id
                    WHERE p.account_id=? AND s.source=? AND p.status<>'complete'
                    ORDER BY CASE p.status WHEN 'pending' THEN 0 ELSE 1 END,
                             p.published_at DESC,p.id DESC""", (account_id, source))]
            for post in posts:
                post["items"] = [dict(x) for x in c.execute(
                    "SELECT media_id,position,kind,extension FROM media WHERE post_id=? ORDER BY position",
                    (post["id"],))]
            return posts

    def source_errors(self, account_id, source, limit=10):
        with self.connect() as c:
            return [row[0] for row in c.execute("""SELECT p.error FROM posts p
                    JOIN post_sources s ON s.post_id=p.id
                    WHERE p.account_id=? AND s.source=? AND p.status='partial' AND p.error IS NOT NULL
                    ORDER BY p.updated_at DESC LIMIT ?""", (account_id, source, limit))]

    def set_saved_schedule(self, account_id, enabled, interval_minutes):
        with self.connect() as c:
            c.execute("""UPDATE accounts SET auto_saved=?,saved_interval_minutes=?,
                         saved_next_sync_at=CASE WHEN ?=1 THEN CURRENT_TIMESTAMP ELSE saved_next_sync_at END
                         WHERE id=?""",
                      (int(enabled), int(interval_minutes), int(enabled), account_id))

    def saved_due(self, account_id):
        with self.connect() as c:
            return c.execute("""SELECT 1 FROM accounts WHERE id=? AND auto_saved=1
                         AND (saved_next_sync_at IS NULL OR saved_next_sync_at<=CURRENT_TIMESTAMP)""",
                              (account_id,)).fetchone() is not None

    def mark_saved_sync(self, account_id, error="", rate_limited=False):
        with self.connect() as c:
            account = c.execute("SELECT saved_interval_minutes,saved_failures FROM accounts WHERE id=?",
                                (account_id,)).fetchone()
            if not account:
                return
            failures = min(8, int(account["saved_failures"] or 0) + 1) if error else 0
            delay = min(24 * 60, max(30, int(account["saved_interval_minutes"])) * (2 ** min(failures, 5))) \
                if rate_limited else int(account["saved_interval_minutes"])
            c.execute("""UPDATE accounts SET saved_last_sync=CURRENT_TIMESTAMP,saved_failures=?,
                         saved_next_sync_at=datetime('now','+'||?||' minutes') WHERE id=?""",
                      (failures, delay, account_id))

    def delete_creator(self, account_id, username, delete_archive=False):
        sources = (f"creator:{username}:posts", f"creator:{username}:reels")
        files, removed, shared = [], 0, 0
        with self.connect() as c:
            creator = c.execute("SELECT 1 FROM creators WHERE account_id=? AND username=? AND manual=1",
                                (account_id, username)).fetchone()
            if not creator:
                return None
            if delete_archive:
                rows = c.execute("""SELECT DISTINCT s.post_id FROM post_sources s JOIN posts p ON p.id=s.post_id
                                   WHERE p.account_id=? AND s.source IN (?,?)""",
                                 (account_id, *sources)).fetchall()
                for item in rows:
                    post_id = item["post_id"]
                    c.execute("DELETE FROM post_sources WHERE post_id=? AND source IN (?,?)",
                              (post_id, *sources))
                    remaining = c.execute("SELECT COUNT(*) FROM post_sources WHERE post_id=?", (post_id,)).fetchone()[0]
                    if remaining:
                        shared += 1
                        continue
                    files.extend(x[0] for x in c.execute(
                        "SELECT relative_path FROM media WHERE post_id=? AND relative_path IS NOT NULL", (post_id,)))
                    c.execute("DELETE FROM posts WHERE id=?", (post_id,))
                    removed += 1
            c.execute("DELETE FROM creators WHERE account_id=? AND username=?", (account_id, username))
        return {"files": files, "removed_posts": removed, "shared_posts": shared}

    def upsert_post(self, account_id, post, source):
        with self.connect() as c:
            c.execute("""INSERT INTO posts(account_id,shortcode,username,caption,published_at,source_url)
                         VALUES(?,?,?,?,?,?) ON CONFLICT(account_id,shortcode) DO UPDATE SET
                         username=excluded.username, caption=excluded.caption,
                         published_at=excluded.published_at, source_url=excluded.source_url,
                         updated_at=CURRENT_TIMESTAMP""",
                      (account_id, post["shortcode"], post["username"], post["caption"],
                       post["published_at"], post["source_url"]))
            row = c.execute("SELECT id FROM posts WHERE account_id=? AND shortcode=?",
                            (account_id, post["shortcode"])).fetchone()
            post_id = row["id"]
            c.execute("INSERT OR IGNORE INTO post_sources(post_id,source) VALUES(?,?)", (post_id, source))
            for item in post["items"]:
                c.execute("""INSERT INTO media(post_id,media_id,position,kind,extension)
                             VALUES(?,?,?,?,?) ON CONFLICT(post_id,media_id) DO UPDATE SET
                             position=excluded.position, kind=excluded.kind""",
                          (post_id, item["media_id"], item["position"], item["kind"], item["extension"]))
            return post_id

    def post_media(self, post_id):
        with self.connect() as c:
            return [dict(x) for x in c.execute(
                "SELECT * FROM media WHERE post_id=? ORDER BY position", (post_id,))]

    def set_media_file(self, post_id, media_id, relative_path, size, extension):
        with self.connect() as c:
            c.execute("UPDATE media SET relative_path=?,size=?,extension=? WHERE post_id=? AND media_id=?",
                      (relative_path, size, extension, post_id, media_id))

    def set_post_status(self, post_id, status, error=None):
        with self.connect() as c:
            c.execute("UPDATE posts SET status=?,error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                      (status, error, post_id))

    def post(self, post_id):
        with self.connect() as c:
            row = c.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
            return dict(row) if row else None

    def posts(self, account_id, limit=100):
        with self.connect() as c:
            posts = [dict(x) for x in c.execute(
                "SELECT * FROM posts WHERE account_id=? ORDER BY published_at DESC,id DESC LIMIT ?",
                (account_id, limit))]
            for post in posts:
                post["media"] = [dict(x) for x in c.execute(
                    "SELECT * FROM media WHERE post_id=? ORDER BY position", (post["id"],))]
                post["sources"] = [x[0] for x in c.execute(
                    "SELECT source FROM post_sources WHERE post_id=? ORDER BY source", (post["id"],))]
            return posts

    def create_run(self, account_id, kind):
        run_id = uuid4().hex
        with self.connect() as c:
            c.execute("INSERT INTO runs(id,account_id,kind) VALUES(?,?,?)", (run_id, account_id, kind))
        return run_id

    def finish_run(self, run_id, status, counts, message=""):
        with self.connect() as c:
            c.execute("""UPDATE runs SET status=?,downloaded=?,skipped=?,failed=?,message=?,
                         finished_at=CURRENT_TIMESTAMP WHERE id=?""",
                      (status, counts["downloaded"], counts["skipped"], counts["failed"], message[:2000], run_id))

    def add_run_log(self, run_id, level, source, message):
        with self.connect() as c:
            c.execute("INSERT INTO run_logs(run_id,level,source,message) VALUES(?,?,?,?)",
                      (run_id, str(level)[:10].upper(), str(source)[:100], str(message)[:6000]))

    def run_logs(self, run_id, limit=500):
        with self.connect() as c:
            rows = c.execute("SELECT id,level,source,message,created_at FROM run_logs WHERE run_id=? "
                             "ORDER BY id DESC LIMIT ?", (run_id, min(max(1, int(limit)), 500))).fetchall()
            return [dict(x) for x in reversed(rows)]

    def setting(self, key, default=""):
        with self.connect() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key, value):
        with self.connect() as c:
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def runs(self, limit=30):
        with self.connect() as c:
            return [dict(x) for x in c.execute(
                "SELECT r.*,a.username,(SELECT COUNT(*) FROM run_logs l WHERE l.run_id=r.id) AS log_count "
                "FROM runs r JOIN accounts a ON a.id=r.account_id "
                "ORDER BY r.started_at DESC,r.rowid DESC LIMIT ?", (limit,))]
