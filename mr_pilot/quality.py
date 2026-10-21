"""Code Quality: check MRs against the team's written standards. GitLab only gets comments for errors and
predicted risks; they are followed up (resolved) when the team fixes them."""
import logging
import time

import requests

from . import migrations, voice
from .ai import AIManager
from .threads import ThreadTracker, line_text
from .util import short_error
from .reviewer import extract_json
from .standards import (SEV_RANK, Standards, annotate_diff, count_by_severity, sort_violations,
                        violation)

log = logging.getLogger("mr_pilot.quality")

SEV_ICON = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}

AI_PROMPT = """Kamu reviewer senior yang mengecek kode terhadap STANDAR TIM berikut.
Komentarmu akan diposting di GitLab, jadi laporkan HANYA dua hal ini, pada baris yang DITAMBAHKAN (L<nomor> +):
- error: pasti bermasalah sekarang. Bug, crash, data salah, celah keamanan, kontrak API rusak, atau melanggar
  aturan wajib di standar yang berdampak nyata.
- risk: belum error hari ini, tapi hampir pasti jadi masalah ke depan. Contoh: error/response tidak ditangani,
  request tanpa autentikasi/interceptor standar, race condition, query tanpa limit yang akan lambat saat data
  besar, nilai yang akan pecah saat input kosong/null, ketergantungan yang akan rusak saat konfigurasi berubah.
  Tulis prediksinya dengan skenario konkret: kapan dan bagaimana masalahnya akan muncul.
JANGAN laporkan: penamaan variabel, format/gaya, panjang file atau komponen, console.log, tipe any, key index,
TODO, komentar kode, atau saran "lebih rapi" lainnya. Kalau ragu, jangan laporkan.
Jangan mengulang hal yang sudah dicek oleh aturan otomatis: {auto_rules}
Gunakan nomor baris dari prefiks L<nomor>. Maksimal {max_items} temuan, paling penting dulu. Lebih baik 0 temuan
daripada temuan lemah.
confidence: high hanya jika kamu yakin dan bisa menunjuk barisnya.
Tulis message dalam bahasa {language}: 2-3 kalimat. Apa masalahnya, kapan/bagaimana dampaknya terjadi, cara
memperbaikinya.
<<VOICE>>

=== STANDAR TIM ===
{standards}
=== AKHIR STANDAR ===

Jawab HANYA JSON: {{"violations": [{{"rule": "nama/bagian standar", "severity": "error|risk",
"file": "path", "line": 123, "message": "...", "confidence": "high|medium|low"}}]}}"""
AI_PROMPT = AI_PROMPT.replace("<<VOICE>>", voice.VOICE_RULES.replace("{", "{{").replace("}", "}}"))


