"""GitLab SAST / Secret Detection collector and DORA metrics."""
import copy
import json
import os
import sys
import time

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mr_pilot import dora as D  # noqa: E402
from mr_pilot import insights  # noqa: E402
from mr_pilot.config import DEFAULTS, ConfigError, validate  # noqa: E402
from mr_pilot.security import SecurityWatcher, fingerprint, parse_report  # noqa: E402
from mr_pilot.store import Store  # noqa: E402

DAY = 86400
SECRET_VALUE = "glpat" + "-" + "".join(chr(65 + (i * 7) % 26) for i in range(20))  # fake, built at runtime


def cfg(**over):
    c = copy.deepcopy(DEFAULTS)
    for k, v in over.items():
        c[k].update(v)
    return c


def sast_report(*items):
    return json.dumps({"version": "15.0.7", "scan": {"type": "sast", "analyzer": {"name": "Semgrep"}},
                       "vulnerabilities": [
                           {"id": f"id{n}", "name": name, "description": f"desc {name}", "severity": sev,
                            "location": {"file": f, "start_line": line},
                            "identifiers": [{"type": "semgrep_id", "name": rule, "value": rule,
                                             "url": f"https://semgrep.dev/r/{rule}"}]}
                           for n, (name, sev, f, line, rule) in enumerate(items)]}).encode()


def secret_report(f="deploy/cm.yaml", line=3):
    return json.dumps({"scan": {"type": "secret_detection", "analyzer": {"name": "Gitleaks"}},
                       "vulnerabilities": [{
                           "name": "GitLab personal access token",
                           "description": f"GitLab personal access token detected; match: {SECRET_VALUE}",
                           "severity": "Critical", "raw_source_code_extract": SECRET_VALUE,
                           "location": {"file": f, "start_line": line, "commit": {"sha": "abc"}},
                           "identifiers": [{"type": "gitleaks_rule_id", "name": "gitlab_pat",
                                            "value": "gitlab_personal_access_token"}]}]}).encode()


# ---------------------------------------------------------------- parsing
def test_parse_report_normalises_and_masks_secrets():
    s = parse_report(sast_report(("SQL injection", "High", "a.go", 10, "gosec.G202-1")), "sast")[0]
    assert s["severity"] == "high" and s["file"] == "a.go" and s["line"] == 10 and s["rule"] == "gosec.G202-1"
    assert s["rule_url"].startswith("https://semgrep.dev") and s["scanner"] == "Semgrep"
    sec = parse_report(secret_report(), "secret")[0]
    blob = json.dumps(sec)
    assert SECRET_VALUE not in blob and "AbCdEfGhIj" not in blob and "raw_source_code_extract" not in blob
    assert sec["severity"] == "critical" and sec["kind"] == "secret"
    with pytest.raises(ValueError):
        parse_report(b"not json", "sast")


def test_fingerprint_survives_line_shift():
    a = fingerprint(7, parse_report(sast_report(("X", "High", "a.go", 10, "r1"), ("X", "High", "a.go", 20, "r1")), "sast"))
    b = fingerprint(7, parse_report(sast_report(("X", "High", "a.go", 15, "r1"), ("X", "High", "a.go", 25, "r1")), "sast"))
    assert {f["fp"] for f in a} == {f["fp"] for f in b} and len({f["fp"] for f in a}) == 2


def test_ci_config_is_valid_yaml_with_downloadable_reports():
    with open(os.path.join(ROOT, "standards", "ci", "gitlab-ci-security.yml"), encoding="utf-8") as f:
        c = yaml.safe_load(f)
    tpl = [i["template"] for i in c["include"]]
    assert "Jobs/SAST.gitlab-ci.yml" in tpl and "Jobs/Secret-Detection.gitlab-ci.yml" in tpl
    assert c["sast"]["artifacts"]["paths"] == ["gl-sast-report.json"]
    assert c["secret_detection"]["artifacts"]["paths"] == ["gl-secret-detection-report.json"]
    assert c["variables"]["AST_ENABLE_MR_PIPELINES"] == "true"


