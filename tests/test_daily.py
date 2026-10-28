"""Daily check: paste the Teams text, auto-correct, checks against yesterday and GitLab, Teams format back."""
import copy
import json
import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mr_pilot.config import DEFAULTS  # noqa: E402
from mr_pilot.daily import Daily, is_none_blocker, parse_date, parse_text, split_status, tidy  # noqa: E402
from mr_pilot.store import Store  # noqa: E402

# same shape as the lead's real Teams message (fictional names and tickets)
SAMPLE = """DAILY CHECK TEAM FE
Tanggal: 23 Oktober 2026
 
1. GROUP FE — MAS BUDI & MBAK SARI
 
A. PIC: Mbak Sari
 
1. ABC-101 — Implementasi UI Permohonan Akses Data
Link: https://jira.example/browse/ABC-101
 
- Progress: tinggal di push.
- Status: In Progress — Testing.
- Next Action: membuat Merge Request (MR) jika hasil pengujian sudah sesuai.
- Blocker: Belum ada blocker yang dilaporkan.
 
2. ABC-102 — Enhancement List Status
Link: https://jira.example/browse/ABC-102
 
- Progress: Implementasi selesai dan menunggu pengujian.
- Status: In Progres.
- Next Action: perlu pemyesuaian API.
- Blocker: bvelum ada.
 
B. PIC: Mas Budi
 
1. ABC-110 — Validasi Minimum Top Up
Link yang dicantumkan: https://jira.example/browse/ABC-199
 
- Progress: deploy.
- Status: In Progress.
- Next Action: Memulai implementasi setelah task prioritas selesai.
- Blocker: Bergantung pada penyelesaian task yang sedang berjalan.
 
2. ABC-111 — Bulk Download Mobile
 
- Progress: Belum dimulai.
- Status: Ready for QA.
- Next Action: Melakukan pengujian QA untuk memvalidasi expiry date OTP pada email dan SMS.
- Blocker: -
 
---
 
DAILY CHECK TEAM BE
Tanggal: 23 Oktober 2026
 
2. GROUP BE — MAS JOKO & MAS RUDI
Versi: V2
 
A. PIC: Mas Joko
 
1. ABC-200 — Expiry OTP
Link: https://jira.example/browse/ABC-200
 
Subtask 1: ABC-201 — Implement Expiry Validation
Link: https://jira.example/browse/ABC-201
 
- Progress: sudah selesai.
- Status: Ready for QA.
- Next Action: Melakukan pengujian QA untuk memvalidasi expiry date OTP pada email dan SMS.
- Blocker: Belum ada blocker yang dilaporkan.
 
B. PIC: Mas Rudi
 
1. Konfirmasi Logic Minimal Transaksi dengan Product
 
- Progress: Logic minimal transaksi di sisi BE sudah dibuat.
- Status: In Progress.
- Next Action: Mengonfirmasi ketentuan kepada tim Product.
- Blocker: Menunggu konfirmasi dari tim Product.
 
2. ABC-210 — Integrasi Mobile V2
 
- Progress: pengherjaan sudah samnpai 90% dan sambil test.
- Status: Code Review.
- Next Action: Direncanakan mulai besok, 23 Oktober 2026.
- Blocker: belum ada.
 
C. Planning & Analisis — eKYC
 
Scope Analisis:
- Analisis wrapper API.
- Analisis skema prepaid dan postpaid.
 
Monitoring Progress:
 
- Progress: Belum ada pembaruan progres terbaru.
- Status: Belum dikonfirmasi.
- Next Action: Melakukan analisis kebutuhan.
- Blocker: Belum ada informasi terkait blocker.
"""


def svc():
    cfg = copy.deepcopy(DEFAULTS)
    store = Store(":memory:")
    return Daily(cfg, store), store


def test_helpers():
    assert parse_date("23 Oktober 2026") == date(2026, 10, 23) and parse_date("1 Jan 2027") == date(2027, 1, 1)
    assert split_status("In Progres.", ["In Progress"]) == ("In Progress", "", True)
    assert split_status("Done — SIT", ["Done", "SIT"]) == ("Done", "SIT", False)
    assert split_status("Waiting Deploy", ["Open"])[0] == "Waiting Deploy", "status baru dibiarkan"
    for none in ("-", "Belum ada blocker yang dilaporkan.", "tidak ada", "Belum ada.", ""):
        assert is_none_blocker(none), none
    assert not is_none_blocker("Menunggu konfirmasi dari tim Product.")
    assert not is_none_blocker("Belum ada konfirmasi, menunggu tim Product.")


