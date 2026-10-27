"""Delivery report: team -> person -> feature -> MRs, grouping modes, edits, outputs."""
import copy
import io
import json
import os
import sys
import time
import zipfile
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mr_pilot.config import DEFAULTS  # noqa: E402
from mr_pilot.delivery import Delivery, clean_title, mr_kind, period, ticket_of  # noqa: E402
from mr_pilot.store import Store  # noqa: E402

TODAY = date(2026, 10, 28)  # Wednesday


def ts(d, h=10):
    return time.mktime(datetime(d.year, d.month, d.day, h).timetuple())


def add(store, iid, title, user, name, branch="", day=date(2026, 10, 27), status="merged", labels=None,
        milestone="", desc=""):
    store.upsert(f"7:{iid}", project_id=7, iid=iid, title=title, web_url=f"https://gl/7/{iid}", status=status,
                 author=name, author_username=user, source_branch=branch, target_branch="staging",
                 project="idas/be", decided_at=ts(day) if status == "merged" else None,
                 mr_created_at=datetime(day.year, day.month, day.day).isoformat(), first_seen=ts(day),
                 labels=json.dumps(labels or []), milestone=milestone, description=desc)


class FakeAI:
    def __init__(self, groups):
        self.groups, self.calls = groups, []

    def complete(self, system, user, task="review", validate=None):
        self.calls.append((system, user, task))
        items = [{"id": k, "group": g, "kind": kind} for k, (g, kind) in self.groups.items() if k in user]
        return json.dumps({"items": items}), "fake"


def make(tmp_path=None, ai=None, mode="ai"):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["delivery"]["group_by"] = mode
    cfg["delivery"]["team"] = [{"username": "septyan", "name": "Mas Septyan", "team": "BE"},
                               {"username": "arfandy", "name": "Mas Arfandy", "team": "BE"},
                               {"username": "dede", "name": "Mas Dede", "team": "Mobile"}]
    store = Store(":memory:")
    return Delivery(cfg, store, ai), store


def seed(store):
    add(store, 1, "feat(IDAS-5413): add quota bundle to price order", "septyan", "Septyan D N",
        labels=["feature::Kuota bundle"], milestone="Sprint 21")
    add(store, 2, "fix(IDAS-5420): quota bundle shows 0 on old app", "septyan", "Septyan D N",
        labels=["feature::Kuota bundle", "bug"], milestone="Sprint 21")
    add(store, 3, "IDAS-5327/fix role signature", "arfandy", "Arfandy Surya", branch="fix/IDAS-5327-role-signature")
    add(store, 4, "feat(IDAS-5500): dynamic role screen", "dede", "Dede", status="notified")
    add(store, 5, "chore: bump sdk", "budi", "Budi")
    add(store, 6, "feat(IDAS-1): last month", "septyan", "Septyan", day=date(2026, 9, 10))


def test_helpers():
    assert mr_kind("feat(IDAS-1): x") == "feature" and mr_kind("fix: y") == "bugfix"
    assert mr_kind("IDAS-5327/fix role", "fix/IDAS-5327-x") == "bugfix" and mr_kind("Update screen") is None
    assert ticket_of("IDAS-5327/fix role") == "IDAS-5327" and ticket_of("", "feat/idas_12-x") == "IDAS-12"
    assert clean_title("feat(IDAS-5413): add quota bundle") == "add quota bundle"
    s, e, label = period("this_week", today=TODAY)
    assert date.fromtimestamp(s) == date(2026, 10, 26) and date.fromtimestamp(e) == date(2026, 11, 2)
    assert label == "26 Oktober – 1 November 2026"
    s, e, label = period("last_month", today=TODAY)
    assert date.fromtimestamp(s) == date(2026, 9, 1) and label == "1–30 September 2026"
    s, e, _ = period("custom", "2026-10-01", "2026-10-15", today=TODAY)
    assert date.fromtimestamp(e) == date(2026, 10, 16)


