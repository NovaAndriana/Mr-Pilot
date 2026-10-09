"""Stateful fakes of GitLab, Telegram Bot API, an OpenAI-compatible AI, and the Teams flow.

They behave like the real services where it matters for MR Pilot (status codes, pagination,
Telegram HTML validation and 4096-char limit, long polling, 409/429), and support fault injection."""
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse


def _now_iso(offset=0):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + offset))


class _Base(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    fake = None

    def log_message(self, *a):
        pass

    def _raw(self):
        if getattr(self, "_raw_cache", None) is None or getattr(self, "_raw_for", None) is not self.requestline:
            n = int(self.headers.get("Content-Length") or 0)
            self._raw_cache, self._raw_for = (self.rfile.read(n) if n else b""), self.requestline
        return self._raw_cache

    def send_response(self, *a, **k):
        # keep-alive safety: a fault answered before reading the body would corrupt the next request
        self._raw()
        super().send_response(*a, **k)

    def _body(self):
        raw = self._raw()
        if not raw:
            return {}
        if "json" in (self.headers.get("Content-Type") or ""):
            return json.loads(raw)
        return {k: v[0] for k, v in parse_qs(raw.decode()).items()}

    def _json(self, code, body, headers=None):
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _drop(self):
        """Simulate a network failure: close without answering."""
        self.close_connection = True
        try:
            self.connection.shutdown(2)
        except OSError:
            pass


class Server:
    def __init__(self, handler_cls):
        cls = type(handler_cls.__name__, (handler_cls,), {"fake": self})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.lock = threading.RLock()
        self.faults = []  # [(method, path_regex, action, remaining)]
        self.log = []
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def fault(self, method, path_rx, action, times=1):
        """action: int status | 'drop' | ('sleep', seconds) | callable(handler)->bool(handled)"""
        with self.lock:
            self.faults.append([method, re.compile(path_rx), action, times])

    def take_fault(self, method, path):
        with self.lock:
            for f in self.faults:
                if f[0] in (method, "*") and f[1].search(path) and f[3] != 0:
                    f[3] -= 1
                    return f[2]
        return None

    def stop(self):
        self.httpd.shutdown()


# =================================================================== GitLab
class FakeGitLab(Server):
    TOKEN = "glpat-e2eTESTtoken123456"

    def __init__(self):
        self.user = {"id": 1, "username": "nova.andriana", "name": "Nova Andriana"}
        self.mrs = {}          # (pid, iid) -> mr dict
        self.commits = {}      # (pid, iid) -> [commit] newest first
        self.commit_diffs = {}  # sha -> [diff]
        self.mr_diffs = {}     # (pid, iid) -> [diff]
        self.notes = {}        # (pid, iid) -> [note]
        self.discussions = []
        self.commit_comments = []
        self.statuses = []
        self.approvals = []
        self.merges = []
        self.deleted_branches = []
        self.raw_requests = []
        self.note_seq = 1000
        self.merge_behaviour = {}  # (pid, iid) -> callable or None
        super().__init__(_GitLabHandler)

    def add_mr(self, iid, title, sha, author=("Dewi Lestari", "dewi.l"), pipeline="success", description="",
               files=None, commit_msg=None, pid=7, conflicts=False, draft=False, reviewers=("nova.andriana",),
               target="staging", assignees=(), delete_branch_checkbox=False):
        files = files or {"internal/x/usecase.go": "+func A() {}\n"}
        diffs = [{"new_path": p, "old_path": p, "new_file": False, "deleted_file": False, "renamed_file": False,
                  "diff": "@@ -1,1 +1,%d @@\n%s" % (body.count("\n") + 1, body)} for p, body in files.items()]
        mr = {"id": pid * 10000 + iid, "iid": iid, "project_id": pid, "title": title, "description": description,
              "state": "opened", "draft": draft, "work_in_progress": draft, "sha": sha,
              "author": {"name": author[0], "username": author[1]},
              "reviewers": [{"username": r} for r in reviewers],
              "assignees": [{"username": a} for a in assignees],
              "force_remove_source_branch": delete_branch_checkbox,
              "source_branch": f"feat/{iid}", "target_branch": target,
              "references": {"short": f"!{iid}", "full": f"idas/idas-repo-be!{iid}"},
              "web_url": f"{self.url}/idas/idas-repo-be/-/merge_requests/{iid}",
              "head_pipeline": {"status": pipeline} if pipeline else None,
              "has_conflicts": conflicts, "changes_count": str(len(files)),
              "created_at": _now_iso(-3600), "diff_refs": {"base_sha": "b" * 40, "start_sha": "s" * 40, "head_sha": sha}}
        with self.lock:
            self.mrs[(pid, iid)] = mr
            self.commits[(pid, iid)] = [{"id": sha, "message": commit_msg or title, "author_name": author[0],
                                         "parent_ids": ["p" * 40], "created_at": _now_iso(-1800)}]
            self.commit_diffs[sha] = diffs
            self.mr_diffs[(pid, iid)] = diffs
            self.notes.setdefault((pid, iid), [])
        return mr

    def push(self, iid, sha, files, msg="fix: update", pid=7):
        diffs = [{"new_path": p, "old_path": p, "diff": "@@ -1,1 +1,%d @@\n%s" % (b.count("\n") + 1, b)}
                 for p, b in files.items()]
        with self.lock:
            mr = self.mrs[(pid, iid)]
            mr["sha"] = sha
            mr["diff_refs"]["head_sha"] = sha
            self.commits[(pid, iid)].insert(0, {"id": sha, "message": msg, "author_name": mr["author"]["name"],
                                                "parent_ids": ["p" * 40], "created_at": _now_iso()})
            self.commit_diffs[sha] = diffs
            self.mr_diffs[(pid, iid)] = self.mr_diffs[(pid, iid)] + diffs

    def add_note(self, iid, body, username="system", pid=7, created=None, updated=None):
        with self.lock:
            self.note_seq += 1
            n = {"id": self.note_seq, "body": body, "system": False, "author": {"username": username},
                 "created_at": created or _now_iso(), "updated_at": updated or created or _now_iso()}
            self.notes.setdefault((pid, iid), []).insert(0, n)
            return n

    def notes_by_me(self, iid, pid=7):
        return [n for n in self.notes.get((pid, iid), []) if n["author"]["username"] == self.user["username"]]


class _GitLabHandler(_Base):
    def _route(self, method):
        f = self.fake
        u = urlparse(self.path)
        path, q = unquote(u.path), parse_qs(u.query)
        f.log.append((method, path))
        if self.headers.get("PRIVATE-TOKEN") != f.TOKEN:
            return self._json(401, {"message": "401 Unauthorized"})
        act = f.take_fault(method, path)
        if act == "drop":
            return self._drop()
        if isinstance(act, tuple) and act[0] == "sleep":
            time.sleep(act[1])
        elif isinstance(act, int):
            return self._json(act, {"message": f"{act} injected"})
        body = self._body() if method in ("POST", "PUT") else {}
        p = path[len("/api/v4"):]
        mm = re.match(r"^/projects/(\d+)/merge_requests/(\d+)/merge$", p)
        if mm and method == "PUT":  # custom merge behaviour runs outside the lock (it may sleep, like a slow GitLab)
            key = (int(mm.group(1)), int(mm.group(2)))
            beh, mr = f.merge_behaviour.get(key), f.mrs.get(key)
            if beh and mr:
                res = beh(self, mr, body)
                if res is not None:
                    return res
        with f.lock:
            if p == "/user":
                return self._json(200, f.user)
            if p == "/merge_requests":
                rev = (q.get("reviewer_username") or [None])[0]
                asg = (q.get("assignee_username") or [None])[0]
                rows = [m for m in f.mrs.values() if m["state"] == "opened"
                        and (rev is None or rev in [r["username"] for r in m["reviewers"]])
                        and (asg is None or asg in [a["username"] for a in m["assignees"]])]
                return self._paged(rows, q)
            m = re.match(r"^/projects/(\d+)/merge_requests/(\d+)(/.*)?$", p)
            if m:
                key, sub = (int(m.group(1)), int(m.group(2))), m.group(3) or ""
                mr = f.mrs.get(key)
                if not mr:
                    return self._json(404, {"message": "404 Not found"})
                if sub == "" and method == "GET":
                    return self._json(200, mr)
                if sub == "" and method == "PUT":  # update MR (e.g. remove_source_branch checkbox)
                    if "remove_source_branch" in body:
                        mr["force_remove_source_branch"] = bool(body["remove_source_branch"])
                    return self._json(200, mr)
                if sub == "/diffs":
                    return self._paged(f.mr_diffs[key], q)
                if sub == "/commits":
                    return self._paged(f.commits[key], q)
                if sub == "/notes" and method == "GET":
                    return self._json(200, f.notes[key])
                if sub == "/notes" and method == "POST":
                    f.note_seq += 1
                    n = {"id": f.note_seq, "body": body["body"], "system": False,
                         "author": {"username": f.user["username"]}, "created_at": _now_iso(), "updated_at": _now_iso()}
                    f.notes[key].insert(0, n)
                    return self._json(201, n)
                mm = re.match(r"^/notes/(\d+)$", sub)
                if mm and method == "PUT":
                    for n in f.notes[key]:
                        if n["id"] == int(mm.group(1)):
                            n["body"], n["updated_at"] = body["body"], _now_iso()
                            return self._json(200, n)
                    return self._json(404, {"message": "404 Note Not Found"})
                if sub == "/discussions" and method == "POST":
                    f.discussions.append((key, body))
                    return self._json(201, {"id": "d1", "notes": [{"body": body["body"]}]})
                if sub == "/approve" and method == "POST":
                    f.approvals.append((key, body.get("sha")))
                    return self._json(201, {"approved": True})
                if sub == "/merge" and method == "PUT":
                    if body.get("sha") and body["sha"] != mr["sha"]:
                        return self._json(409, {"message": "SHA does not match HEAD of source branch"})
                    if mr["has_conflicts"]:
                        return self._json(406, {"message": "Branch cannot be merged"})
                    if mr["state"] != "opened":
                        return self._json(405, {"message": "405 Method Not Allowed"})
                    mr["state"] = "merged"
                    f.merges.append((key, body))
                    # like GitLab: explicit param wins, otherwise the MR's own checkbox decides
                    if body.get("should_remove_source_branch", mr.get("force_remove_source_branch")):
                        f.deleted_branches.append(mr["source_branch"])
                    return self._json(200, dict(mr))
            m = re.match(r"^/projects/(\d+)/repository/files/(.+)/raw$", p)
            if m and method == "GET":
                f.raw_requests.append((m.group(2), (q.get("ref") or [""])[0]))
                for diffs in f.mr_diffs.values():
                    for d in diffs:
                        if d["new_path"] == m.group(2):
                            text = "".join(ln[1:] + "\n" for ln in d["diff"].splitlines()[1:] if ln[:1] in "+ ")
                            b = text.encode()
                            self.send_response(200)
                            self.send_header("Content-Type", "text/plain; charset=utf-8")
                            self.send_header("Content-Length", str(len(b)))
                            self.end_headers()
                            self.wfile.write(b)
                            return
                return self._json(404, {"message": "404 File Not Found"})
            m = re.match(r"^/projects/(\d+)/repository/commits/([0-9a-f]+)/(diff|comments)$", p)
            if m:
                if m.group(3) == "diff":
                    return self._paged(f.commit_diffs.get(m.group(2), []), q)
                f.commit_comments.append((m.group(2), body))
                return self._json(201, {"note": body.get("note")})
            m = re.match(r"^/projects/(\d+)/statuses/([0-9a-f]+)$", p)
            if m and method == "POST":
                f.statuses.append((m.group(2), body))
                return self._json(201, body)
        return self._json(404, {"message": "404 Not Found", "path": p})

    def _paged(self, rows, q):
        per = int((q.get("per_page") or ["20"])[0])
        page = int((q.get("page") or ["1"])[0])
        chunk = rows[(page - 1) * per: page * per]
        hdr = {"X-Next-Page": str(page + 1) if page * per < len(rows) else ""}
        return self._json(200, chunk, hdr)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")


# ================================================================= Telegram
_ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "tg-spoiler",
                 "blockquote", "span"}


