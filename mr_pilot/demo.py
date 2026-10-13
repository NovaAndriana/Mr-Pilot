"""Demo data for `--demo`: lets you see the dashboard without GitLab/Telegram. Names are fictional."""
import random
import threading
import time
from datetime import datetime, timezone

DEVS = [("Rizky Pratama", "rizky.p"), ("Dewi Lestari", "dewi.l"), ("Bima Saputra", "bima.s"), ("Sinta Maharani", "sinta.m")]
RULES = [("go-no-fmt-print", "warning", "Gunakan logger terstruktur proyek, bukan fmt.Print / log.Print."),
         ("go-ignored-error", "warning", "Error diabaikan (_). Tangani atau beri komentar alasannya."),
         ("todo-without-ticket", "info", "TODO/FIXME wajib menyebut tiket, mis. TODO(IDAS-123)."),
         ("ts-no-console", "warning", "Hapus console.log sebelum merge."),
         ("ts-no-any", "warning", "Hindari tipe any, gunakan tipe spesifik atau unknown."),
         ("go-sql-concat", "error", "SQL disusun dengan string concat/Sprintf (risiko injection)."),
         ("no-hardcoded-secret", "error", "Secret/password di-hardcode. Pindahkan ke env var."),
         ("commit-message-convention", "info", "Pesan commit tidak sesuai konvensi.")]