def test_ai_groups_team_person_feature():
    ai = FakeAI({"7:1": ("Kuota bundle e-meterai", "feature"), "7:2": ("Kuota bundle e-meterai", "bugfix"),
                 "7:3": ("Tanda tangan per role", "bugfix"), "7:4": ("Tanda tangan per role", "feature"),
                 "7:5": ("Dependency", "other")})
    dl, store = make(ai=ai)
    seed(store)
    rep = dl.build("this_week", today=TODAY)
    assert rep["totals"]["mrs"] == 5 and rep["totals"]["merged"] == 4 and rep["totals"]["open"] == 1
    assert [t["team"] for t in rep["teams"]] == ["BE", "Mobile", "Belum terdaftar"]
    be = rep["teams"][0]
    sep = next(p for p in be["people"] if p["name"] == "Mas Septyan")
    assert [g["name"] for g in sep["groups"]] == ["Kuota bundle e-meterai"]
    assert [m["kind"] for m in sep["groups"][0]["mrs"]] == ["feature", "bugfix"] and sep["groups"][0]["kind"] == "feature"
    assert sep["counts"]["feature"] == 1 and sep["counts"]["bugfix"] == 1
    feat = {f["name"]: f for f in rep["features"]}
    assert feat["Tanda tangan per role"]["people"] == ["Mas Arfandy", "Mas Dede"]
    assert rep["unregistered"] == [{"username": "budi", "name": "Budi"}]
    # cached per MR: a second build doesn't ask the AI again
    n = len(ai.calls)
    dl.build("this_week", today=TODAY)
    assert len(ai.calls) == n
    assert ai.calls[0][2] == "delivery" and "Septyan" not in ai.calls[0][1], "nama orang tidak dikirim ke AI"


def test_manual_edit_and_rename_win():
    ai = FakeAI({"7:1": ("Kuota", "feature"), "7:2": ("Kuota", "bugfix"), "7:3": ("Role", "bugfix"),
                 "7:4": ("Role", "feature"), "7:5": ("Lain", "other")})
    dl, store = make(ai=ai)
    seed(store)
    dl.build("this_week", today=TODAY)
    dl.set_override("7:3", group="Kuota", kind="feature")
    dl.rename_group("Kuota", "Kuota bundle e-meterai")
    rep = dl.build("this_week", today=TODAY)
    arf = next(p for t in rep["teams"] for p in t["people"] if p["name"] == "Mas Arfandy")
    g = arf["groups"][0]
    assert g["name"] == "Kuota bundle e-meterai" and g["mrs"][0]["kind"] == "feature" and g["source"] == "manual"
    sep = next(p for t in rep["teams"] for p in t["people"] if p["name"] == "Mas Septyan")
    assert sep["groups"][0]["name"] == "Kuota bundle e-meterai"
    # regroup asks the AI again but keeps the manual edit
    ai.groups["7:3"] = ("Role", "bugfix")
    rep = dl.build("this_week", today=TODAY, regroup=True)
    arf = next(p for t in rep["teams"] for p in t["people"] if p["name"] == "Mas Arfandy")
    assert arf["groups"][0]["name"] == "Kuota bundle e-meterai"


def test_label_milestone_ticket_modes_and_ai_failure():
    dl, store = make(mode="label")
    seed(store)
    dl.set_mode("label", "feature::")
    rep = dl.build("this_week", today=TODAY)
    sep = next(p for t in rep["teams"] for p in t["people"] if p["name"] == "Mas Septyan")
    assert sep["groups"][0]["name"] == "Kuota bundle" and len(sep["groups"][0]["mrs"]) == 2
    arf = next(p for t in rep["teams"] for p in t["people"] if p["name"] == "Mas Arfandy")
    assert arf["groups"][0]["name"].startswith("IDAS-5327"), "tanpa label: per tiket"
    dl.set_mode("milestone")
    rep = dl.build("this_week", today=TODAY)
    assert "Sprint 21" in {f["name"] for f in rep["features"]}
    dl.set_mode("ticket")
    rep = dl.build("this_week", today=TODAY)
    assert "IDAS-5413 · add quota bundle to price order" in {f["name"] for f in rep["features"]}

    class Broken:
        def complete(self, *a, **k):
            raise RuntimeError("kuota habis")
    dl2, store2 = make(ai=Broken())
    seed(store2)
    rep = dl2.build("this_week", today=TODAY)
    assert "kuota habis" in rep["note"] and rep["totals"]["mrs"] == 5


def test_team_from_dashboard_overrides_config():
    dl, store = make(mode="ticket")
    seed(store)
    dl.save_team([{"username": "@budi", "name": "Mas Budi", "team": "FE"}, {"username": "septyan", "name": "Septyan"},
                  {"username": "budi"}])
    assert [m["username"] for m in dl.team()] == ["budi", "septyan"] and dl.team()[1]["team"] == "Lainnya"
    rep = dl.build("this_week", today=TODAY)
    assert "FE" in [t["team"] for t in rep["teams"]]


