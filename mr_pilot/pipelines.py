"""Pipeline watch: when the pipeline of an MR you review turns red, read the failed job logs and explain
why in plain language (AI when available, error-line extraction otherwise). Also tells you when a
pipeline you were waiting on to merge turns green."""
import logging
import re

from .formatting import e
from .reviewer import extract_json
from .util import redact, short_error
from .voice import VOICE_RULES, pipeline_comment

log = logging.getLogger("mr_pilot.pipeline")

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07|section_(start|end):\d+:[\w.-]+\r?")
_ERR = re.compile(r"(error|fail(ed|ure)?|panic|fatal|exception|traceback|cannot|undefined|not found|denied|"
                  r"timed? ?out|exit (code|status) [1-9]|npm ERR|--- FAIL|✕|×|expected|assert|"
                  r"[\w./-]+\.(go|ts|tsx|js|jsx|py|java|kt|rb|php|rs|cs|swift|dart|vue):\d+)", re.I)
_NOISE = re.compile(r"^(\$ |Running with gitlab-runner|Preparing |Getting source|Fetching changes|Checking out|"
                    r"Skipping Git|Executing \"step_script\"|Cleaning up|Uploading artifacts|Saving cache|"
                    r"Restoring cache|Using docker image|Pulling docker image|Created fresh repository)")

SYSTEM = """Kamu engineer DevOps senior. Jelaskan kenapa job CI/CD GitLab ini gagal kepada Tech Lead yang masih
baru mengenal CI dan Kubernetes. Bahasa @@LANGUAGE@@ yang sederhana, langsung ke inti, tanpa basa-basi.
Baca log dari bawah: penyebab sebenarnya biasanya ada di baris error pertama yang relevan, bukan baris terakhir.
Bedakan masalah kode (test gagal, compile error, lint) dari masalah infrastruktur (runner, jaringan, registry,
kuota, timeout, flaky) yang cukup di-retry.
Jawab HANYA JSON:
{"category": "test|build|lint|dependency|deploy|infra|flaky|config",
 "cause": "1-2 kalimat penyebabnya",
 "where": "file:baris / nama test / step yang gagal, kosong jika tidak jelas",
 "fix": "langkah perbaikan konkret untuk developer (1-3 kalimat)",
 "code_problem": true,
 "confidence": "high|medium|low"}
"cause" dan "fix" juga dipasang sebagai komentar di MR untuk developernya.
"""
SYSTEM += VOICE_RULES

CATEGORY = {"test": "Test gagal", "build": "Build/compile gagal", "lint": "Lint/format", "dependency": "Dependency",
            "deploy": "Deploy", "infra": "Infrastruktur/runner", "flaky": "Kemungkinan flaky", "config": "Konfigurasi CI"}


def clean_log(text):
    return redact(_ANSI.sub("", text or "").replace("\r\n", "\n"))


def error_lines(text, limit=14):
    """The most telling lines of a job log: error-looking lines (deduplicated), newest kept."""
    out, seen = [], set()
    for ln in clean_log(text).splitlines():
        s = ln.strip()
        if not s or len(s) < 4 or _NOISE.match(s) or not _ERR.search(s):
            continue
        key = re.sub(r"\d+", "#", s)[:120]
        if key in seen:
            continue
        seen.add(key)
        out.append(s[:220])
    return out[-limit:]