def test_parse_structure():
    p = parse_text(SAMPLE)
    assert p["day"] == date(2026, 10, 23) and not p["problems"]
    t = {x["ticket"] or x["title"]: x for x in p["tasks"]}
    assert t["ABC-101"]["team"] == "FE" and t["ABC-101"]["pic"] == "Mbak Sari"
    assert t["ABC-101"]["grp"] == "FE — MAS BUDI & MBAK SARI"
    assert t["ABC-201"]["parent"] == p["tasks"].index(t["ABC-200"]) and not t["ABC-200"]["fields"]
    assert t["Konfirmasi Logic Minimal Transaksi dengan Product"]["ticket"] is None
    eky = t["Planning & Analisis — eKYC"]
    assert eky["pic"] == "" and eky["scope"] == ["Analisis wrapper API", "Analisis skema prepaid dan postpaid"]
    assert eky["fields"]["status"].startswith("Belum dikonfirmasi")
    assert p["groups"]["BE — MAS JOKO & MAS RUDI"]["version"] == "V2"
    assert t["ABC-110"]["link"].endswith("ABC-199")


def test_tidy_corrections():
    p = parse_text(SAMPLE)
    corr = tidy(p)
    t = {x["ticket"] or x["title"]: x for x in p["tasks"]}
    assert t["ABC-102"]["fields"]["status"] == "In Progress"
    assert t["ABC-102"]["fields"]["next_action"] == "Perlu penyesuaian API."
    assert t["ABC-102"]["fields"]["blocker"] == "-"
    assert t["ABC-210"]["fields"]["progress"] == "Pengerjaan sudah sampai 90% dan sambil test."
    assert t["ABC-210"]["fields"]["pct"] == 90 and t["ABC-111"]["fields"]["pct"] == 0
    assert t["ABC-101"]["fields"]["status_note"] == "Testing" and t["ABC-101"]["fields"]["progress"] == "Tinggal di push."
    assert t["ABC-111"]["link"] == "https://jira.example/browse/ABC-111", "link dibuat dari tiket"
    whys = " ".join(c["why"] for c in corr)
    assert "pemyesuaian → penyesuaian" in whys and "nama status disamakan" in whys


def test_import_checks_and_teams_text():
    d, store = svc()
    rep = d.import_text(SAMPLE)
    assert rep["day"] == "2026-10-23" and rep["summary"]["tasks"] == 8
    tasks = {}

    def walk(t):
        tasks[t.get("ticket") or t["title"]] = t
        for s in t["subtasks"]:
            walk(s)
    for tm in rep["teams"]:
        for g in tm["groups"]:
            for b in g["blocks"]:
                for t in b["tasks"]:
                    walk(t)
    codes = lambda k: {w["code"] for w in tasks[k]["warnings"]}  # noqa: E731
    assert "done-not-moved" in codes("ABC-102")
    assert {"link", "next-vs-progress"} <= codes("ABC-110")
    assert {"status-vs-progress", "dup-next"} <= codes("ABC-111") and "dup-next" in codes("ABC-201")
    assert "stale-date" in codes("ABC-210") and "review-no-mr" in codes("ABC-210")
    assert "no-update" in codes("Planning & Analisis — eKYC")
    assert tasks["ABC-200"].get("_header_only") and [s["ticket"] for s in tasks["ABC-200"]["subtasks"]] == ["ABC-201"]
    assert [b["blocker"] for b in rep["blockers"]] == ["Bergantung pada penyelesaian task yang sedang berjalan.",
                                                       "Menunggu konfirmasi dari tim Product."]
    assert [t["team"] for t in rep["teams"]] == ["FE", "BE"]
    txt = d.teams_text("2026-10-23")
    assert txt.startswith("DAILY CHECK TEAM FE\nTanggal: 23 Oktober 2026\n")
    assert "1. GROUP FE — MAS BUDI & MBAK SARI" in txt and "2. GROUP BE — MAS JOKO & MAS RUDI\nVersi: V2" in txt
    assert "- Status: In Progress — Testing." in txt and "- Blocker: Tidak ada." in txt
    assert "Subtask 1: ABC-201 — Implement Expiry Validation" in txt and "C. Planning & Analisis" in txt
    assert "Scope:\n- Analisis wrapper API." in txt and "bvelum" not in txt and "In Progres." not in txt
    assert "DAILY CHECK TEAM BE" not in d.teams_text("2026-10-23", "FE")
    # importing the same text again updates instead of duplicating
    d.import_text(SAMPLE)
    assert store.query("SELECT COUNT(*) n FROM daily_tasks")[0]["n"] == 9