FILES = ["internal/workflow/balance_deduction.go", "internal/repo/postgres/workflow_step.go",
         "pkg/api/echo/internal/workflow.go", "web/src/pages/Workflow/Detail.tsx",
         "web/src/services/quota.ts", "mobile/src/screens/SignScreen.tsx", "internal/quota/usage.go"]


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def seed(store, now=None):
    rnd = random.Random(7)
    now = now or time.time()
    store.kv_set("gitlab_user", "nova.andriana")
    store.kv_set("last_poll_ok", now - 40)

    active = [
        (375, "feat(IDAS-5323): show per user quota usage in workflow balance deduction", 0, 2.3, "APPROVE", "success",
         [("go-ignored-error", "warning", FILES[0], 212), ("pr-checklist-unchecked", "info", "", None)]),
        (381, "fix(IDAS-5340): reject workflow tanpa phases", 2, 7.5, "NEEDS_ATTENTION", "success",
         [("go-sql-concat", "error", FILES[1], 88), ("go-no-fmt-print", "warning", FILES[1], 120)]),
        (384, "feat(IDAS-5351): halaman detail kuota per signer", 1, 26, "REQUEST_CHANGES", "failed",
         [("ts-no-any", "warning", FILES[3], 41), ("ts-no-console", "warning", FILES[4], 17),
          ("no-hardcoded-secret", "error", FILES[4], 9)]),
        (386, "refactor(IDAS-5360): pisahkan usecase quota estimator", 3, 0.6, "APPROVE", "running", []),
        (387, "Update signing screen", 2, 0.2, None, "pending", []),
    ]
    for iid, title, dev, age_h, verdict, pipe, viols in active:
        name, user = DEVS[dev]
        key = f"42:{iid}"
        created = now - age_h * 3600
        store.upsert(key, project_id=42, iid=iid, sha=f"{rnd.getrandbits(40):010x}", title=title,
                     web_url=f"https://code.idas.id/idas/idas-repo-be/-/merge_requests/{iid}",
                     status="notified" if verdict else "waiting_bot", first_seen=created + 60,
                     project="idas/idas-repo-be", author=name, author_username=user,
                     source_branch=f"feat/IDAS-{5300 + iid % 100}", target_branch="staging",
                     verdict=verdict, pipeline=pipe, mr_created_at=_iso(created),
                     review={"source": "llm", "verdict": verdict or "UNKNOWN",
                             "summary": "Perubahan konsisten dengan deskripsi; perhatikan temuan di bawah ini dengan teliti." if verdict else "",
                             "solves": "Endpoint balance-deduction sebelumnya hanya menampilkan total kuota, tidak per user.",
                             "changes": ["Response quota berubah dari array menjadi object per user",
                                         "Validasi workflow tanpa phases (422 wkfl_015)"],
                             "good_points": ["Filter query step mencegah unfiltered query", "Test table-driven lengkap"],
                             "findings": [{"severity": "minor", "title": "Assertion test line 1110 tidak konsisten",
                                           "file": "quota_deduction_detail_test.go:1110", "detail": ""}],
                             "breaking_changes": ["Field quota kini object, bisa null"] if iid == 375 else []})
        vs = [{"rule": r, "severity": s, "path": p, "line": ln, "message": dict((x[0], x[2]) for x in RULES).get(
            r, "Item checklist belum dicentang"), "source": "rule" if p else "convention", "stack": "go"}
              for r, s, p, ln in viols]
        store.replace_mr_violations(key, "x", "idas/idas-repo-be", vs)

    for i in range(14):
        iid = 340 + i
        name, user = DEVS[i % 4]
        created = now - rnd.uniform(1, 13) * 86400
        decided = created + rnd.uniform(1.5, 30) * 3600
        status = "merged" if i % 5 else "rejected"
        store.upsert(f"42:{iid}", project_id=42, iid=iid, sha="0" * 10, title=f"feat(IDAS-{5200 + i}): perbaikan modul {i}",
                     web_url=f"https://code.idas.id/idas/idas-repo-be/-/merge_requests/{iid}", status=status,
                     first_seen=created, project="idas/idas-repo-be", author=name, author_username=user,
                     source_branch="feat/x", target_branch="staging", verdict="APPROVE", pipeline="success",
                     mr_created_at=_iso(created), decided_at=min(decided, now - 600))

    for d in range(30):
        n = max(0, int(rnd.gauss(6 - d * 0.08, 2.5)))
        for _ in range(n):
            r, s, m = rnd.choice(RULES)
            name, _ = rnd.choice(DEVS)
            ts = now - d * 86400 - rnd.uniform(0, 80000)
            csha = f"{rnd.getrandbits(40):010x}"
            store.kv_set(f"cq_commit:42:{csha}", f"1|{ts:.0f}")
            store.db.execute(
                "INSERT INTO violations(ts,mr_key,scope,sha,commit_sha,author,project,stack,rule,severity,source,path,line,message)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, "42:300", "commit", "c", csha, name, "idas/idas-repo-be", "go", 
                 r, s, "rule", rnd.choice(FILES), rnd.randint(5, 400), m))
    store.db.commit()
    for i in range(170):
        store.kv_set(f"cq_commit:42:clean{i}", f"0|{now - rnd.uniform(0, 30) * 86400:.0f}")

    for i in range(140):
        prov = rnd.choice(["claude_code"] * 5 + ["gemini"] * 3 + ["groq"] * 2)
        ok = rnd.random() > (0.04 if prov != "groq" else 0.15)
        ms = int(rnd.gauss({"claude_code": 21000, "gemini": 9000, "groq": 2500}[prov], 2000))
        tin, tout = (int(rnd.uniform(6000, 22000)), int(rnd.uniform(600, 2500))) if ok else (0, 0)
        store.db.execute("INSERT INTO ai_calls(ts,provider,task,ok,ms,error,tokens_in,tokens_out) "
                         "VALUES(?,?,?,?,?,?,?,?)",
                         (now - rnd.uniform(0, 7 * 86400), prov, rnd.choice(["review", "standards"]), int(ok), max(300, ms),
                          "" if ok else rnd.choice(["HTTP 429: rate limit", "timeout"]), tin, tout))
    store.db.commit()

    evs = [("system", "MR Pilot aktif", "Memantau MR untuk @nova.andriana", "info", 300),
           ("mr_new", "MR baru !386: refactor(IDAS-5360): pisahkan usecase quota estimator", "oleh Dewi Lestari → staging", "info", 36),
           ("quality", "Standar kode !386: 0 error, 0 warning, 0 info", "0 warning diposting ke GitLab", "success", 34),
           ("review", "Review !386 selesai: ✅ Approve", "Pemisahan usecase rapi, test lengkap.", "success", 33),
           ("merged", "!372 di-merge ke staging", "fix(IDAS-5301): timeout signing · Teams: terkirim ✅", "success", 25),
           ("mr_new", "MR baru !387: Update signing screen", "oleh Bima Saputra → staging", "info", 12),
           ("waiting", "!387 menunggu komentar bot review", "", "info", 11)]
    for t, title, det, lvl, mins in evs:
        store.db.execute("INSERT INTO events(ts,type,mr_key,title,detail,level) VALUES(?,?,?,?,?,?)",
                         (now - mins * 60, t, None, title, det, lvl))
    store.db.commit()
    seed_security(store, now)
    seed_dora(store, now)


