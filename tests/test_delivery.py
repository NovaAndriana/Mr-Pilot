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
    cfg["delivery"]["team"] = [{"username": "joko", "name": "Mas Joko", "team": "BE"},
                               {"username": "rudi", "name": "Mas Rudi", "team": "BE"},
                               {"username": "sari", "name": "Mbak Sari", "team": "Mobile"}]
    store = Store(":memory:")
    return Delivery(cfg, store, ai), store


def seed(store):
    add(store, 1, "feat(IDAS-5413): add quota bundle to price order", "joko", "Joko S",
        labels=["feature::Kuota bundle"], milestone="Sprint 21")
    add(store, 2, "fix(IDAS-5420): quota bundle shows 0 on old app", "joko", "Joko S",
        labels=["feature::Kuota bundle", "bug"], milestone="Sprint 21")
    add(store, 3, "IDAS-5327/fix role signature", "rudi", "Rudi H", branch="fix/IDAS-5327-role-signature")
    add(store, 4, "feat(IDAS-5500): dynamic role screen", "sari", "Sari", status="notified")
    add(store, 5, "chore: bump sdk", "budi", "Budi")
    add(store, 6, "feat(IDAS-1): last month", "joko", "Joko", day=date(2026, 9, 10))


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
    sep = next(p for p in be["people"] if p["name"] == "Mas Joko")
    assert [g["name"] for g in sep["groups"]] == ["Kuota bundle e-meterai"]
    assert [m["kind"] for m in sep["groups"][0]["mrs"]] == ["feature", "bugfix"] and sep["groups"][0]["kind"] == "feature"
    assert sep["counts"]["feature"] == 1 and sep["counts"]["bugfix"] == 1
    feat = {f["name"]: f for f in rep["features"]}
    assert feat["Tanda tangan per role"]["people"] == ["Mas Rudi", "Mbak Sari"]
    assert rep["unregistered"] == [{"username": "budi", "name": "Budi"}]
    # cached per MR: a second build doesn't ask the AI again
    n = len(ai.calls)
    dl.build("this_week", today=TODAY)
    assert len(ai.calls) == n
    assert ai.calls[0][2] == "delivery" and "Joko" not in ai.calls[0][1], "nama orang tidak dikirim ke AI"


def test_manual_edit_and_rename_win():
    ai = FakeAI({"7:1": ("Kuota", "feature"), "7:2": ("Kuota", "bugfix"), "7:3": ("Role", "bugfix"),
                 "7:4": ("Role", "feature"), "7:5": ("Lain", "other")})
    dl, store = make(ai=ai)
    seed(store)
    dl.build("this_week", today=TODAY)
    dl.set_override("7:3", group="Kuota", kind="feature")
    dl.rename_group("Kuota", "Kuota bundle e-meterai")
    rep = dl.build("this_week", today=TODAY)
    arf = next(p for t in rep["teams"] for p in t["people"] if p["name"] == "Mas Rudi")
    g = arf["groups"][0]
    assert g["name"] == "Kuota bundle e-meterai" and g["mrs"][0]["kind"] == "feature" and g["source"] == "manual"
    sep = next(p for t in rep["teams"] for p in t["people"] if p["name"] == "Mas Joko")
    assert sep["groups"][0]["name"] == "Kuota bundle e-meterai"
    # regroup asks the AI again but keeps the manual edit
    ai.groups["7:3"] = ("Role", "bugfix")
    rep = dl.build("this_week", today=TODAY, regroup=True)
    arf = next(p for t in rep["teams"] for p in t["people"] if p["name"] == "Mas Rudi")
    assert arf["groups"][0]["name"] == "Kuota bundle e-meterai"


def test_label_milestone_ticket_modes_and_ai_failure():
    dl, store = make(mode="label")
    seed(store)
    dl.set_mode("label", "feature::")
    rep = dl.build("this_week", today=TODAY)
    sep = next(p for t in rep["teams"] for p in t["people"] if p["name"] == "Mas Joko")
    assert sep["groups"][0]["name"] == "Kuota bundle" and len(sep["groups"][0]["mrs"]) == 2
    arf = next(p for t in rep["teams"] for p in t["people"] if p["name"] == "Mas Rudi")
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
    dl.save_team([{"username": "@budi", "name": "Mas Budi", "team": "FE"}, {"username": "joko", "name": "Joko"},
                  {"username": "budi"}])
    assert [m["username"] for m in dl.team()] == ["budi", "joko"] and dl.team()[1]["team"] == "Lainnya"
    rep = dl.build("this_week", today=TODAY)
    assert "FE" in [t["team"] for t in rep["teams"]]


def test_text_docx_and_weekly(tmp_path):
    dl, store = make(mode="ticket")
    seed(store)
    sent = []
    dl.send = sent.append
    rep = dl.build("this_week", today=TODAY)
    txt = dl.text(rep, html=False)
    assert "Tim BE" in txt and "Mas Joko" in txt and "<b>" not in txt and "4 MR selesai, 1 masih berjalan" in txt
    assert "<b>Tim BE</b>" in dl.text(rep, html=True)
    blob = dl.docx(rep)
    z = zipfile.ZipFile(io.BytesIO(blob))
    doc = z.read("word/document.xml").decode()
    assert "Laporan Delivery Tim" in doc and "Mas Joko" in doc and "!1 feat(IDAS-5413)" in doc
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
                    "merged_at": "2026-10-27T03:00:00Z", "author": {"name": "S", "username": "joko"}}
    dl, store = make(mode="label")
    store.upsert("7:9", project_id=7, iid=9, title="feat: x", status="merged", decided_at=ts(date(2026, 10, 27)),
                 author_username="joko", author="S")
    dl.gl = GL()
    rep = dl.build("this_week", today=TODAY)
    assert dl.gl.n == 1 and rep["features"][0]["name"] == "feature::Kuota"
    dl.build("this_week", today=TODAY)
    assert dl.gl.n == 1, "sekali saja"


