"""Pipeline explanations, morning digest + reminders, and GitLab suggestions."""
import copy
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mr_pilot.config import DEFAULTS  # noqa: E402
from mr_pilot.digest import Digest, age_text  # noqa: E402
from mr_pilot.pipelines import PipelineWatcher, error_lines  # noqa: E402
from mr_pilot.store import Store  # noqa: E402
from mr_pilot.suggestions import comment_body, post_findings, postable  # noqa: E402

TRACE = ("\x1b[0KRunning with gitlab-runner 17.0\n$ go test ./...\nok   pkg/a 0.1s\n"
         "--- FAIL: TestQuota (0.01s)\n    quota_test.go:42: expected 5, got 4\nFAIL\n"
         "FAIL\tidas/quota\t0.02s\nexport TOKEN=glpat-abcdefghijklmnopqrstu\n"
         "\x1b[31;1mERROR: Job failed: exit code 1\x1b[0;m\n")


def cfg(**over):
    c = copy.deepcopy(DEFAULTS)
    for k, v in over.items():
        c[k].update(v)
    return c


class Sent(list):
    def __call__(self, text, **kw):
        self.append((text, kw))
        return len(self)


# --------------------------------------------------------------------- pipeline
def test_error_lines_strip_ansi_dedupe_and_redact():
    lines = error_lines(TRACE)
    assert "--- FAIL: TestQuota (0.01s)" in lines and "ERROR: Job failed: exit code 1" in lines
    assert not any("\x1b" in x for x in lines) and not any("glpat-abc" in x for x in lines)
    assert not any(x.startswith("Running with gitlab-runner") for x in lines)


class GL:
    def __init__(self):
        self.pipes = [{"id": 11, "status": "failed", "sha": "a"}]
        self.notes = []

    def mr_pipelines(self, pid, iid):
        return self.pipes

    def failed_jobs(self, pid, pipeline_id):
        return [{"id": 5, "name": "test", "stage": "test", "web_url": "http://gl/jobs/5"},
                {"id": 6, "name": "lint", "stage": "test", "allow_failure": True, "web_url": "http://gl/jobs/6"}]

    def job_trace(self, pid, job_id, tail=20000):
        return TRACE

    def add_note(self, pid, iid, body):
        self.notes.append(body)


class AI:
    def __init__(self, ready=True):
        self.ready, self.calls = ready, []

    def available(self, task):
        return ["x"] if self.ready else []

    def complete(self, system, user, task, validate=None):
        self.calls.append((system, user, task))
        return json.dumps({"category": "test", "cause": "TestQuota gagal: hasil kuota 4, seharusnya 5.",
                           "where": "quota_test.go:42", "fix": "Periksa perhitungan sisa kuota.",
                           "code_problem": True}), "fake"


def _watcher(tmp_path, ai=None):
    store = Store(str(tmp_path / "db"))
    store.upsert("7:1", project_id=7, iid=1, sha="a", status="notified", title="feat: kuota",
                 web_url="http://gl/mr/1")
    sent, events = Sent(), []
    w = PipelineWatcher(cfg(), GL(), store, ai, sent, lambda *a, **k: events.append(a))
    return w, store, sent


def test_failed_pipeline_explained_once_with_ai(tmp_path):
    ai = AI()
    w, store, sent = _watcher(tmp_path, ai)
    w.check_all()
    assert len(sent) == 1
    text = sent[0][0]
    assert "Pipeline gagal" in text and "TestQuota gagal" in text and "quota_test.go:42" in text
    assert "Test gagal" in text and "--- FAIL: TestQuota" in text and "glpat-abc" not in text
    assert ai.calls[0][2] == "pipeline" and "glpat-abc" not in ai.calls[0][1]
    assert "lint" not in ai.calls[0][1].split("Job gagal:")[1].split("\n")[0]  # allow_failure job skipped
    note = w.gl.notes[0]
    assert "gagal di job `test`" in note and "Periksa perhitungan" in note and "quota_test.go:42" in note
    assert "**" not in note and "<sub>" not in note and "otomatis" not in note.lower(), "komentar natural"
    w.check_all()
    w.check_all()
    assert len(sent) == 1 and len(w.gl.notes) == 1, "satu pipeline dijelaskan sekali saja"
    assert store.get("7:1")["pipeline"] == "failed"


def test_failed_pipeline_without_ai_shows_error_lines(tmp_path):
    w, _, sent = _watcher(tmp_path, AI(ready=False))
    w.check_all()
    text = sent[0][0]
    assert "Baris error di log" in text and "expected 5, got 4" in text and "Penyebab" not in text
    assert not w.gl.notes, "tanpa analisis tidak ada komentar MR"


def test_green_after_waiting_or_after_red(tmp_path):
    w, store, sent = _watcher(tmp_path, AI())
    w.check_all()
    w.gl.pipes = [{"id": 12, "status": "running"}]
    w.check_all()
    w.gl.pipes = [{"id": 12, "status": "success"}]
    assert w.check(store.get("7:1")) is None  # red -> running -> green: last state was running
    store.kv_set("wait_green:7:1", 1)  # user tapped Merge while running
    w.gl.pipes = [{"id": 13, "status": "success"}]
    assert w.check(store.get("7:1")) == "green"
    assert "hijau" in sent[-1][0] and any("Merge" in b[0] for row in sent[-1][1]["buttons"] for b in row)
    assert store.kv_get("wait_green:7:1") is None


