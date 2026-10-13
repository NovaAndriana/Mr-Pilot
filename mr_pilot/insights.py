"""Data for the separate Security and DORA dashboards (read from SQLite only; collectors fill it)."""
import json
import os
import time
from collections import Counter
from datetime import datetime, timedelta

from . import dora as D
from .security import SEV_ORDER

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECURITY_CI = os.path.join("standards", "ci", "gitlab-ci-security.yml")


def _days(n):
    today = datetime.now().date()
    return [(today - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def _day(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def ci_snippet(cfg):
    for base in (cfg.get("_base_dir") or ".", PKG_ROOT):
        p = os.path.join(base, SECURITY_CI)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return f.read()
    return ""


def security_data(cfg, store, project=None, days=30):
    now = time.time()
    where, args = "", ()
    if project:
        where, args = " AND project_id=?", (int(project),)
    open_rows = store.query(f"SELECT * FROM sec_findings WHERE status='open'{where}", args)
    open_rows.sort(key=lambda r: (r["kind"] != "secret", SEV_ORDER.get(r["severity"], 9), r["project"] or "",
                                  r["file"] or "", r["line"] or 0))
    since = now - days * 86400
    opened = store.query(f"SELECT first_seen FROM sec_findings WHERE first_seen>=?{where}", (since,) + args)
    fixed = store.query(f"SELECT fixed_at, first_seen FROM sec_findings WHERE status='fixed' AND fixed_at>=?{where}",
                        (since,) + args)
    days_l = _days(days)
    o, f = Counter(_day(r["first_seen"]) for r in opened), Counter(_day(r["fixed_at"]) for r in fixed)
    trend = [{"date": d, "opened": o.get(d, 0), "fixed": f.get(d, 0)} for d in days_l]
    sev = Counter(r["severity"] for r in open_rows)
    fix_times = sorted(r["fixed_at"] - r["first_seen"] for r in fixed if r["first_seen"])
    projects = store.query("SELECT * FROM sec_projects ORDER BY project")
    per = {}
    for r in store.query("SELECT project_id, kind, severity, COUNT(*) n FROM sec_findings WHERE status='open' "
                         "GROUP BY project_id, kind, severity"):
        p = per.setdefault(r["project_id"], {"critical": 0, "high": 0, "medium": 0, "low": 0, "other": 0, "secret": 0})
        if r["kind"] == "secret":
            p["secret"] += r["n"]
        p[r["severity"] if r["severity"] in p else "other"] += r["n"]
    for p in projects:
        p["open"] = per.get(p["project_id"], {"critical": 0, "high": 0, "medium": 0, "low": 0, "other": 0, "secret": 0})
    mrs = []
    for rec in store.query("SELECT key, project_id, iid, title, web_url, sha, project FROM mrs WHERE status='notified'"):
        raw = store.kv_get(f"sec_mr_new:{rec['project_id']}:{rec['iid']}")
        if not raw:
            continue
        try:
            res = json.loads(raw)
        except ValueError:
            continue
        if res.get("sha") and rec.get("sha") and res["sha"] != rec["sha"]:
            continue
        mrs.append({**rec, **{k: res.get(k) for k in ("summary", "secrets", "serious", "findings", "pipeline_url",
                                                      "checked_at")}})
    mrs.sort(key=lambda m: (-(m.get("serious") or 0), -len(m.get("findings") or [])))
    return {
        "enabled": bool((cfg.get("security") or {}).get("enabled", True)),
        "kpi": {"open": len(open_rows), "critical": sev.get("critical", 0), "high": sev.get("high", 0),
                "medium": sev.get("medium", 0), "low": sev.get("low", 0) + sev.get("info", 0) + sev.get("unknown", 0),
                "secrets": sum(1 for r in open_rows if r["kind"] == "secret"),
                "opened": len(opened), "fixed": len(fixed),
                "median_fix_hours": round(fix_times[len(fix_times) // 2] / 3600, 1) if fix_times else None,
                "projects": len(projects),
                "configured": sum(1 for p in projects if p["state"] in ("ok", "partial"))},
        "projects": projects,
        "findings": open_rows[:500],
        "trend": trend,
        "mrs": mrs,
        "last_sync": max([p["checked_at"] or 0 for p in projects] or [0]) or None,
        "min_severity": (cfg.get("security") or {}).get("alert_min_severity", "high"),
        "ci_snippet": ci_snippet(cfg),
        "days": days,
    }


def dora_data(cfg, store, days=30, project=None, now=None):
    now = now or time.time()
    q, qi, args = "SELECT * FROM dora_deploys", "SELECT * FROM dora_incidents", ()
    if project:
        q += " WHERE project_id=?"
        qi += " WHERE project_id=?"
        args = (int(project),)
    deploys, incidents = store.query(q, args), store.query(qi, args)
    cur = D.compute(deploys, incidents, now - days * 86400, now, now)
    prev = D.compute(deploys, incidents, now - 2 * days * 86400, now - days * 86400, now)
    weeks = max(4, min(26, days // 7 + 4))
    all_deploys = store.query("SELECT * FROM dora_deploys")
    all_inc = store.query("SELECT * FROM dora_incidents")
    sources = []
    for pid in sorted({d["project_id"] for d in all_deploys} | {
            int(r["k"].split(":")[1]) for r in store.query("SELECT k FROM kv WHERE k LIKE 'dora_src:%'")}):
        try:
            s = json.loads(store.kv_get(f"dora_src:{pid}") or "{}")
        except ValueError:
            s = {}
        sources.append({"project_id": pid, "project": s.get("project") or str(pid), "source": s.get("source"),
                        "label": s.get("label"), "at": s.get("at")})
    errors = [{"project": r["k"].split(":", 1)[1], "error": r["v"]}
              for r in store.query("SELECT k, v FROM kv WHERE k LIKE 'dora_err:%'")]
    fail_reason = cur["failed_reasons"]
    recent = sorted([d for d in deploys if d["finished_at"] >= now - days * 86400],
                    key=lambda d: -d["finished_at"])[:60]
    recent = [{"project": d["project"], "finished_at": d["finished_at"], "sha": (d["sha"] or "")[:8],
               "title": d["title"], "web_url": d["web_url"], "n_commits": d["n_commits"],
               "lead_median": d["lead_median"], "failed": fail_reason.get(d["key"]),
               "fix": bool(d["fix_commit"]), "rollback": bool(d["rollback"])} for d in recent]
    for k in ("failed_keys", "failed_reasons"):
        cur.pop(k, None)
        prev.pop(k, None)
    return {
        "enabled": bool((cfg.get("dora") or {}).get("enabled", True)),
        "days": days, "current": cur, "previous": prev,
        "weekly": D.weekly(deploys, incidents, weeks, now),
        "projects": [{k: v for k, v in r.items() if k not in ("failed_keys", "failed_reasons")}
                     for r in D.project_rows(all_deploys, all_inc, days, now)],
        "project_list": [{"project_id": s["project_id"], "project": s["project"]} for s in sources],
        "sources": sources, "errors": errors,
        "deployments": recent,
        "incidents": sorted([i for i in incidents if i["created_at"] >= now - days * 86400],
                            key=lambda i: -i["created_at"])[:30],
        "thresholds": {k: [[n, v] for n, v in t] for k, t in D.THRESHOLDS.items()},
        "config": {k: (cfg.get("dora") or {}).get(k) for k in ("source", "environment", "production_branch",
                                                                "incident_labels", "lookback_days")},
    }
