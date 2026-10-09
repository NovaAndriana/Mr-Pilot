"""Code Quality + dashboard tests. Run: python -m pytest -q"""
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mr_pilot.config import DEFAULTS, _merge  # noqa: E402
from mr_pilot.dashboard import Dashboard, validate_rules_yaml  # noqa: E402
from mr_pilot.quality import CodeQuality  # noqa: E402
from mr_pilot.standards import Standards, added_lines  # noqa: E402
from mr_pilot.store import Store  # noqa: E402

GO_DIFF = """@@ -10,3 +10,6 @@ func Get(id string) {
 	ctx := context.Background()
-	old()
+	data, _ := repo.Find(id)
+	fmt.Println(data)
+	q := "SELECT * FROM t WHERE id=" + id
 	return
"""
TS_DIFF = """@@ -1,2 +1,3 @@
 import x from "y";
+const a: any = 1; console.log(a);
 export default x;
"""


def std_cfg(tmp=None):
    cfg = _merge(DEFAULTS, {"code_quality": {"enabled": True}})
    d = tmp or os.path.join(ROOT, "standards")
    cfg["code_quality"]["standards_dir"] = d
    return cfg


def test_added_lines_numbers():
    assert list(added_lines(GO_DIFF)) == [(11, "\tdata, _ := repo.Find(id)"), (12, "\tfmt.Println(data)"),
                                          (13, '\tq := "SELECT * FROM t WHERE id=" + id')]


def test_rules_go_and_ts():
    std = Standards(std_cfg()["code_quality"])
    assert not std.rule_errors, std.rule_errors
    assert std.stack_of("internal/a/b.go") == "go" and std.stack_of("main.go") == "go"
    assert std.stack_of("mobile/src/App.tsx") == "react-native"
    assert std.stack_of("web/src/App.tsx") == "react"
    go = {(v["rule"], v["line"]) for v in std.check_file_diff("internal/a/b.go", GO_DIFF)}
    assert ("go-ignored-error", 11) in go and ("go-no-fmt-print", 12) in go and ("go-sql-concat", 13) in go
    assert not std.check_file_diff("internal/a/b_test.go", GO_DIFF.replace("SELECT", "x"))  \
        or all(v["rule"] not in ("go-no-fmt-print", "go-ignored-error") for v in
               std.check_file_diff("internal/a/b_test.go", GO_DIFF))
    ts = {v["rule"] for v in std.check_file_diff("web/src/a.ts", TS_DIFF)}
    assert {"ts-no-any", "ts-no-console"} <= ts


def test_secret_and_todo_rules():
    std = Standards(std_cfg()["code_quality"])
    d = ('@@ -0,0 +1,5 @@\n+password := "SuperSecret123"\n+// TODO cek\n+// TODO(IDAS-12): ok\n'
         '+  "api_key": "abcd1234efgh"\n+pwd := os.Getenv("DB_PASSWORD")\n')
    got = [(v["rule"], v["line"]) for v in std.check_file_diff("x.go", d)]
    assert ("no-hardcoded-secret", 1) in got and ("todo-without-ticket", 2) in got
    assert ("no-hardcoded-secret", 4) in got and ("no-hardcoded-secret", 5) not in got
    assert ("todo-without-ticket", 3) not in got


def test_conventions_and_checklist():
    std = Standards(std_cfg()["code_quality"])
    assert not std.check_title("feat(IDAS-5323): show per user quota usage")
    assert std.check_title("Update signing screen")[0]["rule"] == "mr-title-convention"
    assert std.check_commit_message("update stuff")[0]["severity"] == "info"
    assert not std.check_commit_message("Merge branch 'staging' into feat/x")
    assert std.check_checklist("no checklist")[0]["rule"] == "pr-checklist-missing"
    desc = "## Pull Request Checklist\n- [x] Code follows coding standard\n- [ ] Unit test added/updated\n"
    vs = std.check_checklist(desc)
    assert [v["rule"] for v in vs] == ["pr-checklist-unchecked"] and "Unit test added/updated" in vs[0]["message"]
    # match_template: also every item of standards/pr-checklist.md must be present
    std.cfg["conventions"]["require_pr_checklist"] = "match_template"
    rules = {v["rule"] for v in std.check_checklist(desc)}
    assert "pr-checklist-unchecked" in rules and "pr-checklist-incomplete" in rules


