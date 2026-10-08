"""Code Quality: check MRs against the team's written standards and warn on the commits."""
import hashlib
import logging
import time

import requests

from .ai import AIManager
from .util import short_error
from .reviewer import extract_json
from .standards import (SEV_RANK, Standards, annotate_diff, count_by_severity, sort_violations,
                        violation)

log = logging.getLogger("mr_pilot.quality")

SEV_ICON = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}

AI_PROMPT = """Kamu adalah reviewer yang mengecek kepatuhan kode terhadap STANDAR TIM berikut.
Hanya laporkan pelanggaran yang JELAS terlihat pada baris yang DITAMBAHKAN (baris berawalan L<nomor> +).
Jangan laporkan hal yang tidak diatur di standar. Jangan mengulang hal yang sudah dicek oleh aturan otomatis:
{auto_rules}
Gunakan nomor baris dari prefiks L<nomor>. Maksimal {max_items} pelanggaran, paling penting dulu.
severity: error (melanggar aturan wajib/berisiko), warning (melanggar standar), info (saran).
confidence: high hanya jika kamu yakin itu melanggar standar yang tertulis.
Tulis message dalam bahasa {language}, singkat dan berisi saran perbaikan.

=== STANDAR TIM ===
{standards}
=== AKHIR STANDAR ===

Jawab HANYA JSON: {{"violations": [{{"rule": "nama/bagian standar", "severity": "error|warning|info",
"file": "path", "line": 123, "message": "...", "confidence": "high|medium|low"}}]}}"""


