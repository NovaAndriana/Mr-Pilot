"""How MR Pilot writes on GitLab: like a teammate reviewing, not like a bot.

No badges, rule ids, emoji headers, tables or "automatic" footers. Every comment still says what the
problem is, why it matters (with a concrete scenario when we have one), where it is, and how to fix it.
Phrasing varies a little (deterministically per comment) so a page of comments doesn't read templated."""
import hashlib
import re

_CLOSE_ERROR = ["Yang ini perlu dibereskan dulu sebelum merge ya.",
                "Tolong diperbaiki dulu sebelum di-merge.",
                "Ini perlu diubah sebelum bisa di-merge."]
_LEAD_MAJOR = ["", "", "Coba dicek lagi bagian ini. "]


def _pick(options, *seed):
    h = int(hashlib.sha1("|".join(str(s) for s in seed).encode()).hexdigest(), 16)
    return options[h % len(options)]


_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐⭕‼⁉️]")


def humanize(text):
    """Remove the usual machine tells from model-written text: em dashes, emoji, label prefixes."""
    t = (text or "").strip()
    t = _EMOJI.sub("", t)
    t = t.replace(" — ", ", ").replace("—", ", ").replace(" – ", ", ")
    t = re.sub(r"^(Catatan|Temuan|Masalah|Saran|Rekomendasi|Note|Issue)\s*:\s*", "", t, flags=re.I)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


def _first_lower(s):
    return s[:1].lower() + s[1:] if s and not s[:2].isupper() else s


def _join(*parts):
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def _footer(footer):
    return f"\n\n<sub>{footer}</sub>" if footer else ""


# --------------------------------------------------------------- standards
def standard_comment(v, inline=True, footer=""):
    """Comment for one code-standard finding (rule- or AI-based). `inline` = posted on the line itself."""
    msg = humanize(v.get("message") or v.get("rule") or "")
    sev = v.get("severity")
    seed = (v.get("rule"), v.get("path"), v.get("line"))
    where = f"`{v['path']}:{v['line']}`" if v.get("path") and v.get("line") else (
        f"`{v['path']}`" if v.get("path") else "")
    lead = msg
    if sev == "info":
        lead = "nit: " + _first_lower(msg)
    if not inline and where:
        lead = f"Di {where}: {_first_lower(lead) if sev != 'info' else lead}"
    snippet = ""
    if not inline and v.get("snippet") and v.get("source") != "ai" and not str(v.get("rule") or "").startswith("migration-"):
        snippet = f"```\n{v['snippet']}\n```"
    close = _pick(_CLOSE_ERROR, *seed) if sev == "error" else ""
    return _join(lead, snippet, humanize(v.get("why")), v.get("fix"), close) + _footer(footer)


def overflow_comment(rest, footer=""):
    lines = [f"- `{v.get('path') or '-'}{':' + str(v['line']) if v.get('line') else ''}`: {humanize(v.get('message'))}"
             for v in rest[:30]]
    more = f"\n- …dan {len(rest) - 30} lainnya" if len(rest) > 30 else ""
    return f"Masih ada {len(rest)} catatan lain di commit ini:\n\n" + "\n".join(lines) + more + _footer(footer)


_GROUPS = (("error", "Perlu diperbaiki sebelum merge"), ("warning", "Sebaiknya dirapikan"),
           ("info", "Catatan kecil"))


def summary_note(sha, vs, had_before=False, footer=""):
    """The one MR-level note that is kept up to date per commit. None = nothing worth posting."""
    short = (sha or "")[:8]
    if not vs:
        if not had_before:
            return None
        return f"Update untuk commit `{short}`: catatan standar kode sebelumnya sudah beres semua. Thanks!" + \
            _footer(footer)
    out = [f"Catatan review standar kode untuk commit `{short}`:"]
    for sev, title in _GROUPS:
        items = [v for v in vs if v.get("severity") == sev]
        if not items:
            continue
        out.append(f"\n**{title}**\n")
        for v in items[:20]:
            loc = f"`{v['path']}:{v['line']}` " if v.get("path") and v.get("line") else (
                f"`{v['path']}` " if v.get("path") else "")
            out.append(f"- {loc}{humanize(v.get('message'))}")
        if len(items) > 20:
            out.append(f"- …dan {len(items) - 20} lainnya")
    if any(v.get("path") and v.get("line") for v in vs):
        out.append("\nDetail tiap poin ada di komentar pada baris kodenya.")
    return "\n".join(out) + _footer(footer)


# ----------------------------------------------------------------- review
def finding_comment(f, inline=True, footer=""):
    """AI review finding as a reviewer comment; with a GitLab suggestion block when we have the fix code."""
    sev = f.get("severity")
    detail = humanize(f.get("detail")) or humanize(f.get("title"))
    if sev == "minor":
        detail = "nit: " + _first_lower(detail)
    elif sev == "major":
        detail = _pick(_LEAD_MAJOR, f.get("file"), f.get("line"), f.get("title")) + detail
    where = ""
    if not inline and f.get("file"):
        where = f"Di `{f['file']}{':' + str(f['line']) if f.get('line') else ''}`:"
    code = (f.get("fix_code") or "").rstrip("\n")
    block = ""
    if code and " ".join(code.split()) != " ".join((f.get("evidence") or "").split()):
        fence = "````" if "```" in code else "```"
        block = f"{fence}suggestion:-0+0\n{code}\n{fence}" if inline else f"Kira-kira seperti ini:\n{fence}\n{code}\n{fence}"
    close = _pick(_CLOSE_ERROR, f.get("file"), f.get("line")) if sev == "blocker" else ""
    return _join(where, detail, humanize(f.get("suggestion")), block, close) + _footer(footer)


# --------------------------------------------------------------- pipeline
def pipeline_comment(job, info, footer=""):
    cause = humanize(info.get("cause"))
    parts = [f"Pipeline-nya gagal di job `{job.get('name')}` (stage `{job.get('stage')}`). {cause}".strip()]
    if info.get("where"):
        parts.append(f"Sumbernya di `{info['where']}`.")
    if info.get("fix"):
        parts.append(humanize(info["fix"]))
    if info.get("code_problem") is False:
        parts.append("Kelihatannya bukan dari kodenya, coba Retry job-nya dulu.")
    if job.get("web_url"):
        parts.append(f"Log lengkapnya: {job['web_url']}")
    return _join(*parts) + _footer(footer)


# ----------------------------------------------------------- prompt rules
VOICE_RULES = """Gaya tulisan untuk teks yang akan dibaca developer (detail, suggestion, message, cause, fix):
- Tulis seperti reviewer manusia ke rekan setim: bahasa Indonesia santai tapi profesional, kalimat utuh.
- Kalimat pertama langsung menyebut masalah konkretnya, sebut nama variabel/fungsi yang terlibat.
- Jelaskan dampaknya dengan skenario nyata (input apa, kondisi apa, apa yang terjadi).
- Tutup dengan cara memperbaikinya.
- Jangan pakai emoji, judul, label seperti "Masalah:" / "Saran:", tanda pisah panjang, atau kata "AI"."""