# MR !131 (AkuSign Mobile 2.0): the team's own template, fully ticked -> no checklist noise
MR131 = """## Linked Ticket
IDAS-5327
## What does this MR do?
Fix dynamic role is only for akusign_enterprise
## Checklist
- [x] Ticket ID is linked above
- [x] Branch follows the `TICKET-ID/purpose` naming pattern
- [x] MR is within size limits (≤ 25 files, ≤ 2,500 lines)
- [x] Commits follow Conventional Commits format
"""


def test_team_checklist_fully_ticked_is_clean():
    std = Standards(std_cfg()["code_quality"])
    assert std.check_checklist(MR131) == []
    assert [v["rule"] for v in std.check_checklist(MR131.replace("- [x] MR is", "- [ ] MR is"))] == \
        ["pr-checklist-unchecked"]


def test_title_warning_suggests_title_from_branch():
    std = Standards(std_cfg()["code_quality"])
    v = std.check_title("Idas 5327/fix role signature", "IDAS-5327/fix-role-signature")[0]
    assert "Saran: `fix(IDAS-5327): role signature`" in v["message"] and "^(" not in v["message"]
    assert "Saran" not in std.check_title("Update", "main")[0]["message"]


def test_validate_rules_yaml():
    assert validate_rules_yaml(open(os.path.join(ROOT, "standards", "rules.yaml"), encoding="utf-8").read()) == []
    errs = validate_rules_yaml("rules:\n  - id: a\n    pattern: '('\n  - id: a\n    pattern: x\n    severity: fatal\n")
    assert any("regex" in e for e in errs) and any("duplikat" in e for e in errs) and any("severity" in e for e in errs)
    assert validate_rules_yaml("rules: [")[0].startswith("YAML")


class FakeResp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


class QGL:
    def __init__(self):
        self.commit_comments, self.notes, self.edits, self.statuses, self.discussions = [], [], [], [], []

    def get_commits(self, pid, iid):
        return [{"id": "c1" * 20, "message": "update stuff", "author_name": "Dewi", "parent_ids": ["p"]},
                {"id": "m1" * 20, "message": "Merge branch x", "author_name": "Dewi", "parent_ids": ["a", "b"]}]

    def get_commit_diff(self, pid, sha):
        return [{"new_path": "internal/a/b.go", "diff": GO_DIFF}]

    def get_diffs(self, pid, iid):
        return [{"new_path": "internal/a/b.go", "diff": GO_DIFF}, {"new_path": "go.sum", "diff": "+x"}]

    def commit_comment(self, pid, sha, note, path=None, line=None):
        self.commit_comments.append((sha, path, line, note))

    def add_note(self, pid, iid, body):
        self.notes.append(body)
        return {"id": 99}

    def edit_note(self, pid, iid, nid, body):
        self.edits.append((nid, body))

    def commit_status(self, pid, sha, state, name, description="", target_url=None, ref=None):
        self.statuses.append((sha, state, name, description))

    def mr_discussion(self, *a, **k):
        self.discussions.append(a)


def mr(sha="h1"):
    return {"project_id": 7, "iid": 381, "sha": sha, "title": "fix: reject workflow", "description": "",
            "references": {"full": "idas/be!381"}, "web_url": "https://x/381", "source_branch": "fix/x",
            "diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": sha}}


