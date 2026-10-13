"""DORA metrics (delivery performance) computed from GitLab data, without needing GitLab Ultimate.

- Deployment frequency: successful deployments to production (GitLab environment with tier/name production),
  or, when the team doesn't use environments, successful pipelines on the production branch / merged MRs.
- Lead time for changes: commit -> running in production. Commits of a deployment = compare(previous, this).
- Change failure rate: share of deployments that needed remediation: the next deployment is a hotfix/revert/
  rollback, or an incident (issue type incident or a configured label) was opened after it.
- Failed deployment recovery time: incident opened -> closed, or bad deployment -> the deployment that fixed it.
All read-only. Per deployment results are cached in SQLite, so each sync only asks GitLab for new ones."""
import json
import logging
import re
import statistics
import time
from datetime import datetime, timezone

from .security import project_info, watched_projects
from .util import short_error

log = logging.getLogger("mr_pilot.dora")

DAY = 86400
LEVEL_LABEL = {"elite": "Elite", "high": "High", "medium": "Medium", "low": "Low", None: "–"}
LEVEL_ICON = {"elite": "🟢", "high": "🟢", "medium": "🟡", "low": "🔴", None: "⚪"}
# Bands follow the DORA 2024 clusters. Change failure rate there is not monotonic (high 20%, medium 10%),
# so medium uses <= 30% here; recovery "medium" uses < 1 week (between high's 1 day and low's 1 week+).
THRESHOLDS = {
    "deploy_freq": [("elite", 1.0), ("high", 1 / 7), ("medium", 1 / 30)],        # per day, higher is better
    "lead_time": [("elite", DAY), ("high", 7 * DAY), ("medium", 30 * DAY)],      # seconds, lower is better
    "cfr": [("elite", 0.05), ("high", 0.20), ("medium", 0.30)],                  # ratio, lower is better
    "recovery": [("elite", 3600), ("high", DAY), ("medium", 7 * DAY)],           # seconds, lower is better
}
DEFAULT_FIX = r"(?i)\b(hotfix|hot-fix|revert|rollback|roll back)\b"


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def level(metric, value):
    if value is None:
        return None
    for name, limit in THRESHOLDS[metric]:
        if (value >= limit) if metric == "deploy_freq" else (value <= limit if metric == "cfr" else value < limit):
            return name
    return "low"


def median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _sample(values, n=400):
    """Keep at most n values spread evenly over the sorted list (the median stays the same)."""
    v = sorted(round(x) for x in values)
    if len(v) <= n:
        return v
    return [v[round(i * (len(v) - 1) / (n - 1))] for i in range(n)]


def human_dur(sec):
    if sec is None:
        return "–"
    if sec < 3600:
        return f"{max(1, round(sec / 60))} mnt"
    if sec < 2 * DAY:
        return f"{sec / 3600:.1f} jam".replace(".0 jam", " jam")
    return f"{sec / DAY:.1f} hari".replace(".0 hari", " hari")


def human_freq(per_day):
    if per_day is None:
        return "–"
    if per_day >= 1:
        return f"{per_day:.1f}/hari".replace(".0/", "/")
    if per_day * 7 >= 1:
        return f"{per_day * 7:.1f}/minggu".replace(".0/", "/")
    return f"{per_day * 30:.1f}/bulan".replace(".0/", "/")


