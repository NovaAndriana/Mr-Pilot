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
        store.db.execute("INSERT INTO ai_calls(ts,provider,task,ok,ms,error) VALUES(?,?,?,?,?,?)",
                         (now - rnd.uniform(0, 7 * 86400), prov, rnd.choice(["review", "standards"]), int(ok), max(300, ms),
                          "" if ok else rnd.choice(["HTTP 429: rate limit", "timeout"])))
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
