"""Review sources: LLM (Claude / OpenAI-compatible) or the existing AI-review bot comment."""
import json
import logging
import os
import re
import time
from datetime import datetime
from fnmatch import fnmatch


from .review_context import (build_review_diff, code_change_size, gather_files, has_test_change,
                             verify_findings)
from .util import short_error

log = logging.getLogger("mr_pilot.review")

VERDICTS = ("APPROVE", "NEEDS_ATTENTION", "REQUEST_CHANGES", "UNKNOWN")


def empty_review(source, summary, verdict="UNKNOWN"):
    return {"source": source, "verdict": verdict, "summary": summary,
            "findings": [], "breaking_changes": [],
            "solves": "", "changes": [], "good_points": []}


def _str_list(v, limit=6):
    if isinstance(v, str):
        v = [v]
    return [str(x).strip() for x in (v or []) if str(x).strip()][:limit]


# ---------------------------------------------------------------- diff utils
def build_diff_text(diffs, ignore, max_chars):
    parts, skipped, total, truncated = [], [], 0, False
    for d in diffs:
        path = d.get("new_path") or d.get("old_path") or "?"
        if any(fnmatch(path, p) or fnmatch(os.path.basename(path), p) for p in ignore):
            skipped.append(path)
            continue
        if d.get("new_file"):
            status = "file baru"
        elif d.get("deleted_file"):
            status = "dihapus"
        elif d.get("renamed_file"):
            status = f"rename dari {d.get('old_path')}"
        else:
            status = "diubah"
        body = d.get("diff") or "(diff kosong / terlalu besar)"
        chunk = f"\n### {path} ({status})\n{body}"
        if total + len(chunk) > max_chars:
            truncated = True
            skipped.append(path + " (tidak muat)")
            continue
        parts.append(chunk)
        total += len(chunk)
    return "".join(parts), skipped, truncated


# ----------------------------------------------------------- JSON handling
_FENCE = re.compile(r"```(?:json|JSON)?\s*\n(.*?)```", re.S)


def extract_json(text):
    """First JSON object in an LLM reply: fenced ```json block first, else scan for a decodable `{`.
    Tolerates prose before/after, braces in prose, and several objects."""
    text = (text or "").strip()
    dec = json.JSONDecoder()
    for block in _FENCE.findall(text) + [text]:
        block = block.strip()
        i = block.find("{")
        while i != -1:
            try:
                obj, _ = dec.raw_decode(block, i)
                if isinstance(obj, dict):
                    return obj
            except ValueError:
                pass
            i = block.find("{", i + 1)
    raise ValueError("Respons AI tidak berisi JSON yang valid")


_SEV_MAP = {"blocker": "blocker", "critical": "blocker", "kritis": "blocker", "high": "major", "major": "major",
            "medium": "minor", "minor": "minor", "low": "minor", "info": "info", "suggestion": "info"}
_VERDICT_MAP = {"APPROVE": "APPROVE", "APPROVED": "APPROVE", "LGTM": "APPROVE",
                "NEEDS_ATTENTION": "NEEDS_ATTENTION", "COMMENT": "NEEDS_ATTENTION",
                "REQUEST_CHANGES": "REQUEST_CHANGES", "CHANGES_REQUESTED": "REQUEST_CHANGES", "REJECT": "REQUEST_CHANGES"}


def validate_review(raw):
    """Raise if an LLM reply isn't a usable review (lets AIManager fall back to the next provider)."""
    data = extract_json(raw)
    if "verdict" not in data and "findings" not in data and "summary" not in data:
        raise ValueError("JSON review tidak punya verdict/summary/findings")
    return data