# ------------------------------------------------------------- security GL
class SecGL:
    def __init__(self):
        self.pipes = {"main": [{"id": 100, "status": "success", "sha": "s100", "web_url": "http://gl/p/100"}]}
        self.jobs = {100: [{"id": 1, "name": "semgrep-sast", "status": "success"},
                           {"id": 2, "name": "secret_detection", "status": "success"},
                           {"id": 3, "name": "go-test", "status": "success"}]}
        self.artifacts = {(1, "gl-sast-report.json"): sast_report(("SQL injection", "High", "a.go", 10, "g1"),
                                                                  ("Weak hash", "Medium", "h.go", 5, "g2")),
                          (2, "gl-secret-detection-report.json"): json.dumps({"vulnerabilities": []}).encode()}
        self.mr_pipes = {}
        self.calls = []

    def get_project(self, pid):
        return {"id": 7, "path_with_namespace": "idas/be", "web_url": "http://gl/idas/be", "default_branch": "main"}

    def pipelines(self, pid, ref=None, status=None, per_page=10, updated_after=None):
        return self.pipes.get(ref, [])

    def pipeline_jobs(self, pid, pipeline_id):
        return self.jobs.get(pipeline_id, [])

    def job_artifact(self, pid, job_id, path):
        self.calls.append((job_id, path))
        return self.artifacts.get((job_id, path))

    def mr_pipelines(self, pid, iid):
        return self.mr_pipes.get(iid, [])


def _sec(tmp_path, **over):
    store = Store(str(tmp_path / "db"))
    sent, events = [], []
    c = cfg(security={"projects": [7], **over})
    w = SecurityWatcher(c, SecGL(), store, lambda t, **k: sent.append(t), lambda *a, **k: events.append(a))
    return w, store, sent, events


def test_not_configured_project(tmp_path):
    w, store, sent, _ = _sec(tmp_path)
    w.gl.jobs = {100: [{"id": 3, "name": "go-test", "status": "success"}]}
    w.sync_all()
    p = store.query("SELECT * FROM sec_projects")[0]
    assert p["state"] == "not_configured" and "gitlab-ci-security.yml" in p["detail"] and not sent


def test_scan_baseline_then_new_and_fixed(tmp_path):
    w, store, sent, events = _sec(tmp_path)
    w.sync_all()
    rows = store.query("SELECT * FROM sec_findings WHERE status='open' ORDER BY severity")
    assert len(rows) == 2 and not sent, "scan pertama = baseline, tanpa notif"
    assert rows[0]["web_url"] == "http://gl/idas/be/-/blob/s100/a.go#L10"
    assert store.query("SELECT state FROM sec_projects")[0]["state"] == "ok"
    # same pipeline again: nothing re-downloaded
    n = len(w.gl.calls)
    w.sync_all()
    assert len(w.gl.calls) == n
    # new pipeline: SQL injection fixed, secret leaked, weak hash moved lines
    w.gl.pipes["main"].insert(0, {"id": 101, "status": "success", "sha": "s101", "web_url": "http://gl/p/101"})
    w.gl.jobs[101] = [{"id": 11, "name": "semgrep-sast", "status": "success"},
                      {"id": 12, "name": "secret_detection", "status": "success"}]
    w.gl.artifacts[(11, "gl-sast-report.json")] = sast_report(("Weak hash", "Medium", "h.go", 9, "g2"))
    w.gl.artifacts[(12, "gl-secret-detection-report.json")] = secret_report()
    w.sync_all()
    st = {r["name"]: r["status"] for r in store.query("SELECT name, status FROM sec_findings")}
    assert st == {"SQL injection": "fixed", "Weak hash": "open", "GitLab personal access token": "open"}
    assert len(sent) == 1 and "GitLab personal access token" in sent[0] and "Weak hash" not in sent[0]
    assert SECRET_VALUE not in sent[0] and SECRET_VALUE not in json.dumps(store.query("SELECT * FROM sec_findings"))
    assert "/keamanan" not in sent[0] and "idas/be" in sent[0]
    rep = w.report()
    assert "idas/be" in rep and "1 secret" in rep and "Diperbaiki 7 hari terakhir: 1" in rep


