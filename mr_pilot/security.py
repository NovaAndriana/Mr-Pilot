"""GitLab SAST + Secret Detection results, without needing GitLab Ultimate.

The scanners run in the team's own pipelines (see standards/ci/gitlab-ci-security.yml). MR Pilot only reads:
it downloads the JSON reports from the jobs of the latest pipeline on each project's main branch, keeps
the open findings (and when they were fixed), alerts on new serious ones, and checks every MR waiting
for Nova's decision for findings the MR itself introduces. Raw secret values are never stored or shown."""
import hashlib
import json
import logging
import re
import time
from collections import Counter

from .formatting import e
from .util import redact, short_error

log = logging.getLogger("mr_pilot.security")

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4, "unknown": 5}
SEV_LABEL = {"critical": "Critical", "high": "High", "medium": "Medium", "low": "Low", "info": "Info",
             "unknown": "Unknown"}
KINDS = {  # kind -> (report file in the job artifacts, job-name pattern)
    "sast": ("gl-sast-report.json", re.compile(r"sast", re.I)),
    "secret": ("gl-secret-detection-report.json", re.compile(r"secret", re.I)),
}
KIND_LABEL = {"sast": "SAST", "secret": "Secret"}
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-+/=]{20,}")
_CONFIG_HINT = "standards/ci/gitlab-ci-security.yml"


def _norm_sev(s):
    s = str(s or "unknown").strip().lower()
    return s if s in SEV_ORDER else "unknown"


def _scrub(text, secret=False):
    t = redact(text or "")
    if secret:  # a secret report may quote the leaked value; keep only the shape of it
        t = _LONG_TOKEN.sub(lambda m: m.group(0)[:4] + "…", t)
    return t


def parse_report(raw, kind):
    """GitLab security report JSON (bytes/str) -> list of normalised findings. Secrets are masked."""
    data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
    scan = data.get("scan") or {}
    default_scanner = ((scan.get("analyzer") or scan.get("scanner") or {}).get("name")) or ""
    secret = kind == "secret"
    out = []
    for v in data.get("vulnerabilities") or []:
        if not isinstance(v, dict):
            continue
        loc = v.get("location") or {}
        idents = [i for i in (v.get("identifiers") or []) if isinstance(i, dict)]
        primary = idents[0] if idents else {}
        url = next((i.get("url") for i in idents if i.get("url")), "")
        name = v.get("name") or v.get("message") or primary.get("name") or "Temuan keamanan"
        try:
            line = int(loc.get("start_line")) if loc.get("start_line") not in (None, "") else None
        except (TypeError, ValueError):
            line = None
        out.append({
            "kind": kind,
            "severity": _norm_sev(v.get("severity")),
            "name": _scrub(name, secret)[:200],
            "description": _scrub(v.get("description") or "", secret)[:1200],
            "file": str(loc.get("file") or "")[:400],
            "line": line,
            "rule": str(primary.get("value") or primary.get("name") or "")[:120],
            "rule_url": str(url)[:400],
            "scanner": str((v.get("scanner") or {}).get("name") or default_scanner)[:60],
        })
    return out


def project_info(gl, store, pid):
    """{id, path, web_url, default_branch}, cached for a day (also under the numeric id)."""
    k = f"proj:{pid}"
    cached = store.kv_get(k)
    if cached:
        try:
            d = json.loads(cached)
            if time.time() - d.get("_at", 0) < 86400:
                return d
        except ValueError:
            pass
    p = gl.get_project(pid)
    d = {"id": p["id"], "path": p.get("path_with_namespace") or str(pid), "web_url": p.get("web_url") or "",
         "default_branch": p.get("default_branch") or "main", "_at": time.time()}
    store.kv_set(k, json.dumps(d))
    if d["id"] != pid:  # configured as "group/repo": also cache under the numeric id
        store.kv_set(f"proj:{d['id']}", json.dumps(d))
    return d