def normalize(data, source):
    if not isinstance(data, dict):
        data = {}
    verdict = _VERDICT_MAP.get(str(data.get("verdict", "")).strip().upper().replace(" ", "_").replace("-", "_"),
                               "NEEDS_ATTENTION")
    findings = []
    raw_findings = data.get("findings") or []
    if not isinstance(raw_findings, list):
        raw_findings = [raw_findings]
    for f in raw_findings:
        if isinstance(f, str):
            f = {"severity": "minor", "title": f}
        if not isinstance(f, dict):
            continue
        sev = _SEV_MAP.get(str(f.get("severity", "minor")).strip().lower(), "minor")
        file_ = str(f.get("file", "") or "").strip()
        line = f.get("line")
        if not line and re.search(r":\d+$", file_):  # "path:123" style
            file_, line = file_.rsplit(":", 1)
        try:
            line = int(line) if line not in (None, "") else None
        except (TypeError, ValueError):
            line = None
        findings.append({"severity": sev, "title": str(f.get("title", "")).strip()[:200],
                         "file": file_, "line": line,
                         "category": str(f.get("category", "") or "").strip().lower()[:20],
                         "evidence": str(f.get("evidence", "") or "").strip()[:600],
                         "detail": str(f.get("detail", "") or "").strip()[:800],
                         "suggestion": str(f.get("suggestion", "") or "").strip()[:600],
                         "confidence": str(f.get("confidence", "") or "").strip().lower()[:10]})
    order = {"blocker": 0, "major": 1, "minor": 2, "info": 3}
    findings = [f for f in findings if f["title"]][:8]
    findings.sort(key=lambda x: order[x["severity"]])
    # never "approve" with blocker/major findings
    if verdict == "APPROVE" and any(f["severity"] in ("blocker", "major") for f in findings):
        verdict = "NEEDS_ATTENTION"
    return {"source": source, "verdict": verdict,
            "summary": str(data.get("summary", "") or "").strip()[:1500],
            "findings": findings,
            "breaking_changes": _str_list(data.get("breaking_changes"), 5),
            "solves": str(data.get("solves", "") or "").strip(),
            "changes": _str_list(data.get("changes")),
            "good_points": _str_list(data.get("good_points"), 4),
            "risk": str(data.get("risk", "") or "").strip().lower() if str(data.get("risk", "")).strip().lower()
            in ("low", "medium", "high") else "",
            "tests": str(data.get("tests", "") or "").strip()[:300],
            "questions": _str_list(data.get("questions"), 3)}