class CodeQuality:
    def __init__(self, cfg, gl, store, base_dir=".", ai=None):
        self.root = cfg
        self.ai = ai or AIManager(cfg, store)
        self.cfg = cfg.get("code_quality") or {}
        self.enabled = bool(self.cfg.get("enabled"))
        self.gl = gl
        self.store = store
        self.std = Standards(self.cfg, base_dir) if self.enabled else None
        self.report = self.cfg.get("report") or {}
        self.ignore = cfg["review"].get("ignore_files") or []
        self.threads = ThreadTracker(gl, store)

    # --------------------------------------------------------------- public
    def run(self, mr, dry=False):
        """Check MR; post warnings; return summary dict for Telegram/dashboard."""
        if not self.enabled:
            return None
        self.std.reload()
        pid, iid, sha = mr["project_id"], mr["iid"], mr["sha"]
        key = f"{pid}:{iid}"
        project = ((mr.get("references") or {}).get("full") or "").split("!")[0]
        posted = 0

        # 1) every new commit: rules + commit-message convention -> warning ON the commit
        try:
            commits = self.gl.get_commits(pid, iid)
        except Exception:
            log.exception("gagal ambil commits")
            commits = []
        # GitLab returns newest first. On a long-lived branch seen for the first time, only the newest
        # commits are checked so one MR can't trigger hundreds of API calls and comments.
        max_commits = int(self.cfg.get("max_commits_per_run", 30))
        for n, c in enumerate(commits):
            csha = c.get("id")
            if not csha or self.store.kv_get(f"cq_commit:{pid}:{csha}"):
                continue
            if n >= max_commits:
                self.store.kv_set(f"cq_commit:{pid}:{csha}", "skip")
                continue
            if len(c.get("parent_ids") or []) > 1:  # merge commit
                self.store.kv_set(f"cq_commit:{pid}:{csha}", "skip")
                continue
            try:
                diffs = self.gl.get_commit_diff(pid, csha)
            except Exception:
                log.exception("gagal ambil diff commit %s", csha[:8])
                continue
            vs = self.std.check_diffs(diffs, self.ignore) + self.std.check_commit_message(c.get("message"))
            vs = sort_violations(vs)
            if vs and not dry and self.report.get("commit_comments", True) and self.report.get("post_on") == "commit":
                posted += self._post_commit_comments(pid, c, vs)
            self.store.add_commit_violations(key, csha, c.get("author_name"), project, vs)
            self.store.kv_set(f"cq_commit:{pid}:{csha}", f"{len(vs)}|{time.time():.0f}")

        # 2) current state of the whole MR (what would land if merged)
        diffs = self.gl.get_diffs(pid, iid)
        current = self.std.check_diffs(diffs, self.ignore)
        if self.cfg.get("migration_checks", True):
            current += migrations.check(diffs)
        current += self.std.check_title(mr.get("title"), mr.get("source_branch"))
        current += self.std.check_branch(mr.get("source_branch"))
        current += self.std.check_size(diffs)
        current += self.std.check_checklist(mr.get("description"))

        # 3) AI check against the written standards (only for a new head sha)
        ai_vs, ai_note, ai_ok = [], "", False
        if self.cfg.get("ai_check") and self._llm_ok():
            cache_key = f"cq_ai:{pid}:{iid}:{sha}"  # same commit can head two MRs (staging + main)
            cached = self.store.kv_get(cache_key)
            if cached is None:
                ai_vs, ai_note = self._ai_check(mr, diffs)
                if not ai_note:  # only cache a successful check; a failed one is retried next time
                    self.store.kv_set(cache_key, len(ai_vs))
                    ai_ok = True
            else:
                ai_vs = [v for v in self.store.mr_violations(key) if v.get("source") == "ai"]
                ai_ok = True
        for v in ai_vs:
            if not v.get("snippet") and v.get("path") and v.get("line"):
                v["snippet"] = line_text(diffs, v["path"], v["line"]).strip()[:200]
        current = sort_violations(current + ai_vs)
        self.store.replace_mr_violations(key, sha, project, current)

        # 3b) GitLab threads: close what the team fixed, then comment only on new errors / risks
        threads = {"open": 0, "fixed": [], "fixed_total": 0}
        if not dry and self.report.get("post_on", "mr") == "mr":
            items = [self._item(v) for v in current]
            threads = self.threads.reconcile(mr, diffs, current=items, ai_ok=ai_ok,
                                             adopt=self.report.get("follow_up_existing", True))
            new = self._post_threads(mr, current, {d.get("new_path") for d in diffs if not d.get("deleted_file")})
            posted += new
            threads["open"] += new

        counts = count_by_severity(current)
        summary = {"enabled": True, "counts": counts, "total": len(current),
                   "top": [{k: v.get(k) for k in ("rule", "severity", "path", "line", "message", "source")}
                           for v in current[:6]],
                   "posted": posted, "ai_note": ai_note, "threads": threads,
                   "rule_errors": self.std.rule_errors[:3]}

        # 4) summary note on the MR + commit status on head
        if not dry:
            if self.report.get("mr_summary_note", False):
                self._upsert_summary_note(pid, iid, mr, [v for v in current if self.postable(v)], counts)
            if self.report.get("commit_status", True):
                self._set_status(pid, sha, mr, counts)
        return summary

    def has_blocking(self, summary):
        return bool(summary and summary.get("counts", {}).get("error"))

    # -------------------------------------------------------------- helpers
    def _llm_ok(self):
        if not self.ai.available("standards"):
            return False
        econ = getattr(self.ai, "economy", None)
        return not (econ and econ("standards"))  # free-tier quota almost used up: rules only

    def _footer_text(self):
        return self.report.get("comment_footer", "") or ""

    def _comment_body(self, v, inline=True):
        return voice.standard_comment(v, inline=inline, footer=self._footer_text())

    def _post_commit_comments(self, pid, commit, vs):
        csha, limit, n = commit["id"], int(self.report.get("max_comments_per_commit", 10)), 0
        min_sev = SEV_RANK.get(self.report.get("min_severity_to_post", "warning"), 1)
        to_post = [v for v in vs if SEV_RANK.get(v["severity"], 3) <= min_sev]
        for v in to_post[:limit]:
            try:
                if v.get("path") and v.get("line"):
                    self.gl.commit_comment(pid, csha, self._comment_body(v, True), v["path"], v["line"])
                else:
                    self.gl.commit_comment(pid, csha, self._comment_body(v, False))
                v["posted"] = True
                n += 1
            except Exception:
                log.exception("gagal posting komentar commit %s", csha[:8])
        rest = to_post[limit:]
        if rest:
            try:
                self.gl.commit_comment(pid, csha, voice.overflow_comment(rest, self._footer_text()))
                n += 1
            except Exception:
                log.exception("gagal posting ringkasan commit")
        return n

    def _ai_check(self, mr, diffs):
        llm = self.root["review"]["llm"]
        per_stack, chunks, stacks, total = {}, [], set(), 0
        max_chars = int(self.cfg.get("ai_max_diff_chars", 60000))
        for d in diffs:
            path = d.get("new_path") or ""
            sid = self.std.stack_of(path)
            if not sid or d.get("deleted_file"):
                continue
            body = annotate_diff(d.get("diff"))
            if total + len(body) > max_chars:
                continue
            stacks.add(sid)
            per_stack[path] = sid
            chunks.append(f"\n### {path}\n{body}")
            total += len(body)
        if not chunks:
            return [], ""
        auto = ", ".join(sorted({r["id"] for r in self.std.rules})) or "-"
        system = AI_PROMPT.format(auto_rules=auto, max_items=int(self.cfg.get("ai_max_violations", 8)),
                                  language=llm.get("language", "Indonesia"),
                                  standards=self.std.documents(stacks)[:30000])
        try:
            raw, _ = self.ai.complete(system, f"Judul MR: {mr.get('title')}\n" + "".join(chunks), "standards",
                                      validate=extract_json)
            data = extract_json(raw)
        except Exception as ex:
            log.warning("AI standards check gagal: %s", short_error(ex))
            return [], f"Cek AI gagal: {short_error(ex)}"
        out = []
        min_conf = self.cfg.get("ai_min_confidence", "high")
        rank = {"high": 0, "medium": 1, "low": 2}
        for x in data.get("violations") or []:
            if rank.get(str(x.get("confidence", "low")).lower(), 2) > rank.get(min_conf, 0):
                continue
            path = str(x.get("file") or "")
            try:
                line = int(x.get("line")) if x.get("line") else None
            except (TypeError, ValueError):
                line = None
            sev = str(x.get("severity", "")).lower()
            if sev not in ("error", "risk"):
                continue  # style / naming / "nice to have": not worth a comment
            v = violation(str(x.get("rule") or "standar")[:60], "error" if sev == "error" else "warning",
                          str(x.get("message") or "")[:500], path, line, "ai", stack=per_stack.get(path, ""))
            v["risk"] = sev == "risk"
            out.append(v)
        return out, ""

    # ----------------------------------------------------------- threads
    def postable(self, v):
        """Only what the team must act on goes to GitLab: errors, and risks the AI predicts.
        Warnings/info stay on Telegram and the dashboard."""
        if v.get("source") == "convention":
            return False
        if v.get("source") == "ai":
            if not self.report.get("ai_inline_comments", True):
                return False
            return v["severity"] == "error" or (v["severity"] == "warning" and self.report.get("ai_post_risks", True))
        if not self.report.get("commit_comments", True):
            return False
        return SEV_RANK.get(v["severity"], 3) <= SEV_RANK.get(self.report.get("min_severity_to_post", "error"), 0)

    @staticmethod
    def _item(v):
        return {"source": "ai" if v.get("source") == "ai" else ("rule" if v.get("source") == "rule" else v.get("source")),
                "path": v.get("path"), "line": v.get("line"), "rule": v.get("rule"), "snippet": v.get("snippet"),
                "severity": v.get("severity"), "message": (v.get("message") or "")[:200]}

    def _post_threads(self, mr, current, paths):
        # cap on threads open at the same time on one MR: the rest stays on Telegram/dashboard, and is only
        # commented after earlier ones are fixed (no trickle of new comments on every re-check)
        limit = int(self.report.get("max_open_threads", self.report.get("max_comments_per_run", 3)))
        open_items = self.threads.load(mr["project_id"], mr["iid"])
        n_open = sum(1 for t in open_items if t.get("status") == "open")
        n = 0
        for v in current:
            if n_open >= limit or not self.postable(v):
                continue
            if v.get("path") and v["path"] not in paths:
                continue  # e.g. an AI finding on a file this MR doesn't touch
            item = self._item(v)
            # already discussed: still open, or a person resolved it (their call, don't repeat)
            if self.threads.find(open_items, item, ("open", "resolved")):
                continue
            if self.threads.post(mr, item, lambda inline, v=v: self._comment_body(v, inline)):
                v["posted"] = True
                n += 1
                n_open += 1
                open_items = self.threads.load(mr["project_id"], mr["iid"])
        return n

    def _upsert_summary_note(self, pid, iid, mr, vs, counts):
        k = f"cq_note:{pid}:{iid}"
        note_id = self.store.kv_get(k)
        body = voice.summary_note(mr["sha"], vs, had_before=bool(note_id), footer=self._footer_text())
        if body is None:
            return  # nothing to say, don't spam a "clean" note
        try:
            if note_id:
                try:
                    self.gl.edit_note(pid, iid, note_id, body)
                    return
                except requests.HTTPError as ex:
                    if ex.response is None or ex.response.status_code not in (403, 404):
                        raise
                    log.info("Komentar ringkasan lama tidak ada lagi (dihapus?), buat baru")
            self.store.kv_set(k, self.gl.add_note(pid, iid, body)["id"])
        except Exception as ex:
            log.warning("gagal update ringkasan MR: %s", short_error(ex))

    def _set_status(self, pid, sha, mr, counts):
        fail_on = self.report.get("status_fail_on", "none")  # none | error | warning
        failed = (fail_on == "error" and counts["error"]) or \
                 (fail_on == "warning" and (counts["error"] or counts["warning"]))
        desc = ("Sesuai standar" if not sum(counts.values()) else
                f"{counts['error']} error, {counts['warning']} warning, {counts['info']} info")
        url = (self.root.get("dashboard") or {}).get("public_url") or mr.get("web_url")
        try:
            self.gl.commit_status(pid, sha, "failed" if failed else "success",
                                  self.report.get("status_name", "code-standard"), desc,
                                  target_url=url, ref=mr.get("source_branch"))
        except Exception:
            log.exception("gagal set commit status")