class PipelineWatcher:
    ACTIVE = ("notified", "waiting_bot", "error", "notify_failed")

    def __init__(self, cfg, gl, store, ai, send, event):
        self.cfg = cfg.get("pipeline") or {}
        self.sb_mode = (cfg.get("merge") or {}).get("source_branch", "ask")
        self.lang = ((cfg.get("review") or {}).get("llm") or {}).get("language", "Indonesia")
        self.gl, self.store, self.ai, self.send, self.event = gl, store, ai, send, event

    @property
    def enabled(self):
        return bool(self.cfg.get("enabled", True))

    def check_all(self):
        if not self.enabled:
            return
        for rec in self.store.by_status(*self.ACTIVE):
            try:
                self.check(rec)
            except Exception as ex:
                log.warning("cek pipeline !%s: %s", rec.get("iid"), short_error(ex))

    def check(self, rec):
        pid, iid = rec["project_id"], rec["iid"]
        key = f"{pid}:{iid}"
        pls = self.gl.mr_pipelines(pid, iid) or []
        if not pls:
            return None
        p = pls[0]
        state = f"{p.get('id')}:{p.get('status')}"
        prev = self.store.kv_get(f"pipe:{key}")
        if prev == state:
            return None
        self.store.kv_set(f"pipe:{key}", state)
        self.store.upsert(key, pipeline=p.get("status"))
        status = p.get("status")
        if status == "failed" and not self.store.kv_get(f"pipe_explained:{pid}:{p['id']}"):
            self.store.kv_set(f"pipe_explained:{pid}:{p['id']}", 1)
            return self.explain(rec, p)
        if status == "success":
            was_red = bool(prev and prev.endswith(":failed"))
            if self.store.kv_get(f"wait_green:{key}") or was_red:
                self.store.kv_del(f"wait_green:{key}")
                from .formatting import review_buttons
                self.send(f"✅ Pipeline <a href=\"{e(rec.get('web_url'), True)}\">!{iid}</a> "
                          f"{'kembali ' if was_red else ''}hijau. {e(rec.get('title'))}\nSiap di-merge.",
                          buttons=review_buttons(pid, iid, rec.get("web_url") or "", self.sb_mode))
                return "green"
        return None

    # ------------------------------------------------------------------ explain
    def explain(self, rec, p):
        pid, iid = rec["project_id"], rec["iid"]
        jobs = (self.gl.failed_jobs(pid, p["id"]) or [])
        jobs = [j for j in jobs if not j.get("allow_failure")][:3] or jobs[:1]
        if not jobs:
            return None
        parts, lines = [], []
        for j in jobs:
            try:
                trace = clean_log(self.gl.job_trace(pid, j["id"], int(self.cfg.get("log_chars", 20000))))
            except Exception as ex:
                trace = f"(log tidak bisa dibaca: {short_error(ex)})"
            parts.append(f"### Job {j.get('name')} (stage {j.get('stage')})\n{trace[-12000:]}")
            lines += error_lines(trace)
        info = self._ai_explain(rec, jobs, "\n\n".join(parts)) or {}
        main = jobs[0]
        job_links = ", ".join(f"<a href=\"{e(j.get('web_url'), True)}\">{e(j.get('name'))}</a>" for j in jobs)
        msg = [f"🔴 <b>Pipeline gagal</b> <a href=\"{e(rec.get('web_url'), True)}\">!{iid}</a> {e(rec.get('title'))}",
               f"Job: {job_links} · stage <code>{e(main.get('stage'))}</code>"]
        if info.get("cause"):
            cat = CATEGORY.get(info.get("category"), "")
            msg += ["", f"<b>Penyebab{(' (' + e(cat) + ')') if cat else ''}:</b> {e(info['cause'])}"]
            if info.get("where"):
                msg.append(f"<b>Lokasi:</b> <code>{e(info['where'])}</code>")
            if info.get("fix"):
                msg.append(f"<b>Saran:</b> {e(info['fix'])}")
            if info.get("code_problem") is False:
                msg.append("<i>Kemungkinan bukan masalah kode: coba Retry job dulu.</i>")
        if lines:
            snippet = "\n".join(lines[-10:])[-1400:]
            msg += ["", "<b>Baris error di log:</b>", f"<pre>{e(snippet)}</pre>"]
        self.send("\n".join(msg))
        if self.cfg.get("comment_on_mr", True) and info.get("cause"):
            body = pipeline_comment(main, info)
            try:
                self.gl.add_note(pid, iid, body)
            except Exception as ex:
                log.warning("komentar pipeline !%s gagal: %s", iid, short_error(ex))
        self.event("pipeline", f"Pipeline !{iid} gagal di {main.get('name')}",
                   info.get("cause") or (lines[-1] if lines else ""), f"{pid}:{iid}", "error")
        return info or {"lines": lines}

    def _ai_explain(self, rec, jobs, logs):
        if not self.ai or not self.ai.available("pipeline") or getattr(self.ai, "economy", lambda t: False)("pipeline"):
            return None
        user = (f"MR: {rec.get('title')}\nBranch: {rec.get('source_branch')} -> {rec.get('target_branch')}\n"
                f"Job gagal: {', '.join(str(j.get('name')) for j in jobs)}\n\nLog (bagian akhir):\n{logs}")
        try:
            raw, _ = self.ai.complete(SYSTEM.replace("@@LANGUAGE@@", self.lang), user, "pipeline",
                                      validate=extract_json)
            data = extract_json(raw)
        except Exception as ex:
            log.info("Penjelasan AI pipeline gagal: %s", short_error(ex))
            return None
        out = {k: str(data.get(k) or "").strip()[:500] for k in ("category", "cause", "where", "fix")}
        out["category"] = out["category"].lower()
        out["code_problem"] = data.get("code_problem") is not False
        return out if out["cause"] else None
