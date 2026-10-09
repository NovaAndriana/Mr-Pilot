"""Code standard engine: stacks, written standards (markdown), regex rules, conventions."""
import logging
import os
import re
from fnmatch import fnmatch

import yaml

log = logging.getLogger("mr_pilot.standards")

SEVERITIES = ("error", "warning", "info")
SEV_RANK = {"error": 0, "warning": 1, "info": 2}
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_CHECKBOX = re.compile(r"^\s*[-*]\s*\[( |x|X)\]\s*(.+?)\s*$")


def glob_match(path, pattern):
    """fnmatch where '**/' also matches zero directories."""
    path = path.replace("\\", "/")
    if fnmatch(path, pattern):
        return True
    if pattern.startswith("**/") and fnmatch(path, pattern[3:]):
        return True
    return "/" not in pattern and fnmatch(os.path.basename(path), pattern)


def any_match(path, patterns):
    return any(glob_match(path, p) for p in patterns or [])


def added_lines(diff):
    """Yield (new_line_number, text) for every '+' line in a unified diff."""
    line_no = None
    for raw in (diff or "").splitlines():
        m = _HUNK.match(raw)
        if m:
            line_no = int(m.group(1))
            continue
        if line_no is None or raw.startswith("\\"):
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            yield line_no, raw[1:]
            line_no += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
        else:
            line_no += 1


def annotate_diff(diff):
    """Diff with new-file line numbers, so an AI can cite exact lines."""
    out, line_no = [], None
    for raw in (diff or "").splitlines():
        m = _HUNK.match(raw)
        if m:
            line_no = int(m.group(1))
            out.append(raw)
            continue
        if line_no is None:
            continue
        if raw.startswith("+"):
            out.append(f"L{line_no:<5}+{raw[1:]}")
            line_no += 1
        elif raw.startswith("-"):
            out.append(f"      -{raw[1:]}")
        elif not raw.startswith("\\"):
            out.append(f"L{line_no:<5} {raw[1:]}")
            line_no += 1
    return "\n".join(out)


def violation(rule, severity, message, path="", line=None, source="rule", snippet="", stack=""):
    return {"rule": rule, "severity": severity if severity in SEVERITIES else "warning",
            "message": message, "path": path, "line": line, "source": source,
            "snippet": (snippet or "").strip()[:200], "stack": stack}