def test_missing_artifact_explains_fix(tmp_path):
    w, store, _, _ = _sec(tmp_path)
    w.gl.artifacts = {}
    w.sync_all()
    p = store.query("SELECT * FROM sec_projects")[0]
    assert p["state"] == "no_report" and "artifacts:paths" in p["detail"]


def test_failed_sast_job_keeps_other_kind_and_findings(tmp_path):
    w, store, _, _ = _sec(tmp_path)
    w.sync_all()
    w.gl.pipes["main"].insert(0, {"id": 102, "status": "failed", "sha": "s102"})
    w.gl.jobs[102] = [{"id": 21, "name": "semgrep-sast", "status": "failed"},
                      {"id": 22, "name": "secret_detection", "status": "success"}]
    w.gl.artifacts[(22, "gl-secret-detection-report.json")] = json.dumps({"vulnerabilities": []}).encode()
    w.sync_all()
    assert store.query("SELECT COUNT(*) n FROM sec_findings WHERE status='open'")[0]["n"] == 2, \
        "SAST yang gagal jalan tidak boleh menandai temuan SAST sebagai beres"
    p = store.query("SELECT * FROM sec_projects")[0]
    assert p["state"] == "partial" and "gagal" in p["detail"]


def test_mr_check_flags_new_findings_once(tmp_path):
    w, store, sent, events = _sec(tmp_path)
    w.sync_all()
    store.upsert("7:5", project_id=7, iid=5, sha="m1", status="notified", title="feat: x", web_url="http://gl/mr/5")
    w.gl.mr_pipes[5] = [{"id": 500, "status": "running", "sha": "m1"}]
    w.check_mrs()
    assert w.mr_result(7, 5) is None, "pipeline belum selesai"
    w.gl.mr_pipes[5] = [{"id": 500, "status": "success", "sha": "m1", "web_url": "http://gl/p/500"}]
    w.gl.jobs[500] = [{"id": 51, "name": "semgrep-sast", "status": "success"},
                      {"id": 52, "name": "secret_detection", "status": "success"}]
    w.gl.artifacts[(51, "gl-sast-report.json")] = sast_report(("SQL injection", "High", "a.go", 12, "g1"),
                                                              ("Weak hash", "Medium", "h.go", 5, "g2"),
                                                              ("Path traversal", "High", "d.go", 3, "g3"))
    w.gl.artifacts[(52, "gl-secret-detection-report.json")] = secret_report("cfg.yaml", 1)
    w.check_mrs()
    r = w.mr_result(7, 5, "m1")
    assert r and r["serious"] == 2 and r["secrets"] == 1
    assert [f["name"] for f in r["findings"]] == ["GitLab personal access token", "Path traversal"], \
        "temuan yang sudah ada di branch utama tidak dihitung sebagai baru"
    assert len(sent) == 1 and "!5" in sent[0] and "konfirmasi" in sent[0]
    w.check_mrs()
    assert len(sent) == 1, "satu pipeline dicek sekali"
    assert w.mr_result(7, 5, "other-sha") is None


# ----------------------------------------------------------------- DORA
def ts_iso(t):
    return D.iso(t)