PROJECTS = [(42, "idas/idas-repo-be"), (43, "idas/akusign-web"), (44, "idas/akusign-mobile")]
SEC = [  # project, kind, severity, name, file, line, rule, age days, fixed after days (None = open)
    (42, "sast", "high", "Improper neutralization of special elements in SQL command", "internal/repo/postgres/report.go", 88, "gosec.G202-1", 9, None),
    (42, "secret", "critical", "GitLab personal access token", "deploy/k8s/staging/configmap.yaml", 14, "gitlab_personal_access_token", 3, None),
    (42, "sast", "medium", "Use of weak cryptographic primitive (MD5)", "internal/sign/hash.go", 31, "gosec.G401-1", 20, None),
    (42, "sast", "medium", "Incorrect permission assignment for file", "cmd/worker/main.go", 57, "gosec.G302-1", 26, None),
    (42, "sast", "low", "Deferring unsafe method Close on writable file", "internal/export/csv.go", 44, "gosec.G307-1", 15, None),
    (43, "sast", "high", "Cross-site scripting via dangerouslySetInnerHTML", "src/pages/Document/Preview.tsx", 62, "react-dangerouslysetinnerhtml", 5, None),
    (43, "sast", "medium", "Insecure randomness for token", "src/services/session.ts", 18, "insecure-random", 12, None),
    (44, "sast", "medium", "Sensitive data in AsyncStorage", "src/store/auth.ts", 27, "rn-asyncstorage-sensitive", 6, None),
    (42, "sast", "high", "Path traversal in file download", "internal/handler/download.go", 40, "gosec.G304-1", 28, 4),
    (43, "secret", "critical", "Generic API key", "src/config/env.ts", 7, "generic-api-key", 22, 1),
    (42, "sast", "medium", "Unhandled error from Close", "internal/repo/postgres/tx.go", 120, "gosec.G104-1", 25, 6),
    (44, "sast", "low", "Hardcoded URL", "src/api/client.ts", 3, "hardcoded-url", 18, 2),
]


def seed_security(store, now):
    import hashlib
    for pid, path in PROJECTS:
        store.db.execute("INSERT OR REPLACE INTO sec_projects(project_id, project, web_url, ref, state, detail, last_scan, "
                         "pipeline_id, pipeline_url, checked_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (pid, path, f"https://code.idas.id/{path}", "main" if pid != 44 else "develop",
                          "ok" if pid != 44 else "partial", "" if pid != 44 else "belum ada job Secret",
                          now - 1500, 9000 + pid, f"https://code.idas.id/{path}/-/pipelines/{9000 + pid}", now - 300))
    store.db.execute("INSERT OR REPLACE INTO sec_projects(project_id, project, web_url, ref, state, detail, checked_at) "
                     "VALUES(45, 'idas/emeterai-service', 'https://code.idas.id/idas/emeterai-service', 'main', "
                     "'not_configured', 'Pipeline branch main belum punya job SAST / Secret Detection.', ?)", (now - 300,))
    for pid, kind, sev, name, f, line, rule, age, fixed in SEC:
        path = dict(PROJECTS)[pid]
        fp = hashlib.sha1(f"{pid}{rule}{f}".encode()).hexdigest()[:20]
        store.db.execute(
            "INSERT OR REPLACE INTO sec_findings(fp, project_id, project, kind, severity, name, description, file, line, "
            "rule, rule_url, scanner, status, first_seen, last_seen, fixed_at, pipeline_id, ref, sha, web_url) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fp, pid, path, kind, sev, name, "Contoh penjelasan dari scanner untuk demo.", f, line, rule, "",
             "Semgrep" if kind == "sast" else "Gitleaks", "open" if fixed is None else "fixed", now - age * 86400,
             now - 1500, None if fixed is None else now - fixed * 86400, 9000 + pid, "main", "a1b2c3d4",
             f"https://code.idas.id/{path}/-/blob/main/{f}#L{line}"))
    store.kv_set("sec_mr_new:42:384", '{"sha": null, "pipeline_id": 9384, "summary": "1 secret, 1 critical", '
                 '"secrets": 1, "serious": 1, "checked_at": %d, "findings": [{"kind": "secret", "severity": "critical", '
                 '"name": "Generic API key", "file": "web/src/services/quota.ts", "line": 9, "rule": "generic-api-key", '
                 '"rule_url": ""}]}' % now)
    store.db.commit()


