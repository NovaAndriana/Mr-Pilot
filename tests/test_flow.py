"""Run: python -m pytest -q  (atau: python tests/test_flow.py)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mr_pilot.app import App  # noqa: E402
from mr_pilot.config import DEFAULTS, _merge  # noqa: E402
from mr_pilot.formatting import build_messages  # noqa: E402
from mr_pilot.reviewer import build_diff_text, extract_json, normalize, parse_bot_review  # noqa: E402
from mr_pilot.store import Store  # noqa: E402
from mr_pilot.teams import build_context, render  # noqa: E402

BOT_BODY = """AI Code Review (Agentic Mode)
## REVIEW OTOMATIS — feat(IDAS-5323)
### 2. SECURITY
🔴 **BLOCKER AREA — Database Query Safety**
✓ **DITERIMA** — workflow_step.go adalah security fix yang penting
⚠️ **KECIL** — Line 1110 dalam `quota_deduction_detail_test.go`: expectation tidak konsisten
## KESIMPULAN
**STATUS: ✅ APPROVED**
MR ini refactor yang solid dengan security improvement penting. Highlights:
1. **Security fix kritikal**: Filter pada GetManyWorkflowStep mencegah unfiltered query
2. **Correctness**: Struktur data baru (per-user quota) consistent di semua layer
3. **Test coverage**: Comprehensive, termasuk edge cases
"""

DESC = """## Reasons for Change
Jira: IDAS-5323

Endpoint balance-deduction sebelumnya hanya menampilkan total kuota, tidak per user.
- Workflow tanpa fase bisa lolos sehingga query step tidak terfilter.