# --------------------------------------------------------------- LLM review
SYSTEM_PROMPT = """Kamu Staff Engineer yang mereview merge request untuk Tech Lead. Tujuanmu: menemukan masalah
NYATA yang akan merusak produksi, keamanan, atau kontrak API, bukan memberi komentar sebanyak-banyaknya.

Cara kerja (lakukan di kepala, jangan ditulis):
1. Pahami tujuan MR dari judul, deskripsi, dan commit. Bandingkan dengan apa yang benar-benar diubah.
2. Untuk setiap perubahan logika, telusuri: dari mana data datang, bisa nil/kosong/duplikat?, siapa pemanggilnya,
   apa yang terjadi saat error, apakah transaksi/lock/konteks dibawa dengan benar. Pakai "isi lengkap file"
   untuk melihat kode di sekitar perubahan, jangan menebak.
3. Baru tulis temuan yang bisa kamu buktikan dengan baris kode.

Checklist (cek yang relevan dengan file yang berubah):
- Kontrak API: field response dihapus/ganti nama/ganti tipe, array<->object, null vs kosong, status code, validasi request.
- Security: authn/authz per endpoint & per resource (IDOR), query tanpa filter tenant/user, SQL/command injection,
  secret/token di kode atau log, data pribadi di log, input tidak divalidasi, CORS/CSRF.
- Go: error diabaikan (`_ =`, `_, _ :=`), error di-shadow, nil map/pointer, goroutine bocor/tanpa ctx, defer di loop,
  rows/body tidak di-Close, transaksi tanpa rollback, race (map/slice dipakai bersama), time zone, int overflow.
- TypeScript/React/React Native: deps useEffect/useMemo salah, state dimutasi langsung, promise tanpa catch,
  `any` di batas API, key list, dangerouslySetInnerHTML, re-render berat, kebocoran listener/timer.
- Data & DB: migrasi tidak backward-compatible, index hilang untuk query baru, N+1, query di dalam loop, pagination.
- Konkurensi & idempotensi: retry ganda, double submit, update tanpa kondisi.
- Test: logika baru/berubah tanpa test, test yang tidak menguji apa-apa, assertion keliru.

Kalibrasi severity:
- blocker: pasti salah di produksi, celah keamanan, kebocoran data, data rusak, atau breaking change tanpa koordinasi.
- major: bug yang sangat mungkin terjadi pada kasus nyata, error penting tidak ditangani, logika berisiko tanpa test.
- minor: maintainability/kejelasan yang layak diperbaiki. Gaya, format, dan selera pribadi TIDAK dilaporkan.

Aturan keras:
- Setiap temuan WAJIB punya "evidence": salin PERSIS 1-3 baris kode dari diff/file (tanpa nomor baris).
  Kalau tidak bisa menunjuk barisnya, jangan laporkan.
- "line" = nomor baris di file BARU (angka di kiri diff / isi file).
- Lebih baik 3 temuan tajam daripada 10 temuan lemah. Maksimal 8. Jangan mengulang hal yang sama.
- Kalau diff terpotong, sebutkan di summary dan jangan menebak isi yang tidak terlihat.
- verdict: REQUEST_CHANGES jika ada blocker; NEEDS_ATTENTION jika ada major atau ada pertanyaan penting;
  APPROVE jika hanya minor/tidak ada temuan.
- Tulis dalam bahasa @@LANGUAGE@@, singkat dan langsung ke inti.
@@EXTRA_RULES@@
@@STANDARDS@@
Jawab HANYA dengan JSON valid (tanpa teks lain) dengan skema:
{"verdict": "APPROVE|NEEDS_ATTENTION|REQUEST_CHANGES",
  "risk": "low|medium|high",
  "summary": "2-3 kalimat penilaianmu atas MR ini",
  "solves": "1-2 kalimat: masalah apa yang diselesaikan / manfaat MR ini",
  "changes": ["3-6 poin perubahan utama, bahasa sederhana"],
  "good_points": ["maks 4 hal yang memang bagus. Kosongkan jika tidak ada, jangan dibuat-buat"],
  "breaking_changes": ["..."],
  "tests": "1 kalimat: apakah perubahan penting sudah ter-cover test",
  "questions": ["maks 3 pertanyaan penting untuk author jika ada hal yang tidak jelas"],
  "findings": [{"severity": "blocker|major|minor",
                "category": "bug|security|breaking|performance|test|data|standard",
                "file": "path/file.go", "line": 123,
                "title": "judul singkat",
                "evidence": "baris kode persis",
                "detail": "kenapa ini masalah + dampaknya",
                "suggestion": "perbaikan konkret (boleh potongan kode singkat)",
                "confidence": "high|medium"}]}"""

VERIFY_PROMPT = """Kamu reviewer kedua yang skeptis. Reviewer pertama menulis temuan di bawah. Periksa SETIAP temuan
terhadap kode yang diberikan: apakah masalahnya benar-benar ada dan severity-nya tepat? Tolak temuan yang
spekulatif, sudah ditangani di kode lain yang terlihat, salah membaca kode, atau hanya soal gaya.
Jawab HANYA JSON: {"checks": [{"id": 0, "valid": true, "severity": "blocker|major|minor", "reason": "singkat"}]}"""