# ------------------------------------------------------------------------ digest
def _store_with_waiting(tmp_path, now):
    s = Store(str(tmp_path / "db"))
    s.upsert("7:1", project_id=7, iid=1, sha="a", status="notified", title="feat: kuota", author="Dewi",
             web_url="http://gl/1", notified_at=now - 30 * 3600, verdict="REQUEST_CHANGES", pipeline="failed")
    s.upsert("7:2", project_id=7, iid=2, sha="b", status="notified", title="fix: tanda tangan", author="Bima",
             web_url="http://gl/2", notified_at=now - 2 * 3600, verdict="APPROVE", pipeline="success")
    s.upsert("7:3", project_id=7, iid=3, sha="c", status="merged", title="x", decided_at=now - 3600)
    s.upsert("7:4", project_id=7, iid=4, sha="d", status="waiting_bot", title="y")
    return s


def _at(y, mo, d, h, mi):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


def test_digest_content_and_once_per_workday(tmp_path):
    now = _at(2026, 10, 14, 8, 31)  # Wednesday
    s = _store_with_waiting(tmp_path, now)
    sent = Sent()
    d = Digest(cfg(reminders={"enabled": False}), s, sent)
    assert d.tick(_at(2026, 10, 14, 8, 0)) is None  # before 08:30
    text = d.tick(now)
    assert text and "Rabu 14 Okt" in text and "Menunggu keputusan Anda: 2" in text
    assert text.index("!1") < text.index("!2"), "yang paling lama menunggu di atas"
    assert "30 jam" in text and "Pipeline merah: 1" in text and "bot review: 1" in text and "1 merged" in text
    assert d.tick(now + 3600) is None, "sekali sehari"
    assert d.tick(_at(2026, 10, 17, 9, 0)) is None, "Sabtu tidak dikirim"
    assert d.tick(_at(2026, 10, 19, 9, 0)), "Senin dikirim lagi"


def test_digest_empty_queue():
    import tempfile
    s = Store(os.path.join(tempfile.mkdtemp(), "db"))
    assert "Tidak ada MR yang menunggu" in Digest(cfg(), s, Sent()).build()


def test_reminders_after_hours_repeat_and_quiet_hours(tmp_path):
    now = _at(2026, 10, 14, 10, 0)
    s = _store_with_waiting(tmp_path, now)
    sent = Sent()
    d = Digest(cfg(digest={"enabled": False}), s, sent)
    assert d.remind(now) == [1], "hanya yang menunggu > 24 jam"
    assert "menunggu keputusan Anda <b>30 jam</b>" in sent[0][0] and sent[0][1]["buttons"]
    assert d.remind(now + 3600) == [], "tidak diulang sebelum repeat_hours"
    assert d.remind(now + 25 * 3600) == [1, 2]
    assert d.remind(_at(2026, 10, 14, 22, 0) + 3 * 86400) == [], "di luar jam kerja diam"
    assert age_text(50 * 3600) == "2 hari" and age_text(90) == "1 mnt"


# ------------------------------------------------------------------- suggestions
FINDINGS = {"findings": [
    {"severity": "major", "file": "a.go", "line": 3, "title": "SQL injection", "detail": "id digabung",
     "suggestion": "Pakai placeholder", "evidence": 'q := "SELECT * FROM t WHERE id=" + id',
     "fix_code": '\tq := "SELECT * FROM t WHERE id = ?"', "verified": True},
    {"severity": "minor", "file": "a.go", "line": 99, "title": "Di luar diff", "evidence": "x", "fix_code": "y",
     "verified": True},
    {"severity": "major", "file": "a.go", "line": 2, "title": "Halusinasi", "evidence": "nope", "verified": False},
    {"severity": "major", "file": "", "line": None, "title": "Tanpa lokasi"},
]}


def test_comment_body_has_apply_block():
    f = FINDINGS["findings"][0]
    body = comment_body(f, inline=True)
    assert "```suggestion:-0+0\n\tq := \"SELECT * FROM t WHERE id = ?\"\n```" in body
    assert "id digabung" in body and "Pakai placeholder" in body
    assert "🟠" not in body and "Major" not in body and "<sub>" not in body and "Saran review" not in body
    assert "suggestion:" not in comment_body(f, inline=False) and "`a.go:3`" in comment_body(f, inline=False)
    assert comment_body({**f, "fix_code": "a ``` b"}, True).count("````") == 2  # fence can't break
    assert "suggestion" not in comment_body({**f, "fix_code": f["evidence"]}, True), "tidak ada perubahan"
    assert [x["title"] for x in postable(FINDINGS)] == ["SQL injection", "Di luar diff"]


class DiscGL:
    def __init__(self):
        self.disc = []

    def mr_discussion(self, pid, iid, body, position=None):
        if position and position["new_line"] not in (1, 2, 3):
            raise requests.HTTPError("400 line_code can't be blank")
        self.disc.append((body, position))


def test_post_findings_inline_fallback_and_once(tmp_path):
    store = Store(str(tmp_path / "db"))
    gl = DiscGL()
    mr = {"project_id": 7, "iid": 1, "sha": "a", "diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "a"}}
    assert post_findings(gl, store, mr, FINDINGS) == (2, 1, 0)
    inline, outside = gl.disc
    assert inline[1]["new_line"] == 3 and "```suggestion" in inline[0]
    assert outside[1] is None and "`a.go:99`" in outside[0] and "suggestion:" not in outside[0]
    assert post_findings(gl, store, mr, FINDINGS) == (0, 0, 2) and len(gl.disc) == 2
    assert post_findings(gl, store, {**mr, "sha": "new"}, FINDINGS)[0] == 2, "commit baru = saran baru"