def test_carry_over_stalled_and_blocker_escalation():
    d, store = svc()
    d.import_text(SAMPLE)
    for day in ("2026-10-24", "2026-10-25"):
        assert d.carry_over(day) > 0
    rep = d.day("2026-10-25")
    flat = {}
    for tm in rep["teams"]:
        for g in tm["groups"]:
            for b in g["blocks"]:
                for t in b["tasks"]:
                    flat[t.get("ticket") or t["title"]] = t
    assert "stalled" in {w["code"] for w in flat["ABC-102"]["warnings"]}
    assert flat["ABC-102"]["changed"] is False
    esc = [e for e in rep["escalate"] if e["task"] == "Konfirmasi Logic Minimal Transaksi dengan Product"]
    assert esc and "3 hari" in esc[0]["msg"]
    # an edit on the day clears "unchanged" and gets tidied
    tid = flat["ABC-102"]["id"]
    d.save_entry(tid, "2026-10-25", {"status": "ready for qa", "progress": "sdh dites"})
    t = next(t for tm in d.day("2026-10-25")["teams"] for g in tm["groups"] for b in g["blocks"] for t in b["tasks"]
             if t["id"] == tid)
    assert t["status"] == "Ready for QA" and t["progress"] == "Sudah dites." and t["changed"] is True
    # status-only change keeps the note
    d.save_entry(flat["ABC-101"]["id"], "2026-10-25", {"status": "In Progress"})
    assert store.query("SELECT status_note FROM daily_entries WHERE task_id=? AND day='2026-10-25'",
                       (flat["ABC-101"]["id"],))[0]["status_note"] == "Testing"
    # Done is not carried
    d.save_entry(tid, "2026-10-25", {"status": "Done"})
    d.carry_over("2026-10-26")
    assert not store.query("SELECT 1 FROM daily_entries WHERE task_id=? AND day='2026-10-26'", (tid,))


def test_gitlab_evidence():
    d, store = svc()
    d.import_text(SAMPLE)
    store.upsert("7:5", project_id=7, iid=5, title="feat(ABC-102): list status", status="merged",
                 source_branch="feat/ABC-102-list", target_branch="staging", web_url="u")
    store.upsert("7:6", project_id=7, iid=6, title="x", status="notified", source_branch="feat/abc_210-mobile",
                 target_branch="staging", pipeline="failed", web_url="u")
    rep = d.day("2026-10-23")
    flat = {t.get("ticket"): t for tm in rep["teams"] for g in tm["groups"] for b in g["blocks"] for t in b["tasks"]}
    assert "merged-still-early" in {w["code"] for w in flat["ABC-102"]["warnings"]}
    c210 = {w["code"] for w in flat["ABC-210"]["warnings"]}
    assert "review-no-mr" not in c210 and "pipeline-failed" in c210 and flat["ABC-210"]["mrs"][0]["iid"] == 6


def test_manual_task_and_statuses():
    d, store = svc()
    d.import_text(SAMPLE)
    tid = d.save_task({"team": "qa", "pic": "Mbak Ani", "ticket": "abc-300", "title": "Regression test"})
    d.save_entry(tid, "2026-10-23", {"status": "Waiting Deploy", "progress": "50%"})
    rep = d.day("2026-10-23")
    t = next(t for tm in rep["teams"] if tm["team"] == "QA" for g in tm["groups"] for b in g["blocks"] for t in b["tasks"])
    assert t["ticket"] == "ABC-300" and t["link"] == "https://jira.example/browse/ABC-300" and t["pct"] == 50
    assert "unknown-status" in {w["code"] for w in t["warnings"]}
    d.save_statuses(d.statuses() + ["Waiting Deploy", "waiting deploy"])
    assert d.statuses()[-1] == "Waiting Deploy" and d.statuses().count("Waiting Deploy") == 1
    assert "unknown-status" not in {w["code"] for w in next(
        t for tm in d.day("2026-10-23")["teams"] if tm["team"] == "QA" for g in tm["groups"] for b in g["blocks"]
        for t in b["tasks"])["warnings"]}
    try:
        d.save_task({"ticket": "nope", "title": ""})
        raise AssertionError("tiket tidak valid harus ditolak")
    except ValueError:
        pass


