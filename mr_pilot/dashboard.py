"""Realtime dashboard: tiny stdlib HTTP server + Server-Sent Events. No extra dependencies."""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import yaml

from .standards import Standards
from .util import redact

log = logging.getLogger("mr_pilot.dashboard")

ACTIVE = ("notified", "waiting_bot", "error", "merging")
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_FILE_RE = re.compile(r"^(?!\.)[A-Za-z0-9_.-]{1,80}\.(md|ya?ml)$")


def _iso_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _day(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _days(n):
    today = datetime.now().date()
    return [(today - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


# =================================================================== data
class DashboardData:
    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store

    def _mr_view(self, r, viol_counts):
        rv = r.get("review") or {}
        created = _iso_ts(r.get("mr_created_at")) or r.get("first_seen")
        return {
            "key": r["key"], "iid": r["iid"], "title": r.get("title"), "url": r.get("web_url"),
            "project": r.get("project"), "author": r.get("author"), "author_username": r.get("author_username"),
            "source_branch": r.get("source_branch"), "target_branch": r.get("target_branch"),
            "status": r.get("status"), "verdict": r.get("verdict") or rv.get("verdict"),
            "pipeline": r.get("pipeline"), "sha": (r.get("sha") or "")[:8],
            "created_ts": created, "updated_ts": r.get("updated_at"), "decided_ts": r.get("decided_at"),
            "violations": viol_counts.get(r["key"], {"error": 0, "warning": 0, "info": 0}),
            "summary": rv.get("summary", ""),
        }

    def _viol_counts(self):
        out = {}
        for row in self.store.query("SELECT mr_key, severity, COUNT(*) n FROM violations "
                                    "WHERE scope='mr' GROUP BY mr_key, severity"):
            out.setdefault(row["mr_key"], {"error": 0, "warning": 0, "info": 0})[row["severity"]] = row["n"]
        return out

    def summary(self):
        now = time.time()
        vc = self._viol_counts()
        mrs = self.store.all_mrs(500)
        active = [self._mr_view(r, vc) for r in mrs if r.get("status") in ACTIVE]
        active.sort(key=lambda m: m["created_ts"] or 0)
        recent = [self._mr_view(r, vc) for r in mrs
                  if r.get("status") in ("merged", "rejected", "closed")][:15]

        decided = [r for r in mrs if r.get("decided_at") and r["decided_at"] >= now - 30 * 86400]
        waits = []
        for r in decided:
            start = _iso_ts(r.get("mr_created_at")) or r.get("first_seen")
            if start and r["decided_at"] > start:
                waits.append((r["decided_at"] - start) / 3600)
        merged_7 = sum(1 for r in mrs if r.get("status") == "merged" and (r.get("decided_at") or 0) >= now - 7 * 86400)
        merged_prev = sum(1 for r in mrs if r.get("status") == "merged"
                          and now - 14 * 86400 <= (r.get("decided_at") or 0) < now - 7 * 86400)

        days = _days(14)
        flow = {d: {"date": d, "opened": 0, "merged": 0} for d in days}
        for r in mrs:
            c = _iso_ts(r.get("mr_created_at"))
            if c and _day(c) in flow:
                flow[_day(c)]["opened"] += 1
            if r.get("status") == "merged" and r.get("decided_at") and _day(r["decided_at"]) in flow:
                flow[_day(r["decided_at"])]["merged"] += 1

        return {
            "now": now,
            "user": self.store.kv_get("gitlab_user") or self.cfg["gitlab"].get("username") or "",
            "auth": bool(self.cfg["dashboard"].get("password")),
            "health": {
                "last_poll_ok": float(self.store.kv_get("last_poll_ok", 0) or 0),
                "last_poll_error": self.store.kv_get("last_poll_error", ""),
                "poll_interval": int(self.cfg["gitlab"]["poll_interval_seconds"]),
                "review_mode": self.cfg["review"]["mode"],
                "quality_enabled": bool(self.cfg["code_quality"]["enabled"]),
                "teams_mode": self.cfg["teams"]["mode"],
            },
            "kpi": {
                "waiting": len(active),
                "waiting_attention": sum(1 for m in active if m["verdict"] in ("REQUEST_CHANGES", "NEEDS_ATTENTION")
                                         or m["violations"]["error"] or m["pipeline"] == "failed"),
                "merged_7d": merged_7, "merged_prev_7d": merged_prev,
                "avg_decision_hours": round(sum(waits) / len(waits), 1) if waits else None,
                "open_errors": sum(m["violations"]["error"] for m in active),
                "open_warnings": sum(m["violations"]["warning"] for m in active),
                "failing_pipelines": sum(1 for m in active if m["pipeline"] == "failed"),
            },
            "active": active,
            "recent": recent,
            "flow": list(flow.values()),
            "events": self.store.events_after(0, 40),
        }

    def quality(self, days=30):
        since = time.time() - days * 86400
        rows = self.store.query("SELECT * FROM violations WHERE ts>=? AND scope='commit'", (since,))
        ai_rows = self.store.query("SELECT * FROM violations WHERE ts>=? AND scope='mr' AND source IN ('ai','convention')",
                                   (since,))
        all_rows = rows + ai_rows
        daily = {d: {"date": d, "error": 0, "warning": 0, "info": 0} for d in _days(days)}
        for r in all_rows:
            d = _day(r["ts"])
            if d in daily:
                daily[d][r["severity"]] = daily[d].get(r["severity"], 0) + 1

        def top(field, n=8, src=None):
            agg = {}
            for r in (src if src is not None else all_rows):
                k = r.get(field) or "-"
                a = agg.setdefault(k, {"name": k, "total": 0, "error": 0, "warning": 0, "info": 0})
                a["total"] += 1
                a[r["severity"]] = a.get(r["severity"], 0) + 1
            return sorted(agg.values(), key=lambda x: (-x["total"], -x["error"], x["name"]))[:n]

        checked = clean = commits = 0
        for row in self.store.query("SELECT v FROM kv WHERE k LIKE 'cq_commit:%'"):
            n, _, ts = str(row["v"]).partition("|")  # "<violations>|<checked_ts>"
            if not ts or float(ts) < since or n == "skip":
                continue
            checked += 1
            if n == "0":
                clean += 1
            else:
                commits += 1
        active_keys = [r["key"] for r in self.store.all_mrs(500) if r.get("status") in ACTIVE]
        open_v = []
        for k in active_keys:
            mr = self.store.get(k)
            for v in self.store.mr_violations(k):
                open_v.append({"mr": f"!{mr['iid']}", "mr_title": mr.get("title"), "url": mr.get("web_url"),
                               "severity": v["severity"], "rule": v["rule"], "path": v.get("path"),
                               "line": v.get("line"), "message": v.get("message"), "source": v.get("source")})
        open_v.sort(key=lambda v: {"error": 0, "warning": 1}.get(v["severity"], 2))
        return {
            "enabled": bool(self.cfg["code_quality"]["enabled"]),
            "days": days,
            "kpi": {"total": len(all_rows),
                    "error": sum(1 for r in all_rows if r["severity"] == "error"),
                    "warning": sum(1 for r in all_rows if r["severity"] == "warning"),
                    "commits_checked": checked, "commits_clean": clean,
                    "commits_with_issues": commits},
            "daily": list(daily.values()),
            "top_rules": top("rule"),
            "by_author": top("author", 12, rows),
            "top_files": top("path", 10),
            "open": open_v[:200],
        }

    def mr_detail(self, key):
        r = self.store.get(key)
        if not r:
            return None
        view = self._mr_view(r, self._viol_counts())
        view["review"] = r.get("review")
        view["violations_list"] = self.store.mr_violations(key)
        view["events"] = self.store.query("SELECT * FROM events WHERE mr_key=? ORDER BY id DESC LIMIT 20", (key,))
        return view


# ============================================================== standards
class StandardsFiles:
    def __init__(self, cfg):
        self.cfg = cfg
        self.dir = cfg["code_quality"]["standards_dir"]

    def list(self):
        cq = self.cfg["code_quality"]
        files = []
        if os.path.isdir(self.dir):
            for name in sorted(os.listdir(self.dir)):
                p = os.path.join(self.dir, name)
                if os.path.isfile(p) and _FILE_RE.match(name):
                    with open(p, encoding="utf-8") as f:
                        files.append({"name": name, "content": f.read(), "mtime": os.path.getmtime(p),
                                      "role": self._role(name)})
        stacks = [{"id": k, "name": v.get("name"), "paths": v.get("paths"), "document": v.get("document")}
                  for k, v in (cq.get("stacks") or {}).items()]
        std = Standards(cq)
        return {"enabled": bool(cq["enabled"]), "dir": self.dir, "files": files, "stacks": stacks,
                "rules_count": len(std.rules), "rule_errors": std.rule_errors,
                "conventions": cq.get("conventions"), "report": cq.get("report"),
                "ai_check": cq.get("ai_check")}

    def _role(self, name):
        cq = self.cfg["code_quality"]
        if name == cq.get("rules_file"):
            return "Aturan otomatis"
        if name == cq.get("general_document"):
            return "Standar umum"
        if name == (cq.get("conventions") or {}).get("pr_checklist"):
            return "PR checklist"
        for v in (cq.get("stacks") or {}).values():
            if v.get("document") == name:
                return f"Standar {v.get('name')}"
        return "Dokumen"

    def save(self, name, content):
        if not _FILE_RE.match(name or ""):
            return 400, {"error": "Nama file harus berakhiran .md atau .yaml, tanpa folder."}
        if not isinstance(content, str):
            return 400, {"error": "Isi file harus teks."}
        if len(content) > 1_000_000:
            return 400, {"error": "Isi file terlalu besar (maks 1 MB)."}
        if name.endswith((".yaml", ".yml")):
            errs = validate_rules_yaml(content)
            if errs:
                return 400, {"error": "Aturan tidak disimpan.", "details": errs}
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                old = f.read()
            with open(path + ".bak", "w", encoding="utf-8") as f:
                f.write(old)
        mode = (os.stat(path).st_mode & 0o777) if os.path.exists(path) else 0o644
        fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            os.chmod(tmp, mode)  # mkstemp makes 0600: keep the file readable/editable from the host
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return 200, {"ok": True, "name": name, "mtime": os.path.getmtime(path)}

    def test(self, path, code):
        std = Standards(self.cfg["code_quality"])
        lines = (code or "").splitlines()
        diff = f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + ln for ln in lines)
        vs = std.check_file_diff(path or "main.go", diff)
        return {"stack": std.stack_of(path or ""), "stack_name": std.stack_name(std.stack_of(path or "")),
                "violations": vs, "rule_errors": std.rule_errors}


def validate_rules_yaml(content):
    try:
        data = yaml.safe_load(content) or {}
    except yaml.YAMLError as ex:
        return [f"YAML tidak valid: {ex}"]
    if not isinstance(data, dict) or not isinstance(data.get("rules", []), list):
        return ["Format harus: rules: [ ... ]"]
    errs, ids = [], set()
    for i, r in enumerate(data.get("rules") or []):
        label = f"rule #{i + 1} ({(r or {}).get('id', '?')})"
        if not isinstance(r, dict):
            errs.append(f"{label}: harus berupa objek")
            continue
        if not r.get("id"):
            errs.append(f"{label}: id wajib diisi")
        elif r["id"] in ids:
            errs.append(f"{label}: id duplikat")
        ids.add(r.get("id"))
        try:
            re.compile(r.get("pattern") or "")
            if not r.get("pattern"):
                errs.append(f"{label}: pattern wajib diisi")
        except re.error as ex:
            errs.append(f"{label}: regex tidak valid ({ex})")
        if str(r.get("severity", "warning")).lower() not in ("error", "warning", "info"):
            errs.append(f"{label}: severity harus error | warning | info")
    return errs


# ================================================================ server
class Dashboard:
    def __init__(self, cfg, store, ai=None):
        from .ai import AIManager
        d = cfg["dashboard"]
        self.ai = ai or AIManager(cfg, store)
        self.cfg = cfg
        self.host, self.port = d.get("host", "127.0.0.1"), int(d.get("port", 8787))
        self.password = str(d.get("password") or "")
        self.env_path = os.path.join(cfg.get("_base_dir", "."), ".env")
        if self.host not in ("127.0.0.1", "localhost", "::1") and not self.password:
            raise ValueError("dashboard.password wajib diisi jika dashboard dibuka ke jaringan "
                             f"(host {self.host}).")
        self.secret = hashlib.sha256((self.password + (cfg["gitlab"].get("token") or "") + "mrpilot").encode()).digest()
        self.data = DashboardData(cfg, store)
        self.files = StandardsFiles(cfg)
        self.store = store
        self.httpd = None
        self.failed = {}            # ip -> [timestamps of failed logins]
        self.fail_lock = threading.Lock()

    LOCK_MAX, LOCK_WINDOW = 10, 900  # 10 wrong passwords per 15 minutes per address

    def locked_out(self, ip):
        now = time.time()
        with self.fail_lock:
            hits = [t for t in self.failed.get(ip, []) if now - t < self.LOCK_WINDOW]
            self.failed[ip] = hits
            return len(hits) >= self.LOCK_MAX

    def note_failure(self, ip):
        with self.fail_lock:
            self.failed.setdefault(ip, []).append(time.time())
            if len(self.failed) > 1000:  # bound memory
                self.failed = dict(list(self.failed.items())[-500:])

    def clear_failures(self, ip):
        with self.fail_lock:
            self.failed.pop(ip, None)

    # sessions: HMAC tokens "<expiry>.<id>.<sig>"; logout puts <id> on a revocation list (kept until expiry)
    def make_token(self, ttl=7 * 86400):
        head = f"{int(time.time() + ttl)}.{secrets.token_hex(8)}"
        return head + "." + hmac.new(self.secret, head.encode(), "sha256").hexdigest()

    def _parse_token(self, tok):
        try:
            exp, sid, sig = (tok or "").split(".")
            good = hmac.new(self.secret, f"{exp}.{sid}".encode(), "sha256").hexdigest()
            if hmac.compare_digest(sig, good) and int(exp) > time.time():
                return sid, int(exp)
        except ValueError:
            pass
        return None, 0

    def _revoked(self):
        try:
            data = json.loads(self.store.kv_get("revoked_sessions", "{}") or "{}")
        except ValueError:
            data = {}
        now = time.time()
        return {k: v for k, v in data.items() if v > now}  # drop expired entries

    def check_token(self, tok):
        if not self.password:
            return True
        sid, _ = self._parse_token(tok)
        return bool(sid) and sid not in self._revoked()

    def revoke_token(self, tok):
        sid, exp = self._parse_token(tok)
        if sid:
            with self.fail_lock:
                rev = self._revoked()
                rev[sid] = exp
                self.store.kv_set("revoked_sessions", json.dumps(rev))

    def serve(self):
        dash = self

        class Handler(_Handler):
            app = dash
        delay = 2
        while True:  # port still held (old instance shutting down, another app): keep trying, don't die silently
            try:
                self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
                break
            except OSError as ex:
                log.error("Dashboard tidak bisa memakai port %s (%s). Coba lagi %ss lagi; ubah DASHBOARD_PORT "
                          "kalau port dipakai aplikasi lain.", self.port, ex.strerror or ex, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)
        self.httpd.daemon_threads = True
        log.info("Dashboard: http://%s:%s", self.host, self.port)
        self.httpd.serve_forever()

    def start_background(self):
        t = threading.Thread(target=self.serve, name="dashboard", daemon=True)
        t.start()
        return t


class _Handler(BaseHTTPRequestHandler):
    app: Dashboard = None
    server_version = "MRPilot"

    def log_message(self, fmt, *args):
        log.debug("http %s", fmt % args)

    # ---------------------------------------------------------------- utils
    def _qint(self, q, name, default, lo, hi):
        try:
            v = int((q.get(name) or [str(default)])[0])
        except ValueError:
            v = default
        return max(lo, min(v, hi))

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False, default=str).encode() if "json" in ctype else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                             "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                             "font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; "
                             "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _cookie(self):
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return c["mrp_session"].value if "mrp_session" in c else ""

    def _authed(self):
        return self.app.check_token(self._cookie())

    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("Content-Length tidak valid") from None
        if n > 2_000_000:
            raise ValueError("terlalu besar")
        data = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("Body harus objek JSON")
        return data

    def _static(self, name, ctype):
        with open(os.path.join(WEB_DIR, name), "rb") as f:
            self._send(200, f.read(), ctype)

    # --------------------------------------------------------------- routes
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/login":
            return self._static("login.html", "text/html; charset=utf-8")
        if u.path == "/healthz":
            from . import __version__
            hb = float(self.app.store.kv_get("heartbeat", 0) or 0)
            age = round(time.time() - hb, 1) if hb else None
            alive = age is not None and age < int(os.environ.get("MRP_HEALTH_MAX_AGE", "600"))
            # 503 until the bot loop is really running (setup waits on this), 200 afterwards
            return self._send(200 if alive else 503, {"ok": alive, "version": __version__, "loop_age": age})
        if not self._authed():
            if u.path.startswith("/api/"):
                return self._send(401, {"error": "login"})
            return self._send(302, b"", headers={"Location": "/login"})
        try:
            if u.path in ("/", "/index.html"):
                return self._static("index.html", "text/html; charset=utf-8")
            if u.path == "/api/summary":
                return self._send(200, self.app.data.summary())
            if u.path == "/api/quality":
                return self._send(200, self.app.data.quality(self._qint(q, "days", 30, 1, 365)))
            if u.path == "/api/mr":
                d = self.app.data.mr_detail((q.get("key") or [""])[0])
                return self._send(200 if d else 404, d or {"error": "tidak ditemukan"})
            if u.path == "/api/events":
                return self._send(200, self.app.store.events_after(self._qint(q, "after", 0, 0, 2 ** 62),
                                                                   self._qint(q, "limit", 200, 1, 1000)))
            if u.path == "/api/standards":
                return self._send(200, self.app.files.list())
            if u.path == "/api/ai":
                return self._send(200, {**self.app.ai.describe(), "stats": self.app.store.ai_stats(7)})
            if u.path == "/api/stream":
                return self._stream(self._qint(q, "after", 0, 0, 2 ** 62))
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as ex:
            log.exception("dashboard GET %s", u.path)
            return self._send(500, {"error": redact(str(ex))[:300]})
        self._send(404, {"error": "not found"})

    def _same_origin(self):
        """Browsers send Origin on POST; refuse cross-site form posts (logout CSRF)."""
        origin = self.headers.get("Origin")
        return not origin or urlparse(origin).netloc == (self.headers.get("Host") or "")

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/logout":
            if not self._same_origin():
                return self._send(403, {"error": "forbidden"})
            self.app.revoke_token(self._cookie())
            return self._send(303, b"", headers={
                "Location": "/login?e=3",
                "Set-Cookie": "mrp_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
        if u.path == "/login":
            ip = self.client_address[0]
            if self.app.locked_out(ip):
                log.warning("Login dashboard diblokir sementara untuk %s (terlalu banyak percobaan)", ip)
                return self._send(302, b"", headers={"Location": "/login?e=2"})
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 8192)
            except ValueError:
                n = 0
            form = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            pw = (form.get("password") or [""])[0]
            if self.app.password and hmac.compare_digest(pw.encode(), self.app.password.encode()):
                self.app.clear_failures(ip)
                tok = self.app.make_token()
                return self._send(302, b"", headers={
                    "Location": "/",
                    "Set-Cookie": f"mrp_session={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age={7 * 86400}"})
            hint = ""
            if pw.strip() == self.app.password:
                hint = " (beda spasi di awal/akhir)"
            elif len(pw) != len(self.app.password):
                hint = " (panjangnya berbeda)"
            self.app.note_failure(ip)
            log.warning("Login dashboard gagal%s. Password aktif = DASHBOARD_PASSWORD di %s saat MR Pilot "
                        "terakhir dinyalakan; cek dengan perintah `password`.", hint, self.app.env_path)
            time.sleep(1)
            return self._send(302, b"", headers={"Location": "/login?e=1"})
        return self._mutate("POST", u)

    def do_PUT(self):
        return self._mutate("PUT", urlparse(self.path))

    def _mutate(self, method, u):
        if not self._authed():
            return self._send(401, {"error": "login"})
        if self.headers.get("X-MRPilot") != "1":  # simple CSRF guard (custom header needs same-origin JS)
            return self._send(403, {"error": "forbidden"})
        try:
            body = self._json_body()
            m = re.fullmatch(r"/api/standards/([A-Za-z0-9_-][A-Za-z0-9_.-]*)", u.path)
            if method == "PUT" and m:
                name = m.group(1)
                code, res = self.app.files.save(name, body.get("content", ""))
                if code == 200:
                    self.app.store.add_event("standards", f"Standar {name} diperbarui", "lewat dashboard")
                return self._send(code, res)
            if method == "PUT" and u.path == "/api/ai":
                self.app.ai.save_overrides(body)
                self.app.store.add_event("ai", "Pengaturan AI diperbarui", "lewat dashboard")
                return self._send(200, {**self.app.ai.describe(), "stats": self.app.store.ai_stats(7)})
            if method == "POST" and u.path == "/api/ai/test":
                res = self.app.ai.test(body.get("name"))
                self.app.store.add_event("ai", f"Tes AI {body.get('name')}: {'berhasil' if res['ok'] else 'gagal'}",
                                         res.get("error") or f"{res.get('ms')} ms", level="success" if res["ok"] else "error")
                return self._send(200, res)
            if method == "POST" and u.path == "/api/ai/models":
                try:
                    return self._send(200, {"models": self.app.ai.list_models(body.get("name"))})
                except Exception as ex:
                    return self._send(200, {"models": [], "error": redact(ex)[:300]})
            if method == "POST" and u.path == "/api/standards/test":
                return self._send(200, self.app.files.test(body.get("path"), body.get("code")))
        except (ValueError, TypeError, KeyError) as ex:  # bad input from the form: answer 400, no traceback
            log.warning("dashboard %s %s ditolak: %s", method, u.path, redact(ex))
            return self._send(400, {"error": redact(ex)[:300]})
        except Exception as ex:
            log.exception("dashboard %s %s", method, u.path)
            return self._send(500, {"error": redact(ex)[:300]})
        self._send(404, {"error": "not found"})

    def _stream(self, after):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last = after or self.app.store.last_event_id()
        beat = time.time()
        try:
            self.wfile.write(f"event: hello\ndata: {json.dumps({'last': last})}\n\n".encode())
            self.wfile.flush()
            while True:
                evs = self.app.store.events_after(last, 50)
                for ev in evs:
                    last = ev["id"]
                    self.wfile.write(f"id: {ev['id']}\nevent: activity\ndata: "
                                     f"{json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                if evs:
                    self.wfile.flush()
                if time.time() - beat > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    beat = time.time()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return


def random_password():
    return secrets.token_urlsafe(12)