def watched_projects(cfg_section, root_cfg, store, extra_table=None):
    """Configured projects, else projects of MRs MR Pilot has seen recently (+ ones already tracked)."""
    conf = cfg_section.get("projects") or root_cfg["gitlab"].get("projects") or []
    if conf:
        return list(dict.fromkeys(conf))
    since = time.time() - int(cfg_section.get("auto_project_days", 120)) * 86400
    rows = store.query("SELECT DISTINCT project_id FROM mrs WHERE updated_at >= ? AND project_id IS NOT NULL", (since,))
    ids = [r["project_id"] for r in rows]
    if extra_table:
        ids += [r["project_id"] for r in store.query(f"SELECT DISTINCT project_id FROM {extra_table}")]
    return list(dict.fromkeys(ids))


def loose_key(f):
    """Identity that survives line shifts: what was found, and in which file."""
    return f"{f['kind']}|{f['rule']}|{f['file']}|{f['name']}"


def fingerprint(project_id, findings):
    """Stable id per finding: loose key + occurrence number within the same file (ordered by line)."""
    seen = Counter()
    for f in sorted(findings, key=lambda x: (loose_key(x), x.get("line") or 0)):
        k = loose_key(f)
        seen[k] += 1
        f["fp"] = hashlib.sha1(f"{project_id}|{k}|{seen[k]}".encode()).hexdigest()[:20]
    return findings


def is_serious(f, min_sev="high"):
    return f["kind"] == "secret" or SEV_ORDER[f["severity"]] <= SEV_ORDER.get(min_sev, 1)


def summarize(findings):
    c = Counter(f["severity"] for f in findings)
    secrets = sum(1 for f in findings if f["kind"] == "secret")
    parts = [f"{secrets} secret"] if secrets else []
    parts += [f"{c[s]} {SEV_LABEL[s].lower()}" for s in ("critical", "high", "medium", "low") if c[s]]
    return ", ".join(parts) or f"{len(findings)} temuan"