def test_ai_polish_only_small_fixes():
    class AI:
        def complete(self, system, user, task="review", validate=None):
            items = json.loads(user)
            out = []
            for x in items:
                if "Logic minimal" in x["text"]:
                    out.append({"id": x["id"], "text": "Semua sudah beres total dan siap rilis ke produksi."})
                if x["text"].startswith("Tinggal di push"):
                    out.append({"id": x["id"], "text": "Tinggal di-push."})
            return json.dumps({"items": out}), "fake"
    d, _ = svc()
    d.ai = AI()
    pv = d.preview(SAMPLE, use_ai=True)
    ai = [c for c in pv["corrections"] if c["why"] == "ejaan (AI)"]
    assert [c["after"] for c in ai] == ["Tinggal di-push."], "AI yang mengubah makna ditolak"


def test_dashboard_routes():
    from tests.test_quality import _req, _serve
    cfg = copy.deepcopy(DEFAULTS)
    cfg["gitlab"]["token"] = ""
    store = Store(":memory:")
    srv, base = _serve(cfg, store)
    h = {"X-MRPilot": "1"}
    try:
        assert _req(base + "/daily")[0] == 200
        pv = json.loads(_req(base + "/api/daily/preview", "POST", {"text": SAMPLE}, h)[1])
        assert pv["day"] == "2026-10-23" and len(pv["tasks"]) == 9
        assert _req(base + "/api/daily/import", "POST", {"text": SAMPLE}, {})[0] == 403
        rep = json.loads(_req(base + "/api/daily/import", "POST", {"text": SAMPLE}, h)[1])
        assert rep["summary"]["tasks"] == 8
        tid = rep["teams"][0]["groups"][0]["blocks"][0]["tasks"][0]["id"]
        code, body, _ = _req(base + "/api/daily/entry", "POST",
                             {"day": "2026-10-23", "task_id": tid, "entry": {"status": "Code Review"}}, h)
        assert code == 200 and json.loads(body)["teams"][0]["groups"][0]["blocks"][0]["tasks"][0]["status"] == "Code Review"
        assert _req(base + "/api/daily/entry", "POST", {"day": "kemarin", "task_id": tid, "entry": {}}, h)[0] == 400
        txt = _req(base + "/api/daily.txt?day=2026-10-23&team=BE")[1].decode()
        assert txt.startswith("DAILY CHECK TEAM BE")
        rep = json.loads(_req(base + "/api/daily/carry", "POST", {"day": "2026-10-24"}, h)[1])
        assert rep["carried"] > 0 and rep["prev_day"] == "2026-10-23"
        assert _req(base + "/api/daily/statuses", "PUT", {"statuses": []}, h)[0] == 400
    finally:
        srv.shutdown()


def test_edit_returns_corrections_and_ai_option():
    d, store = svc()
    d.import_text(SAMPLE)
    tid = d.save_task({"team": "FE", "grp": "FE — MAS BUDI & MBAK SARI", "pic": "Mbak Sari", "ticket": "ABC-150",
                       "title": "Riwayat kuota"})
    corr = d.save_entry(tid, "2026-10-23", {"status": "in progres", "progress": "pengherjaan 40%", "blocker": "bvelum ada"})
    fields = {c["field"]: c for c in corr}
    assert set(fields) == {"status", "progress", "blocker"} and fields["status"]["after"] == "In Progress"
    row = store.query("SELECT * FROM daily_entries WHERE task_id=?", (tid,))[0]
    assert (row["status"], row["progress"], row["pct"], row["blocker"]) == ("In Progress", "Pengerjaan 40%.", 40, "-")
    assert d.save_entry(tid, "2026-10-23", {"next_action": "Lanjut testing."}) == [], "teks yang sudah rapi: tidak ada catatan"
    # new task lands under its PIC in the same group
    rep = d.day("2026-10-23")
    blk = next(b for tm in rep["teams"] for g in tm["groups"] for b in g["blocks"] if b["name"] == "Mbak Sari")
    assert [t["ticket"] for t in blk["tasks"]][-1] == "ABC-150"

    class AI:
        def complete(self, system, user, task="review", validate=None):
            items = json.loads(user)
            return json.dumps({"items": [{"id": x["id"], "text": x["text"].replace("di kerjakan", "dikerjakan")}
                                         for x in items if "di kerjakan" in x["text"]]}), "fake"
    d.ai = AI()
    corr = d.save_entry(tid, "2026-10-23", {"progress": "masih di kerjakan"}, use_ai=True)
    assert corr and corr[-1]["why"] == "ejaan (AI)" and corr[-1]["after"] == "Masih dikerjakan."
    assert d.save_entry(tid, "2026-10-23", {"progress": "masih di kerjakan"}) == [], "tanpa AI: hanya koreksi bawaan"