class Standards:
    def __init__(self, cq_cfg, base_dir="."):
        self.cfg = cq_cfg
        d = cq_cfg.get("standards_dir") or "standards"
        self.dir = d if os.path.isabs(d) else os.path.join(base_dir, d)
        self.stacks = cq_cfg.get("stacks") or {}
        self.rules, self.rule_errors = [], []
        self._mtime = None
        self.reload()

    # ------------------------------------------------------------- loading
    def path(self, name):
        return os.path.join(self.dir, name)

    def read(self, name):
        p = self.path(name)
        if not name or not os.path.exists(p):
            return ""
        with open(p, encoding="utf-8") as f:
            return f.read()

    def _rules_mtime(self):
        p = self.path(self.cfg.get("rules_file") or "rules.yaml")
        return os.path.getmtime(p) if os.path.exists(p) else None

    def reload(self, force=False):
        mt = self._rules_mtime()
        if not force and mt == self._mtime and self._mtime is not None:
            return
        self._mtime = mt
        self.rules, self.rule_errors = [], []
        raw = self.read(self.cfg.get("rules_file") or "rules.yaml")
        if not raw:
            return
        try:
            data = yaml.safe_load(raw) or {}
        except yaml.YAMLError as ex:
            self.rule_errors.append(f"rules.yaml tidak valid: {ex}")
            log.error(self.rule_errors[-1])
            return
        for r in data.get("rules") or []:
            if not isinstance(r, dict) or r.get("enabled") is False:
                continue
            try:
                rx = re.compile(r["pattern"])
            except (KeyError, re.error) as ex:
                self.rule_errors.append(f"rule {r.get('id')}: pattern tidak valid ({ex})")
                continue
            stacks = r.get("stacks") or ["*"]
            if isinstance(stacks, str):
                stacks = [s.strip() for s in stacks.split(",")]
            self.rules.append({"id": r.get("id") or r["pattern"][:30], "rx": rx, "stacks": stacks,
                               "severity": str(r.get("severity", "warning")).lower(),
                               "message": r.get("message") or r.get("id"),
                               "paths": r.get("paths") or [], "exclude": r.get("exclude_paths") or []})

    # -------------------------------------------------------------- stacks
    def stack_of(self, path):
        for sid, s in self.stacks.items():
            if any_match(path, s.get("paths")) and not any_match(path, s.get("exclude")):
                return sid
        return None

    def stack_name(self, sid):
        return (self.stacks.get(sid) or {}).get("name") or sid or "Umum"

    def documents(self, stack_ids):
        parts = []
        gen = self.read(self.cfg.get("general_document") or "")
        if gen:
            parts.append(gen)
        for sid in sorted(s for s in stack_ids if s):
            doc = self.read((self.stacks.get(sid) or {}).get("document") or "")
            if doc:
                parts.append(doc)
        return "\n\n---\n\n".join(parts)

    # ---------------------------------------------------------- rule check
    def check_file_diff(self, path, diff, ignore=()):
        if not path or any_match(path, ignore):
            return []
        sid = self.stack_of(path)
        rules = [r for r in self.rules
                 if ("*" in r["stacks"] or (sid and sid in r["stacks"]))
                 and (not r["paths"] or any_match(path, r["paths"]))
                 and not any_match(path, r["exclude"])]
        if not rules:
            return []
        out = []
        for line_no, text in added_lines(diff):
            for r in rules:
                if r["rx"].search(text):
                    out.append(violation(r["id"], r["severity"], r["message"], path, line_no,
                                         "rule", text, sid or ""))
        return out

    def check_diffs(self, diffs, ignore=()):
        out = []
        for d in diffs:
            if d.get("deleted_file"):
                continue
            out += self.check_file_diff(d.get("new_path") or d.get("old_path"), d.get("diff"), ignore)
        return out

    # --------------------------------------------------------- conventions
    def check_title(self, title, branch=None):
        pat = (self.cfg.get("conventions") or {}).get("mr_title_pattern")
        if pat and not re.search(pat, title or ""):
            hint = suggest_title(title, branch)
            if hint and not re.search(pat, hint):
                hint = None
            msg = "Judul MR belum mengikuti format `tipe(TIKET): deskripsi`, mis. `feat(IDAS-123): tampilkan kuota`."
            if hint:
                msg += f" Saran: `{hint}`"
            return [violation("mr-title-convention", "warning", msg, source="convention", snippet=title)]
        return []

    def check_commit_message(self, message):
        conv = self.cfg.get("conventions") or {}
        pat = conv.get("commit_message_pattern")
        first = (message or "").strip().splitlines()[0] if (message or "").strip() else ""
        if not pat or re.match(r"^(Merge|Revert) ", first):
            return []
        if not re.search(pat, first):
            return [violation("commit-message-convention", conv.get("commit_message_severity", "info"),
                              "Pesan commit tidak sesuai konvensi. Contoh: fix(IDAS-123): validasi phases kosong",
                              source="convention", snippet=first)]
        return []

    def checklist_items(self):
        name = (self.cfg.get("conventions") or {}).get("pr_checklist") or ""
        items = []
        for line in self.read(name).splitlines():
            m = _CHECKBOX.match(line)
            if m:
                items.append(m.group(2))
        return items

    def check_checklist(self, description):
        """off | present | warn_unchecked (default: the MR's OWN checklist must be ticked, whatever its items)
        | match_template (also every item of the pr_checklist file must be in the MR)."""
        conv = self.cfg.get("conventions") or {}
        mode = conv.get("require_pr_checklist", "warn_unchecked")
        if mode in (False, "off", None):
            return []
        found, labels = {}, {}
        for line in (description or "").splitlines():
            m = _CHECKBOX.match(line)
            if m:
                found[_norm(m.group(2))] = m.group(1).lower() == "x"
                labels[_norm(m.group(2))] = m.group(2).strip()
        if not found:
            return [violation("pr-checklist-missing", "info",
                              "Deskripsi MR belum memuat checklist (kotak - [ ]). Pakai template MR tim.",
                              source="convention")]
        if mode == "present":
            return []
        if mode != "match_template":
            unchecked = [labels[k] for k, done in found.items() if not done]
            if not unchecked:
                return []
            return [violation("pr-checklist-unchecked", "info",
                              f"{len(unchecked)} item checklist belum dicentang: " + "; ".join(unchecked[:5]),
                              source="convention")]
        expected = self.checklist_items()
        if not expected:
            return []
        unchecked = [it for it in expected if found.get(_norm(it)) is False]
        missing = [it for it in expected if _norm(it) not in found]
        out = []
        if unchecked:
            out.append(violation("pr-checklist-unchecked", "info",
                                 f"{len(unchecked)} item checklist belum dicentang: " + "; ".join(unchecked[:5]),
                                 source="convention"))
        if missing:
            out.append(violation("pr-checklist-incomplete", "info",
                                 f"{len(missing)} item checklist tidak ada: " + "; ".join(missing[:5]),
                                 source="convention"))
        return out


_TICKET = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z]+)[-_ ]?(\d+)(?!\d)")
_TYPES = ("feat", "fix", "refactor", "perf", "test", "docs", "chore", "ci", "build", "revert")
_TYPE_ALIASES = {"feature": "feat", "bugfix": "fix", "hotfix": "fix", "bug": "fix", "doc": "docs"}


def suggest_title(title, branch=None):
    """Conventional title from what the author already wrote: 'Idas 5327/fix role signature' or branch
    'IDAS-5327/fix-role-signature' -> 'fix(IDAS-5327): role signature'."""
    for src in (branch or "", title or ""):
        m = _TICKET.search(src)
        if not m:
            continue
        ticket = f"{m.group(1).upper()}-{m.group(2)}"
        rest = re.split(r"[/\s_-]+", (src[:m.start()] + " " + src[m.end():]).strip(" /:-_"))
        words = [w for w in rest if w]
        kind = None
        if words and (words[0].lower() in _TYPES or words[0].lower() in _TYPE_ALIASES):
            w = words.pop(0).lower()
            kind = _TYPE_ALIASES.get(w, w)
        if not words:
            continue
        return f"{kind or 'feat'}({ticket}): {' '.join(words).lower()}"
    return None


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def sort_violations(vs):
    return sorted(vs, key=lambda v: (SEV_RANK.get(v["severity"], 3), v.get("path") or "", v.get("line") or 0))


def count_by_severity(vs):
    c = {s: 0 for s in SEVERITIES}
    for v in vs:
        c[v["severity"]] = c.get(v["severity"], 0) + 1
    return c