def test_text_docx_and_weekly(tmp_path):
    dl, store = make(mode="ticket")
    seed(store)
    sent = []
    dl.send = sent.append
    rep = dl.build("this_week", today=TODAY)
    txt = dl.text(rep, html=False)
    assert "Tim BE" in txt and "Mas Septyan" in txt and "<b>" not in txt and "4 MR selesai, 1 masih berjalan" in txt
    assert "<b>Tim BE</b>" in dl.text(rep, html=True)
    blob = dl.docx(rep)
    z = zipfile.ZipFile(io.BytesIO(blob))
    doc = z.read("word/document.xml").decode()
    assert "Laporan Delivery Tim" in doc and "Mas Septyan" in doc and "!1 feat(IDAS-5413)" in doc
    assert {"[Content_Types].xml", "word/styles.xml", "word/numbering.xml"} <= set(z.namelist())
    # weekly: Friday 16:00, once per week
    fri = ts(date(2026, 10, 30), 16) + 60
    assert not dl.weekly_tick(ts(date(2026, 10, 30), 15))
    assert dl.weekly_tick(fri) and not dl.weekly_tick(fri + 600)
    assert len(sent) == 1 and "Delivery tim" in sent[0]


def test_enrich_old_records():
    class GL:
        def __init__(self):
            self.n = 0

        def get_mr(self, pid, iid):
            self.n += 1
            return {"labels": ["feature::Kuota"], "milestone": {"title": "S1"}, "description": "x",
                    "merged_at": "2026-10-27T03:00:00Z", "author": {"name": "S", "username": "septyan"}}
    dl, store = make(mode="label")
    store.upsert("7:9", project_id=7, iid=9, title="feat: x", status="merged", decided_at=ts(date(2026, 10, 27)),
                 author_username="septyan", author="S")
    dl.gl = GL()
    rep = dl.build("this_week", today=TODAY)
    assert dl.gl.n == 1 and rep["features"][0]["name"] == "feature::Kuota"
    dl.build("this_week", today=TODAY)
    assert dl.gl.n == 1, "sekali saja"


def test_dashboard_routes():
    from tests.test_quality import _req, _serve
    cfg = copy.deepcopy(DEFAULTS)
    cfg["gitlab"]["token"] = ""
    cfg["delivery"]["group_by"] = "ticket"
    store = Store(":memory:")
    today = date.today()
    add(store, 1, "feat(IDAS-5413): add quota bundle", "septyan", "Septyan", day=today)
    add(store, 2, "fix(IDAS-5413): quota zero", "septyan", "Septyan", day=today)
    srv, base = _serve(cfg, store)
    h = {"X-MRPilot": "1"}
    try:
        code, body, _ = _req(base + "/delivery")
        assert code == 200 and b"Delivery tim" in body
        rep = json.loads(_req(base + "/api/delivery?period=this_week")[1])
        assert rep["totals"]["mrs"] == 2 and rep["teams"][0]["team"] == "Belum terdaftar"
        assert _req(base + "/api/delivery?period=custom&from=2026-1-1")[0] == 400
        assert _req(base + "/api/delivery/team", "PUT", {"members": []})[0] == 403, "CSRF header wajib"
        code, body, _ = _req(base + "/api/delivery/team", "PUT",
                             {"members": [{"username": "septyan", "name": "Mas Septyan", "team": "BE"}]}, h)
        assert code == 200
        rep = json.loads(_req(base + "/api/delivery")[1])
        assert rep["teams"][0]["team"] == "BE" and rep["teams"][0]["people"][0]["name"] == "Mas Septyan"
        g = rep["features"][0]["name"]
        assert _req(base + "/api/delivery/rename", "POST", {"old": g, "new": "Kuota bundle"}, h)[0] == 200
        assert _req(base + "/api/delivery/override", "POST", {"key": "x", "group": "a"}, h)[0] == 400
        assert _req(base + "/api/delivery/override", "POST", {"key": "7:2", "kind": "improvement"}, h)[0] == 200
        rep = json.loads(_req(base + "/api/delivery")[1])
        assert rep["features"][0]["name"] == "Kuota bundle" and rep["totals"]["improvement"] == 1
        assert _req(base + "/api/delivery/mode", "PUT", {"mode": "nope"}, h)[0] == 400
        code, body, hdr = _req(base + "/api/delivery.docx")
        assert code == 200 and body[:2] == b"PK" and "attachment" in hdr["Content-Disposition"]
        code, body, _ = _req(base + "/api/delivery.txt")
        assert code == 200 and "Mas Septyan" in body.decode()
    finally:
        srv.shutdown()
