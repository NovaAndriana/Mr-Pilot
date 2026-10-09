"""Smarter LLM review: richer context going in, evidence checks coming out.

Going in  : diff annotated with real line numbers, source files ordered by importance (code before
            tests/docs), full content of the most-changed files (so the model sees callers, nil checks,
            transactions around the change), commit messages, and the team's standard documents.
Coming out: every finding must quote its code. Quotes that don't exist in the MR are hallucinations:
            the finding is downgraded and marked unverified; wrong line numbers are corrected.
"""
import os
import re
from fnmatch import fnmatch

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_TEST = re.compile(r"(^|/)(tests?|__tests__|spec)/|_test\.go$|\.(test|spec)\.[jt]sx?$|_test\.py$|test_[^/]*\.py$", re.I)
_DOC = re.compile(r"\.(md|txt|rst|adoc)$|(^|/)docs?/", re.I)
_CONF = re.compile(r"\.(ya?ml|json|toml|ini|env\.example|lock|sum)$|(^|/)(Dockerfile|Makefile)$", re.I)


def file_kind(path):
    if _TEST.search(path):
        return "test"
    if _DOC.search(path):
        return "doc"
    if _CONF.search(path):
        return "config"
    return "code"


def changed_lines(diff):
    return sum(1 for ln in (diff or "").splitlines()
               if ln[:1] in "+-" and not ln.startswith(("+++", "---")))


def annotate_diff(diff):
    """Prefix added/context lines with their line number in the NEW file, so findings cite real lines."""
    out, n = [], None
    for ln in (diff or "").splitlines():
        m = _HUNK.match(ln)
        if m:
            n = int(m.group(1))
            out.append(ln)
            continue
        if n is None or ln.startswith("\\"):
            out.append(ln)
            continue
        if ln.startswith("-"):
            out.append(f"{'':>5} {ln}")
        else:  # '+' or context
            out.append(f"{n:>5} {ln}")
            n += 1
    return "\n".join(out)


def _ignored(path, ignore):
    return any(fnmatch(path, p) or fnmatch(os.path.basename(path), p) for p in ignore or [])


def order_diffs(diffs, ignore=()):
    """Code first (most changed first), then tests, config, docs. Ignored files dropped (returned separately)."""
    rank = {"code": 0, "test": 1, "config": 2, "doc": 3}
    keep, skipped = [], []
    for d in diffs:
        path = d.get("new_path") or d.get("old_path") or "?"
        if _ignored(path, ignore):
            skipped.append(path)
        else:
            keep.append(d)
    keep.sort(key=lambda d: (rank[file_kind(d.get("new_path") or d.get("old_path") or "")],
                             -changed_lines(d.get("diff"))))
    return keep, skipped


def build_review_diff(diffs, ignore, max_chars):
    """Annotated diff within budget. Returns (text, skipped, truncated, paths_included)."""
    ordered, skipped = order_diffs(diffs, ignore)
    parts, total, truncated, included = [], 0, False, []
    for d in ordered:
        path = d.get("new_path") or d.get("old_path") or "?"
        if d.get("new_file"):
            status = "file baru"
        elif d.get("deleted_file"):
            status = "dihapus"
        elif d.get("renamed_file"):
            status = f"rename dari {d.get('old_path')}"
        else:
            status = "diubah"
        body = annotate_diff(d.get("diff")) if d.get("diff") else "(diff kosong / terlalu besar)"
        chunk = f"\n### {path} ({status}, {file_kind(path)})\n{body}\n"
        if total + len(chunk) > max_chars:
            truncated = True
            skipped.append(path + " (tidak muat)")
            continue
        parts.append(chunk)
        total += len(chunk)
        included.append(path)
    return "".join(parts), skipped, truncated, included


def numbered(text, limit_lines=None):
    lines = text.splitlines()
    if limit_lines:
        lines = lines[:limit_lines]
    return "\n".join(f"{i:>5}| {ln}" for i, ln in enumerate(lines, 1))


def gather_files(gl, mr, diffs, ignore, budget, per_file_max=40000, max_files=12):
    """Full post-change content of the most-changed code files, within `budget` characters."""
    if budget <= 0:
        return [], {}
    ordered, _ = order_diffs(diffs, ignore)
    out, raw, used = [], {}, 0
    for d in ordered:
        if len(out) >= max_files:
            break
        path = d.get("new_path")
        if not path or d.get("deleted_file") or file_kind(path) in ("doc", "config"):
            continue
        try:
            text = gl.get_file_raw(mr["project_id"], path, mr.get("sha") or mr.get("source_branch"))
        except Exception:
            continue
        if not isinstance(text, str) or not text.strip() or "\x00" in text[:2000] or len(text) > per_file_max:
            continue
        block = f"\n### {path} (isi lengkap setelah perubahan)\n{numbered(text)}\n"
        if used + len(block) > budget:
            continue
        out.append(block)
        raw[path] = text
        used += len(block)
    return out, raw


# ------------------------------------------------------------- verification
def _norm(s):
    s = re.sub(r"^\s*\d*\s*[|+\- ]?\s?", "", s or "", flags=re.M)  # drop our line prefixes / diff markers
    return re.sub(r"\s+", " ", s).strip().lower()


def _evidence_lines(evidence):
    return [x for x in (_norm(ln) for ln in (evidence or "").splitlines()) if len(x) >= 6]


def verify_findings(findings, diff_raw, files_raw):
    """Check each finding's quoted code exists in the MR. Fix wrong line numbers when the file is known.
    Unverifiable blocker/major findings are downgraded one level and marked unverified."""
    corpus = _norm(diff_raw + "\n" + "\n".join(files_raw.values()))
    down = {"blocker": "major", "major": "minor"}
    for f in findings:
        ev = _evidence_lines(f.get("evidence"))
        found = bool(ev) and all(x in corpus for x in ev)
        f["verified"] = found
        if not found and f["severity"] in down:
            f["severity"] = down[f["severity"]]
            f["confidence"] = "low"
        path = (f.get("file") or "").split(":")[0]
        if found and path in files_raw and ev:
            lines = [_norm(x) for x in files_raw[path].splitlines()]
            hits = [i for i, x in enumerate(lines, 1) if ev[0] in x]
            if hits and f.get("line") not in hits:
                f["line"] = min(hits, key=lambda i: abs(i - int(f.get("line") or 0)))
    return findings


def has_test_change(diffs):
    return any(file_kind(d.get("new_path") or d.get("old_path") or "") == "test" for d in diffs)


def code_change_size(diffs):
    return sum(changed_lines(d.get("diff")) for d in diffs
               if file_kind(d.get("new_path") or d.get("old_path") or "") == "code")