## Changes
### 1. Bentuk baru `quota` pada response balance-deduction (breaking untuk FE)
- quota berubah dari array menjadi object
### 2. Perbaikan workflow tanpa fase
- 422 wkfl_015
### Pengujian
- unit test
"""


def mr_fixture(sha="abc", state="opened", pipeline="success"):
    return {"id": 1, "iid": 375, "project_id": 7, "sha": sha, "state": state,
            "title": "feat(IDAS-5323): show per user quota usage in workflow balance deduction",
            "description": DESC, "created_at": "2026-10-06T04:20:00Z", "source_branch": "feat/IDAS-5323-quota",
            "target_branch": "staging", "author": {"name": "Rudi Nugraha", "username": "rudi"},
            "references": {"short": "!375", "full": "idas/idas-repo-be!375"},
            "web_url": "https://code.idas.id/idas/idas-repo-be/-/merge_requests/375",
            "head_pipeline": {"status": pipeline}, "changes_count": "26", "has_conflicts": False}


class FakeResp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


class FakeGL:
    def __init__(self):
        self.mr = mr_fixture()
        self.merged, self.approved, self.notes_posted = False, False, []

    def me(self):
        return {"username": "nova.andriana"}

    def list_review_mrs(self, u, watch=("reviewer", "assignee")):
        return [self.mr] if self.mr["state"] == "opened" else []

    def get_mr(self, pid, iid):
        return dict(self.mr)

    def get_commits(self, pid, iid):
        return [{"created_at": "2026-10-06T04:00:00Z"}]

    def get_notes(self, pid, iid):
        return [{"system": False, "body": BOT_BODY, "author": {"username": "system"},
                 "created_at": "2026-10-06T05:00:00Z"}]

    def get_diffs(self, pid, iid):
        return []

    def approve(self, pid, iid, sha=None):
        self.approved = True
        return FakeResp(201, {})

    def merge(self, pid, iid, **kw):
        self.merged = True
        self.mr["state"] = "merged"
        return FakeResp(200, {**self.mr, "state": "merged"})

    def add_note(self, pid, iid, body):
        self.notes_posted.append(body)


class FakeTG:
    def __init__(self):
        self.sent, self.edits, self.n = [], [], 100

    def send(self, text, buttons=None, force_reply=False, html=True):
        self.n += 1
        self.sent.append((self.n, text, buttons))
        return self.n

    def edit(self, mid, text, buttons=None):
        self.edits.append((mid, text))

    def answer(self, *a, **k):
        pass

    def get_updates(self, offset, timeout):
        return []


def make_app(teams_mode="telegram_copy"):
    cfg = _merge(DEFAULTS, {"gitlab": {"username": "nova.andriana"},
                            "telegram": {"chat_id": 42, "allowed_user_ids": [42]},
                            "teams": {"mode": teams_mode}})
    gl, tg = FakeGL(), FakeTG()
    return App(cfg, gl=gl, tg=tg, store=Store(":memory:")), gl, tg


def test_description_and_good_points():
    from mr_pilot.reviewer import describe_from_description
    solves, changes = describe_from_description(DESC)
    assert "per user" in solves and "Jira" not in solves
    assert len(changes) == 2 and changes[0].startswith("Bentuk baru")
    r = parse_bot_review(BOT_BODY)
    assert len(r["good_points"]) == 3 and r["good_points"][0].startswith("Security fix")
    assert "Highlights" not in r["summary"]


def test_card_has_detail():
    app, gl, tg = make_app()
    app.poll_gitlab()
    card = tg.sent[-1][1]
    for s in ("Rudi Nugraha (@rudi)", "Masalah yang diselesaikan", "Bentuk baru",
              "Yang sudah bagus", "Security fix", "Jira IDAS-5323"):
        assert s in card, s


def test_long_detail_split_into_two_messages():
    from mr_pilot.formatting import build_messages
    rv = {"source": "llm", "verdict": "APPROVE", "summary": "ok", "breaking_changes": [],
          "findings": [{"severity": "minor", "title": "t" * 190, "file": "a.go", "detail": "d" * 300}] * 8,
          "solves": "s" * 450, "changes": ["c" * 180] * 6, "good_points": ["g" * 200] * 4}
    detail, card = build_messages(mr_fixture(), rv, [])
    assert detail and "Detail MR" in detail and "Verdict" in card
    assert len(detail) <= 3950 and len(card) <= 3950


def test_parse_bot_review():
    r = parse_bot_review(BOT_BODY)
    assert r["verdict"] == "APPROVE"
    assert len(r["findings"]) == 1 and r["findings"][0]["severity"] == "minor"
    assert "1110" in r["findings"][0]["title"]
    assert "quota_deduction_detail_test.go" in r["findings"][0]["title"]
    assert "solid" in r["summary"]


AGENTIC_BOT = """## 🤖 AI Code Review (Agentic Mode)
Perfect. Now I have enough context to perform the review.
### ✅ Correctness & Logic
1. **ProductPrice.NetAmount()** — Perhitungan diskon benar.
### ⚠️ BLOCKER: Migration Timestamp Collision
**Masalah**: file migrasi lama dinamai ulang.
### 📝 Minor Findings (Non-Blocking)
1. **Line 50**: alias tidak diperlukan.
## 🚫 FINAL VERDICT
**REJECT** — Migration renaming menimbulkan risiko collision dan violate best practice.
Jika migration naming bisa diresolve, semua aspek lainnya adalah **PASS**.
"""


def test_parse_agentic_bot_final_verdict():
    r = parse_bot_review(AGENTIC_BOT)
    assert r["verdict"] == "REQUEST_CHANGES", "REJECT di FINAL VERDICT"
    assert r["summary"].startswith("Migration renaming")
    assert [f["severity"] for f in r["findings"]] == ["blocker"], "judul 'Minor Findings (Non-Blocking)' bukan temuan"
    ok = parse_bot_review(AGENTIC_BOT.replace("**REJECT**", "**APPROVE**").replace("### ⚠️ BLOCKER: Migration Timestamp Collision\n", ""))
    assert ok["verdict"] == "APPROVE"


def test_normalize_downgrades_approve_with_major():
    raw = '```json\n{"verdict":"approve","summary":"x","findings":[{"severity":"major","title":"t"}]}\n```'
    r = normalize(extract_json(raw), "llm")
    assert r["verdict"] == "NEEDS_ATTENTION"


def test_diff_ignore_and_limit():
    diffs = [{"new_path": "go.sum", "diff": "x"}, {"new_path": "a.go", "diff": "+a" * 10},
             {"new_path": "b.go", "diff": "+b" * 1000}]
    text, skipped, trunc = build_diff_text(diffs, ["go.sum"], 200)
    assert "a.go" in text and "go.sum" in skipped and trunc


def test_teams_template():
    ctx = build_context(mr_fixture())
    assert ctx["jira"] == "IDAS-5323" and ctx["author_first"] == "Rudi"
    assert render("MR {ref} ({jira}) by {author_first} {nope}", ctx) == "MR !375 (IDAS-5323) by Rudi"


def test_message_is_valid_length():
    long = {"source": "llm", "verdict": "APPROVE", "summary": "s" * 3000,
            "findings": [{"severity": "minor", "title": "t" * 500, "file": "", "detail": "d" * 300}] * 8,
            "breaking_changes": []}
    for part in build_messages(mr_fixture(), long, []):
        assert part is None or len(part) <= 4000


def test_full_flow_merge():
    app, gl, tg = make_app()
    app.poll_gitlab()
    assert len(tg.sent) == 1 and "IDAS-5323" in tg.sent[0][1] and "Approve" in tg.sent[0][1]
    app.poll_gitlab()  # same sha -> no duplicate
    assert len(tg.sent) == 1
    msg_id = tg.sent[0][0]
    app.on_callback({"id": "c1", "from": {"id": 42}, "data": "m|7|375", "message": {"message_id": msg_id}})
    assert gl.approved and gl.merged
    assert any("Merged" in t for _, t in tg.edits)
    assert any("Rudi" in t for _, t, _ in tg.sent[1:])  # Teams text for copy


def test_unauthorized_user_cannot_merge():
    app, gl, tg = make_app()
    app.poll_gitlab()
    app.on_callback({"id": "c1", "from": {"id": 999}, "data": "m|7|375", "message": {"message_id": 1}})
    assert not gl.merged


def test_pipeline_running_blocks_merge():
    app, gl, tg = make_app()
    gl.mr["head_pipeline"] = {"status": "running"}
    app.poll_gitlab()
    app.on_callback({"id": "c", "from": {"id": 42}, "data": "m|7|375", "message": {"message_id": 1}})
    assert not gl.merged and "running" in tg.sent[-1][1]


def test_new_commit_rereviews_instead_of_merge():
    app, gl, tg = make_app()
    app.poll_gitlab()
    gl.mr["sha"] = "def"
    gl.get_commits = lambda p, i: [{"created_at": "2026-10-06T04:00:00Z"}]
    app.on_callback({"id": "c", "from": {"id": 42}, "data": "m|7|375", "message": {"message_id": 1}})
    assert not gl.merged and "commit baru" in tg.sent[1][1]


def test_reject_posts_comment():
    app, gl, tg = make_app()
    app.poll_gitlab()
    app.on_callback({"id": "c", "from": {"id": 42}, "data": "x|7|375", "message": {"message_id": 1}})
    prompt_id = tg.sent[-1][0]
    app.on_message({"from": {"id": 42}, "text": "Tolong rapikan test line 1110 dulu ya",
                    "reply_to_message": {"message_id": prompt_id}})
    assert gl.notes_posted == ["Tolong rapikan test line 1110 dulu ya"]
    assert app.store.get("7:375")["status"] == "rejected"


def test_confirm_when_not_approved():
    app, gl, tg = make_app()
    gl.get_notes = lambda p, i: [{"system": False, "author": {"username": "system"},
                                  "created_at": "2026-10-06T05:00:00Z",
                                  "body": "AI Code Review\n🔴 BLOCKER: SQL injection di repo.go\nSTATUS: REQUEST CHANGES"}]
    app.poll_gitlab()
    app.on_callback({"id": "c", "from": {"id": 42}, "data": "m|7|375", "message": {"message_id": 1}})
    assert not gl.merged and "Yakin" in tg.sent[-1][1]
    app.on_callback({"id": "c", "from": {"id": 42}, "data": "mf|7|375", "message": {"message_id": 5}})
    assert gl.merged


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)


def test_telegram_thread_defers_heavy_actions_but_answers_light_ones():
    """While MR Pilot is busy reviewing, /status answers at once and a Merge tap is acknowledged, then runs
    from the main loop (one thread per MR)."""
    app, gl, tg = make_app()
    app.poll_gitlab()
    msg_id = tg.sent[0][0]
    app._threaded = True
    app._busy = "mereview !999"
    n = len(tg.sent)
    app.on_message({"from": {"id": 42}, "text": "/status"})
    assert len(tg.sent) == n + 1 and "Menunggu keputusan" in tg.sent[-1][1], "perintah ringan langsung dibalas"
    app.on_callback({"id": "c1", "from": {"id": 42}, "data": "m|7|375", "message": {"message_id": msg_id}})
    assert not gl.merged, "merge tidak jalan di thread Telegram"
    assert "sedang mereview" in tg.sent[-1][1], "pengguna diberi tahu bahwa perintah mengantre"
    app._busy = ""
    app._run_actions()
    assert gl.merged and gl.approved


def test_telegram_thread_receives_while_main_loop_is_busy():
    import threading
    import time
    app, gl, tg = make_app()
    got = threading.Event()
    calls = {"n": 0}

    def get_updates(offset, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"update_id": 5, "message": {"from": {"id": 42}, "text": "/start"}}]
        time.sleep(0.05)
        return []
    tg.get_updates = get_updates
    orig = tg.send
    tg.send = lambda *a, **k: (got.set(), orig(*a, **k))[1]
    app._threaded = True
    app._ensure_tg_thread()  # main loop never runs here (it would be busy): the thread alone must answer
    assert got.wait(3), "bot harus membalas walau loop utama sibuk"
    app.stop_requested = True
    assert app.store.kv_get("tg_last_ok")
