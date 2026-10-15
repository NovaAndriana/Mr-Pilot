"""Post AI review findings to the MR as inline comments. When the AI gave replacement code for the exact
line, the comment carries a GitLab ```suggestion``` block, so the developer can click "Apply suggestion"."""
import logging

from . import voice
from .threads import ThreadTracker

log = logging.getLogger("mr_pilot.suggest")

def postable(review, severities=("blocker", "major")):
    """Findings worth a GitLab comment: serious (minor stays on Telegram), located (file + line) and not
    shown to be a hallucination."""
    return [f for f in (review or {}).get("findings") or []
            if f.get("file") and f.get("line") and f.get("verified") is not False
            and (severities is None or f.get("severity") in severities)]


def _fence(code):
    return "````" if "```" in code else "```"


def comment_body(f, inline=True, footer=""):
    """Written like a teammate's review comment; carries a ```suggestion``` block when inline + fix code."""
    return voice.finding_comment(f, inline=inline, footer=footer)


def _norm(s):
    return " ".join((s or "").split())


def has_fix(f):
    code = (f.get("fix_code") or "").strip()
    return bool(code) and _norm(code) != _norm(f.get("evidence"))


def post_findings(gl, store, mr, review, severities=("blocker", "major")):
    """Post each postable finding once per MR (not again on every commit while it is still open).
    Returns (posted, with_apply_button, skipped). Fixed ones are closed later by ThreadTracker.reconcile."""
    tracker = ThreadTracker(gl, store)
    posted = applicable = skipped = 0
    for f in postable(review, severities):
        item = {"source": "review", "path": f["file"], "line": int(f["line"]), "title": f.get("title"),
                "snippet": f.get("evidence") or "", "severity": f.get("severity"),
                "message": (f.get("detail") or f.get("title") or "")[:200]}
        if tracker.find(tracker.load(mr["project_id"], mr["iid"]), item, ("open", "resolved")):
            skipped += 1
            continue
        res = tracker.post(mr, item, lambda inline, f=f: comment_body(f, inline))
        if not res:
            continue
        posted += 1
        applicable += int(res["inline"] and has_fix(f))
    return posted, applicable, skipped
