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
    account_id TEXT NOT NULL, username TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
    full_sync INTEGER NOT NULL DEFAULT 0, manual INTEGER NOT NULL DEFAULT 0,
    last_sync TEXT, PRIMARY KEY (account_id, username)
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
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "insnet.sqlite3"
        with self.connect() as conn:
            conn.executescript(SCHEMA)

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
            return [dict(x) for x in c.execute(
                "SELECT * FROM creators WHERE account_id=? ORDER BY enabled DESC, username",
                (account_id,))]

    def upsert_creator(self, account_id, username, manual=False, enabled=False):
        with self.connect() as c:
            c.execute("""INSERT INTO creators(account_id,username,manual,enabled)
                         VALUES(?,?,?,?) ON CONFLICT(account_id,username) DO UPDATE SET
                         manual=MAX(creators.manual,excluded.manual)""",
                      (account_id, username, int(manual), int(enabled)))

    def set_creator(self, account_id, username, enabled=None, full_sync=None):
        fields, values = [], []
        if enabled is not None:
            fields.append("enabled=?")
            values.append(int(enabled))
        if full_sync is not None:
            fields.append("full_sync=?")
            values.append(int(full_sync))
        if not fields:
            return False
        with self.connect() as c:
            cur = c.execute(f"UPDATE creators SET {','.join(fields)} WHERE account_id=? AND username=?",
                            (*values, account_id, username))
            return cur.rowcount == 1

    def mark_creator_sync(self, account_id, username):
        with self.connect() as c:
            c.execute("UPDATE creators SET last_sync=CURRENT_TIMESTAMP WHERE account_id=? AND username=?",
                      (account_id, username))

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
            c.execute("""UPDATE media SET relative_path=?,size=?,extension=?
                         WHERE post_id=? AND media_id=?""",
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
                "SELECT * FROM posts WHERE account_id=? ORDER BY published_at DESC, id DESC LIMIT ?",
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
            rows = c.execute(
                "SELECT id,level,source,message,created_at FROM run_logs WHERE run_id=? "
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
                "SELECT r.*,a.username FROM runs r JOIN accounts a ON a.id=r.account_id "
                "ORDER BY r.started_at DESC, r.rowid DESC LIMIT ?", (limit,))]