# ------------------------------------------------------------------ compute
def compute(deploys, incidents, start, end, now=None):
    """deploys: rows of dora_deploys (any order, may span more than the window); incidents: dora_incidents rows.
    Returns the four metrics for deployments finished in [start, end)."""
    now = now or time.time()
    by_proj = {}
    for d in sorted(deploys, key=lambda r: r["finished_at"]):
        by_proj.setdefault(d["project_id"], []).append(d)
    inc_by_proj = {}
    for i in incidents:
        inc_by_proj.setdefault(i["project_id"], []).append(i)

    in_window, failed, recovery, lead_values = [], {}, [], []
    for pid, ds in by_proj.items():
        for n, d in enumerate(ds):
            nxt = ds[n + 1] if n + 1 < len(ds) else None
            reason = None
            if nxt and (nxt.get("fix_commit") or nxt.get("rollback")):
                reason = "rollback" if nxt.get("rollback") else "hotfix/revert"
                if start <= d["finished_at"] < end:
                    recovery.append(("deploy", nxt["finished_at"] - d["finished_at"]))
            if reason:
                failed[d["key"]] = reason
        for i in inc_by_proj.get(pid, []):
            prev = [d for d in ds if d["finished_at"] <= i["created_at"]]
            if prev and i["created_at"] - prev[-1]["finished_at"] <= 7 * DAY:
                failed.setdefault(prev[-1]["key"], "incident")
            if start <= i["created_at"] < end and i.get("closed_at"):
                recovery.append(("incident", i["closed_at"] - i["created_at"]))
        for d in ds:
            if start <= d["finished_at"] < end:
                in_window.append(d)
                try:
                    lead_values += json.loads(d.get("lead_values") or "[]")
                except ValueError:
                    pass
    days = max(1.0, (min(end, now) - start) / DAY)
    n = len(in_window)
    n_failed = sum(1 for d in in_window if d["key"] in failed)
    freq = n / days if n else 0.0
    lt = median(lead_values)
    cfr = (n_failed / n) if n else None
    rec = median([r for _, r in recovery])
    open_inc = sum(1 for lst in inc_by_proj.values() for i in lst if not i.get("closed_at") and i["created_at"] >= start)
    return {
        "deployments": n, "days": round(days, 1), "failed": n_failed, "open_incidents": open_inc,
        "deploy_freq": freq, "lead_time": lt, "cfr": cfr, "recovery": rec,
        "levels": {"deploy_freq": level("deploy_freq", freq) if n else "low", "lead_time": level("lead_time", lt),
                   "cfr": level("cfr", cfr), "recovery": level("recovery", rec)},
        "failed_keys": [d["key"] for d in in_window if d["key"] in failed],
        "failed_reasons": {d["key"]: failed[d["key"]] for d in in_window if d["key"] in failed},
        "commits": len(lead_values),
    }


def weekly(deploys, incidents, weeks=12, now=None):
    """Trend per week (Monday-start, local time), oldest first."""
    now = now or time.time()
    lt = time.localtime(now)
    monday = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)) - lt.tm_wday * DAY
    out = []
    for k in range(weeks - 1, -1, -1):
        s = monday - k * 7 * DAY
        m = compute(deploys, incidents, s, s + 7 * DAY, now)
        out.append({"date": time.strftime("%Y-%m-%d", time.localtime(s)), "deployments": m["deployments"],
                    "lead_hours": round(m["lead_time"] / 3600, 1) if m["lead_time"] is not None else None,
                    "cfr_pct": round(m["cfr"] * 100, 1) if m["cfr"] is not None else None,
                    "recovery_hours": round(m["recovery"] / 3600, 1) if m["recovery"] is not None else None,
                    "failed": m["failed"]})
    return out