def build_system_prompt(language, extra_rules, standards=""):
    # str.replace, bukan .format: extra_rules/standar buatan user boleh berisi { } tanpa merusak prompt
    std = (f"\nStandar tim (pelanggaran dilaporkan dengan category \"standard\"):\n{standards}\n"
           if standards else "")
    return (SYSTEM_PROMPT.replace("@@LANGUAGE@@", str(language or "Indonesia"))
            .replace("@@EXTRA_RULES@@", str(extra_rules or "")).replace("@@STANDARDS@@", std))


# --------------------------------------------------------- bot comment parse
_FINDING_RE = re.compile(r"(BLOCKER|CRITICAL|KRITIS|MAJOR|KECIL|MINOR|⚠️|🔴|❌)", re.I)
_SKIP_RE = re.compile(r"\bAREA\b|✓|✅|DITERIMA|PASSED|\bSTATUS\b|minor note", re.I)
_GOOD_HDR_RE = re.compile(r"(highlights?|kelebihan|strengths?|poin positif|yang sudah bagus)\s*\**\s*:?", re.I)
_LIST_RE = re.compile(r"\s*(?:\d+[.)]|[-*•])\s+")


def _clean(s):
    # strip markdown markers but keep underscores inside names like quota_detail_test.go
    s = re.sub(r"\*\*|\*|`|(?<!\w)__|__(?!\w)|^\s*#+\s*|^\s*>\s*", "", s)
    return re.sub(r"\s+", " ", s).strip(" -•:")


def parse_bot_review(body):
    up = body.upper()
    status_m = re.search(r"STATUS\s*[:\-]?\s*[^\w\n]*\s*([A-Z _]+)", up)
    status = status_m.group(1).strip() if status_m else ""
    if "REQUEST" in status or "CHANGES" in status or "REJECT" in status:
        verdict = "REQUEST_CHANGES"
    elif "APPROV" in status:
        verdict = "APPROVE"
    elif re.search(r"REQUEST(ED)?\s+CHANGES|CHANGES\s+REQUESTED", up):
        verdict = "REQUEST_CHANGES"
    elif "APPROVED" in up:
        verdict = "APPROVE"
    else:
        verdict = "NEEDS_ATTENTION"

    summary, good = "", []
    m = re.search(r"(KESIMPULAN|CONCLUSION|SUMMARY|RINGKASAN)\s*\n(.+)", body, re.I | re.S)
    if m:
        lines = []
        for l in m.group(2).splitlines():
            if not l.strip() or re.match(r"\s*\**STATUS", l, re.I):
                continue
            if _LIST_RE.match(l) or re.match(r"\s*#", l):
                break
            gh = _GOOD_HDR_RE.search(l)
            if gh:
                lines.append(l[:gh.start()])
                break
            lines.append(l)
        summary = _clean(" ".join(lines[:4]))[:600]

    # good points: list under "Highlights:" (or similar); fallback to accepted lines
    hm = _GOOD_HDR_RE.search(body)
    if hm:
        for l in body[hm.end():].splitlines()[1:]:
            if not l.strip():
                if good:
                    break
                continue
            if not _LIST_RE.match(l):
                break
            good.append(_clean(_LIST_RE.sub("", l, count=1)))
    if not good:
        for l in body.splitlines():
            if re.search(r"✓|DITERIMA|PASSED", l) and not re.search(r"\bSTATUS\b", l, re.I):
                t = _clean(re.sub(r"✓|✅|DITERIMA|PASSED", "", l))
                if len(t) > 10:
                    good.append(t)
    good = [g[:200] for g in good if g][:4]

    findings = []
    for line in body.splitlines():
        mm = _FINDING_RE.search(line)
        if not mm:
            continue
        tag = mm.group(1).upper()
        if tag in ("BLOCKER", "CRITICAL", "KRITIS", "🔴", "❌"):
            sev = "blocker"
        elif tag == "MAJOR":
            sev = "major"
        else:
            sev = "minor"
        # skip section headings ("BLOCKER AREA — ...") and lines marked as passed
        if _SKIP_RE.search(line):
            continue
        title = _clean(line)
        if len(title) < 6:
            continue
        findings.append({"severity": sev, "title": title[:200], "file": "", "detail": ""})
    rv = empty_review("bot", summary, verdict)
    rv.update(findings=findings[:8], good_points=good)
    return rv


