"""The comment threads MR Pilot opens on an MR, followed up like a human reviewer would.

- Post a finding once (no repeats on every new commit while it is still open).
- After each new commit, re-check every open thread against the new code:
  fixed -> short reply + resolve the thread; still there and the developer answered -> one short note
  that it is still the same; resolved by a person -> leave it alone (they decided).
Threads are MR discussions (not commit comments) so they can be resolved."""
import hashlib
import json
import logging
import time

from .standards import added_lines
from .util import short_error

log = logging.getLogger("mr_pilot.threads")

_FIXED = ["Sudah diperbaiki di `{sha}`, thanks!", "Oke, sudah beres di `{sha}`. Thanks!",
          "Sudah aman di `{sha}`, thanks ya."]
_STILL = ["Saya cek di `{sha}`, bagian ini masih sama ya.",
          "Di `{sha}` kodenya masih seperti ini, belum berubah."]


def _pick(options, *seed):
    return options[int(hashlib.sha1("|".join(map(str, seed)).encode()).hexdigest(), 16) % len(options)]


def norm(s):
    return " ".join(str(s or "").split())


def added_text(diffs):
    """{path: {normalised added line, ...}} of the current MR diff."""
    out = {}
    for d in diffs or []:
        if d.get("deleted_file"):
            continue
        p = d.get("new_path") or d.get("old_path")
        out.setdefault(p, set()).update(norm(t) for _, t in added_lines(d.get("diff")) if norm(t))
    return out


def line_text(diffs, path, line):
    for d in diffs or []:
        if (d.get("new_path") or d.get("old_path")) == path:
            for n, t in added_lines(d.get("diff")):
                if n == line:
                    return t
    return ""