def telegram_html_error(text):
    """Return Telegram-like error string if `text` isn't valid Telegram HTML, else None."""
    stack = []
    for m in re.finditer(r"<(/?)([a-zA-Z-]+)([^>]*)>|<|&(#?\w+;)?", text):
        if m.group(0) == "<":
            return "Bad Request: can't parse entities: Unexpected end tag or unsupported start tag"
        if m.group(0).startswith("&"):
            ent = m.group(4)
            if not ent or (not ent.startswith("#") and ent[:-1] not in ("lt", "gt", "amp", "quot", "apos")):
                return "Bad Request: can't parse entities: Unsupported entity"
            continue
        closing, tag = m.group(1), m.group(2).lower()
        if tag not in _ALLOWED_TAGS:
            return f"Bad Request: can't parse entities: Unsupported start tag \"{tag}\""
        if closing:
            if not stack or stack.pop() != tag:
                return "Bad Request: can't parse entities: Can't find end tag corresponding to start tag"
        else:
            stack.append(tag)
    if stack:
        return "Bad Request: can't parse entities: Can't find end tag corresponding to start tag"
    return None


class FakeTelegram(Server):
    TOKEN = "123456789:AAE2E-test-token-ABCDEFGHIJKLMNOPQ"
    CHAT = 4242

    def __init__(self):
        self.messages = {}      # id -> {text, markup, parse_mode, edits}
        self.sent = []          # ordered message ids
        self.answers = []       # (callback_id, text)
        self.updates = []
        self.update_seq = 100
        self.msg_seq = 1
        self.cv = threading.Condition()
        self.conflict = False
        self.reject_html_once = False
        self.rejected_html = []  # texts Telegram refused to parse (should stay empty)
        self.polls = 0
        super().__init__(_TelegramHandler)

    # --- helpers for tests -----------------------------------------------
    def _push(self, upd):
        with self.cv:
            self.update_seq += 1
            upd["update_id"] = self.update_seq
            self.updates.append(upd)
            self.cv.notify_all()

    def press(self, msg_id, data, user=CHAT):
        self._push({"callback_query": {"id": f"cb{self.update_seq + 1}", "from": {"id": user}, "data": data,
                                       "message": {"message_id": msg_id, "chat": {"id": self.CHAT}}}})

    def say(self, text, reply_to=None, user=CHAT):
        msg = {"message_id": 90000 + self.update_seq, "from": {"id": user}, "chat": {"id": self.CHAT, "type": "private"},
               "text": text}
        if reply_to:
            msg["reply_to_message"] = {"message_id": reply_to}
        self._push({"message": msg})

    def buttons(self, msg_id):
        mk = (self.messages[msg_id].get("markup") or {}).get("inline_keyboard") or []
        return {b["text"]: b.get("callback_data") or b.get("url") for row in mk for b in row}

    def find(self, needle, since=0):
        """Newest message id (sent after index `since`) whose current text contains needle."""
        with self.lock:
            for mid in reversed(self.sent[since:]):
                if needle in self.messages[mid]["text"]:
                    return mid
        return None

    def texts(self, since=0):
        with self.lock:
            return [self.messages[m]["text"] for m in self.sent[since:]]