# --------------------------------------------------- MR description parsing
_REASON_HDR = re.compile(r"reason|alasan|latar|background|problem|masalah|why|tujuan|context|konteks", re.I)
_CHANGE_HDR = re.compile(r"^(?!.*breaking).*(change|perubahan|what|implement)", re.I)
_HDR_LINE = re.compile(r"^\s*(#{1,6}\s+(.+?)|\*\*([^*]+)\*\*\s*:?)\s*$")


def _sections(desc):
    """Split markdown into [(heading, level, [lines])]; level 9 = bold pseudo-heading."""
    out, cur = [], ("", 0, [])
    for line in (desc or "").splitlines():
        m = _HDR_LINE.match(line)
        if m:
            out.append(cur)
            if m.group(2):
                level = len(line.strip()) - len(line.strip().lstrip("#"))
                cur = (m.group(2).strip(), level, [])
            else:
                cur = (m.group(3).strip(), 9, [])
        else:
            cur[2].append(line)
    out.append(cur)
    return out


def describe_from_description(desc):
    """Best-effort 'solves' + 'changes' from the MR description written by the author."""
    secs = _sections(desc)
    solves, changes = [], []
    i = 0
    while i < len(secs):
        title, level, lines = secs[i]
        if title and _REASON_HDR.search(title) and not solves:
            for l in lines:
                t = _clean(_LIST_RE.sub("", l, count=1))
                if t and not re.match(r"^(jira|ticket|link)\b", t, re.I):
                    solves.append(t)
        elif title and _CHANGE_HDR.search(title) and not changes:
            # sub-headings under "Changes" are the change items; else its bullets
            j = i + 1
            while j < len(secs) and secs[j][0] and secs[j][1] > level:
                sub = _clean(re.sub(r"^\d+[.)]\s*", "", secs[j][0]))
                if sub and not re.match(r"^(pengujian|testing|test)\b", sub, re.I):
                    changes.append(sub)
                j += 1
            if not changes:
                changes = [_clean(_LIST_RE.sub("", l, count=1)) for l in lines if _LIST_RE.match(l)]
        i += 1
    text = " ".join(solves)
    if len(text) > 450:
        text = text[:450].rsplit(" ", 1)[0] + "…"
    return text, [c[:180] for c in changes if c][:6]