def test_code_quality_run_posts_on_commit_and_dedups():
    cfg = std_cfg()
    cfg["code_quality"]["ai_check"] = False
    gl, store = QGL(), Store(":memory:")
    cq = CodeQuality(cfg, gl, store)
    s = cq.run(mr())
    # 1 error (sql concat) + warnings: ignored error, fmt.Println; missing PR checklist is only info
    assert s["counts"]["error"] == 1 and s["counts"]["warning"] == 2 and s["counts"]["info"] >= 1
    # warnings posted ON the commit with file + line (info commit-message not posted: min severity warning)
    lines = sorted((c[1], c[2]) for c in gl.commit_comments)
    assert lines == [("internal/a/b.go", 11), ("internal/a/b.go", 12), ("internal/a/b.go", 13)]
    assert all(c[0] == "c1" * 20 for c in gl.commit_comments)  # merge commit skipped
    assert "go-sql-concat" in gl.notes[0] and gl.statuses[0][1] == "success" and "1 error" in gl.statuses[0][3]
    # second run: commit not re-commented, summary note edited instead of new
    cq.run(mr("h2"))
    assert len(gl.commit_comments) == 3 and len(gl.notes) == 1 and gl.edits[0][0] == "99"
    assert len(store.mr_violations("7:381")) == 4  # 3 rules + checklist
    assert store.query("SELECT COUNT(*) n FROM violations WHERE scope='commit'")[0]["n"] == 4


def test_status_fail_on_error():
    cfg = std_cfg()
    cfg["code_quality"]["ai_check"] = False
    cfg["code_quality"]["report"]["status_fail_on"] = "error"
    gl = QGL()
    CodeQuality(cfg, gl, Store(":memory:")).run(mr())
    assert gl.statuses[0][1] == "failed"


class FakeAI:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def available(self, task=None):
        return ["fake"]

    def complete(self, system, user, task="review", validate=None):
        self.calls.append(task)
        if validate:
            validate(self.reply)
        return self.reply, "fake"


def test_ai_violations_filtered_and_posted():
    cfg = std_cfg()
    ai = FakeAI(json.dumps({"violations": [
        {"rule": "Handler akses repo langsung", "severity": "warning", "file": "internal/a/b.go", "line": 11,
         "message": "Pindahkan ke usecase", "confidence": "high"},
        {"rule": "ragu", "severity": "error", "file": "internal/a/b.go", "line": 12, "message": "x", "confidence": "low"}]}))
    gl = QGL()
    s = CodeQuality(cfg, gl, Store(":memory:"), ai=ai).run(mr())
    assert ai.calls == ["standards"]
    ai = [v for v in s["top"] if v["source"] == "ai"]
    assert len(ai) == 1 and ai[0]["rule"] == "Handler akses repo langsung"
    assert len(gl.discussions) == 1


def test_app_card_and_merge_confirm_with_quality_errors():
    from test_flow import make_app
    app, gl, tg = make_app()
    app.cfg["code_quality"]["enabled"] = True
    app.cfg["code_quality"]["ai_check"] = False
    app.cfg["code_quality"]["standards_dir"] = os.path.join(ROOT, "standards")
    from mr_pilot.quality import CodeQuality as CQ
    q = QGL()
    gl.get_commit_diff, gl.commit_comment, gl.commit_status = q.get_commit_diff, q.commit_comment, q.commit_status
    gl.get_diffs, gl.edit_note = q.get_diffs, q.edit_note
    gl.add_note = q.add_note
    gl.get_commits = lambda p, i: [{"id": "c9" * 20, "message": "fix(IDAS-1): x", "author_name": "A", "parent_ids": ["p"]}]
    app.quality = CQ(app.cfg, gl, app.store)
    app.poll_gitlab()
    card = tg.sent[-1][1]
    assert "Standar kode" in card and "go-sql-concat" in card
    app.on_callback({"id": "c", "from": {"id": 42}, "data": "m|7|375", "message": {"message_id": 1}})
    assert not gl.merged and "Standar kode: ❌ 1 error" in tg.sent[-1][1]
    ev_types = {e["type"] for e in app.store.events_after(0, 100)}
    assert {"mr_new", "quality", "review"} <= ev_types