class DoraGL:
    def __init__(self, now):
        self.now = now
        self.envs = [{"name": "staging", "tier": "staging"}, {"name": "prod-k8s", "tier": "production"}]
        t = now - 20 * DAY
        self.deps = [  # sha, finished
            {"id": 1, "sha": "a1", "finished_at": ts_iso(t)},
            {"id": 2, "sha": "a2", "finished_at": ts_iso(t + 2 * DAY)},
            {"id": 3, "sha": "a3", "finished_at": ts_iso(t + 4 * DAY)},     # bad
            {"id": 4, "sha": "a4", "finished_at": ts_iso(t + 4 * DAY + 3 * 3600)},  # hotfix
            {"id": 5, "sha": "a5", "finished_at": ts_iso(t + 10 * DAY)},    # bad -> rolled back
            {"id": 6, "sha": "a4", "finished_at": ts_iso(t + 10 * DAY + 3600)},  # rollback to a4
        ]
        self.commits = {
            ("a1", "a2"): [{"title": "feat: x", "committed_date": ts_iso(t + 1 * DAY), "parent_ids": ["p"]},
                           {"title": "Merge branch", "committed_date": ts_iso(t + 2 * DAY - 60), "parent_ids": ["p", "q"]}],
            ("a2", "a3"): [{"title": "feat: y", "committed_date": ts_iso(t + 3 * DAY), "parent_ids": ["p"]}],
            ("a3", "a4"): [{"title": "hotfix: null quota", "committed_date": ts_iso(t + 4 * DAY + 3600), "parent_ids": ["p"]}],
            ("a4", "a5"): [{"title": "feat: z", "committed_date": ts_iso(t + 8 * DAY), "parent_ids": ["p"]}],
        }
        self.issues_ = [{"iid": 9, "title": "Signing down", "created_at": ts_iso(t + 2 * DAY + 3600),
                         "closed_at": ts_iso(t + 2 * DAY + 3 * 3600), "web_url": "http://gl/i/9"}]
        self.compares = 0

    def get_project(self, pid):
        return {"id": 7, "path_with_namespace": "idas/be", "web_url": "http://gl/idas/be", "default_branch": "main"}

    def environments(self, pid):
        return self.envs

    def deployments(self, pid, env, since_iso):
        assert env == "prod-k8s"
        return [dict(d) for d in self.deps]

    def compare(self, pid, frm, to):
        self.compares += 1
        return {"commits": self.commits.get((frm, to), [])}

    def issues(self, pid, since_iso, **f):
        return self.issues_ if f.get("issue_type") == "incident" else []

    def pipelines(self, pid, ref=None, status=None, per_page=10, updated_after=None):
        return [{"id": 70 + n, "sha": f"p{n}", "updated_at": ts_iso(self.now - (10 - n) * DAY)} for n in range(5)]


def test_dora_from_deployments(tmp_path):
    now = time.time()
    store = Store(str(tmp_path / "db"))
    sent = []
    col = D.DoraCollector(cfg(dora={"projects": [7]}), DoraGL(now), store, sent.append)
    assert col.sync_project(7, now) == 6
    assert col.gl.compares == 4, "tidak ada compare untuk deployment pertama dan rollback"
    assert col.sync_project(7, now) == 0 and col.gl.compares == 4, "deployment lama tidak dihitung ulang"
    rows = {r["sha"] + str(r["rollback"]): r for r in store.query("SELECT * FROM dora_deploys")}
    assert rows["a40"]["fix_commit"] == 1 and rows["a41"]["rollback"] == 1
    assert rows["a20"]["n_commits"] == 1, "merge commit tidak dihitung"
    _, _, m, _ = col.data(30, now=now)
    assert m["deployments"] == 6 and m["failed"] == 3, "a2 (incident), a3 (hotfix), a5 (rollback)"
    assert round(m["cfr"], 2) == 0.5 and m["levels"]["cfr"] == "low"
    # lead times: 1d (a2), 1d (a3), 2h (a4), 2d (a5) -> median 1 day
    assert abs(m["lead_time"] - DAY) < 1 and m["levels"]["lead_time"] == "high"
    # recovery: incident 2h, hotfix 3h, rollback 1h -> median 2h
    assert abs(m["recovery"] - 2 * 3600) < 1 and m["levels"]["recovery"] == "high"
    assert abs(m["deploy_freq"] - 6 / 30) < 1e-9 and m["levels"]["deploy_freq"] == "high"
    rep = col.report(30, now)
    assert "6 deployment" in rep and "50%" in rep and "3/6" in rep
    src = json.loads(store.kv_get("dora_src:7"))
    assert src["source"] == "deployments" and "prod-k8s" in src["label"]
    w = D.weekly(store.query("SELECT * FROM dora_deploys"), store.query("SELECT * FROM dora_incidents"), 6, now)
    assert len(w) == 6 and sum(x["deployments"] for x in w) == 6


