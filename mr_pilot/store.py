"""SQLite state: MRs, code-standard violations, activity events (dashboard), key/value."""
import json
import os
import sqlite3
import threading
import time

COLUMNS = ["project_id", "iid", "sha", "title", "web_url", "status", "review",
           "tg_msg_id", "tg_text", "first_seen", "attempts", "updated_at",
           "project", "author", "author_username", "source_branch", "target_branch",
           "verdict", "pipeline", "quality", "decided_at", "mr_created_at"]

_EXTRA_COLS = {"project": "TEXT", "author": "TEXT", "author_username": "TEXT",
               "source_branch": "TEXT", "target_branch": "TEXT", "verdict": "TEXT",
               "pipeline": "TEXT", "quality": "TEXT", "decided_at": "REAL", "mr_created_at": "TEXT"}


class Store:
    def __init__(self, path):
        d = os.path.dirname(path)
        if d and path != ":memory:":
            os.makedirs(d, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, timeout=15, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if path != ":memory:":
            # Rollback journal, not WAL: WAL needs shared-memory mmap that fails on some bind mounts
            # (Docker Desktop on Windows/macOS, network drives). One process writes, so WAL gains little.
            self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA busy_timeout=15000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS mrs(
                key TEXT PRIMARY KEY, project_id INTEGER, iid INTEGER, sha TEXT,
                title TEXT, web_url TEXT, status TEXT, review TEXT,
                tg_msg_id INTEGER, tg_text TEXT, first_seen REAL,
                attempts INTEGER DEFAULT 0, updated_at REAL);
            CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS violations(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, mr_key TEXT, scope TEXT,
                sha TEXT, commit_sha TEXT, author TEXT, project TEXT, stack TEXT,
                rule TEXT, severity TEXT, source TEXT, path TEXT, line INTEGER,
                message TEXT, snippet TEXT, posted INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS ix_viol_mr ON violations(mr_key, scope);
            CREATE INDEX IF NOT EXISTS ix_viol_ts ON violations(ts);
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, type TEXT,
                mr_key TEXT, title TEXT, detail TEXT, level TEXT);
            CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
            CREATE TABLE IF NOT EXISTS ai_calls(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, provider TEXT, task TEXT,
                ok INTEGER, ms INTEGER, error TEXT);
        """)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(mrs)")}
        for col, typ in _EXTRA_COLS.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE mrs ADD COLUMN {col} {typ}")
        self.db.commit()

    # ------------------------------------------------------------------ mrs
    @staticmethod
    def _row(r):
        if r is None:
            return None
        d = dict(r)
        for k in ("review", "quality"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        return d

    def get(self, key):
        with self.lock:
            return self._row(self.db.execute("SELECT * FROM mrs WHERE key=?", (key,)).fetchone())

    def upsert(self, key, **fields):
        fields["updated_at"] = time.time()
        for k in ("review", "quality"):
            if k in fields and not isinstance(fields[k], (str, type(None))):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        bad = set(fields) - set(COLUMNS)
        if bad:
            raise KeyError(bad)
        cols = ["key"] + list(fields)
        sql = (f"INSERT INTO mrs({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
               f"ON CONFLICT(key) DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in fields))
        with self.lock:
            self.db.execute(sql, [key] + list(fields.values()))
            self.db.commit()

    def by_status(self, *statuses):
        q = f"SELECT * FROM mrs WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY updated_at"
        with self.lock:
            return [self._row(r) for r in self.db.execute(q, statuses).fetchall()]

    def all_mrs(self, limit=200):
        with self.lock:
            return [self._row(r) for r in self.db.execute(
                "SELECT * FROM mrs ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()]

    # ------------------------------------------------------------------- kv
    def kv_get(self, k, default=None):
        with self.lock:
            r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def kv_set(self, k, v):
        with self.lock:
            self.db.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
            self.db.commit()

    def kv_del(self, k):
        with self.lock:
            self.db.execute("DELETE FROM kv WHERE k=?", (k,))
            self.db.commit()

    # ------------------------------------------------------------ violations
    def replace_mr_violations(self, mr_key, sha, project, vs):
        """Current state of an MR (scope='mr'): replaced on every check."""
        with self.lock:
            self.db.execute("DELETE FROM violations WHERE mr_key=? AND scope='mr'", (mr_key,))
            self._insert(mr_key, "mr", sha, None, None, project, vs)

    def add_commit_violations(self, mr_key, commit_sha, author, project, vs):
        """History (scope='commit'): what each commit introduced. Used for trends."""
        with self.lock:
            self._insert(mr_key, "commit", commit_sha, commit_sha, author, project, vs)

    def _insert(self, mr_key, scope, sha, commit_sha, author, project, vs):
        now = time.time()
        self.db.executemany(
            "INSERT INTO violations(ts,mr_key,scope,sha,commit_sha,author,project,stack,rule,severity,"
            "source,path,line,message,snippet,posted) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(now, mr_key, scope, sha, commit_sha, v.get("author") or author, project, v.get("stack"),
              v["rule"], v["severity"], v.get("source"), v.get("path"), v.get("line"),
              v.get("message"), v.get("snippet"), int(bool(v.get("posted")))) for v in vs])
        self.db.commit()

    def mr_violations(self, mr_key):
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM violations WHERE mr_key=? AND scope='mr' ORDER BY "
                "CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, path, line",
                (mr_key,)).fetchall()]

    # --------------------------------------------------------------- events
    def add_event(self, type_, title, detail="", mr_key=None, level="info"):
        with self.lock:
            cur = self.db.execute("INSERT INTO events(ts,type,mr_key,title,detail,level) VALUES(?,?,?,?,?,?)",
                                  (time.time(), type_, mr_key, title, detail, level))
            self.db.commit()
            return cur.lastrowid

    def events_after(self, after_id=0, limit=50):
        with self.lock:
            rows = self.db.execute("SELECT * FROM events WHERE id>? ORDER BY id DESC LIMIT ?",
                                   (after_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def last_event_id(self):
        with self.lock:
            r = self.db.execute("SELECT MAX(id) FROM events").fetchone()
        return r[0] or 0

    # ------------------------------------------------------------- ai usage
    def add_ai_call(self, provider, task, ok, ms, error=""):
        with self.lock:
            self.db.execute("INSERT INTO ai_calls(ts,provider,task,ok,ms,error) VALUES(?,?,?,?,?,?)",
                            (time.time(), provider, task, int(bool(ok)), ms, (error or "")[:300]))
            self.db.commit()

    def ai_stats(self, days=7):
        since = time.time() - days * 86400
        with self.lock:
            rows = self.db.execute(
                "SELECT provider, COUNT(*) n, SUM(ok) ok, AVG(CASE WHEN ok=1 THEN ms END) avg_ms, MAX(ts) last_ts "
                "FROM ai_calls WHERE ts>=? GROUP BY provider", (since,)).fetchall()
            errs = self.db.execute("SELECT provider, task, ts, error FROM ai_calls WHERE ok=0 AND ts>=? "
                                   "ORDER BY ts DESC LIMIT 15", (since,)).fetchall()
        return {"by_provider": [dict(r) for r in rows], "recent_errors": [dict(r) for r in errs]}

    # -------------------------------------------------------------- upkeep
    def prune(self, events_days=90, ai_days=90, violations_days=365):
        """Retention so the database doesn't grow forever. Returns rows deleted."""
        now = time.time()
        n = 0
        with self.lock:
            for sql, days in (("DELETE FROM events WHERE ts < ?", events_days),
                              ("DELETE FROM ai_calls WHERE ts < ?", ai_days),
                              ("DELETE FROM violations WHERE scope='commit' AND ts < ?", violations_days)):
                n += self.db.execute(sql, (now - days * 86400,)).rowcount
            # reject prompts that were never answered
            n += self.db.execute("DELETE FROM kv WHERE k LIKE 'reject:%' AND rowid NOT IN "
                                 "(SELECT rowid FROM kv WHERE k LIKE 'reject:%' ORDER BY rowid DESC LIMIT 50)").rowcount
            self.db.commit()
        return n

    def integrity_ok(self):
        with self.lock:
            return self.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"

    # ------------------------------------------------------------- dashboard
    def query(self, sql, params=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, params).fetchall()]
