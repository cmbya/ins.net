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
    skipped INTEGER NOT NULL DEFAULT 0, ignored INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS run_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'INFO', source TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, level TEXT NOT NULL DEFAULT 'ERROR',
    source TEXT NOT NULL DEFAULT 'scheduler', message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_account_date ON posts(account_id, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_run_logs_id ON run_logs(run_id, id);
CREATE INDEX IF NOT EXISTS idx_system_events_created ON system_events(created_at DESC, id DESC);
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
        "verify_cursor": "INTEGER NOT NULL DEFAULT 0",
        "next_sync_at": "TEXT",
        "last_error": "TEXT NOT NULL DEFAULT ''",
        "failures": "INTEGER NOT NULL DEFAULT 0",
    }
    ACCOUNT_COLUMNS = {
        "label": "TEXT NOT NULL DEFAULT ''",
        "enabled": "INTEGER NOT NULL DEFAULT 1",
        "cookie_status": "TEXT NOT NULL DEFAULT 'unverified'",
        "cookie_checked_at": "TEXT",
        "creator_subdir": "TEXT NOT NULL DEFAULT ''",
    }

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "insnet.sqlite3"
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            creator_added = self._ensure_columns(conn, "creators", self.CREATOR_COLUMNS)
            self._ensure_columns(conn, "accounts", self.ACCOUNT_COLUMNS)
            self._ensure_columns(conn, "runs", {"ignored": "INTEGER NOT NULL DEFAULT 0"})
            added = self._ensure_columns(conn, "posts", {"deleted_at": "TEXT", "synced_at": "TEXT"})
            if "synced_at" in added:
                conn.execute("UPDATE posts SET synced_at=updated_at WHERE status='complete'")
            # Imported follow-list entries leave the monitor list while their profile is retained for dashboard history.
            conn.execute("UPDATE creators SET manual=2,enabled=0 WHERE manual=0")
            # Migrate the old full_sync checkbox exactly once. Do not overwrite
            # choices made in the new UI on every restart.
            if "sync_mode" in creator_added:
                conn.execute("UPDATE creators SET sync_mode=CASE WHEN full_sync=1 THEN 'all' ELSE 'recent20' END")

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
                 WHERE p.account_id=c.account_id AND s.source='creator:'||c.username||':posts'
                   AND p.status='complete' AND p.deleted_at IS NULL) AS archived_count
                FROM creators c WHERE c.account_id=? AND c.manual=1 ORDER BY c.enabled DESC,c.username""",
                              (account_id,)).fetchall()
            return [{key: value for key, value in dict(x).items()
                     if key not in ("interval_minutes", "max_per_run")} for x in rows]

    def creator(self, account_id, username):
        with self.connect() as c:
            row = c.execute("SELECT * FROM creators WHERE account_id=? AND username=?",
                            (account_id, username)).fetchone()
            return dict(row) if row else None

    def add_creator(self, account_id, username):
        interval = int(self.setting("creator_interval", "360"))
        maximum = int(self.setting("creator_max", "20"))
        with self.connect() as c:
            c.execute("""INSERT INTO creators(account_id,username,manual,enabled,next_sync_at,interval_minutes,max_per_run)
                         VALUES(?,?,1,1,CURRENT_TIMESTAMP,?,?)
                         ON CONFLICT(account_id,username) DO UPDATE SET manual=1,enabled=1,
                         interval_minutes=excluded.interval_minutes,max_per_run=excluded.max_per_run,
                         next_sync_at=CURRENT_TIMESTAMP""",
                      (account_id, username, interval, maximum))

    def set_creator(self, account_id, username, *, enabled=None, sync_mode=None):
        fields, values = [], []
        for field, value in (("enabled", enabled), ("sync_mode", sync_mode)):
            if value is not None:
                fields.append(f"{field}=?")
                values.append(int(value) if field == "enabled" else value)
        if enabled is True:
            fields.append("next_sync_at=CURRENT_TIMESTAMP")
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

    def update_creator_verify_cursor(self, account_id, username, cursor):
        with self.connect() as c:
            c.execute("UPDATE creators SET verify_cursor=? WHERE account_id=? AND username=?",
                      (int(cursor), account_id, username))

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
                    WHERE p.account_id=? AND s.source=? AND p.status<>'complete' AND p.deleted_at IS NULL
                    ORDER BY CASE p.status WHEN 'pending' THEN 0 ELSE 1 END,
                             p.published_at DESC,p.id DESC""", (account_id, source))]
            for post in posts:
                post["items"] = [dict(x) for x in c.execute(
                    "SELECT media_id,position,kind,extension FROM media WHERE post_id=? ORDER BY position",
                    (post["id"],))]
            return posts

    def creator_complete_posts(self, account_id, username, after_id=0, limit=50):
        source = f"creator:{username}:posts"
        with self.connect() as c:
            posts = [dict(x) for x in c.execute("""SELECT p.* FROM posts p JOIN post_sources s ON s.post_id=p.id
                    WHERE p.account_id=? AND s.source=? AND p.status='complete'
                      AND p.deleted_at IS NULL AND p.id>?
                    ORDER BY p.id LIMIT ?""", (account_id, source, int(after_id), int(limit)))]
            for post in posts:
                post["account_id"] = account_id
                post["items"] = [dict(x) for x in c.execute(
                    "SELECT media_id,position,kind,extension,relative_path,size FROM media WHERE post_id=? ORDER BY position",
                    (post["id"],))]
            return posts

    def source_errors(self, account_id, source, limit=10):
        with self.connect() as c:
            return [row[0] for row in c.execute("""SELECT p.error FROM posts p
                    JOIN post_sources s ON s.post_id=p.id
                    WHERE p.account_id=? AND s.source=? AND p.status='partial' AND p.deleted_at IS NULL AND p.error IS NOT NULL
                    ORDER BY p.updated_at DESC LIMIT ?""", (account_id, source, limit))]

    def delete_creator(self, account_id, username, delete_archive=False):
        """Remove a creator from the monitor list; optionally hide records, never files."""
        with self.connect() as c:
            creator = c.execute("SELECT 1 FROM creators WHERE account_id=? AND username=? AND manual=1",
                                (account_id, username)).fetchone()
            if not creator:
                return None
            removed = 0
            if delete_archive:
                cur = c.execute("UPDATE posts SET deleted_at=CURRENT_TIMESTAMP WHERE account_id=? AND username=? AND deleted_at IS NULL",
                                (account_id, username))
                removed = cur.rowcount
            c.execute("UPDATE creators SET manual=2,enabled=0 WHERE account_id=? AND username=?", (account_id, username))
        return {"files": [], "removed_posts": removed, "shared_posts": 0}

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
            c.execute("UPDATE posts SET status=?,error=?,updated_at=CURRENT_TIMESTAMP, "
                      "synced_at=CASE WHEN ?='complete' THEN COALESCE(synced_at,CURRENT_TIMESTAMP) ELSE synced_at END WHERE id=?",
                      (status, error, status, post_id))

    def post(self, post_id):
        with self.connect() as c:
            row = c.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
            return dict(row) if row else None

    def posts(self, account_id, limit=100):
        with self.connect() as c:
            posts = [dict(x) for x in c.execute(
                "SELECT * FROM posts WHERE account_id=? AND deleted_at IS NULL ORDER BY published_at DESC,id DESC LIMIT ?",
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

    def recover_interrupted_runs(self):
        with self.connect() as c:
            rows = c.execute("SELECT id FROM runs WHERE status='running'").fetchall()
            for row in rows:
                c.execute("UPDATE runs SET status='interrupted',message=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",
                          ("容器重启前任务未完成，请重新同步", row["id"]))
                c.execute("INSERT INTO run_logs(run_id,level,source,message) VALUES(?,?,?,?)",
                          (row["id"], "WARNING", "startup", "检测到进程重启，任务已标记中断；请重新启动同步"))
        return len(rows)

    def add_system_event(self, level, source, message):
        with self.connect() as c:
            c.execute("INSERT INTO system_events(level,source,message) VALUES(?,?,?)",
                      (str(level)[:10].upper(), str(source)[:100], str(message)[:6000]))

    def finish_run(self, run_id, status, counts, message=""):
        with self.connect() as c:
            c.execute("""UPDATE runs SET status=?,downloaded=?,skipped=?,ignored=?,failed=?,message=?,
                         finished_at=CURRENT_TIMESTAMP WHERE id=?""",
                      (status, counts["downloaded"], counts["skipped"], counts.get("ignored", 0),
                       counts["failed"], message[:2000], run_id))

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

    def dashboard(self, account_id=""):
        clause, args = (" AND p.account_id=?", [account_id]) if account_id else ("", [])
        where = ("p.status='complete' AND p.deleted_at IS NULL "
                 "AND EXISTS(SELECT 1 FROM post_sources s WHERE s.post_id=p.id AND substr(s.source,-6)=':posts')") + clause
        with self.connect() as c:
            total = c.execute(f"SELECT COUNT(*) FROM posts p WHERE {where}", args).fetchone()[0]
            size = c.execute(f"SELECT COALESCE(SUM(m.size),0) FROM media m JOIN posts p ON p.id=m.post_id WHERE {where}", args).fetchone()[0]
            types = dict(c.execute(f"SELECT m.kind,COUNT(*) FROM media m JOIN posts p ON p.id=m.post_id WHERE {where} GROUP BY m.kind", args))
            creators = [dict(r) for r in c.execute(f"""SELECT p.account_id,p.username,
                COALESCE(cr.display_name,'') AS display_name,COALESCE(cr.avatar_url,'') AS avatar_url,
                MAX(CASE WHEN cr.manual=1 THEN 1 ELSE 0 END) AS monitored, COUNT(*) AS count FROM posts p LEFT JOIN creators cr ON cr.account_id=p.account_id AND cr.username=p.username
                WHERE {where} GROUP BY p.account_id,p.username ORDER BY count DESC,p.username""", args)]
            trend = [dict(r) for r in c.execute(f"""SELECT date(p.synced_at,'+8 hours') AS day,COUNT(*) AS count
                FROM posts p WHERE {where} AND p.synced_at>=datetime('now','-13 days')
                GROUP BY day ORDER BY day""", args)]
            return {"total": total, "bytes": size, "images": types.get("image",0), "videos": types.get("video",0),
                    "authors": creators, "trend": trend}

    def records(self, filters):
        where, args = ["1=1"], []
        for name, column in (("account", "p.account_id"), ("status", "p.status")):
            if filters.get(name):
                where.append(column+"=?"); args.append(filters[name])
        for name, column in (("author", "p.username"), ("title", "p.caption")):
            if filters.get(name):
                where.append(column+" LIKE ?"); args.append('%'+filters[name]+'%')
        for field in ("synced_at", "published_at"):
            for suffix, op in (("from", ">="), ("to", "<=")):
                value = filters.get(field+"_"+suffix)
                if value:
                    where.append(f"date(p.{field},'+8 hours'){op}?"); args.append(value)
        where.append("p.deleted_at IS NOT NULL" if filters.get("deleted") == "1" else "p.deleted_at IS NULL")
        # The product only synchronizes manually selected creators. Keep legacy
        # saved-list rows in SQLite for migration safety, but omit them from UI.
        where.append("EXISTS(SELECT 1 FROM post_sources s WHERE s.post_id=p.id AND substr(s.source,-6)=':posts')")
        page, limit = max(1,int(filters.get("page",1))), min(100,max(1,int(filters.get("limit",20))))
        sql = " AND ".join(where)
        with self.connect() as c:
            total = c.execute(f"SELECT COUNT(*) FROM posts p WHERE {sql}",args).fetchone()[0]
            rows = [dict(r) for r in c.execute(f"""SELECT p.*,COALESCE(NULLIF(a.label,''),a.username) AS account_label,
                COALESCE(cr.display_name,'') AS display_name FROM posts p JOIN accounts a ON p.account_id=a.id
                LEFT JOIN creators cr ON cr.account_id=p.account_id AND cr.username=p.username
                WHERE {sql} ORDER BY p.synced_at DESC,p.id DESC LIMIT ? OFFSET ?""",(*args,limit,(page-1)*limit))]
            for row in rows:
                row["media"] = [dict(r) for r in c.execute("SELECT * FROM media WHERE post_id=? ORDER BY position",(row["id"],))]
                row["sources"] = [r[0] for r in c.execute("SELECT source FROM post_sources WHERE post_id=?",(row["id"],))]
        return {"items":rows,"total":total,"page":page,"limit":limit}

    def hide_records(self, account_id, username=None, ids=None, restore=False):
        # Hide dashboard rows while retaining media paths for next-run file validation. Never unlink media.
        where, args = ["account_id=?"], [account_id]
        if username:
            where.append("username=?"); args.append(username)
            where.append("deleted_at IS NULL")
        elif ids:
            where.append("id IN ("+','.join('?' for _ in ids)+")"); args.extend(ids)
            where.append("deleted_at IS NOT NULL" if restore else "deleted_at IS NULL")
        else:
            raise ValueError("请选择作品或作者")
        with self.connect() as c:
            result = c.execute("UPDATE posts SET deleted_at="+("NULL" if restore else "CURRENT_TIMESTAMP")+
                               " WHERE "+' AND '.join(where),args)
            return result.rowcount

    def system_logs(self, filters):
        run_where, run_args = ["1=1"], []
        event_where, event_args = ["1=1"], []
        if filters.get("date"):
            run_where.append("date(l.created_at,'+8 hours')=?"); run_args.append(filters["date"])
            event_where.append("date(e.created_at,'+8 hours')=?"); event_args.append(filters["date"])
        if filters.get("account"):
            run_where.append("r.account_id=?"); run_args.append(filters["account"])
            event_where.append("0=1")
        if filters.get("level"):
            run_where.append("l.level=?"); run_args.append(filters["level"])
            event_where.append("e.level=?"); event_args.append(filters["level"])
        if filters.get("run"):
            run_where.append("l.run_id=?"); run_args.append(filters["run"])
            event_where.append("0=1")
        if filters.get("q"):
            run_where.append("(l.message LIKE ? OR l.source LIKE ?)"); run_args.extend(['%'+filters['q']+'%']*2)
            event_where.append("(e.message LIKE ? OR e.source LIKE ?)"); event_args.extend(['%'+filters['q']+'%']*2)
        page, limit = max(1,int(filters.get("page",1))), min(500,max(1,int(filters.get("limit",100))))
        run_from = " FROM run_logs l JOIN runs r ON r.id=l.run_id LEFT JOIN accounts a ON a.id=r.account_id WHERE "+' AND '.join(run_where)
        event_from = " FROM system_events e WHERE "+' AND '.join(event_where)
        with self.connect() as c:
            total = c.execute("SELECT COUNT(*)"+run_from,run_args).fetchone()[0]
            total += c.execute("SELECT COUNT(*)"+event_from,event_args).fetchone()[0]
            rows = [dict(r) for r in c.execute("""SELECT * FROM (
                SELECT l.id AS id,l.run_id AS run_id,l.level AS level,l.source AS source,
                       l.message AS message,l.created_at AS created_at,r.kind AS kind,r.status AS status,
                       COALESCE(NULLIF(a.label,''),a.username,'系统') AS account_label
                """+run_from+""" UNION ALL
                SELECT -e.id AS id,NULL AS run_id,e.level AS level,e.source AS source,
                       e.message AS message,e.created_at AS created_at,'system' AS kind,'event' AS status,
                       '系统' AS account_label
                """+event_from+""" ) ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?""",
                (*run_args,*event_args,limit,(page-1)*limit))]
        return {"items":rows,"total":total,"page":page,"limit":limit}

    def admin_accounts(self):
        legacy = {"cookie_path", "saved_subdir", "saved_max_per_run", "saved_recent_only",
                  "auto_saved", "saved_interval_minutes", "saved_next_sync_at",
                  "saved_last_sync", "saved_failures"}
        return [{k:v for k,v in a.items() if k not in legacy} for a in self.accounts()]

    def admin_config(self):
        defaults = {"creator_interval":"360","creator_max":"20","scheduler_enabled":"1","log_days":"30"}
        return {k:int(self.setting(k,v)) for k,v in defaults.items()}

    def save_admin_config(self, config):
        with self.connect() as c:
            for key, value in config.items():
                c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
            interval = int(config["creator_interval"])
            maximum = int(config["creator_max"])
            c.execute("""UPDATE creators SET
                         next_sync_at=CASE WHEN interval_minutes<>? THEN
                           CASE WHEN last_sync IS NULL THEN CURRENT_TIMESTAMP
                                ELSE datetime(last_sync,'+'||?||' minutes') END
                           ELSE next_sync_at END,
                         interval_minutes=?,max_per_run=? WHERE manual=1""",
                      (interval, interval, interval, maximum))

    def purge_old_logs(self):
        days = max(1,int(self.setting('log_days','30')))
        with self.connect() as c:
            c.execute("DELETE FROM run_logs WHERE created_at<datetime('now','-'||?||' days')",(days,))
            c.execute("DELETE FROM system_events WHERE created_at<datetime('now','-'||?||' days')",(days,))
