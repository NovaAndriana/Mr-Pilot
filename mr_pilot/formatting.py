"""Telegram message rendering (HTML parse mode)."""
import html
from datetime import datetime

from .teams import _JIRA

VERDICT_LABEL = {
    "APPROVE": "✅ Approve",
    "NEEDS_ATTENTION": "⚠️ Perlu perhatian",
    "REQUEST_CHANGES": "⛔ Request changes",
    "UNKNOWN": "❔ Belum ada review AI",
}
SOURCE_LABEL = {"llm": "AI (API)", "bot": "bot AI Review di MR", "none": "-"}
SEV_ICON = {"blocker": "🔴", "major": "🟠", "minor": "🟡", "info": "🔵"}
PIPE_ICON = {"success": "✅", "failed": "❌", "running": "⏳", "pending": "⏳",
             "created": "⏳", "canceled": "⚪", "skipped": "⚪", "manual": "✋"}
MAX_LEN = 3900
Q_ICON = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}


def e(s, attr=False):
    """HTML-escape for Telegram. attr=True also escapes quotes (for href="...")."""
    return html.escape("" if s is None else str(s), quote=attr)


def mr_ref(mr):
    refs = mr.get("references") or {}
    return refs.get("full") or f"!{mr.get('iid')}"


def _fit(lines, limit=MAX_LEN):
    """Join lines, cutting on whole lines so HTML tags never break."""
    out, size = [], 0
    for ln in lines:
        if size + len(ln) + 1 > limit:
            out.append("…(dipotong, lihat MR)")
            break
        out.append(ln)
        size += len(ln) + 1
    return "\n".join(out)


def _when(iso):
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone()
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        return ""


def detail_lines(mr, review):
    """Who made it, what changed, what it solves, what's good."""
    a = mr.get("author") or {}
    who = e(a.get("name") or a.get("username") or "-")
    if a.get("username"):
        who += f" (@{e(a['username'])})"
    jira = _JIRA.search(mr.get("title") or "") or _JIRA.search((mr.get("source_branch") or "").upper())
    meta = [x for x in (f"Jira {jira.group(1)}" if jira else "", _when(mr.get("created_at"))) if x]
    lines = ["<b>📝 Detail MR</b>", f"👤 <b>Pembuat:</b> {who}" + (f"  ·  {e(' · '.join(meta))}" if meta else "")]
    if review.get("solves"):
        lines += ["", "<b>🎯 Masalah yang diselesaikan:</b>", e(review["solves"])]
    if review.get("changes"):
        lines += ["", "<b>🔧 Perubahan:</b>"] + [f"• {e(c)}" for c in review["changes"][:6]]
    if review.get("good_points"):
        lines += ["", "<b>👍 Yang sudah bagus:</b>"] + [f"• {e(g)}" for g in review["good_points"][:4]]
    if len(lines) == 2:
        lines.append("<i>Deskripsi MR kosong, detail perubahan tidak tersedia.</i>")
    return lines


def quality_lines(q):
    if not q:
        return []
    c = q.get("counts") or {}
    if not q.get("total"):
        return ["", "<b>📏 Standar kode:</b> ✅ sesuai standar"]
    lines = ["", f"<b>📏 Standar kode:</b> ❌ {c.get('error', 0)} error · ⚠️ {c.get('warning', 0)} warning"
                 f" · ℹ️ {c.get('info', 0)} info"]
    for v in q.get("top") or []:
        loc = f" <code>{e(v['path'])}:{v['line']}</code>" if v.get("path") and v.get("line") else ""
        src = " (AI)" if v.get("source") == "ai" else ""
        lines.append(f"{Q_ICON.get(v['severity'], '•')} <code>{e(v['rule'])}</code>{loc}{src}")
        lines.append(f"   {e((v.get('message') or '')[:160])}")
    if q.get("total", 0) > len(q.get("top") or []):
        lines.append(f"   …{q['total'] - len(q['top'])} lainnya (lihat ringkasan di MR / dashboard)")
    if q.get("posted"):
        lines.append(f"<i>{q['posted']} warning sudah diposting ke commit/MR di GitLab.</i>")
    for err in q.get("rule_errors") or []:
        lines.append(f"<i>⚙️ {e(err)}</i>")
    return lines


def build_messages(mr, review, flags, header="🔔 MR baru untuk direview", quality=None):
    """Return (detail_text_or_None, card_text). Detail is merged into the card when it fits."""
    hp = mr.get("head_pipeline") or mr.get("pipeline") or {}
    st = hp.get("status") or "tidak ada"
    head = [
        f"<b>{e(header)}</b>",
        f"<b>{e(mr_ref(mr))}</b>  {e(mr.get('title'))}",
        f"🌿 <code>{e(mr.get('source_branch'))}</code> → <code>{e(mr.get('target_branch'))}</code>",
        f"🧪 Pipeline: {PIPE_ICON.get(st, '❔')} {e(st)}  ·  📄 {e(mr.get('changes_count') or '?')} file",
    ]
    rv = ["", f"<b>Verdict:</b> {VERDICT_LABEL.get(review.get('verdict'), review.get('verdict'))}"
              f"  <i>(sumber: {SOURCE_LABEL.get(review.get('source'), review.get('source'))})</i>"]
    if review.get("summary"):
        rv.append(e(review["summary"]))
    if review.get("breaking_changes"):
        rv += ["", "<b>💥 Breaking change:</b>"] + [f"• {e(b)}" for b in review["breaking_changes"][:5]]
    if review.get("findings"):
        rv += ["", "<b>Temuan:</b>"]
        for f in review["findings"][:8]:
            loc = f" <code>{e(f['file'])}</code>" if f.get("file") else ""
            rv.append(f"{SEV_ICON.get(f['severity'], '•')} <b>{e(f['title'])}</b>{loc}")
            if f.get("detail"):
                rv.append(f"   {e(f['detail'][:300])}")
    rv += quality_lines(quality)
    if flags:
        rv += ["", "<b>Cek otomatis:</b>"] + [f"• {e(x)}" for x in flags]

    detail = detail_lines(mr, review)
    combined = head + [""] + detail + rv
    if len("\n".join(combined)) <= MAX_LEN:
        return None, "\n".join(combined)
    # too long: detail goes in its own message, card keeps the review + buttons
    ref = [f"<b>{e(mr_ref(mr))}</b>  {e(mr.get('title'))}", ""]
    return _fit(ref + detail), _fit(head + rv)


def review_buttons(pid, iid, url, source_branch="ask"):
    """source_branch: ask -> Merge keeps the branch + extra "Merge + hapus branch"; keep; delete."""
    if source_branch == "delete":
        rows = [[("✅ Merge + hapus branch", f"md|{pid}|{iid}"), ("❌ Tolak", f"x|{pid}|{iid}")]]
    else:
        rows = [[("✅ Merge", f"m|{pid}|{iid}"), ("❌ Tolak", f"x|{pid}|{iid}")]]
        if source_branch == "ask":
            rows.append([("🗑️ Merge + hapus branch", f"md|{pid}|{iid}")])
    rows.append([("🔁 Review ulang", f"rr|{pid}|{iid}"), ("🔗 Buka MR", f"url:{url}")])
    return rows