def seed_dora(store, now):
    import json
    rnd = random.Random(11)
    for pid, path in PROJECTS:
        rate = {42: 0.55, 43: 0.35, 44: 0.15}[pid]
        t = now - 100 * 86400
        n = 0
        while t < now:
            t += rnd.expovariate(rate) * 86400
            if t >= now:
                break
            if time.localtime(t).tm_wday >= 5:
                continue
            n += 1
            commits = rnd.randint(1, 9)
            base = {42: 20, 43: 40, 44: 90}[pid] * 3600
            leads = sorted(rnd.lognormvariate(0, 0.7) * base for _ in range(commits))
            fix = rnd.random() < {42: 0.12, 43: 0.2, 44: 0.25}[pid]
            store.db.execute(
                "INSERT OR REPLACE INTO dora_deploys(key, project_id, project, env, source, sha, ref, finished_at, n_commits, "
                "lead_median, lead_values, fix_commit, rollback, web_url, title) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"{pid}:deployments:{n}", pid, path, "environment production", "deployments",
                 f"{rnd.getrandbits(40):010x}", "main", t, commits, leads[len(leads) // 2],
                 json.dumps([round(x) for x in leads]), int(fix), 0, f"https://code.idas.id/{path}/-/jobs/{n}",
                 "hotfix: perbaiki validasi" if fix else "deploy production"))
        store.kv_set(f"dora_src:{pid}", json.dumps({"source": "deployments", "label": "environment production",
                                                    "at": now - 900, "project": path}))
    for k, (pid, title, days_ago, hours) in enumerate([(42, "Signing timeout di production", 12, 3.5),
                                                       (43, "Preview dokumen kosong", 34, 20),
                                                       (42, "Kuota tidak berkurang setelah tanda tangan", 2, None)]):
        created = now - days_ago * 86400
        store.db.execute("INSERT OR REPLACE INTO dora_incidents(key, project_id, project, title, created_at, closed_at, web_url) "
                         "VALUES(?,?,?,?,?,?,?)", (f"{pid}:{900 + k}", pid, dict(PROJECTS)[pid], title, created,
                                                   created + hours * 3600 if hours else None,
                                                   f"https://code.idas.id/{dict(PROJECTS)[pid]}/-/issues/{900 + k}"))
    store.db.commit()


def simulate(store, every=20):
    """Emit a fake event periodically so the live feed can be seen moving."""
    samples = [("quality", "Standar kode !387: 0 error, 2 warning, 1 info", "3 warning diposting ke GitLab", "warning"),
               ("review", "Review !387 selesai: ⚠️ Perlu perhatian", "Komponen terlalu besar, pecah jadi 2.", "warning"),
               ("mr_updated", "Commit baru di !384", "feat(IDAS-5351): halaman detail kuota per signer", "info"),
               ("merged", "!375 di-merge ke staging", "Teams: terkirim ✅", "success")]

    def run():
        i = 0
        while True:
            time.sleep(every)
            t, title, det, lvl = samples[i % len(samples)]
            store.add_event(t, title, det, None, lvl)
            store.kv_set("last_poll_ok", time.time())
            i += 1
    threading.Thread(target=run, daemon=True).start()
