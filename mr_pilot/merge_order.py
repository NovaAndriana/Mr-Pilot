"""Merge order between open MRs that target the same branch.

Two MRs can each be fine alone and still break each other depending on which lands first: MR B is built on
top of MR A's branch, B calls a function A introduces, A renames something B uses, both add migration
number 0042, both bump the same dependency, or both edit the same lines. MR Pilot compares every MR waiting
for a decision with the other open MRs on the same target branch, shows the recommended order on the card,
asks for confirmation when you merge out of order, and after a merge warns about (and comments on) the MRs
that will now fail. Read-only analysis; per-commit results are cached so other MRs are fetched once."""
import json
import logging
import re
import time

from .standards import added_lines
from .threads import ThreadTracker, _pick
from .util import short_error

log = logging.getLogger("mr_pilot.order")

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# definitions: Go funcs/methods/types, JS/TS exported or top-level functions/consts/classes/types
_DEFS = [
    re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*[\[(]"),
    re.compile(r"^\s*type\s+([A-Za-z_]\w*)\s+(?:struct|interface|func|[A-Za-z\[\]*])"),
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*[(<]"),
    re.compile(r"^\s*(?:export\s+)(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*[:=]"),
    re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?(?:class|interface|enum|type)\s+([A-Za-z_$][\w$]*)\b"),
]
_IDENT = re.compile(r"\b[A-Za-z_$][\w$]{3,}\b")
_COMMENT = re.compile(r"^\s*(//|#|\*|/\*)")
_MIGRATION_DIR = re.compile(r"(^|/)(migrations?|migrate|db/migrat\w*|schema)/", re.I)
_MIGRATION_VER = re.compile(r"^(\d{3,14})[_\-.]")
_DEP_FILES = re.compile(r"(^|/)(go\.mod|package\.json|requirements[\w.-]*\.txt|pyproject\.toml|pom\.xml|"
                        r"build\.gradle(\.kts)?|Podfile|pubspec\.yaml)$")
_LOCK_FILES = re.compile(r"(^|/)(go\.sum|package-lock\.json|yarn\.lock|pnpm-lock\.yaml|Podfile\.lock|poetry\.lock)$")
_GO_REQ = re.compile(r"^\s*(?:require\s+)?([\w.\-/]+\.[\w.\-/]+)\s+(v[\w.\-+]+)")
_JSON_DEP = re.compile(r'^\s*"(@?[\w.\-/]+)"\s*:\s*"((?:[~^]|>=?)?\d[\w.\-+]*|\*|latest)"')
_DEPENDS = re.compile(r"(?i)(?:depends?\s+on|requires?|after|setelah|tunggu|butuh|blocked\s+by|"
                      r"merge\s+(?:dulu|duluan|setelah))\s*:?\s*!(\d+)")
_COMMON = {"func", "return", "const", "type", "string", "error", "true", "false", "null", "this", "self", "import",
           "export", "default", "async", "await", "function", "class", "interface", "struct", "context", "Context",
           "nil", "err", "else", "range", "make", "append", "then", "catch", "props", "state", "value", "data",
           "React", "useState", "useEffect", "console", "println", "Println", "Printf", "Errorf", "fmt", "json",
           "time", "http", "fetch", "response", "request", "params", "result", "string", "number", "boolean",
           "undefined", "void", "readonly", "public", "private", "static", "string", "Promise", "Array", "Object"}


# --------------------------------------------------------------- analysis
# names too generic to say anything about another MR (every package has its own main/init/New/String...)
_GENERIC = {"main", "init", "New", "new", "String", "Error", "ServeHTTP", "Handler", "handler", "render", "index",
            "Index", "App", "Props", "State", "Config", "config", "Options", "Request", "Response", "Run", "Close",
            "Get", "Set", "Create", "Update", "Delete", "List", "setup", "Setup", "default", "Default"}


def _defs(text):
    if _COMMENT.match(text):
        return None
    for rx in _DEFS:
        m = rx.match(text)
        if m:
            name = m.group(1)
            if len(name) < 4 or name in _GENERIC or name.startswith(("Test", "Benchmark", "Example", "test")):
                return None
            return name
    return None


def analyze(diffs, commits=(), description=""):
    """Compact, JSON-able summary of what an MR changes (computed once per head commit)."""
    files, removed_defs, added_defs, def_lines_old, def_lines_new = {}, set(), set(), {}, {}
    refs, migrations, deps, locks, new_files = set(), [], {}, [], []
    for d in diffs or []:
        path = d.get("new_path") or d.get("old_path") or ""
        old_path = d.get("old_path") or path
        if d.get("new_file"):
            new_files.append(path)
        ranges = []
        old_no = None
        for raw in (d.get("diff") or "").splitlines():
            m = _HUNK.match(raw)
            if m:
                old_no = int(m.group(1))
                if not int(m.group(2) if m.group(2) is not None else 1):  # pure insertion after line old_no
                    ranges.append([old_no, old_no])
                continue
            if old_no is None or raw.startswith("\\"):
                continue
            if raw.startswith("-") and not raw.startswith("---"):
                ranges.append([old_no, old_no])
                name = _defs(raw[1:])
                if name:
                    removed_defs.add(name)
                    def_lines_old.setdefault(name, " ".join(raw[1:].split()))
                old_no += 1
            elif raw.startswith("+") and not raw.startswith("+++"):
                if not ranges or ranges[-1][1] < old_no - 1:
                    ranges.append([max(1, old_no - 1), old_no])
            else:
                old_no += 1
        if not d.get("new_file"):
            files[old_path] = _merge_ranges(ranges)
        else:
            files[path] = [[0, 0]]
        for _, text in added_lines(d.get("diff")):
            name = _defs(text)
            if name:
                added_defs.add(name)
                def_lines_new.setdefault(name, " ".join(text.split()))
            if not _COMMENT.match(text):
                refs.update(t for t in _IDENT.findall(text) if t not in _COMMON)
            if _DEP_FILES.search(path):
                m = _GO_REQ.match(text) or _JSON_DEP.match(text)
                if m:
                    deps.setdefault(path, {})[m.group(1)] = m.group(2)
        if _LOCK_FILES.search(path):
            locks.append(path)
        name = path.rsplit("/", 1)[-1]
        if d.get("new_file") and _MIGRATION_DIR.search(path) and _MIGRATION_VER.match(name):
            migrations.append({"path": path, "version": _MIGRATION_VER.match(name).group(1)})
    changed = sorted(n for n in removed_defs & added_defs if def_lines_old.get(n) != def_lines_new.get(n))
    return {
        "files": files,
        "new_files": new_files,
        "defined": sorted(added_defs - removed_defs),          # brand new symbols
        "removed": sorted(removed_defs - added_defs),          # deleted or renamed symbols
        "changed": changed,                                    # same name, different signature line
        "refs": sorted(refs - added_defs)[:4000],
        "migrations": migrations,
        "deps": deps,
        "locks": sorted(set(locks)),
        "commits": [c.get("id") for c in commits or [] if c.get("id")][:300],
        "depends_on": sorted({int(x) for x in _DEPENDS.findall(description or "")}),
    }


def _merge_ranges(ranges):
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + 3:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _overlap(r1, r2):
    hits = []
    for a1, b1 in r1:
        for a2, b2 in r2:
            if a1 <= b2 + 2 and a2 <= b1 + 2:
                hits.append([max(a1, a2), min(b1, b2) if min(b1, b2) >= max(a1, a2) else max(a1, a2)])
    return hits


# ---------------------------------------------------------------- relations
def relations(a, b, ia, ib):
    """Relations between MR a (!ia) and MR b (!ib). Each: {kind, level, first, second, reason}.
    first/second = iid that should be merged first/second (None when either order hurts)."""
    out = []

    def add(kind, level, first, reason, second=None):
        out.append({"kind": kind, "level": level, "first": first,
                    "second": second if second is not None else (ib if first == ia else ia if first else None),
                    "reason": reason})

    # explicit "depends on !N" in the description
    if ib in a.get("depends_on", []):
        add("explicit", "high", ib, f"deskripsi !{ia} menyebut harus setelah !{ib}")
    if ia in b.get("depends_on", []):
        add("explicit", "high", ia, f"deskripsi !{ib} menyebut harus setelah !{ia}")
    # stacked branches: one MR contains the other's commits
    ca, cb = set(a.get("commits") or []), set(b.get("commits") or [])
    if cb and cb < ca:
        add("stacked", "high", ib, f"branch !{ia} dibuat di atas branch !{ib} (berisi commit !{ib})")
    elif ca and ca < cb:
        add("stacked", "high", ia, f"branch !{ib} dibuat di atas branch !{ia} (berisi commit !{ia})")
    stacked = bool(out and out[-1]["kind"] == "stacked")
    # symbols: one MR introduces what the other uses, or removes/changes what the other uses
    if not stacked:
        for x, y, ix, iy in ((a, b, ia, ib), (b, a, ib, ia)):
            new_used = sorted(set(x.get("defined", [])) & set(y.get("refs", [])) - set(y.get("defined", [])))
            if new_used:
                add("uses_new", "high", ix, f"!{iy} memakai {_names(new_used)} yang baru dibuat di !{ix}; "
                                            f"kalau !{iy} masuk duluan, build-nya error")
            broken = sorted((set(x.get("removed", [])) | set(x.get("changed", []))) & set(y.get("refs", []))
                            - set(y.get("defined", [])))
            if broken:
                add("breaks", "high", iy, f"!{ix} menghapus/mengubah {_names(broken)} yang dipakai kode baru di "
                                          f"!{iy}; kalau !{ix} masuk duluan, !{iy} error. Setelah !{iy} masuk, "
                                          f"!{ix} perlu rebase dan ikut menyesuaikan pemakaian barunya")
    # migrations
    va = {m["version"]: m["path"] for m in a.get("migrations", [])}
    vb = {m["version"]: m["path"] for m in b.get("migrations", [])}
    same = sorted(set(va) & set(vb))
    if same:
        add("migration_clash", "high", None, f"!{ia} dan !{ib} sama-sama membuat migrasi nomor {same[0]}; "
                                             f"salah satu harus ganti nomor")
    elif va and vb:
        lo_a, lo_b = min(va), min(vb)
        if len(lo_a) == len(lo_b):
            first = ia if lo_a < lo_b else ib
            second = ib if first == ia else ia
            add("migration_order", "medium", first,
                f"migrasi !{first} ({min(lo_a, lo_b)}) bernomor lebih kecil dari !{second} ({max(lo_a, lo_b)}); "
                f"kalau dibalik, migrasi !{first} bisa terlewat atau ditolak")
    # dependencies
    for f in set(a.get("deps", {})) & set(b.get("deps", {})):
        diff = sorted(n for n in set(a["deps"][f]) & set(b["deps"][f]) if a["deps"][f][n] != b["deps"][f][n])
        if diff:
            n = diff[0]
            add("dep_version", "high", None, f"`{n}` di {f}: !{ia} memakai {a['deps'][f][n]}, !{ib} memakai "
                                             f"{b['deps'][f][n]}; samakan versinya dulu")
    locks = sorted(set(a.get("locks", [])) & set(b.get("locks", [])))
    if locks and not stacked:
        add("lockfile", "medium", None, f"keduanya mengubah {locks[0]}; yang di-merge belakangan perlu rebase "
                                        f"dan generate ulang file lock")
    # same lines of the same file -> conflict for whoever is second
    if not stacked:
        for f in sorted(set(a.get("files", {})) & set(b.get("files", {})), key=lambda x: (bool(_DEP_FILES.search(x)), x)):
            if _LOCK_FILES.search(f) or (_DEP_FILES.search(f) and any(r["kind"] == "dep_version" for r in out)):
                continue
            hits = _overlap(a["files"][f], b["files"][f])
            if hits:
                add("conflict", "medium", None, f"sama-sama mengubah `{f}` sekitar baris {hits[0][0]}; "
                                                f"yang di-merge belakangan akan conflict dan perlu rebase")
                break
    return out


def _names(names):
    shown = ", ".join(f"`{n}`" for n in names[:3])
    return shown + (f" (+{len(names) - 3})" if len(names) > 3 else "")


def must_wait(rels, iid):
    """Relations saying another MR has to be merged before !iid."""
    return [r for r in rels if r.get("second") == iid and r.get("first") not in (None, iid)]


# ---------------------------------------------------------------- service
class MergeOrder:
    def __init__(self, cfg, gl, store):
        self.cfg = cfg.get("merge_order") or {}
        self.enabled = bool(self.cfg.get("enabled", True))
        self.gl, self.store = gl, store

    def _analysis(self, mr):
        pid, iid, sha = mr["project_id"], mr["iid"], mr.get("sha")
        k = f"mrscan:{pid}:{iid}"
        try:
            cached = json.loads(self.store.kv_get(k) or "null")
        except ValueError:
            cached = None
        if cached and cached.get("sha") == sha and cached.get("v") == 1:
            return cached["a"]
        a = analyze(self.gl.get_diffs(pid, iid), self.gl.get_commits(pid, iid), mr.get("description") or "")
        self.store.kv_set(k, json.dumps({"sha": sha, "v": 1, "a": a, "at": time.time()}))
        return a

    def _others(self, mr):
        target = mr.get("target_branch")
        if not target:
            return []
        rows = self.gl.project_mrs(mr["project_id"], target_branch=target, state="opened")
        rows = [m for m in rows if m["iid"] != mr["iid"] and not (m.get("draft") or m.get("work_in_progress"))]
        rows.sort(key=lambda m: m.get("updated_at") or "", reverse=True)
        return rows[: int(self.cfg.get("max_mrs", 30))]

    def check(self, mr):
        """Relations of `mr` with the other open MRs on the same target branch."""
        if not self.enabled:
            return None
        try:
            me = self._analysis(mr)
            out = []
            for o in self._others(mr):
                try:
                    rels = relations(me, self._analysis(o), mr["iid"], o["iid"])
                except Exception as ex:
                    log.info("analisis !%s: %s", o.get("iid"), short_error(ex))
                    continue
                for r in rels:
                    r.update(other=o["iid"], other_title=o.get("title"), other_url=o.get("web_url"),
                             other_author=(o.get("author") or {}).get("name"))
                out += rels
        except Exception as ex:
            log.warning("cek urutan merge !%s gagal: %s", mr.get("iid"), short_error(ex))
            return None
        out.sort(key=lambda r: (r["level"] != "high", r["kind"]))
        res = {"sha": mr.get("sha"), "at": time.time(), "relations": out[:12]}
        self.store.kv_set(f"order:{mr['project_id']}:{mr['iid']}", json.dumps(res))
        return res

    def affected_by(self, pid, iid):
        try:
            return json.loads(self.store.kv_get(f"order_affected:{pid}:{iid}") or "null")
        except ValueError:
            return None

    def followup(self, mr):
        """New commit on an MR we warned after another merge: close the warning when it no longer applies."""
        if not self.enabled:
            return []
        pid, iid = mr["project_id"], mr["iid"]
        info = self.affected_by(pid, iid)
        if not info or info.get("sha") == mr.get("sha"):
            return []
        try:
            merged = json.loads(self.store.kv_get(f"mrscan:{pid}:{info['merged']}") or "null") or {}
            cur = self._analysis(mr)
        except Exception as ex:
            log.info("cek ulang !%s: %s", iid, short_error(ex))
            return []
        if not merged.get("a"):
            return []
        still = [r for r in relations(merged["a"], cur, info["merged"], iid)
                 if r["kind"] in ("breaks", "migration_clash", "dep_version", "migration_order")
                 and not (r["kind"] == "migration_order" and r["first"] == info["merged"])]
        if mr.get("has_conflicts"):
            still.append({"kind": "conflict"})
        if still:
            info["sha"] = mr.get("sha")
            self.store.kv_set(f"order_affected:{pid}:{iid}", json.dumps(info))
            return []
        self.store.kv_del(f"order_affected:{pid}:{iid}")
        tracker = ThreadTracker(self.gl, self.store)
        items = tracker.load(pid, iid)
        closed = []
        for t in items:
            if t.get("source") == "order" and t.get("status") == "open" and t.get("sha") != mr.get("sha"):
                if tracker._close(pid, iid, t, mr.get("sha")):
                    closed.append(t.get("title"))
        tracker._save(pid, iid, items)
        return closed or [f"setelah !{info['merged']}"]

    def record_affected(self, merged_mr, affected, comment=True):
        """Remember + (optionally) tell the affected MRs, in a thread MR Pilot follows up."""
        tracker = ThreadTracker(self.gl, self.store)
        posted = 0
        for a in affected:
            self.store.kv_set(f"order_affected:{a['project_id']}:{a['iid']}", json.dumps({
                "merged": merged_mr["iid"], "merged_title": merged_mr.get("title"), "at": time.time(),
                "sha": a.get("sha"), "reasons": [r["reason"] for r in a["relations"]][:4]}))
            if not comment:
                continue
            item = {"source": "order", "title": f"setelah !{merged_mr['iid']} di-merge", "path": None, "line": None,
                    "snippet": "", "message": a["relations"][0]["reason"][:200]}
            target = {"project_id": a["project_id"], "iid": a["iid"], "sha": a.get("sha"), "diff_refs": None}
            if tracker.find(tracker.load(a["project_id"], a["iid"]), item, ("open", "resolved")):
                continue
            body = affected_comment(merged_mr["iid"], merged_mr.get("target_branch"), a["relations"],
                                    lambda opts, a=a: _pick(opts, a["iid"], merged_mr["iid"]))
            if tracker.post(target, item, lambda inline, body=body: body):
                posted += 1
        return posted

    def after_merge(self, merged_mr):
        """`merged_mr` just landed: which open MRs on the same target will now fail or conflict?
        Returns [{iid, title, web_url, author, reasons, snippet_path, symbols}]."""
        if not self.enabled:
            return []
        try:
            a = self._analysis(merged_mr)
            out = []
            for o in self._others(merged_mr):
                rels = relations(a, self._analysis(o), merged_mr["iid"], o["iid"])
                bad = [r for r in rels if r["kind"] in ("breaks", "conflict", "migration_clash", "dep_version",
                                                         "lockfile", "migration_order")
                       and not (r["kind"] == "migration_order" and r["first"] == merged_mr["iid"])]
                if bad:
                    out.append({"iid": o["iid"], "title": o.get("title"), "web_url": o.get("web_url"), "sha": o.get("sha"),
                                "author": (o.get("author") or {}).get("name"), "project_id": o["project_id"],
                                "author_username": (o.get("author") or {}).get("username"), "relations": bad})
            return out
        except Exception as ex:
            log.warning("cek MR terdampak setelah merge !%s: %s", merged_mr.get("iid"), short_error(ex))
            return []


# ------------------------------------------------------------ presentation
ICON = {"high": "⛔", "medium": "⚠️"}


def card_lines(order, iid, e):
    """Telegram card section. `e` = HTML escaper."""
    rels = (order or {}).get("relations") or []
    if not rels:
        return []
    out = ["", "<b>🔀 Urutan merge dengan MR lain:</b>"]
    for r in rels[:5]:
        if r.get("first") == r.get("other"):
            head = f"Merge !{r['other']} dulu, baru !{iid}"
        elif r.get("first") == iid:
            head = f"Merge !{iid} dulu, baru !{r['other']}"
        else:
            head = f"Bentrok dengan !{r['other']}"
        out.append(f"{ICON.get(r['level'], '•')} <b>{e(head)}</b>: {e(r['reason'])}")
    if len(rels) > 5:
        out.append(f"   …dan {len(rels) - 5} lainnya")
    return out


def affected_comment(merged_iid, target, rels, voice_pick):
    """Natural comment for an MR that will break now that !merged_iid is in `target`."""
    parts = [f"Info: !{merged_iid} baru saja di-merge ke `{target}`."]
    for r in rels[:3]:
        k = r["kind"]
        if k == "breaks":
            syms = re.findall(r"`([^`]+)`", r["reason"])[:3]
            parts.append(f"MR itu menghapus atau mengubah {', '.join(f'`{x}`' for x in syms)} yang dipakai di MR ini, "
                         f"jadi setelah di-merge kode ini akan error.")
        elif k == "conflict":
            f = re.findall(r"`([^`]+)`", r["reason"])
            parts.append(f"Bagian yang sama di {('`' + f[0] + '`') if f else 'file yang sama'} ikut berubah, jadi MR ini "
                         f"sekarang akan conflict.")
        elif k == "migration_clash":
            parts.append("Nomor migrasinya sama dengan migrasi di MR ini, jadi salah satunya harus ganti nomor.")
        elif k == "migration_order":
            parts.append("Migrasi di MR itu bernomor lebih besar dari migrasi di MR ini, jadi migrasi MR ini bisa "
                         "terlewat. Sebaiknya ganti ke nomor setelahnya.")
        elif k == "dep_version":
            parts.append(r["reason"].split(";")[0].replace(f"!{merged_iid}", "MR itu") + ", jadi versinya sekarang beda "
                         "dengan MR ini.")
        elif k == "lockfile":
            parts.append("File lock dependency ikut berubah, jadi perlu di-generate ulang.")
    parts.append(voice_pick(["Tolong rebase ke `{t}` terbaru dan sesuaikan dulu sebelum di-merge ya.",
                             "Bisa di-rebase ke `{t}` terbaru dan disesuaikan dulu? Thanks."]).format(t=target))
    return " ".join(parts[:1]) + "\n\n" + " ".join(parts[1:-1]) + "\n\n" + parts[-1]