class _TelegramHandler(_Base):
    def _route(self):
        f = self.fake
        m = re.match(r"^/bot([^/]+)/(\w+)$", urlparse(self.path).path)
        if not m:
            return self._json(404, {"ok": False, "error_code": 404, "description": "Not Found"})
        token, method = m.group(1), m.group(2)
        if token != f.TOKEN:
            return self._json(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
        f.log.append(method)
        act = f.take_fault(self.command, method)
        if act == "drop":
            return self._drop()
        if isinstance(act, tuple) and act[0] == "sleep":
            time.sleep(act[1])
        elif isinstance(act, tuple) and act[0] == 429:
            return self._json(429, {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after",
                                    "parameters": {"retry_after": act[1]}})
        elif isinstance(act, int):
            return self._json(act, {"ok": False, "error_code": act, "description": f"injected {act}"})
        p = self._body()
        if method == "getMe":
            return self._json(200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "mrpilot_e2e_bot"}})
        if method == "getUpdates":
            if f.conflict:
                return self._json(409, {"ok": False, "error_code": 409, "description":
                                        "Conflict: terminated by other getUpdates request"})
            f.polls += 1
            off, timeout = int(p.get("offset") or 0), min(int(p.get("timeout") or 0), 25)
            end = time.time() + timeout
            with f.cv:
                f.updates = [u for u in f.updates if u["update_id"] >= off]
                while not f.updates and time.time() < end:
                    f.cv.wait(0.2)
                res = list(f.updates)
            return self._json(200, {"ok": True, "result": res})
        if method in ("sendMessage", "editMessageText"):
            text = p.get("text") or ""
            if int(p.get("chat_id") or 0) != f.CHAT:
                return self._json(400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})
            if len(text) > 4096:
                return self._json(400, {"ok": False, "error_code": 400, "description": "Bad Request: message is too long"})
            if p.get("parse_mode") == "HTML":
                err = telegram_html_error(text)
                if not err and f.reject_html_once:
                    f.reject_html_once = False
                    err = "Bad Request: can't parse entities: injected"
                if err:
                    if "injected" not in err:
                        f.rejected_html.append((err, text))
                    return self._json(400, {"ok": False, "error_code": 400, "description": err})
            with f.lock:
                if method == "sendMessage":
                    mid = f.msg_seq = f.msg_seq + 1
                    f.messages[mid] = {"text": text, "markup": p.get("reply_markup"), "parse_mode": p.get("parse_mode"),
                                       "edits": 0}
                    f.sent.append(mid)
                    return self._json(200, {"ok": True, "result": {"message_id": mid}})
                mid = int(p.get("message_id") or 0)
                msg = f.messages.get(mid)
                if not msg:
                    return self._json(400, {"ok": False, "error_code": 400,
                                            "description": "Bad Request: message to edit not found"})
                if msg["text"] == text and msg["markup"] == p.get("reply_markup"):
                    return self._json(400, {"ok": False, "error_code": 400,
                                            "description": "Bad Request: message is not modified"})
                msg.update(text=text, markup=p.get("reply_markup"), edits=msg["edits"] + 1)
                return self._json(200, {"ok": True, "result": {"message_id": mid}})
        if method == "answerCallbackQuery":
            f.answers.append((p.get("callback_query_id"), p.get("text")))
            return self._json(200, {"ok": True, "result": True})
        return self._json(400, {"ok": False, "error_code": 400, "description": f"unknown method {method}"})

    def do_POST(self):
        self._route()

    def do_GET(self):
        self._route()


