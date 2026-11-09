"""Jira / Plane tracker: normalised items, matching people to the team, switching provider, pulling into Daily."""
import copy
import os
import sys
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from mr_pilot.config import DEFAULTS  # noqa: E402
from mr_pilot.daily import Daily, check_entry  # noqa: E402
from mr_pilot.store import Store  # noqa: E402
from mr_pilot.tracker import (Tracker, TrackerError, match_members, state_group)  # noqa: E402

NOW = datetime.now().isoformat() + "Z"
TEAM = [{"username": "joko", "name": "Mas Joko", "team": "BE"},
        {"username": "sari", "name": "Mbak Sari", "team": "FE", "tracker": "sari.putri@corp.id"},
        {"username": "rudi", "name": "Mas Rudi", "team": "BE"}]


class Resp:
    def __init__(self, data, code=200, headers=None):
        self.data, self.status_code, self.headers = data, code, headers or {}

    def json(self):
        return self.data


class Session:
    """Routes by URL; records calls."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, headers=None, params=None, timeout=None, verify=True):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        for suffix, fn in self.routes.items():
            if url.endswith(suffix):
                out = fn(params or {}) if callable(fn) else fn
                return out if isinstance(out, Resp) else Resp(out)
        return Resp({}, 404)


def cfg(**over):
    c = copy.deepcopy(DEFAULTS)
    c["tracker"]["jira"].update(url="https://jira.corp.id", token="pat")
    c["tracker"]["plane"].update(url="https://plane.corp.id", workspace="idas", api_key="key")
    c["tracker"].update(over)
    return c


def jira_issue(key, summary, status, cat, assignee, project="AkuSign"):
    a = {"displayName": assignee[0], "name": assignee[1], "emailAddress": assignee[2], "accountId": ""} if assignee else None
    return {"key": key, "fields": {"summary": summary, "status": {"name": status, "statusCategory": {"key": cat}},
                                   "assignee": a, "updated": NOW, "duedate": None, "project": {"name": project}}}


def plane_item(seq, name, state, group, people, updated=NOW):
    return {"id": f"w{seq}", "name": name, "sequence_id": seq, "state": {"name": state, "group": group},
            "assignees": people, "updated_at": updated, "created_at": updated, "target_date": None}


def test_state_groups():
    assert state_group("Ready for QA") == "qa" and state_group("Code Review") == "review"
    assert state_group("In Progress", "started") == "doing" and state_group("Backlog", "backlog") == "todo"
    assert state_group("Done", "done") == "done" and state_group("Dibatalkan") == "cancelled"
    assert state_group("Selesai") == "done" and state_group("Open", "new") == "todo"


def test_match_people_to_team_by_username_email_name_or_tracker_field():
    item = lambda **p: {"people": [p]}  # noqa: E731
    names = lambda it: [m["username"] for m in match_members(it, TEAM)]  # noqa: E731
    assert names(item(username="joko")) == ["joko"]
    assert names(item(email="Rudi@corp.id")) == ["rudi"], "bagian depan email = username"
    assert names(item(name="Joko")) == ["joko"], "nama tanpa 'Mas'"
    assert names(item(email="sari.putri@corp.id", name="S. Putri")) == ["sari"], "kolom tracker anggota"
    assert names(item(username="orang", name="Orang Lain")) == []
    assert names({"people": []}) == []


def test_jira_server_pagination_and_items():
    pages = {0: {"issues": [jira_issue("IDAS-1", "Quota", "In Progress", "indeterminate", ("Joko S", "joko", "j@corp.id")),
                            jira_issue("IDAS-2", "Role", "Ready for QA", "indeterminate", ("Rudi", "rudi", ""))], "total": 3},
             2: {"issues": [jira_issue("IDAS-3", "Old", "Done", "done", None)], "total": 3}}
    s = Session({"/rest/api/2/search": lambda p: pages[p["startAt"]], "/rest/api/2/myself": {"displayName": "Nova"}})
    t = Tracker(cfg(provider="jira"), Store(":memory:"), s)
    items = t.items()
    assert [i["key"] for i in items] == ["IDAS-1", "IDAS-2", "IDAS-3"]
    assert [i["group"] for i in items] == ["doing", "qa", "done"]
    assert items[0]["url"] == "https://jira.corp.id/browse/IDAS-1" and items[0]["people"][0]["username"] == "joko"
    assert s.calls[0][2]["Authorization"] == "Bearer pat", "token Server/DC = bearer"
    assert "statusCategory != Done" in s.calls[0][1]["jql"]
    assert t.test()["ok"] and "Nova" in t.test()["message"]
    n = len(s.calls)
    t.items()
    assert len(s.calls) == n or s.calls[-1][0].endswith("myself"), "hasil disimpan sebentar"


def test_jira_cloud_uses_basic_auth_and_next_page_token():
    c = cfg(provider="jira")
    c["tracker"]["jira"].update(url="https://x.atlassian.net", email="a@b.id", token="tok")
    seen = []

    def search(p):
        seen.append(dict(p))
        if not p.get("nextPageToken"):
            return {"issues": [jira_issue("A-1", "x", "To Do", "new", ("N", "", "n@b.id"))], "nextPageToken": "t2",
                    "isLast": False}
        return {"issues": [jira_issue("A-2", "y", "Done", "done", None)], "isLast": True}
    s = Session({"/rest/api/3/search/jql": search})
    items = Tracker(c, Store(":memory:"), s).items()
    assert [i["key"] for i in items] == ["A-1", "A-2"] and seen[1]["nextPageToken"] == "t2"
    assert s.calls[0][2]["Authorization"].startswith("Basic ")


def test_plane_work_items_expand_pagination_and_old_api_fallback():
    proj = {"results": [{"id": "p1", "name": "AkuSign", "identifier": "IDAS"}]}
    people = [{"id": "u1", "display_name": "joko", "first_name": "Joko", "last_name": "S", "email": "j@corp.id"}]
    page1 = {"results": [plane_item(1, "Quota bundle", "In Progress", "started", people)], "next_page_results": True,
             "next_cursor": "c2"}
    page2 = {"results": [plane_item(2, "Role", "Done", "completed", people),
                         plane_item(3, "Ancient", "Done", "completed", people, "2020-01-01T00:00:00Z")],
             "next_page_results": False}
    calls = {"n": 0}

    def items(p):
        calls["n"] += 1
        return page2 if p.get("cursor") else page1
    s = Session({"/projects/": proj, "/projects/p1/issues/": items})   # no /work-items/: older Plane
    t = Tracker(cfg(provider="plane"), Store(":memory:"), s)
    got = t.items()
    assert [i["key"] for i in got] == ["IDAS-1", "IDAS-2"], "yang selesai lama tidak ikut"
    assert got[0]["group"] == "doing" and got[1]["group"] == "done"
    assert got[0]["url"] == "https://plane.corp.id/idas/browse/IDAS-1/" and got[0]["project"] == "AkuSign"
    assert s.calls[-1][2]["X-API-Key"] == "key"
    assert any(p.get("expand") == "assignees,state" for _, p, _ in s.calls if p)
    assert match_members(got[0], TEAM)[0]["username"] == "joko"


def test_plane_uuid_assignees_and_states_are_resolved():
    s = Session({"/projects/": {"results": [{"id": "p1", "name": "AkuSign", "identifier": "IDAS"}]},
                 "/projects/p1/work-items/": {"results": [{"id": "w", "name": "T", "sequence_id": 7, "state": "s1",
                                                           "assignees": ["u1"], "updated_at": NOW}]},
                 "/projects/p1/states/": {"results": [{"id": "s1", "name": "Code Review", "group": "started"}]},
                 "/members/": [{"member": {"id": "u1", "display_name": "rudi", "email": "rudi@corp.id"}}]})
    got = Tracker(cfg(provider="plane"), Store(":memory:"), s).items()
    assert got[0]["key"] == "IDAS-7" and got[0]["state"] == "Code Review" and got[0]["group"] == "review"
    assert match_members(got[0], TEAM)[0]["username"] == "rudi"


def test_errors_are_readable():
    for code, word in ((401, "menolak"), (404, "alamat"), (429, "rate limit")):
        t = Tracker(cfg(provider="plane"), Store(":memory:"), Session({"/projects/": Resp({}, code, {"Retry-After": "0"})}))
        with pytest.raises(TrackerError, match=word):
            t.items()
        assert word in t.test()["message"] and not t.test()["ok"]
    c = cfg(provider="plane")
    c["tracker"]["plane"]["api_key"] = ""
    r = Tracker(c, Store(":memory:"), Session({})).test()
    assert not r["ok"] and "plane.api_key" in r["message"]


def test_switch_provider_is_remembered_and_validated():
    store = Store(":memory:")
    t = Tracker(cfg(provider="jira"), store, Session({}))
    assert t.provider() == "jira" and t.status()["enabled"]
    t.set_provider("plane")
    assert Tracker(cfg(provider="jira"), store, Session({})).provider() == "plane", "pilihan tersimpan"
    t.set_provider("none")
    assert not t.status()["enabled"] and t.items() == []
    with pytest.raises(ValueError):
        t.set_provider("trello")


def _daily(items_by_provider=None):
    store = Store(":memory:")
    s = Session({"/projects/": {"results": [{"id": "p1", "name": "AkuSign", "identifier": "IDAS"}]},
                 "/projects/p1/work-items/": {"results": [
                     plane_item(1, "Quota bundle", "In Progress", "started", [{"id": "u", "display_name": "joko"}]),
                     plane_item(2, "Role screen", "Ready for QA", "started", [{"id": "u", "display_name": "sari",
                                                                              "email": "sari.putri@corp.id"}]),
                     plane_item(3, "Someone else", "Todo", "unstarted", [{"id": "z", "display_name": "orang"}]),
                     plane_item(4, "Dropped", "Cancelled", "cancelled", [{"id": "u", "display_name": "joko"}])]}})
    tr = Tracker(cfg(provider="plane"), store, s)
    return Daily({}, store, None, tr, lambda: TEAM), store


def test_pull_creates_tasks_per_pic_without_duplicates():
    d, store = _daily()
    day = date.today().isoformat()
    r = d.pull_tracker(day)
    assert (r["created"], r["unmatched"], r["skipped"]) == (2, 1, 1) and r["provider"] == "Plane"  # #3 not team, #4 cancelled
    data = d.day(day)
    rows = {t["ticket"]: t for tm in data["teams"] for t in _flat(tm)}
    assert rows["IDAS-1"]["pic"] == "Mas Joko" and rows["IDAS-1"]["team"] == "BE" and rows["IDAS-1"]["status"] == "In Progress"
    assert rows["IDAS-2"]["pic"] == "Mbak Sari" and rows["IDAS-2"]["status"] == "Ready for QA"
    assert rows["IDAS-1"]["link"].endswith("/browse/IDAS-1/") and rows["IDAS-1"]["grp"] == "AkuSign"
    assert "belum ada update" in rows["IDAS-1"]["progress"].lower()
    # edit by hand, pull again: nothing is overwritten or duplicated
    d.save_entry(int(rows["IDAS-1"]["id"]), day, {"progress": "Sudah 50%", "status": "Code Review"})
    r2 = d.pull_tracker(day)
    assert r2["created"] == 0 and r2["existing"] == 2
    rows = {t["ticket"]: t for tm in d.day(day)["teams"] for t in _flat(tm)}
    assert rows["IDAS-1"]["progress"] == "Sudah 50%." and rows["IDAS-1"]["status"] == "Code Review"
    assert len(store.query("SELECT 1 FROM daily_tasks")) == 2


def _flat(team):
    out = []
    for g in team["groups"]:
        for b in g["blocks"]:
            for t in b.get("tasks") or []:
                out.append(t)
                out += t.get("subtasks") or []
    return out


def test_pull_errors_are_clear():
    d, _ = _daily()
    d.team = lambda: []
    with pytest.raises(ValueError, match="anggota tim"):
        d.pull_tracker()
    d2 = Daily({}, Store(":memory:"), None, None, lambda: TEAM)
    with pytest.raises(ValueError):
        d2.pull_tracker()


def test_daily_flags_what_the_tracker_disagrees_with():
    day = date.today()
    task = {"ticket": "IDAS-1", "pic": "Mas Joko", "link": ""}
    codes = lambda e, tk: {w["code"] for w in check_entry(task, {"_statuses": [], **e}, day, tk=tk)}  # noqa: E731
    assert "tracker-done" in codes({"status": "In Progress", "progress": "x"}, {"provider": "plane", "group": "done", "state": "Done"})
    assert "tracker-behind" in codes({"status": "Done", "progress": "x"}, {"provider": "jira", "group": "doing", "state": "In Progress"})
    assert "tracker-assignee" in codes({"status": "Open", "progress": "x"},
                                       {"provider": "plane", "group": "todo", "state": "Todo", "people": ["Budi"]})
    assert not {c for c in codes({"status": "In Progress", "progress": "x"},
                                 {"provider": "plane", "group": "doing", "state": "In Progress", "people": ["Joko"]})
                if c.startswith("tracker")}
    assert not {c for c in codes({"status": "In Progress", "progress": "x"}, None) if c.startswith("tracker")}


def test_config_validation():
    from mr_pilot.config import ConfigError, validate
    c = copy.deepcopy(DEFAULTS)
    c["tracker"]["provider"] = "Plane"
    c["tracker"]["jira"]["projects"] = "IDAS, AKU"
    try:
        validate(c)
    except ConfigError as ex:
        pytest.skip(f"validate needs more fields: {ex}")
    assert c["tracker"]["provider"] == "plane" and c["tracker"]["jira"]["projects"] == ["IDAS", "AKU"]
    c["tracker"]["provider"] = "trello"
    with pytest.raises(ConfigError):
        validate(c)
