"""Review sources: LLM (Claude / OpenAI-compatible) or the existing AI-review bot comment."""
import json
import logging
import os
import re
import time
from datetime import datetime
from fnmatch import fnmatch


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
def extract_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Respons AI tidak berisi JSON")
    return json.loads(text[start:end + 1])


def normalize(data, source):
    verdict = str(data.get("verdict", "UNKNOWN")).upper().replace(" ", "_")
    if verdict not in VERDICTS:
        verdict = "NEEDS_ATTENTION"
    findings = []
    for f in data.get("findings") or []:
        if isinstance(f, str):
            f = {"severity": "minor", "title": f}
        sev = str(f.get("severity", "minor")).lower()
        if sev not in ("blocker", "major", "minor", "info"):
            sev = "minor"
        findings.append({"severity": sev, "title": str(f.get("title", "")).strip(),
                         "file": str(f.get("file", "") or "").strip(),
                         "detail": str(f.get("detail", "") or "").strip()})
    order = {"blocker": 0, "major": 1, "minor": 2, "info": 3}
    findings.sort(key=lambda x: order[x["severity"]])
    # never "approve" with blocker/major findings
    if verdict == "APPROVE" and any(f["severity"] in ("blocker", "major") for f in findings):
        verdict = "NEEDS_ATTENTION"
    return {"source": source, "verdict": verdict,
            "summary": str(data.get("summary", "")).strip(),
            "findings": findings,
            "breaking_changes": _str_list(data.get("breaking_changes"), 5),
            "solves": str(data.get("solves", "") or "").strip(),
            "changes": _str_list(data.get("changes")),
            "good_points": _str_list(data.get("good_points"), 4)}


# --------------------------------------------------------------- LLM review
SYSTEM_PROMPT = """Kamu adalah Tech Lead senior yang mereview merge request untuk timnya.
Review dengan teliti tapi ringkas. Prioritas pengecekan:
1. Breaking change kontrak API (bentuk request/response, field dihapus/berubah tipe, status code baru).
2. Security: authz, query tanpa filter, injection, secret/credential di kode, data sensitif di log.
3. Kebenaran logika & data: edge case, nil/null, transaksi, race condition, migrasi DB.
4. Test: apakah perubahan penting ter-cover, assertion yang salah.
5. Performa: N+1 query, loop berat, panggilan eksternal per item.
6. Konsistensi dengan deskripsi MR.
Aturan:
- Jangan mengarang. Hanya laporkan yang terlihat di diff. Kalau diff terpotong, sebutkan di summary.
- Maksimal 8 temuan, urutkan dari paling penting. Sertakan file:line bila bisa.
- verdict APPROVE hanya jika tidak ada temuan blocker/major.
- Tulis dalam bahasa {language}.
{extra_rules}
Jawab HANYA dengan JSON valid (tanpa teks lain) dengan skema:
{{"verdict": "APPROVE|NEEDS_ATTENTION|REQUEST_CHANGES",
  "summary": "2-3 kalimat penilaianmu atas MR ini",
  "solves": "1-2 kalimat: masalah apa yang diselesaikan / manfaat MR ini bagi produk atau user",
  "changes": ["3-6 poin perubahan utama, bahasa sederhana, mis. 'Response balance-deduction kini menampilkan kuota per user'"],
  "good_points": ["maks 4 hal yang memang bagus dari implementasinya (test, security fix, struktur kode). Kosongkan jika tidak ada, jangan dibuat-buat"],
  "breaking_changes": ["..."],
  "findings": [{{"severity": "blocker|major|minor", "file": "path:line", "title": "judul singkat", "detail": "penjelasan + saran"}}]}}"""


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
            created = _parse_dt(n.get("created_at") or "")
            if newest and created and created < newest:
                return None  # bot review is for an older commit; wait for a new one
            return parse_bot_review(body)
        return None

    def from_llm(self, mr):
        llm = self.cfg["llm"]
        try:
            diffs = self.gl.get_diffs(mr["project_id"], mr["iid"])
            diff_text, skipped, truncated = build_diff_text(
                diffs, self.cfg.get("ignore_files") or [], int(llm["max_diff_chars"]))
            system = SYSTEM_PROMPT.format(language=llm.get("language", "Indonesia"),
                                          extra_rules=llm.get("extra_rules") or "")
            user = (f"Judul MR: {mr['title']}\n"
                    f"Branch: {mr['source_branch']} -> {mr['target_branch']}\n"
                    f"Deskripsi:\n{(mr.get('description') or '-')[:6000]}\n\n"
                    f"File dilewati: {', '.join(skipped) or '-'}\n"
                    f"Diff {'(TERPOTONG) ' if truncated else ''}:\n{diff_text}")
            raw, provider = self.ai.complete(system, user, "review")
            rv = normalize(extract_json(raw), "llm")
            rv["provider"] = provider
            return rv
        except Exception as e:
            log.exception("LLM review gagal")
            return empty_review("llm", f"Review AI gagal: {e}")


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