def _people(rep):
    return {p["name"]: sum(len(g["mrs"]) for g in p["groups"]) for t in rep["teams"] for p in t["people"]}


def test_credit_goes_to_assignee_not_author():
    dl, store = make(mode="ticket")
    me = [{"username": "nova", "name": "Nova"}]
    dl._me = lambda: "nova"
    # author is the developer's colleague; the assignee is who owns the work
    add(store, 1, "feat(IDAS-1): a", "rudi", "Rudi H")
    add(store, 2, "feat(IDAS-2): b", "rudi", "Rudi H")
    add(store, 3, "feat(IDAS-3): c", "rudi", "Rudi H")
    add(store, 4, "feat(IDAS-4): d", "joko", "Joko S")
    store.upsert("7:1", assignees=json.dumps([{"username": "joko", "name": "Joko S"}]))
    store.upsert("7:2", assignees=json.dumps([{"username": "joko", "name": "Joko S"},
                                              {"username": "sari", "name": "Sari"}]))   # two assignees: both
    store.upsert("7:3", assignees=json.dumps(me))        # only the reviewer himself: falls back to the author
    store.upsert("7:4", assignees="[]")                  # nobody assigned: falls back to the author
    rep = dl.build("this_week", today=TODAY)
    assert _people(rep) == {"Mas Joko": 3, "Mbak Sari": 1, "Mas Rudi": 1}, _people(rep)
    assert rep["totals"]["mrs"] == 4, "MR dihitung sekali di total walau assignee-nya dua"
    assert rep["credit"]["by"] == "assignee" and rep["credit"]["fallback"] == 2
    assert set(rep["credit"]["fallback_mrs"]) == {"!3", "!4"}
    assert any(f["people"] == ["Mas Joko", "Mbak Sari"] for f in rep["features"])


def test_credit_by_author_and_setting_persists():
    dl, store = make(mode="ticket")
    add(store, 1, "feat(IDAS-1): a", "rudi", "Rudi H")
    store.upsert("7:1", assignees=json.dumps([{"username": "joko", "name": "Joko S"}]))
    assert _people(dl.build("this_week", today=TODAY)) == {"Mas Joko": 1}
    dl.set_credit_by("author")
    rep = dl.build("this_week", today=TODAY)
    assert _people(rep) == {"Mas Rudi": 1} and rep["credit"]["fallback"] == 0
    try:
        dl.set_credit_by("reviewer")
        raise AssertionError("harus ditolak")
    except ValueError:
        pass


def test_unregistered_assignee_is_listed_and_old_records_are_backfilled():
    class GL:
        n = 0

        def get_mr(self, pid, iid):
            self.n += 1
            return {"labels": [], "assignees": [{"username": "budi", "name": "Budi Baru"}],
                    "author": {"name": "Rudi H", "username": "rudi"}}
    dl, store = make(mode="ticket")
    store.upsert("7:9", project_id=7, iid=9, title="feat(IDAS-9): x", status="merged",
                 decided_at=ts(date(2026, 10, 27)), author_username="rudi", author="Rudi H", labels="[]")
    dl.gl = GL()  # labels already known, assignees not: still fetched once
    rep = dl.build("this_week", today=TODAY)
    assert dl.gl.n == 1 and _people(rep) == {"Budi Baru": 1}
    assert rep["unregistered"] == [{"username": "budi", "name": "Budi Baru"}]
    dl.build("this_week", today=TODAY)
    assert dl.gl.n == 1, "sekali saja"


def test_mr_meta_keeps_assignees():
    from mr_pilot.app import mr_meta
    m = mr_meta({"assignees": [{"username": "joko", "name": "Joko S", "id": 1}], "author": {"username": "rudi"}})
    assert json.loads(m["assignees"]) == [{"username": "joko", "name": "Joko S"}]
    m = mr_meta({"assignee": {"username": "sari", "name": "Sari"}})
    assert json.loads(m["assignees"]) == [{"username": "sari", "name": "Sari"}]


def test_dashboard_routes():
    from tests.test_quality import _req, _serve
    cfg = copy.deepcopy(DEFAULTS)
    cfg["gitlab"]["token"] = ""
    cfg["delivery"]["group_by"] = "ticket"
    store = Store(":memory:")
    today = date.today()
    add(store, 1, "feat(IDAS-5413): add quota bundle", "joko", "Joko", day=today)
    add(store, 2, "fix(IDAS-5413): quota zero", "joko", "Joko", day=today)
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
                             {"members": [{"username": "joko", "name": "Mas Joko", "team": "BE"}]}, h)
        assert code == 200
        rep = json.loads(_req(base + "/api/delivery")[1])
        assert rep["teams"][0]["team"] == "BE" and rep["teams"][0]["people"][0]["name"] == "Mas Joko"
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
        assert code == 200 and "Mas Joko" in body.decode()
    finally:
        srv.shutdown()