class SecurityWatcher:
    def __init__(self, cfg, gl, store, send=None, event=None):
        self.cfg = cfg.get("security") or {}
        self.root = cfg
        self.enabled = bool(self.cfg.get("enabled", True))
        self.gl, self.store = gl, store
        self.send = send or (lambda *a, **k: None)
        self.event = event or (lambda *a, **k: None)
        self.min_sev = str(self.cfg.get("alert_min_severity", "high")).lower()
        self._last_scan = 0.0
        self._last_mr = 0.0

    # ------------------------------------------------------------ scheduling
    def tick(self, now=None, force=False):
        """Called about once a minute from the main loop."""
        if not self.enabled:
            return
        now = now or time.time()
        if force or self.store.kv_get("sec_sync_request"):
            self.store.kv_del("sec_sync_request")
            force = True
        if force or now - self._last_scan >= int(self.cfg.get("interval_minutes", 30)) * 60:
            self._last_scan = now
            self.sync_all()
        if self.cfg.get("mr_check", True) and (force or now - self._last_mr >= 180):
            self._last_mr = now
            self.check_mrs()

    def project_ids(self):
        return watched_projects(self.cfg, self.root, self.store, "sec_projects")

    def project_info(self, pid):
        return project_info(self.gl, self.store, pid)

    def sync_all(self):
        done = 0
        for pid in self.project_ids():
            try:
                self.sync_project(pid)
                done += 1
            except Exception as ex:
                log.warning("scan keamanan %s gagal: %s", pid, short_error(ex))
                self._set_project(pid, None, None, "error", f"Gagal membaca dari GitLab: {short_error(ex)}")
        return done

    # ------------------------------------------------------------- projects
    def _set_project(self, pid, info, ref, state, detail, pipeline=None, scanned=False):
        now = time.time()
        if isinstance(pid, str) and info:
            pid = info["id"]
        if not isinstance(pid, int):
            return
        old = (self.store.query("SELECT * FROM sec_projects WHERE project_id=?", (pid,)) or [{}])[0]
        self.store.execute(
            "INSERT INTO sec_projects(project_id, project, web_url, ref, state, detail, last_scan, pipeline_id, "
            "pipeline_url, checked_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET "
            "project=excluded.project, web_url=excluded.web_url, ref=excluded.ref, state=excluded.state, "
            "detail=excluded.detail, last_scan=excluded.last_scan, pipeline_id=excluded.pipeline_id, "
            "pipeline_url=excluded.pipeline_url, checked_at=excluded.checked_at",
            (pid, (info or {}).get("path") or old.get("project") or str(pid), (info or {}).get("web_url") or old.get("web_url"),
             ref or old.get("ref"), state, detail, now if scanned else old.get("last_scan"),
             (pipeline or {}).get("id") or old.get("pipeline_id"),
             (pipeline or {}).get("web_url") or old.get("pipeline_url"), now))

    def _security_jobs(self, jobs):
        out = {}
        for j in jobs:
            for kind, (_, rx) in KINDS.items():
                if rx.search(j.get("name") or ""):
                    out.setdefault(kind, []).append(j)
        return out

    def _read_reports(self, pid, jobs_by_kind):
        """-> (findings, kinds_read, problems)"""
        findings, read, problems = [], set(), []
        for kind, jobs in jobs_by_kind.items():
            fname = KINDS[kind][0]
            for j in jobs:
                st = j.get("status")
                if st in ("created", "pending", "running", "manual", "skipped", "canceled"):
                    continue
                if st == "failed":
                    problems.append(f"job {j.get('name')} gagal")
                try:
                    raw = self.gl.job_artifact(pid, j["id"], fname)
                except Exception as ex:
                    problems.append(f"{j.get('name')}: {short_error(ex)}")
                    continue
                if raw is None:
                    if st == "success":
                        problems.append(f"laporan {fname} dari job {j.get('name')} tidak bisa diunduh "
                                        f"(tambahkan artifacts:paths, lihat {_CONFIG_HINT})")
                    continue
                try:
                    findings += parse_report(raw, kind)
                    read.add(kind)
                except ValueError as ex:
                    problems.append(f"{fname} tidak valid: {ex}")
        return findings, read, problems

    def sync_project(self, pid):
        info = self.project_info(pid)
        ref = self.cfg.get("branch") or info["default_branch"]
        pipes = [p for p in self.gl.pipelines(pid, ref=ref, per_page=10) if p.get("status") in ("success", "failed")]
        if not pipes:
            self._set_project(pid, info, ref, "no_pipeline", f"Belum ada pipeline selesai di branch {ref}.")
            return None
        chosen, by_kind = None, {}
        for p in pipes[:3]:
            by_kind = self._security_jobs(self.gl.pipeline_jobs(pid, p["id"]))
            if by_kind:
                chosen = p
                break
        if not chosen:
            self._set_project(pid, info, ref, "not_configured",
                              f"Pipeline branch {ref} belum punya job SAST / Secret Detection. "
                              f"Pasang konfigurasi dari {_CONFIG_HINT}.", pipes[0])
            return None
        real_id = info["id"]
        prev = self.store.query("SELECT pipeline_id, state FROM sec_projects WHERE project_id=?", (real_id,))
        if prev and prev[0]["pipeline_id"] == chosen["id"] and prev[0]["state"] in ("ok", "partial"):
            self.store.execute("UPDATE sec_projects SET checked_at=? WHERE project_id=?", (time.time(), real_id))
            return None
        findings, read, problems = self._read_reports(pid, by_kind)
        if not read:
            self._set_project(pid, info, ref, "no_report", "; ".join(problems)[:500] or "Laporan belum tersedia.",
                              chosen)
            return None
        new = self._store_findings(info, ref, chosen, fingerprint(real_id, findings), read)
        missing = [KIND_LABEL[k] for k in KINDS if k not in by_kind]
        detail = "; ".join(problems + ([f"belum ada job {', '.join(missing)}"] if missing else []))[:500]
        self._set_project(pid, info, ref, "partial" if (missing or problems) else "ok", detail, chosen, scanned=True)
        base_key = f"sec_base:{real_id}"
        if self.store.kv_get(base_key) and new:
            serious = [f for f in new if is_serious(f, self.min_sev)]
            if serious:
                self._alert_branch(info, ref, chosen, serious)
        self.store.kv_set(base_key, 1)
        n_open = self.store.query("SELECT COUNT(*) n FROM sec_findings WHERE project_id=? AND status='open'",
                                  (real_id,))[0]["n"]
        self.event("security", f"Scan keamanan {info['path']}: {n_open} temuan terbuka",
                   (f"{len(new)} baru" if new else "tidak ada temuan baru") + f" · pipeline #{chosen['id']}",
                   level="warning" if any(is_serious(f, self.min_sev) for f in new) else "info")
        return new

    def _store_findings(self, info, ref, pipe, findings, kinds_read):
        pid, now = info["id"], time.time()
        sha = pipe.get("sha") or ""
        existing = {r["fp"]: r for r in self.store.query(
            "SELECT fp, kind, status, first_seen FROM sec_findings WHERE project_id=?", (pid,))}
        new, rows = [], []
        for f in findings:
            old = existing.get(f["fp"])
            if not old or old["status"] != "open":
                new.append(f)
            url = ""
            if info.get("web_url") and f["file"]:
                url = f"{info['web_url']}/-/blob/{sha or ref}/{f['file']}" + (f"#L{f['line']}" if f["line"] else "")
            rows.append((f["fp"], pid, info["path"], f["kind"], f["severity"], f["name"], f["description"], f["file"],
                         f["line"], f["rule"], f["rule_url"], f["scanner"], "open",
                         (old or {}).get("first_seen") or now, now, None, pipe["id"], ref, sha, url))
        if rows:
            self.store.execute(
                "INSERT INTO sec_findings(fp, project_id, project, kind, severity, name, description, file, line, rule, "
                "rule_url, scanner, status, first_seen, last_seen, fixed_at, pipeline_id, ref, sha, web_url) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fp) DO UPDATE SET "
                "severity=excluded.severity, name=excluded.name, description=excluded.description, "
                "line=excluded.line, status='open', last_seen=excluded.last_seen, fixed_at=NULL, "
                "pipeline_id=excluded.pipeline_id, ref=excluded.ref, sha=excluded.sha, web_url=excluded.web_url, "
                "first_seen=CASE WHEN sec_findings.status='open' THEN sec_findings.first_seen ELSE excluded.first_seen END",
                rows, many=True)
        current = {f["fp"] for f in findings}
        for kind in kinds_read:  # only a kind that was actually scanned can mark its findings fixed
            gone = [fp for fp, r in existing.items() if r["kind"] == kind and r["status"] == "open" and fp not in current]
            if gone:
                self.store.execute(f"UPDATE sec_findings SET status='fixed', fixed_at=? WHERE fp IN "
                                   f"({','.join('?' * len(gone))})", [now] + gone)
        return new

    def _alert_branch(self, info, ref, pipe, serious):
        serious = sorted(serious, key=lambda f: (f["kind"] != "secret", SEV_ORDER[f["severity"]]))
        lines = [f"🛡️ <b>Temuan keamanan baru di {e(info['path'])}</b> (branch <code>{e(ref)}</code>): "
                 f"{e(summarize(serious))}"]
        for f in serious[:8]:
            loc = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
            lines.append(f"• {KIND_LABEL[f['kind']]} {SEV_LABEL[f['severity']]}: {e(f['name'])} "
                         f"<code>{e(loc)}</code>")
        if len(serious) > 8:
            lines.append(f"…dan {len(serious) - 8} lainnya")
        if pipe.get("web_url"):
            lines.append(f"<a href=\"{e(pipe['web_url'], True)}\">Pipeline #{pipe['id']}</a> · detail di dashboard Keamanan")
        self.send("\n".join(lines))

    # --------------------------------------------------------------- MRs
    def check_mrs(self):
        for rec in self.store.by_status("notified"):
            try:
                self.check_mr(rec)
            except Exception as ex:
                log.info("cek keamanan !%s: %s", rec.get("iid"), short_error(ex))

    def check_mr(self, rec):
        pid, iid = rec["project_id"], rec["iid"]
        pipes = [p for p in self.gl.mr_pipelines(pid, iid) if p.get("status") in ("success", "failed")]
        if not pipes or (rec.get("sha") and pipes[0].get("sha") and pipes[0]["sha"] != rec["sha"]):
            return None  # newest commit's pipeline not finished yet
        p = pipes[0]
        done = f"sec_mr:{pid}:{iid}:{p['id']}"
        if self.store.kv_get(done):
            return None
        by_kind = self._security_jobs(self.gl.pipeline_jobs(pid, p["id"]))
        if not by_kind:
            self.store.kv_set(done, "none")
            return None
        findings, read, _ = self._read_reports(pid, by_kind)
        if not read:
            self.store.kv_set(done, "noreport")
            return None
        info = self.project_info(pid)
        base = Counter(r["k"] for r in self.store.query(
            "SELECT kind || '|' || rule || '|' || file || '|' || name AS k FROM sec_findings "
            "WHERE project_id=? AND status='open'", (info["id"],)))
        have, new = Counter(), []
        for f in sorted(findings, key=lambda x: (loose_key(x), x.get("line") or 0)):
            k = loose_key(f)
            have[k] += 1
            if have[k] > base.get(k, 0):
                new.append(f)
        new.sort(key=lambda f: (f["kind"] != "secret", SEV_ORDER[f["severity"]]))
        result = {"sha": p.get("sha") or rec.get("sha"), "pipeline_id": p["id"], "pipeline_url": p.get("web_url"),
                  "checked_at": time.time(), "summary": summarize(new) if new else "",
                  "secrets": sum(1 for f in new if f["kind"] == "secret"),
                  "serious": sum(1 for f in new if is_serious(f, self.min_sev)),
                  "findings": [{k: f[k] for k in ("kind", "severity", "name", "file", "line", "rule", "rule_url")}
                               for f in new[:30]]}
        self.store.kv_set(f"sec_mr_new:{pid}:{iid}", json.dumps(result))
        self.store.kv_set(done, len(new))
        key = f"{pid}:{iid}"
        if new:
            self.event("security", f"!{iid} membawa temuan keamanan baru", result["summary"], key,
                       "error" if result["serious"] else "warning")
        if result["serious"]:
            lines = [f"🛡️ <b>!{iid}</b> {e(rec.get('title'))}\nMR ini membawa temuan keamanan baru: "
                     f"<b>{e(result['summary'])}</b>"]
            for f in new[:6]:
                loc = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
                lines.append(f"• {KIND_LABEL[f['kind']]} {SEV_LABEL[f['severity']]}: {e(f['name'])} <code>{e(loc)}</code>")
            lines.append("Merge akan meminta konfirmasi."
                         if (self.root.get("merge") or {}).get("confirm_if_security", True) else "")
            if rec.get("web_url"):
                lines.append(f"<a href=\"{e(rec['web_url'], True)}\">Buka MR</a>")
            self.send("\n".join(x for x in lines if x))
        return result

    def mr_result(self, pid, iid, sha=None):
        try:
            r = json.loads(self.store.kv_get(f"sec_mr_new:{pid}:{iid}") or "null")
        except ValueError:
            return None
        if not r or (sha and r.get("sha") and r["sha"] != sha):
            return None
        return r

    # ------------------------------------------------------------- reports
    def report(self):
        """Short HTML summary for Telegram /keamanan."""
        projs = self.store.query("SELECT * FROM sec_projects ORDER BY project")
        if not projs:
            return ("🛡️ <b>Keamanan</b>\nBelum ada project yang dipindai. MR Pilot memindai project dari MR yang "
                    f"masuk ke Anda (atau <code>security.projects</code>). Konfigurasi CI: <code>{_CONFIG_HINT}</code>.")
        rows = self.store.query("SELECT project_id, kind, severity, COUNT(*) n FROM sec_findings WHERE status='open' "
                                "GROUP BY project_id, kind, severity")
        by = {}
        for r in rows:
            by.setdefault(r["project_id"], []).extend([{"kind": r["kind"], "severity": r["severity"]}] * r["n"])
        lines = ["🛡️ <b>Keamanan (temuan terbuka)</b>"]
        state_txt = {"not_configured": "belum dipasang", "no_report": "laporan tidak terbaca", "error": "gagal dicek",
                     "no_pipeline": "belum ada pipeline"}
        for p in projs:
            fs = by.get(p["project_id"], [])
            if p["state"] in state_txt:
                lines.append(f"⚪ {e(p['project'])}: {state_txt[p['state']]}")
            else:
                icon = "🔴" if any(is_serious(f, self.min_sev) for f in fs) else ("🟡" if fs else "🟢")
                lines.append(f"{icon} {e(p['project'])}: {e(summarize(fs)) if fs else 'bersih'}")
        fixed = self.store.query("SELECT COUNT(*) n FROM sec_findings WHERE status='fixed' AND fixed_at>=?",
                                 (time.time() - 7 * 86400,))[0]["n"]
        lines.append(f"\nDiperbaiki 7 hari terakhir: {fixed}. Detail di dashboard → Keamanan.")
        return "\n".join(lines)