class ThreadTracker:
    def __init__(self, gl, store):
        self.gl, self.store = gl, store

    # ------------------------------------------------------------- storage
    def _key(self, pid, iid):
        return f"threads:{pid}:{iid}"

    def load(self, pid, iid):
        try:
            return json.loads(self.store.kv_get(self._key(pid, iid)) or "[]")
        except ValueError:
            return []

    def _save(self, pid, iid, items):
        self.store.kv_set(self._key(pid, iid), json.dumps(items[-200:]))

    # ------------------------------------------------------------ matching
    @staticmethod
    def same(t, item):
        """Is `item` (a finding of the current code) the one thread `t` is about?"""
        if t["source"] != item["source"] or t.get("path") != item.get("path"):
            return False
        if t["source"] == "order":
            return norm(t.get("title")) == norm(item.get("title"))
        if t["source"] == "review":
            return norm(t.get("title")) == norm(item.get("title")) or (
                bool(t.get("snippet")) and norm(t["snippet"]) == norm(item.get("snippet")))
        if t.get("rule") != item.get("rule"):
            return False
        if t["source"] == "rule":
            return norm(t.get("snippet")) == norm(item.get("snippet"))
        # AI wording and line numbers move between runs: same rule in the same file, nearby or same code
        if t.get("snippet") and norm(t["snippet"]) == norm(item.get("snippet")):
            return True
        return abs((t.get("line") or 0) - (item.get("line") or 0)) <= 8

    def find(self, items, item, statuses=("open",)):
        return next((t for t in items if t.get("status") in statuses and self.same(t, item)), None)

    # ---------------------------------------------------------------- post
    def post(self, mr, item, body_fn):
        """item: {source, path, line, rule|title, snippet}. body_fn(inline: bool) -> text. True if posted."""
        pid, iid = mr["project_id"], mr["iid"]
        refs = mr.get("diff_refs") or {}
        res, inline = None, False
        if item.get("path") and item.get("line") and refs.get("head_sha"):
            pos = {"position_type": "text", "base_sha": refs.get("base_sha"), "start_sha": refs.get("start_sha"),
                   "head_sha": refs.get("head_sha"), "new_path": item["path"], "old_path": item["path"],
                   "new_line": int(item["line"])}
            try:
                res = self.gl.mr_discussion(pid, iid, body_fn(True), pos)
                inline = True
            except Exception as ex:  # line not in the diff: GitLab refuses the position
                log.info("posisi inline ditolak (%s:%s): %s", item.get("path"), item.get("line"), short_error(ex))
        if res is None:
            try:
                res = self.gl.mr_discussion(pid, iid, body_fn(False))
            except Exception as ex:
                log.warning("gagal posting komentar: %s", short_error(ex))
                return None
        items = self.load(pid, iid)
        items.append({k: item.get(k) for k in ("source", "path", "line", "rule", "title", "snippet", "severity",
                                               "message")}
                     | {"id": (res or {}).get("id"), "status": "open", "sha": mr.get("sha"), "at": time.time(),
                        "inline": inline})
        self._save(pid, iid, items)
        return {"inline": inline}

    def open_threads(self, pid, iid, refresh=True):
        """Threads MR Pilot opened (not adopted ones) that are still open. refresh=True re-reads GitLab, so a
        thread a person resolved meanwhile no longer counts."""
        items = self.load(pid, iid)
        out = []
        changed = False
        for t in items:
            if t.get("status") != "open" or t.get("adopted"):
                continue
            if refresh and t.get("id"):
                try:
                    d = self.gl.get_discussion(pid, iid, t["id"])
                    if any(n.get("resolved") for n in d.get("notes") or [] if n.get("resolvable")):
                        t.update(status="resolved", closed_at=time.time())
                        changed = True
                        continue
                except Exception as ex:
                    if "404" in str(ex):
                        t.update(status="gone", closed_at=time.time())
                        changed = True
                        continue
            out.append(t)
        if changed:
            self._save(pid, iid, items)
        return out

    # ----------------------------------------------------------- reconcile
    def reconcile(self, mr, diffs, current=None, ai_ok=False, adopt=True):
        """Re-check open threads after a new commit. current = findings of the current code (rule/ai items);
        None = rule/AI threads can't be judged now. Returns {"open", "fixed": [titles], "fixed_total"}."""
        pid, iid, sha = mr["project_id"], mr["iid"], mr.get("sha")
        if adopt:
            self.adopt_existing(mr)
        items = self.load(pid, iid)
        if not items:
            return {"open": 0, "fixed": [], "fixed_total": 0}
        added = added_text(diffs)
        fixed_now = []
        me = self.store.kv_get("gitlab_user")
        for t in items:
            if t.get("status") != "open" or t.get("sha") == sha:
                continue  # can only be fixed by a newer commit
            still = self._still_there(t, added, current, ai_ok)
            if still is None:
                continue
            if not still:
                if self._close(pid, iid, t, sha):
                    fixed_now.append(t.get("title") or t.get("message") or t.get("rule") or "")
            elif t.get("id") and t.get("nudged_sha") != sha and not t.get("adopted"):
                self._nudge_if_answered(pid, iid, t, sha, me)
        self._save(pid, iid, items)
        return {"open": sum(1 for t in items if t.get("status") == "open"), "fixed": fixed_now,
                "fixed_total": sum(1 for t in items if t.get("status") == "fixed")}

    def adopt_existing(self, mr):
        """Open line threads written earlier from this account (e.g. by an older MR Pilot version) are
        followed up too: closed when the commented line changes. Done once per MR."""
        pid, iid = mr["project_id"], mr["iid"]
        flag = f"threads_adopted:{pid}:{iid}"
        me = self.store.kv_get("gitlab_user")
        if not me or self.store.kv_get(flag) or not hasattr(self.gl, "list_discussions"):
            return 0
        try:
            discussions = self.gl.list_discussions(pid, iid)
        except Exception as ex:
            log.info("daftar thread !%s: %s", iid, short_error(ex))
            return 0
        items = self.load(pid, iid)
        known = {t.get("id") for t in items}
        added = 0
        for d in discussions:
            notes = d.get("notes") or []
            first = notes[0] if notes else {}
            pos = first.get("position") or {}
            if (d.get("id") in known or (first.get("author") or {}).get("username") != me
                    or not first.get("resolvable") or first.get("resolved") or first.get("system")
                    or not pos.get("new_path") or not pos.get("new_line") or not pos.get("head_sha")):
                continue
            try:
                text = self.gl.get_file_raw(pid, pos["new_path"], pos["head_sha"]).splitlines()
                snippet = text[int(pos["new_line"]) - 1] if 0 < int(pos["new_line"]) <= len(text) else ""
            except Exception:
                continue
            if not norm(snippet):
                continue
            items.append({"source": "review", "path": pos["new_path"], "line": int(pos["new_line"]),
                          "title": (first.get("body") or "").split("\n")[0][:120], "snippet": snippet.strip(),
                          "id": d["id"], "status": "open", "sha": pos["head_sha"], "at": time.time(),
                          "inline": True, "adopted": True})
            added += 1
        self._save(pid, iid, items)
        self.store.kv_set(flag, added)
        if added:
            log.info("!%s: %s thread lama ikut dipantau", iid, added)
        return added

    def _still_there(self, t, added, current, ai_ok):
        code = added.get(t.get("path"))
        snippet_present = bool(t.get("snippet")) and code is not None and norm(t["snippet"]) in code
        if t["source"] == "order":
            return None  # followed up by MergeOrder.followup (needs the merged MR's analysis)
        if t["source"] == "order":
            return None  # followed up by MergeOrder.followup (needs the merged MR's analysis)
        if t["source"] == "review":
            if not t.get("snippet"):
                return None
            lines = [norm(x) for x in str(t["snippet"]).splitlines() if norm(x)]
            return bool(code) and any(x in code for x in lines)
        if current is None:
            return None
        if any(self.same(t, v) for v in current):
            return True
        if t["source"] == "rule":
            return False  # deterministic check no longer finds it
        if not ai_ok:
            return None
        # AI did not report it again: only believe "fixed" if the code it pointed at actually changed
        return snippet_present

    def _close(self, pid, iid, t, sha):
        did = t.get("id")
        if did:
            try:
                d = self.gl.get_discussion(pid, iid, did)
                if any(n.get("resolved") for n in d.get("notes") or [] if n.get("resolvable")):
                    t.update(status="resolved", closed_at=time.time())  # a person already closed it
                    return False
                self.gl.reply_discussion(pid, iid, did, _pick(_FIXED, did, sha).format(sha=(sha or "")[:8]))
                try:
                    self.gl.resolve_discussion(pid, iid, did, True)
                except Exception as ex:  # not resolvable (e.g. no permission): the reply is enough
                    log.info("thread %s tidak bisa di-resolve: %s", did, short_error(ex))
            except Exception as ex:
                if "404" in str(ex):
                    t.update(status="gone", closed_at=time.time())
                    return False
                log.warning("gagal menutup thread %s: %s", did, short_error(ex))
                return False
        t.update(status="fixed", fixed_sha=sha, closed_at=time.time())
        return True

    def _nudge_if_answered(self, pid, iid, t, sha, me):
        """The developer answered in the thread (e.g. "sudah fix") but the code is still the same."""
        try:
            d = self.gl.get_discussion(pid, iid, t["id"])
        except Exception:
            return
        notes = d.get("notes") or []
        if any(n.get("resolved") for n in notes if n.get("resolvable")):
            t.update(status="resolved", closed_at=time.time())
            return
        if notes and (notes[-1].get("author") or {}).get("username") not in (me, None):
            try:
                self.gl.reply_discussion(pid, iid, t["id"], _pick(_STILL, t["id"], sha).format(sha=(sha or "")[:8]))
                t["nudged_sha"] = sha
            except Exception as ex:
                log.info("gagal membalas thread: %s", short_error(ex))