# ======================================================================= AI
class FakeAI(Server):
    """OpenAI-compatible /v1/chat/completions. `reply` = dict (serialised) | str | int (HTTP error)."""

    def __init__(self, reply=None):
        self.reply = reply
        self.calls = []
        super().__init__(_AIHandler)


class _AIHandler(_Base):
    def do_GET(self):
        if self.path.endswith("/models"):
            return self._json(200, {"data": [{"id": "fake-model"}]})
        self._json(404, {})

    def do_POST(self):
        f = self.fake
        p = self._body()
        f.calls.append(p)
        act = f.take_fault("POST", self.path)
        if act == "drop":
            return self._drop()
        r = f.reply(p) if callable(f.reply) else f.reply
        if isinstance(r, int):
            return self._json(r, {"error": {"message": f"injected {r}"}})
        content = r if isinstance(r, str) else json.dumps(r)
        self._json(200, {"choices": [{"message": {"role": "assistant", "content": content}}]})


# ==================================================================== Teams
class FakeTeams(Server):
    def __init__(self):
        self.posts = []
        super().__init__(_TeamsHandler)


class _TeamsHandler(_Base):
    def do_POST(self):
        f = self.fake
        act = f.take_fault("POST", self.path)
        if isinstance(act, int):
            return self._json(act, {})
        f.posts.append(self._body())
        self._json(202, {})
