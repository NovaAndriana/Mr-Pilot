"""Post AI review findings to the MR as inline comments. When the AI gave replacement code for the exact
line, the comment carries a GitLab ```suggestion``` block, so the developer can click "Apply suggestion"."""
import hashlib
import logging

from .util import short_error

log = logging.getLogger("mr_pilot.suggest")

SEV = {"blocker": "🔴 Blocker", "major": "🟠 Major", "minor": "🟡 Minor", "info": "🔵 Info"}


def postable(review):
    """Findings worth sending: located (file + line) and not shown to be a hallucination."""
    return [f for f in (review or {}).get("findings") or []
            if f.get("file") and f.get("line") and f.get("verified") is not False]


def _fence(code):
    return "````" if "```" in code else "```"


def comment_body(f, inline=True):
    parts = [f"**{SEV.get(f.get('severity'), 'Temuan')} · {f.get('title') or 'Temuan review'}**"]
    if f.get("detail"):
        parts.append(f["detail"])
    if f.get("suggestion"):
        parts.append(f"💡 {f['suggestion']}")
    code = (f.get("fix_code") or "").rstrip("\n")
    if code and _norm(code) != _norm(f.get("evidence")):
        fence = _fence(code)
        if inline:  # GitLab replaces the commented line with this block -> "Apply suggestion"
            parts.append(f"{fence}suggestion:-0+0\n{code}\n{fence}")
        else:
            parts.append(f"Saran kode untuk `{f['file']}:{f['line']}`:\n{fence}\n{code}\n{fence}")
    elif not inline:
        parts.append(f"`{f['file']}:{f['line']}`")
    parts.append("<sub>Saran review</sub>")
    return "\n\n".join(parts)


def _norm(s):
    return " ".join((s or "").split())


def has_fix(f):
    code = (f.get("fix_code") or "").strip()
    return bool(code) and _norm(code) != _norm(f.get("evidence"))


def post_findings(gl, store, mr, review, severities=("blocker", "major", "minor")):
    """Post each postable finding once (per MR commit). Returns (posted, with_apply_button, skipped)."""
    refs = mr.get("diff_refs") or {}
    posted = applicable = skipped = 0
    for f in postable(review):
        if f.get("severity") not in severities:
            continue
        fp = hashlib.sha1(f"{mr['project_id']}|{mr['iid']}|{mr.get('sha')}|{f['file']}|{f['line']}|"
                          f"{f.get('title')}".encode()).hexdigest()[:16]
        if store.kv_get(f"sugg:{fp}"):
            skipped += 1
            continue
        pos = None
        if refs.get("head_sha"):
            pos = {"position_type": "text", "base_sha": refs.get("base_sha"), "start_sha": refs.get("start_sha"),
                   "head_sha": refs.get("head_sha"), "new_path": f["file"], "old_path": f["file"],
                   "new_line": int(f["line"])}
        inline_ok = False
        try:
            if pos:
                gl.mr_discussion(mr["project_id"], mr["iid"], comment_body(f, True), pos)
                inline_ok = True
        except Exception as ex:  # line outside the diff: GitLab refuses the position
            log.info("posisi inline ditolak (%s:%s): %s", f["file"], f["line"], short_error(ex))
        if not inline_ok:
            try:
                gl.mr_discussion(mr["project_id"], mr["iid"], comment_body(f, False))
            except Exception as ex:
                log.warning("gagal posting temuan: %s", short_error(ex))
                continue
        store.kv_set(f"sugg:{fp}", 1)
        posted += 1
        applicable += int(inline_ok and has_fix(f))
    return posted, applicable, skipped