def _parse_dt(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


# ----------------------------------------------------------------- Reviewer
class Reviewer:
    def __init__(self, cfg, gl, ai=None):
        from .ai import AIManager
        self.full = cfg
        self.cfg = cfg["review"]
        self.gl = gl
        self.ai = ai or AIManager(cfg)

    def llm_available(self):
        return bool(self.ai.available("review"))

    def review(self, mr, first_seen, force_llm=False):
        """Return review dict, or None when still waiting for the bot comment."""
        rv = self._review(mr, first_seen, force_llm)
        if rv is not None:
            solves, changes = describe_from_description(mr.get("description"))
            if not rv.get("solves"):
                rv["solves"] = solves
            if not rv.get("changes"):
                rv["changes"] = changes
        return rv

    def _review(self, mr, first_seen, force_llm=False):
        mode = self.cfg["mode"]
        if (force_llm or mode == "llm") and self.llm_available():
            return self.from_llm(mr)
        if mode == "llm":
            return empty_review("none", "Belum ada provider AI yang siap. Atur di dashboard > AI atau jalankan setup.")

        bot = self.from_bot(mr)
        if bot:
            return bot
        if mode == "bot_then_llm" and self.llm_available():
            return self.from_llm(mr)  # bot comment not there yet: don't keep the lead waiting, review now
        waited = (time.time() - first_seen) / 60
        if waited < float(self.cfg["bot"]["wait_minutes"]):
            return None
        if mode == "bot_then_llm" and self.llm_available():
            return self.from_llm(mr)
        return empty_review("none", f"Komentar bot AI review belum muncul setelah "
                                    f"{int(waited)} menit. Cek manual di GitLab.")

    def from_bot(self, mr):
        pid, iid = mr["project_id"], mr["iid"]
        bot = self.cfg["bot"]
        users = {u.lower() for u in bot.get("usernames") or []}
        marker = (bot.get("marker") or "").lower()
        try:
            commits = self.gl.get_commits(pid, iid)
            newest = max((_parse_dt(c.get("created_at") or "") for c in commits if c.get("created_at")),
                         default=None)
        except Exception:
            newest = None
        for n in self.gl.get_notes(pid, iid):
            if n.get("system"):
                continue
            body = n.get("body") or ""
            uname = (n.get("author") or {}).get("username", "").lower()
            if not ((users and uname in users) or (marker and marker in body.lower())):
                continue
            # bots often edit one note per push: the edit time counts, not the creation time
            stamp = _parse_dt(n.get("updated_at") or "") or _parse_dt(n.get("created_at") or "")
            if newest and stamp and stamp < newest:
                return None  # bot review is for an older commit; wait for a new one
            return parse_bot_review(body)
        return None

    def _standards_text(self, paths, budget):
        cq = self.full.get("code_quality") or {}
        if budget <= 0 or not cq.get("enabled"):
            return ""
        try:
            from .standards import Standards
            std = Standards(cq, self.full.get("_base_dir", "."))
            text = std.documents({std.stack_of(p) for p in paths})
        except Exception:
            return ""
        return text[:budget]

    def from_llm(self, mr):
        llm = self.cfg["llm"]
        try:
            diffs = self.gl.get_diffs(mr["project_id"], mr["iid"])
            ignore = self.cfg.get("ignore_files") or []
            diff_text, skipped, truncated, included = build_review_diff(diffs, ignore, int(llm["max_diff_chars"]))
            files, files_raw = gather_files(self.gl, mr, diffs, ignore, int(llm.get("context_chars", 60000)))
            standards = self._standards_text(included, int(llm.get("standards_chars", 8000)))
            try:
                commits = [c.get("title") or (c.get("message") or "").split("\n")[0]
                           for c in self.gl.get_commits(mr["project_id"], mr["iid"])][:20]
            except Exception:
                commits = []
            system = build_system_prompt(llm.get("language", "Indonesia"), llm.get("extra_rules"), standards)
            user = (f"Judul MR: {mr['title']}\n"
                    f"Branch: {mr['source_branch']} -> {mr['target_branch']}\n"
                    f"Deskripsi:\n{(mr.get('description') or '-')[:6000]}\n\n"
                    f"Commit:\n" + "".join(f"- {c}\n" for c in commits) + "\n"
                    f"File dilewati: {', '.join(skipped) or '-'}\n"
                    f"Diff {'(TERPOTONG) ' if truncated else ''}(angka kiri = nomor baris di file baru):\n{diff_text}\n"
                    + (f"\nIsi lengkap file yang berubah (untuk konteks):\n{''.join(files)}" if files else ""))
            raw, provider = self.ai.complete(system, user, "review", validate=validate_review)
            rv = normalize(extract_json(raw), "llm")
            rv["provider"] = provider
            verify_findings(rv["findings"], "\n".join(d.get("diff") or "" for d in diffs), files_raw)
            if llm.get("verify", True) and any(f["severity"] in ("blocker", "major") for f in rv["findings"]):
                self._second_opinion(rv, diff_text, files)
            self._settle_verdict(rv)
            if code_change_size(diffs) >= 20 and not has_test_change(diffs):
                rv["flags"] = ["Logika berubah tanpa perubahan file test"]
            if truncated:
                rv["summary"] = (rv["summary"] + " (Diff terlalu besar, sebagian file tidak direview.)").strip()
            return rv
        except Exception as e:
            log.warning("LLM review gagal: %s", short_error(e))
            return empty_review("llm", f"Review AI gagal: {short_error(e)}")

    def _second_opinion(self, rv, diff_text, files):
        """Skeptical second pass over blocker/major findings: drop false positives, fix severity."""
        idx = [i for i, f in enumerate(rv["findings"]) if f["severity"] in ("blocker", "major")]
        items = [{"id": i, "severity": rv["findings"][i]["severity"], "file": rv["findings"][i]["file"],
                  "line": rv["findings"][i]["line"], "title": rv["findings"][i]["title"],
                  "evidence": rv["findings"][i]["evidence"], "detail": rv["findings"][i]["detail"]} for i in idx]
        user = (f"Temuan:\n{json.dumps(items, ensure_ascii=False, indent=1)}\n\nDiff:\n{diff_text[:60000]}\n"
                + (f"\nIsi file:\n{''.join(files)[:40000]}" if files else ""))
        try:
            raw, _ = self.ai.complete(VERIFY_PROMPT, user, "review", validate=extract_json)
            checks = extract_json(raw).get("checks") or []
        except Exception as e:
            log.info("Verifikasi temuan dilewati: %s", short_error(e))
            return
        drop = set()
        for c in checks if isinstance(checks, list) else []:
            if not isinstance(c, dict) or c.get("id") not in idx:
                continue
            f = rv["findings"][c["id"]]
            if c.get("valid") is False:
                drop.add(c["id"])
                continue
            sev = _SEV_MAP.get(str(c.get("severity", "")).lower())
            if sev in ("blocker", "major", "minor"):
                f["severity"] = sev
        if drop:
            log.info("Verifikasi membuang %s temuan yang tidak terbukti", len(drop))
        rv["findings"] = [f for i, f in enumerate(rv["findings"]) if i not in drop]
        rv["rejected_findings"] = len(drop)

    @staticmethod
    def _settle_verdict(rv):
        order = {"blocker": 0, "major": 1, "minor": 2, "info": 3}
        rv["findings"].sort(key=lambda x: order.get(x["severity"], 3))
        sevs = {f["severity"] for f in rv["findings"]}
        if "blocker" in sevs:
            rv["verdict"] = "REQUEST_CHANGES"
        elif rv["verdict"] == "APPROVE" and "major" in sevs:
            rv["verdict"] = "NEEDS_ATTENTION"
        elif rv["verdict"] == "REQUEST_CHANGES" and not rv.get("breaking_changes"):
            rv["verdict"] = "NEEDS_ATTENTION"  # nothing proven blocking after verification


# ------------------------------------------------------- deterministic flags
def heuristic_flags(mr, cfg):
    flags = []
    hp = mr.get("head_pipeline") or mr.get("pipeline") or {}
    st = hp.get("status")
    if st and st != "success":
        flags.append(f"Pipeline status: {st}")
    if not hp:
        flags.append("MR tidak punya pipeline")
    if mr.get("has_conflicts"):
        flags.append("Ada conflict dengan target branch")
    desc = (mr.get("description") or "")
    if not desc.strip():
        flags.append("Deskripsi MR kosong")
    if re.search(r"breaking", desc + " " + mr.get("title", ""), re.I):
        flags.append("Deskripsi menyebut BREAKING change, koordinasikan dengan konsumen API")
    try:
        if int(str(mr.get("changes_count") or "0").rstrip("+")) > 50:
            flags.append(f"Perubahan besar: {mr.get('changes_count')} file")
    except ValueError:
        pass
    if mr.get("target_branch") in ("main", "master", "production", "prod", "release"):
        flags.append(f"Target branch {mr.get('target_branch')}, cek lebih teliti")
    return flags