# ------------------------------------------------------------------ dashboard
def _serve(cfg, store):
    d = Dashboard(cfg, store)
    d.port = 0
    from http.server import ThreadingHTTPServer
    from mr_pilot.dashboard import _Handler

    class H(_Handler):
        app = d
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _req(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.build_opener(NoRedirect).open(r, timeout=5) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def test_dashboard_api_and_standards_editor():
    from mr_pilot.demo import seed
    tmp = tempfile.mkdtemp()
    shutil.copytree(os.path.join(ROOT, "standards"), os.path.join(tmp, "standards"))
    cfg = std_cfg(os.path.join(tmp, "standards"))
    store = Store(":memory:")
    seed(store)
    srv, base = _serve(cfg, store)
    try:
        code, body, _ = _req(base + "/api/summary")
        s = json.loads(body)
        assert code == 200 and s["kpi"]["waiting"] == 5 and len(s["flow"]) == 14
        q = json.loads(_req(base + "/api/quality?days=30")[1])
        assert q["kpi"]["total"] > 0 and q["kpi"]["commits_checked"] >= q["kpi"]["commits_with_issues"]
        code, body, _ = _req(base + "/", headers={})
        assert code == 200 and b"MR Pilot" in body
        # standards: CSRF header required, invalid yaml rejected, valid saved + event
        assert _req(base + "/api/standards/rules.yaml", "PUT", {"content": "rules: []"})[0] == 403
        h = {"X-MRPilot": "1"}
        code, body, _ = _req(base + "/api/standards/rules.yaml", "PUT", {"content": "rules:\n - id: a\n   pattern: '('"}, h)
        assert code == 400 and "regex" in body.decode()
        assert _req(base + "/api/standards/../x.md", "PUT", {"content": "x"}, h)[0] in (400, 404)
        new = "rules:\n  - id: no-dump\n    stacks: ['*']\n    severity: error\n    pattern: 'var_dump\\('\n    message: hapus var_dump\n"
        assert _req(base + "/api/standards/rules.yaml", "PUT", {"content": new}, h)[0] == 200
        assert os.path.exists(os.path.join(tmp, "standards", "rules.yaml.bak"))
        t = json.loads(_req(base + "/api/standards/test", "POST", {"path": "a.php", "code": "x\nvar_dump($a);"}, h)[1])
        assert [(v["rule"], v["line"]) for v in t["violations"]] == [("no-dump", 2)]
        assert any(e["type"] == "standards" for e in store.events_after(0, 200))
    finally:
        srv.shutdown()
        shutil.rmtree(tmp)


def test_dashboard_password_and_network_guard():
    cfg = std_cfg()
    cfg["dashboard"]["host"] = "0.0.0.0"
    try:
        Dashboard(cfg, Store(":memory:"))
        raise AssertionError("harus menolak tanpa password")
    except ValueError:
        pass
    cfg["dashboard"]["host"] = "127.0.0.1"
    cfg["dashboard"]["password"] = "rahasia"
    srv, base = _serve(cfg, Store(":memory:"))
    try:
        assert _req(base + "/api/summary")[0] == 401
        code, _, hdr = _req(base + "/")
        assert code == 302 and hdr["Location"] == "/login"
        r = urllib.request.Request(base + "/login", data=b"password=rahasia", method="POST")
        try:
            urllib.request.build_opener(NoRedirect).open(r, timeout=5)
        except urllib.error.HTTPError as e:
            cookie = e.headers["Set-Cookie"].split(";")[0]
        assert _req(base + "/api/summary", headers={"Cookie": cookie})[0] == 200
        assert _req(base + "/api/summary", headers={"Cookie": "mrp_session=123.abc"})[0] == 401
    finally:
        srv.shutdown()