def _fp(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


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
            if vs and not dry and self.report.get("commit_comments", True):
                posted += self._post_commit_comments(pid, c, vs)
            self.store.add_commit_violations(key, csha, c.get("author_name"), project, vs)
            self.store.kv_set(f"cq_commit:{pid}:{csha}", f"{len(vs)}|{time.time():.0f}")

        # 2) current state of the whole MR (what would land if merged)
        diffs = self.gl.get_diffs(pid, iid)
        current = self.std.check_diffs(diffs, self.ignore)
        current += self.std.check_title(mr.get("title"))
        current += self.std.check_checklist(mr.get("description"))

        # 3) AI check against the written standards (only for a new head sha)
        ai_vs, ai_note = [], ""
        if self.cfg.get("ai_check") and self._llm_ok():
            cache_key = f"cq_ai:{pid}:{sha}"
            cached = self.store.kv_get(cache_key)
            if cached is None:
                ai_vs, ai_note = self._ai_check(mr, diffs)
                if not dry:
                    posted += self._post_ai_inline(mr, ai_vs)
                if not ai_note:  # only cache a successful check; a failed one is retried next time
                    self.store.kv_set(cache_key, len(ai_vs))
            else:
                ai_vs = [v for v in self.store.mr_violations(key) if v.get("source") == "ai"]
        current = sort_violations(current + ai_vs)
        self.store.replace_mr_violations(key, sha, project, current)

        counts = count_by_severity(current)
        summary = {"enabled": True, "counts": counts, "total": len(current),
                   "top": [{k: v.get(k) for k in ("rule", "severity", "path", "line", "message", "source")}
                           for v in current[:6]],
                   "posted": posted, "ai_note": ai_note,
                   "rule_errors": self.std.rule_errors[:3]}

        # 4) summary note on the MR + commit status on head
        if not dry:
            if self.report.get("mr_summary_note", True):
                self._upsert_summary_note(pid, iid, mr, current, counts)
            if self.report.get("commit_status", True):
                self._set_status(pid, sha, mr, counts)
        return summary

    def has_blocking(self, summary):
        return bool(summary and summary.get("counts", {}).get("error"))

    # -------------------------------------------------------------- helpers
    def _llm_ok(self):
        return bool(self.ai.available("standards"))

    def _footer(self):
        f = self.report.get("comment_footer", "")
        return f"\n\n<sub>{f}</sub>" if f else ""

    def _comment_body(self, v):
        std = self.std.stack_name(v.get("stack")) if v.get("stack") else "Umum"
        body = (f"{SEV_ICON.get(v['severity'], '⚠️')} **Code Standard · {v['severity'].upper()}** "
                f"· `{v['rule']}`\n\n{v['message']}")
        if v.get("snippet") and v["source"] != "ai":
            body += f"\n\n```\n{v['snippet']}\n```"
        body += f"\n\n_Standar: {std}_"
        return body + self._footer()

    def _post_commit_comments(self, pid, commit, vs):
        csha, limit, n = commit["id"], int(self.report.get("max_comments_per_commit", 10)), 0
        min_sev = SEV_RANK.get(self.report.get("min_severity_to_post", "warning"), 1)
        to_post = [v for v in vs if SEV_RANK.get(v["severity"], 3) <= min_sev]
        for v in to_post[:limit]:
            try:
                if v.get("path") and v.get("line"):
                    self.gl.commit_comment(pid, csha, self._comment_body(v), v["path"], v["line"])
                else:
                    self.gl.commit_comment(pid, csha, self._comment_body(v))
                v["posted"] = True
                n += 1
            except Exception:
                log.exception("gagal posting komentar commit %s", csha[:8])
        rest = to_post[limit:]
        if rest:
            lines = [f"- {SEV_ICON.get(v['severity'])} `{v['rule']}` {v.get('path') or ''}"
                     f"{':' + str(v['line']) if v.get('line') else ''}: {v['message']}" for v in rest[:30]]
            try:
                self.gl.commit_comment(pid, csha, f"**Code Standard:** {len(rest)} pelanggaran lain di commit ini\n\n"
                                       + "\n".join(lines) + self._footer())
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
            out.append(violation(str(x.get("rule") or "standar")[:60], str(x.get("severity", "warning")).lower(),
                                 str(x.get("message") or "")[:400], path, line, "ai",
                                 stack=per_stack.get(path, "")))
        return out, ""

    def _post_ai_inline(self, mr, vs):
        if not vs or not self.report.get("ai_inline_comments", True):
            return 0
        refs = mr.get("diff_refs") or {}
        n = 0
        for v in vs:
            fp = _fp(mr["project_id"], mr["iid"], v["rule"], v.get("path"), v.get("line"), v["message"][:60])
            if self.store.kv_get(f"cq_ai_posted:{fp}"):
                continue
            pos = None
            if v.get("path") and v.get("line") and refs.get("head_sha"):
                pos = {"position_type": "text", "base_sha": refs.get("base_sha"),
                       "start_sha": refs.get("start_sha"), "head_sha": refs.get("head_sha"),
                       "new_path": v["path"], "old_path": v["path"], "new_line": v["line"]}
            try:
                self.gl.mr_discussion(mr["project_id"], mr["iid"], self._comment_body(v), pos)
            except Exception:
                try:  # line not in diff -> post without position
                    self.gl.mr_discussion(mr["project_id"], mr["iid"],
                                          self._comment_body(v) + f"\n\n`{v.get('path')}:{v.get('line')}`")
                except Exception:
                    log.exception("gagal posting temuan AI")
                    continue
            v["posted"] = True
            self.store.kv_set(f"cq_ai_posted:{fp}", 1)
            n += 1
        return n

    def _summary_body(self, mr, vs, counts):
        if not vs:
            return ("✅ **Code Standard:** tidak ada pelanggaran standar pada commit terbaru "
                    f"(`{mr['sha'][:8]}`)." + self._footer())
        lines = [f"**Code Standard** · commit `{mr['sha'][:8]}` · "
                 f"❌ {counts['error']} error · ⚠️ {counts['warning']} warning · ℹ️ {counts['info']} info", "",
                 "| | Aturan | Lokasi | Keterangan |", "|---|---|---|---|"]
        for v in vs[:40]:
            loc = f"`{v.get('path')}:{v.get('line')}`" if v.get("path") else "-"
            msg = (v.get("message") or "").replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {SEV_ICON.get(v['severity'])} | `{v['rule']}` | {loc} | {msg} |")
        if len(vs) > 40:
            lines.append(f"\n…dan {len(vs) - 40} lainnya.")
        return "\n".join(lines) + self._footer()

    def _upsert_summary_note(self, pid, iid, mr, vs, counts):
        k = f"cq_note:{pid}:{iid}"
        body = self._summary_body(mr, vs, counts)
        note_id = self.store.kv_get(k)
        if not vs and not note_id:
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