def test_dora_falls_back_to_branch_pipelines(tmp_path):
    now = time.time()
    store = Store(str(tmp_path / "db"))
    gl = DoraGL(now)
    gl.envs = [{"name": "staging", "tier": "staging"}]
    col = D.DoraCollector(cfg(dora={"projects": [7]}), gl, store)
    assert col.sync_project(7, now) == 5
    assert json.loads(store.kv_get("dora_src:7"))["label"] == "pipeline sukses di main"


def test_levels_and_formatting():
    assert D.level("deploy_freq", 2) == "elite" and D.level("deploy_freq", 0.2) == "high"
    assert D.level("deploy_freq", 0.05) == "medium" and D.level("deploy_freq", 0.01) == "low"
    assert D.level("lead_time", 3600) == "elite" and D.level("lead_time", 40 * DAY) == "low"
    assert D.level("cfr", 0.05) == "elite" and D.level("cfr", 0.2) == "high" and D.level("cfr", 0.45) == "low"
    assert D.level("recovery", None) is None
    assert D.human_dur(90) == "2 mnt" and D.human_dur(5400) == "1.5 jam" and D.human_dur(3 * DAY) == "3 hari"
    assert D.human_freq(2) == "2/hari" and D.human_freq(0.5) == "3.5/minggu"
    assert 498 <= D._sample(list(range(1000)))[200] <= 502 and len(D._sample(list(range(1000)))) == 400


def test_weekly_report_once_on_monday(tmp_path):
    store = Store(str(tmp_path / "db"))
    sent = []
    col = D.DoraCollector(cfg(dora={"projects": [7]}), DoraGL(time.time()), store, sent.append)
    monday = time.mktime((2026, 10, 12, 9, 0, 0, 0, 0, -1))
    col.sync_project(7, monday)
    assert not col.weekly_report(monday - 3 * 3600), "sebelum jam digest"
    assert col.weekly_report(monday) and "Laporan mingguan DORA" in sent[-1]
    assert not col.weekly_report(monday + 3600), "sekali per minggu"
    assert not col.weekly_report(monday + DAY), "Selasa tidak"


def test_config_validation():
    c = cfg(dora={"source": "jira"})
    with pytest.raises(ConfigError):
        validate(_valid(c))
    c = cfg(dora={"fix_pattern": "(unclosed"})
    with pytest.raises(ConfigError):
        validate(_valid(c))
    vc = _valid(cfg(security={"enabled": "false", "alert_min_severity": "CRITICAL"}, dora={"incident_labels": "incident, sev1"}))
    validate(vc)
    assert vc["security"]["enabled"] is False and vc["security"]["alert_min_severity"] == "critical"
    assert vc["dora"]["incident_labels"] == ["incident", "sev1"]


def _valid(c):
    c["telegram"]["chat_id"] = 1
    c["gitlab"]["url"] = "https://gl.example"
    return c


def test_dashboard_insights(tmp_path):
    from mr_pilot.demo import seed
    store = Store(str(tmp_path / "db"))
    seed(store)
    s = insights.security_data(cfg(), store)
    assert s["kpi"]["open"] > 0 and s["kpi"]["secrets"] >= 1 and s["mrs"] and "Jobs/SAST" in s["ci_snippet"]
    assert len(s["trend"]) == 30 and {p["state"] for p in s["projects"]} >= {"ok", "not_configured"}
    d = insights.dora_data(cfg(), store, 30)
    assert d["current"]["deployments"] > 0 and len(d["projects"]) == 3 and d["weekly"]
    assert "failed_keys" not in d["current"] and all("failed_keys" not in p for p in d["projects"])
    one = insights.dora_data(cfg(), store, 90, project=42)
    assert {x["project"] for x in one["deployments"]} == {"idas/idas-repo-be"}
    json.dumps(s), json.dumps(d)