# ---------------------------------------------------------------- collector
class DoraCollector:
    def __init__(self, cfg, gl, store, send=None, event=None):
        self.cfg = cfg.get("dora") or {}
        self.root = cfg
        self.enabled = bool(self.cfg.get("enabled", True))
        self.gl, self.store = gl, store
        self.send = send or (lambda *a, **k: None)
        self.event = event or (lambda *a, **k: None)
        self.fix_rx = re.compile(self.cfg.get("fix_pattern") or DEFAULT_FIX)
        self._last = 0.0

    def tick(self, now=None, force=False):
        if not self.enabled:
            return
        now = now or time.time()
        if self.store.kv_get("dora_sync_request"):
            self.store.kv_del("dora_sync_request")
            force = True
        if force or now - self._last >= int(self.cfg.get("interval_minutes", 60)) * 60:
            self._last = now
            self.sync_all()
        self.weekly_report(now)

    def sync_all(self):
        for pid in watched_projects(self.cfg, self.root, self.store, "dora_deploys"):
            try:
                self.sync_project(pid)
            except Exception as ex:
                log.warning("DORA %s gagal: %s", pid, short_error(ex))
                self.store.kv_set(f"dora_err:{pid}", f"{time.strftime('%d/%m %H:%M')} {short_error(ex)}")

    # ------------------------------------------------------------- sources
    def _prod_env(self, pid):
        want = str(self.cfg.get("environment") or "").strip()
        envs = self.gl.environments(pid)
        names = [x.get("name") for x in envs]
        if want and want != "auto":
            return want if want in names else None
        for x in envs:
            if x.get("tier") == "production":
                return x["name"]
        for x in envs:
            if str(x.get("name", "")).lower() in ("production", "prod"):
                return x["name"]
        return None

    def _fetch(self, pid, info, since):
        """-> (source, label, [ {id, sha, ref, finished_at, web_url, title} ] oldest first)"""
        src = str(self.cfg.get("source") or "auto").lower()
        if src in ("auto", "deployments"):
            env = self._prod_env(pid)
            if env:
                deps = []
                for d in self.gl.deployments(pid, env, iso(since)):
                    fin = parse_ts(d.get("finished_at") or (d.get("deployable") or {}).get("finished_at")
                                   or d.get("updated_at"))
                    if fin and d.get("sha"):
                        deps.append({"id": d["id"], "sha": d["sha"], "ref": d.get("ref"), "finished_at": fin,
                                     "web_url": (d.get("deployable") or {}).get("web_url") or "",
                                     "title": (d.get("deployable") or {}).get("name") or ""})
                if deps or src == "deployments":
                    return "deployments", f"environment {env}", deps
            elif src == "deployments":
                return "deployments", "environment production tidak ditemukan", []
        branch = self.cfg.get("production_branch") or info["default_branch"]
        if src == "merges":
            out = []
            for m in self.gl.merged_mrs(pid, branch, iso(since)):
                fin = parse_ts(m.get("merged_at"))
                sha = m.get("merge_commit_sha") or m.get("squash_commit_sha") or m.get("sha")
                if fin and sha and fin >= since:
                    out.append({"id": m["iid"], "sha": sha, "ref": branch, "finished_at": fin,
                                "web_url": m.get("web_url") or "", "title": m.get("title") or "",
                                "mr_iid": m["iid"]})
            return "merges", f"MR merged ke {branch}", sorted(out, key=lambda x: x["finished_at"])
        pipes = self.gl.pipelines(pid, ref=branch, status="success", per_page=100, updated_after=iso(since))
        out = []
        for p in pipes:
            fin = parse_ts(p.get("updated_at") or p.get("created_at"))
            if fin and p.get("sha"):
                out.append({"id": p["id"], "sha": p["sha"], "ref": branch, "finished_at": fin,
                            "web_url": p.get("web_url") or "", "title": f"pipeline #{p['id']}"})
        return "pipelines", f"pipeline sukses di {branch}", sorted(out, key=lambda x: x["finished_at"])

    def sync_project(self, pid, now=None):
        now = now or time.time()
        info = project_info(self.gl, self.store, pid)
        real = info["id"]
        lookback = int(self.cfg.get("lookback_days", 90))
        since = now - (lookback + 14) * DAY  # a little extra so the first deployment in the window has a predecessor
        self._sync_incidents(pid, real, info, since)  # first: a reader never sees deployments without them
        source, label, deps = self._fetch(pid, info, since)
        self.store.kv_set(f"dora_src:{real}", json.dumps({"source": source, "label": label, "at": now,
                                                          "project": info["path"]}))
        self.store.kv_del(f"dora_err:{pid}")
        known = {r["key"] for r in self.store.query("SELECT key FROM dora_deploys WHERE project_id=?", (real,))}
        seen_shas = [r["sha"] for r in self.store.query(
            "SELECT sha FROM dora_deploys WHERE project_id=? AND source=? ORDER BY finished_at", (real, source))]
        added = 0
        prev = None
        for d in deps:
            key = f"{real}:{source}:{d['id']}"
            if key in known:
                prev = d
                if d["sha"] not in seen_shas:
                    seen_shas.append(d["sha"])
                continue
            lead, n_commits, fix = [], None, bool(self.fix_rx.search(d.get("title") or ""))
            rollback = bool(prev and d["sha"] != prev["sha"] and d["sha"] in seen_shas)
            if source == "merges":
                commits = self.gl.get_commits(pid, d["mr_iid"])
            elif prev and prev["sha"] != d["sha"] and not rollback:
                commits = (self.gl.compare(pid, prev["sha"], d["sha"]) or {}).get("commits") or []
            else:
                commits = []
            if prev is not None or source == "merges":
                n_commits = 0
                for c in commits[:1000]:
                    if len(c.get("parent_ids") or []) > 1:
                        continue  # merge commits carry the merge time, not when the change was written
                    n_commits += 1
                    t = parse_ts(c.get("committed_date") or c.get("created_at"))
                    if t:
                        lead.append(max(0.0, d["finished_at"] - t))
                    if self.fix_rx.search(c.get("title") or ""):
                        fix = True
            self.store.execute(
                "INSERT OR REPLACE INTO dora_deploys(key, project_id, project, env, source, sha, ref, finished_at, "
                "n_commits, lead_median, lead_values, fix_commit, rollback, web_url, title) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, real, info["path"], label, source, d["sha"], d.get("ref"), d["finished_at"], n_commits,
                 median(lead), json.dumps(_sample(lead)), int(fix), int(rollback),
                 d.get("web_url"), (d.get("title") or "")[:200]))
            seen_shas.append(d["sha"])
            prev = d
            added += 1
        if added:
            self.event("dora", f"DORA {info['path']}: {added} deployment baru", label)
        return added

    def _sync_incidents(self, pid, real, info, since):
        found = {}
        try:
            for i in self.gl.issues(pid, iso(since), issue_type="incident"):
                found[i["iid"]] = i
        except Exception as ex:  # older GitLab without issue_type filter
            log.debug("incident filter: %s", short_error(ex))
        for lab in self.cfg.get("incident_labels") or []:
            for i in self.gl.issues(pid, iso(since), labels=lab):
                found[i["iid"]] = i
        rows = []
        for iid, i in found.items():
            c = parse_ts(i.get("created_at"))
            if not c:
                continue
            rows.append((f"{real}:{iid}", real, info["path"], (i.get("title") or "")[:200], c,
                         parse_ts(i.get("closed_at")), i.get("web_url") or ""))
        if rows:
            self.store.execute("INSERT OR REPLACE INTO dora_incidents(key, project_id, project, title, created_at, "
                               "closed_at, web_url) VALUES(?,?,?,?,?,?,?)", rows, many=True)

    # -------------------------------------------------------------- reports
    def data(self, days=30, project=None, now=None):
        now = now or time.time()
        q, args = "SELECT * FROM dora_deploys", ()
        qi = "SELECT * FROM dora_incidents"
        if project:
            q += " WHERE project_id=?"
            qi += " WHERE project_id=?"
            args = (int(project),)
        deploys, incidents = self.store.query(q, args), self.store.query(qi, args)
        cur = compute(deploys, incidents, now - days * DAY, now, now)
        prev = compute(deploys, incidents, now - 2 * days * DAY, now - days * DAY, now)
        return deploys, incidents, cur, prev

    def report(self, days=30, now=None):
        _, _, m, p = self.data(days, now=now)
        if not m["deployments"] and not p["deployments"]:
            return ("📈 <b>DORA</b>\nBelum ada data deployment. MR Pilot membaca deployment ke environment "
                    "production, atau pipeline sukses di branch produksi (<code>dora.production_branch</code>).")

        def delta(cur, old, better_low=True, fmt=human_dur):
            if cur is None or old is None or old == cur:
                return ""
            up = cur > old
            good = (not up) if better_low else up
            return f" ({'↑' if up else '↓'} dari {fmt(old)}, {'membaik' if good else 'memburuk'})"
        lv = m["levels"]
        cfr = "–" if m["cfr"] is None else f"{m['cfr'] * 100:.0f}%"
        lines = [f"📈 <b>DORA {days} hari terakhir</b> ({m['deployments']} deployment)",
                 f"{LEVEL_ICON[lv['deploy_freq']]} Deployment frequency: <b>{human_freq(m['deploy_freq'])}</b> · "
                 f"{LEVEL_LABEL[lv['deploy_freq']]}{delta(m['deploy_freq'], p['deploy_freq'] or None, False, human_freq)}",
                 f"{LEVEL_ICON[lv['lead_time']]} Lead time for changes: <b>{human_dur(m['lead_time'])}</b> · "
                 f"{LEVEL_LABEL[lv['lead_time']]}{delta(m['lead_time'], p['lead_time'])}",
                 f"{LEVEL_ICON[lv['cfr']]} Change failure rate: <b>{cfr}</b> "
                 f"({m['failed']}/{m['deployments']}) · {LEVEL_LABEL[lv['cfr']]}",
                 f"{LEVEL_ICON[lv['recovery']]} Waktu pemulihan: <b>{human_dur(m['recovery'])}</b> · "
                 f"{LEVEL_LABEL[lv['recovery']]}"]
        if m["open_incidents"]:
            lines.append(f"⚠️ {m['open_incidents']} incident masih terbuka.")
        lines.append("Detail per project dan tren mingguan di dashboard → DORA.")
        return "\n".join(lines)

    def weekly_report(self, now=None):
        if not self.cfg.get("weekly_report", True):
            return False
        now = now or time.time()
        lt = time.localtime(now)
        hh, mm = (int(x) for x in str((self.root.get("digest") or {}).get("time") or "08:30").split(":")[:2])
        if lt.tm_wday != 0 or (lt.tm_hour, lt.tm_min) < (hh, mm):
            return False
        week = time.strftime("%G-%V", lt)
        if self.store.kv_get("dora_weekly") == week:
            return False
        self.store.kv_set("dora_weekly", week)
        if not self.store.query("SELECT 1 FROM dora_deploys LIMIT 1"):
            return False
        self.send(self.report(30, now).replace("📈 <b>DORA", "📈 <b>Laporan mingguan DORA", 1))
        return True


def project_rows(deploys, incidents, days, now=None):
    """Per-project metrics for the dashboard table."""
    now = now or time.time()
    out = []
    for pid in sorted({d["project_id"] for d in deploys}):
        ds = [d for d in deploys if d["project_id"] == pid]
        m = compute(ds, [i for i in incidents if i["project_id"] == pid], now - days * DAY, now, now)
        out.append({"project_id": pid, "project": ds[-1]["project"], "source": ds[-1]["env"], **m})
    return out

