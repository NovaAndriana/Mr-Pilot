"""Delivery report for the lead's superior: what each person on the team delivered in a period.

Structure: team (BE/FE/Mobile/QA) -> person -> feature (related MRs grouped under one title) -> MRs.
Source: only the MRs that came to the lead (the ones MR Pilot recorded). Grouping is configurable:
  ai        = the AI proposes feature groups from title/branch/description (cached per MR, editable)
  label     = a GitLab label (optionally only labels with a prefix such as "feature::")
  milestone = the GitLab milestone
  ticket    = the ticket number in the title/branch (IDAS-5413)
Manual edits (move an MR to another group, change its kind, rename a group) always win."""
import json
import logging
import re
import time
from datetime import date, datetime, timedelta

from .reviewer import extract_json
from .util import short_error

log = logging.getLogger("mr_pilot.delivery")

KINDS = {"feature": "Fitur", "bugfix": "Bug fixing", "improvement": "Perbaikan teknis", "other": "Lainnya"}
_TYPE_KIND = {"feat": "feature", "feature": "feature", "fix": "bugfix", "bugfix": "bugfix", "hotfix": "bugfix",
              "bug": "bugfix", "refactor": "improvement", "perf": "improvement", "test": "other", "docs": "other",
              "chore": "other", "ci": "other", "build": "other", "revert": "other"}
_TYPE_RE = re.compile(r"^\s*\[?([A-Za-z]+)\]?\s*(\([^)]*\))?!?\s*[:/]")
_TICKET = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]+)[-_ ](\d{2,})(?!\d)")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_ID = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
MONTH_ID = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober",
            "November", "Desember"]
OPEN = ("notified", "waiting_bot", "error", "notify_failed", "merging", "unassigned")

AI_PROMPT = """Kamu membantu Tech Lead menyusun laporan pekerjaan tim untuk atasannya.
Kelompokkan merge request (MR) di bawah ini per fitur atau modul yang sama. MR yang membahas fitur yang sama
(termasuk perbaikan bug dan lanjutan dari fitur itu) masuk ke satu grup, walaupun nomor tiketnya berbeda.

Aturan:
- Nama grup singkat dalam Bahasa Indonesia yang dimengerti atasan, menyebut fiturnya, mis. "Kuota bundle e-meterai",
  "Tanda tangan dinamis per role". Jangan hanya nomor tiket, jangan nama orang.
- Kalau cocok dengan salah satu grup yang sudah ada, pakai nama itu persis: {known}
- kind per MR: feature (fitur baru / pengembangan), bugfix (perbaikan bug), improvement (refactor, performa,
  teknis), other (dokumentasi, CI, chore).
- Jangan mengarang fitur yang tidak terlihat dari judul/deskripsi.

Jawab HANYA JSON: {{"items": [{{"id": "<id MR>", "group": "<nama grup>", "kind": "feature|bugfix|improvement|other"}}]}}"""


def mr_kind(title, branch=""):
    for src in (title or "", branch or ""):
        m = _TYPE_RE.match(src)
        if m and m.group(1).lower() in _TYPE_KIND:
            return _TYPE_KIND[m.group(1).lower()]
    return None


def ticket_of(title, branch=""):
    for src in (title or "", branch or ""):
        m = _TICKET.search(src)
        if m and m.group(1).upper() not in ("V", "API", "HTTP"):
            return f"{m.group(1).upper()}-{m.group(2)}"
    return None


def clean_title(title):
    """'feat(IDAS-5413): add quota bundle' -> 'add quota bundle'."""
    t = re.sub(r"^\s*\[?[A-Za-z]+\]?\s*(\([^)]*\))?!?\s*:\s*", "", title or "")
    t = _TICKET.sub("", t, count=1) if t == (title or "") else t
    return t.strip(" -:/") or (title or "")


def _labels(r):
    try:
        v = json.loads(r.get("labels") or "[]")
        return [str(x) for x in v] if isinstance(v, list) else []
    except ValueError:
        return []


# ----------------------------------------------------------------- period
def period(kind="this_week", start=None, end=None, today=None):
    """-> (start_ts, end_ts, label). Weeks run Monday..Sunday, local time."""
    today = today or date.today()
    if kind == "custom" and start:
        s = date.fromisoformat(start)
        e = date.fromisoformat(end) if end else today
        if e < s:
            s, e = e, s
        e = e + timedelta(days=1)
    elif kind == "last_week":
        s = today - timedelta(days=today.weekday() + 7)
        e = s + timedelta(days=7)
    elif kind == "this_month":
        s = today.replace(day=1)
        e = (s + timedelta(days=32)).replace(day=1)
    elif kind == "last_month":
        e = today.replace(day=1)
        s = (e - timedelta(days=1)).replace(day=1)
    else:
        s = today - timedelta(days=today.weekday())
        e = s + timedelta(days=7)
    last = e - timedelta(days=1)
    if s.month == last.month and s.year == last.year:
        label = f"{s.day}–{last.day} {MONTH_ID[s.month - 1]} {s.year}" if s != last else \
            f"{s.day} {MONTH_ID[s.month - 1]} {s.year}"
    else:
        label = f"{s.day} {MONTH_ID[s.month - 1]} – {last.day} {MONTH_ID[last.month - 1]} {last.year}"
    to_ts = lambda d: time.mktime(datetime(d.year, d.month, d.day).timetuple())  # noqa: E731
    return to_ts(s), to_ts(e), label


def _iso_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# ----------------------------------------------------------------- report
class Delivery:
    def __init__(self, cfg, store, ai=None, gl=None, send=None):
        self.root = cfg
        self.cfg = cfg.get("delivery") or {}
        self.store, self.ai, self.gl = store, ai, gl
        self.send = send or (lambda *a, **k: None)

    @property
    def enabled(self):
        return bool(self.cfg.get("enabled", True))

    # ------------------------------------------------------------ team
    def team(self):
        raw = self.store.kv_get("delivery_team")
        if raw:
            try:
                return [t for t in json.loads(raw) if isinstance(t, dict) and t.get("username")]
            except ValueError:
                pass
        return [t for t in (self.cfg.get("team") or []) if isinstance(t, dict) and t.get("username")]

    def save_team(self, members):
        clean, seen = [], set()
        for m in members or []:
            u = str((m or {}).get("username") or "").strip().lstrip("@")
            if not u or u.lower() in seen:
                continue
            seen.add(u.lower())
            clean.append({"username": u, "name": str(m.get("name") or u).strip()[:80],
                          "team": str(m.get("team") or "").strip()[:30] or "Lainnya"})
        self.store.kv_set("delivery_team", json.dumps(clean, ensure_ascii=False))
        return clean

    def mode(self):
        return self.store.kv_get("delivery_group_by") or self.cfg.get("group_by") or "ai"

    def set_mode(self, mode, label_prefix=None):
        if mode not in ("ai", "label", "milestone", "ticket"):
            raise ValueError("mode harus ai | label | milestone | ticket")
        self.store.kv_set("delivery_group_by", mode)
        if label_prefix is not None:
            self.store.kv_set("delivery_label_prefix", str(label_prefix).strip())

    def label_prefix(self):
        v = self.store.kv_get("delivery_label_prefix")
        return v if v is not None else str(self.cfg.get("label_prefix") or "")

    # ------------------------------------------------------------ data
    def rows(self, start, end):
        include_open = self.cfg.get("include_open", True)
        out = []
        for r in self.store.query("SELECT * FROM mrs"):
            r = dict(r)
            st = r.get("status")
            if st == "merged":
                ts = _iso_ts(r.get("merged_at")) or r.get("decided_at")
                if ts and start <= ts < end:
                    r["_ts"], r["_state"] = ts, "merged"
                    out.append(r)
            elif include_open and st in OPEN:
                created = _iso_ts(r.get("mr_created_at")) or r.get("first_seen") or 0
                if created < end and (r.get("updated_at") or 0) >= start:
                    r["_ts"], r["_state"] = r.get("updated_at") or created, "open"
                    out.append(r)
        return sorted(out, key=lambda r: r["_ts"])

    def enrich(self, rows, limit=40):
        """Older records were saved before labels/description were kept: fill them from GitLab once."""
        if not self.gl:
            return 0
        n = 0
        for r in rows:
            if r.get("labels") is not None or n >= limit:
                continue
            try:
                mr = self.gl.get_mr(r["project_id"], r["iid"])
            except Exception as ex:
                log.info("delivery: detail !%s tidak terbaca: %s", r.get("iid"), short_error(ex))
                r["labels"] = "[]"
                continue
            f = dict(labels=json.dumps(mr.get("labels") or [], ensure_ascii=False),
                     milestone=(mr.get("milestone") or {}).get("title") or "",
                     description=(mr.get("description") or "")[:2000])
            if mr.get("merged_at"):
                f["merged_at"] = mr["merged_at"]
            if not r.get("author_username") and mr.get("author"):
                f["author"], f["author_username"] = mr["author"].get("name"), mr["author"].get("username")
            self.store.upsert(r["key"], **f)
            r.update(f)
            n += 1
        return n

    # ------------------------------------------------------------ grouping
    def _override(self, key):
        try:
            return json.loads(self.store.kv_get(f"dl_override:{key}") or "{}")
        except ValueError:
            return {}

    def set_override(self, key, group=None, kind=None):
        o = self._override(key)
        if group is not None:
            g = str(group).strip()[:120]
            if g:
                o["group"] = g
            else:
                o.pop("group", None)
        if kind is not None:
            if kind and kind not in KINDS:
                raise ValueError("kind harus feature | bugfix | improvement | other")
            if kind:
                o["kind"] = kind
            else:
                o.pop("kind", None)
        if o:
            self.store.kv_set(f"dl_override:{key}", json.dumps(o, ensure_ascii=False))
        else:
            self.store.kv_del(f"dl_override:{key}")
        return o

    def renames(self):
        try:
            return json.loads(self.store.kv_get("dl_renames") or "{}")
        except ValueError:
            return {}

    def rename_group(self, old, new):
        old, new = str(old or "").strip(), str(new or "").strip()[:120]
        if not old or not new or old == new:
            raise ValueError("nama grup lama dan baru wajib diisi dan harus berbeda")
        ren = self.renames()
        for k, v in list(ren.items()):  # a -> b, then b -> c: a -> c
            if v == old:
                ren[k] = new
        ren[old] = new
        ren.pop(new, None)
        self.store.kv_set("dl_renames", json.dumps(ren, ensure_ascii=False))
        for row in self.store.query("SELECT k, v FROM kv WHERE k LIKE 'dl_override:%'"):
            try:
                o = json.loads(row["v"])
            except ValueError:
                continue
            if o.get("group") == old:
                o["group"] = new
                self.store.kv_set(row["k"], json.dumps(o, ensure_ascii=False))

    def _known_groups(self):
        names = set(self.renames().values())
        for row in self.store.query("SELECT v FROM kv WHERE k LIKE 'dl_override:%' OR k LIKE 'dl_ai:%' "
                                    "ORDER BY rowid DESC LIMIT 400"):
            try:
                g = json.loads(row["v"]).get("group")
            except (ValueError, AttributeError):
                continue
            if g:
                names.add(g)
        return sorted(names)[:80]

    def _ai_assign(self, rows, force=False):
        """Cached AI group per MR; the uncached ones go to the AI in batches. -> note ('' = fine)."""
        todo = [r for r in rows if force or not self.store.kv_get(f"dl_ai:{r['key']}")]
        if not todo:
            return ""
        if not self.ai:
            return "AI tidak tersedia, MR dikelompokkan per nomor tiket."
        known = self._known_groups()
        note = ""
        for i in range(0, len(todo), 40):
            batch = todo[i:i + 40]
            lines = []
            for r in batch:
                desc = " ".join((r.get("description") or "").split())[:300]
                lines.append(f"- id: {r['key']} | project: {r.get('project') or '-'} | judul: {r.get('title')} | "
                             f"branch: {r.get('source_branch') or '-'} | label: {', '.join(_labels(r)) or '-'}"
                             + (f" | deskripsi: {desc}" if desc else ""))
            system = AI_PROMPT.format(known=", ".join(f'"{k}"' for k in known) or "(belum ada)")
            try:
                raw, _ = self.ai.complete(system, "MR:\n" + "\n".join(lines), "delivery", validate=extract_json)
                data = extract_json(raw)
            except Exception as ex:
                log.warning("AI delivery gagal: %s", short_error(ex))
                return f"AI gagal mengelompokkan ({short_error(ex)}), sebagian MR dikelompokkan per nomor tiket."
            ids = {r["key"] for r in batch}
            for x in data.get("items") or []:
                k, g = str(x.get("id") or ""), str(x.get("group") or "").strip()[:120]
                if k in ids and g:
                    kind = str(x.get("kind") or "").lower()
                    self.store.kv_set(f"dl_ai:{k}", json.dumps(
                        {"group": g, "kind": kind if kind in KINDS else None}, ensure_ascii=False))
                    known.append(g)
        return note

    def _base_group(self, r, mode):
        prefix = self.label_prefix()
        if mode == "label":
            labs = [lb for lb in _labels(r) if not prefix or lb.lower().startswith(prefix.lower())]
            if labs:
                return labs[0][len(prefix):].strip() if prefix else labs[0], "label"
        elif mode == "milestone":
            if r.get("milestone"):
                return r["milestone"], "milestone"
        elif mode == "ai":
            try:
                g = json.loads(self.store.kv_get(f"dl_ai:{r['key']}") or "{}").get("group")
            except ValueError:
                g = None
            if g:
                return g, "ai"
        t = ticket_of(r.get("title"), r.get("source_branch"))
        if t:
            return t, "ticket"
        return clean_title(r.get("title")), "title"

    def assign(self, rows, mode=None, regroup=False):
        mode = mode or self.mode()
        note = self._ai_assign([r for r in rows if not self._override(r["key"]).get("group")], regroup) \
            if mode == "ai" else ""
        ren = self.renames()
        # ticket groups get a readable name: "IDAS-5413 · add quota bundle" (title of the first MR)
        ticket_title = {}
        for r in rows:
            t = ticket_of(r.get("title"), r.get("source_branch"))
            if t and t not in ticket_title:
                ticket_title[t] = clean_title(r.get("title"))
        for r in rows:
            o = self._override(r["key"])
            group, src = (o["group"], "manual") if o.get("group") else self._base_group(r, mode)
            if src == "ticket" and ticket_title.get(group):
                group = f"{group} · {ticket_title[group]}"
            group = ren.get(group, group)
            ai_kind = None
            if mode == "ai":
                try:
                    ai_kind = json.loads(self.store.kv_get(f"dl_ai:{r['key']}") or "{}").get("kind")
                except ValueError:
                    ai_kind = None
            kind = o.get("kind") or mr_kind(r.get("title"), r.get("source_branch")) or ai_kind or "other"
            r["_group"], r["_group_src"], r["_kind"] = group, src, kind
        return note

    # ------------------------------------------------------------ build
    def build(self, kind="this_week", start=None, end=None, regroup=False, today=None):
        s, e, label = period(kind, start, end, today)
        rows = self.rows(s, e)
        self.enrich(rows)
        mode = self.mode()
        note = self.assign(rows, mode, regroup) if rows else ""
        members = {m["username"].lower(): m for m in self.team()}
        order = [str(x) for x in (self.cfg.get("teams_order") or [])]
        teams, unknown = {}, {}
        for r in rows:
            u = (r.get("author_username") or "").lower()
            m = members.get(u)
            if not m:
                unknown[u or "?"] = r.get("author") or u or "?"
            tname = (m or {}).get("team") or "Belum terdaftar"
            pname = (m or {}).get("name") or r.get("author") or u or "?"
            person = teams.setdefault(tname, {}).setdefault(pname, {"name": pname, "username": u, "groups": {},
                                                                     "counts": {k: 0 for k in KINDS},
                                                                     "merged": 0, "open": 0})
            person["counts"][r["_kind"]] += 1
            person["merged" if r["_state"] == "merged" else "open"] += 1
            g = person["groups"].setdefault(r["_group"], {"name": r["_group"], "source": r["_group_src"], "mrs": []})
            g["mrs"].append(self._mr_item(r))

        def team_rank(t):
            return (order.index(t) if t in order else len(order) + (100 if t == "Belum terdaftar" else 0), t)
        out_teams = []
        for t in sorted(teams, key=team_rank):
            people = []
            for p in sorted(teams[t].values(), key=lambda p: (-(p["merged"] + p["open"]), p["name"])):
                groups = sorted(p["groups"].values(), key=lambda g: (-len(g["mrs"]), g["name"]))
                for g in groups:
                    g["kind"] = _group_kind(g["mrs"])
                people.append({**p, "groups": groups})
            out_teams.append({"team": t, "people": people,
                              "merged": sum(p["merged"] for p in people), "open": sum(p["open"] for p in people)})
        features = {}
        for r in rows:
            f = features.setdefault(r["_group"], {"name": r["_group"], "people": [], "merged": 0, "open": 0,
                                                  "mrs": []})
            m = members.get((r.get("author_username") or "").lower())
            who = (m or {}).get("name") or r.get("author") or "?"
            if who not in f["people"]:
                f["people"].append(who)
            f["merged" if r["_state"] == "merged" else "open"] += 1
            f["mrs"].append(self._mr_item(r))
        feats = sorted(features.values(), key=lambda f: (-(f["merged"] + f["open"]), f["name"]))
        for f in feats:
            f["kind"] = _group_kind(f["mrs"])
            del f["mrs"]
        return {"period": {"kind": kind, "start": s, "end": e, "label": label,
                           "from": date.fromtimestamp(s).isoformat(),
                           "to": (date.fromtimestamp(e) - timedelta(days=1)).isoformat()},
                "mode": mode, "label_prefix": self.label_prefix(), "note": note,
                "totals": {"mrs": len(rows), "merged": sum(1 for r in rows if r["_state"] == "merged"),
                           "open": sum(1 for r in rows if r["_state"] == "open"),
                           **{k: sum(1 for r in rows if r["_kind"] == k) for k in KINDS},
                           "people": sum(len(t["people"]) for t in out_teams)},
                "teams": out_teams, "features": feats,
                "unregistered": [{"username": u, "name": n} for u, n in sorted(unknown.items())],
                "team_members": self.team(), "kinds": KINDS}

    @staticmethod
    def _mr_item(r):
        return {"key": r["key"], "iid": r.get("iid"), "title": r.get("title"), "url": r.get("web_url"),
                "project": r.get("project") or "", "state": r["_state"], "kind": r["_kind"],
                "kind_label": KINDS[r["_kind"]], "group": r["_group"], "group_source": r["_group_src"],
                "ticket": ticket_of(r.get("title"), r.get("source_branch")), "ts": r["_ts"],
                "date": datetime.fromtimestamp(r["_ts"]).strftime("%d/%m"),
                "target_branch": r.get("target_branch") or ""}

    # ------------------------------------------------------------ outputs
    def text(self, rep, html=True, url=""):
        """Telegram (html=True) or plain text for Teams/WhatsApp."""
        b = (lambda s: f"<b>{_e(s)}</b>") if html else (lambda s: s)
        esc = _e if html else (lambda s: s)
        t = rep["totals"]
        lines = [f"{'📦 ' if html else ''}{b('Delivery tim · ' + rep['period']['label'])}",
                 f"{t['merged']} MR selesai"
                 + (f", {t['open']} masih berjalan" if t["open"] else "")
                 + f" · {t['feature']} fitur, {t['bugfix']} bug fixing"
                 + (f", {t['improvement']} perbaikan teknis" if t["improvement"] else "")]
        if not t["mrs"]:
            lines.append("Belum ada MR di periode ini.")
        for team in rep["teams"]:
            lines.append("")
            lines.append(b(f"Tim {team['team']}") if team["team"] != "Belum terdaftar" else b("Belum terdaftar di tim"))
            for p in team["people"]:
                lines.append(f"{'👤 ' if html else '- '}{esc(p['name'])} ({_counts_text(p)})")
                for g in p["groups"]:
                    done = sum(1 for m in g["mrs"] if m["state"] == "merged")
                    run = len(g["mrs"]) - done
                    st = f"{done} MR selesai" + (f", {run} berjalan" if run else "")
                    lines.append(f"   • {esc(KINDS[g['kind']])}: {esc(g['name'])} ({st})")
        if url:
            lines += ["", f"Detail per MR: {url}"]
        return "\n".join(lines)

    def docx(self, rep):
        return build_docx(rep)

    def weekly_tick(self, now=None, url=""):
        if not (self.enabled and self.cfg.get("weekly_telegram", True)):
            return False
        now = now or time.time()
        lt = time.localtime(now)
        day = DAYS.index(str(self.cfg.get("weekly_day") or "fri").lower()[:3])
        try:
            hh, mm = (int(x) for x in str(self.cfg.get("weekly_time") or "16:00").split(":")[:2])
        except ValueError:
            hh, mm = 16, 0
        if lt.tm_wday != day or (lt.tm_hour, lt.tm_min) < (hh, mm):
            return False
        week = time.strftime("%G-%V", lt)
        if self.store.kv_get("delivery_weekly") == week:
            return False
        self.store.kv_set("delivery_weekly", week)
        rep = self.build("this_week", today=date.fromtimestamp(now))
        if not rep["totals"]["mrs"]:
            return False
        self.send(self.text(rep, html=True, url=url))
        return True


def _group_kind(mrs):
    kinds = [m["kind"] for m in mrs]
    for k in ("feature", "bugfix", "improvement"):
        if k in kinds and (k == "feature" or all(x in (k, "other") for x in kinds)):
            return k
    return "feature" if "feature" in kinds else (kinds[0] if kinds else "other")


def _counts_text(p):
    c = p["counts"]
    parts = [f"{c[k]} {KINDS[k].lower()}" for k in ("feature", "bugfix", "improvement", "other") if c[k]]
    return ", ".join(parts) or "0 MR"


def _e(s):
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------------ .docx
def _x(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def build_docx(rep):
    """Minimal Word document (no extra dependency): title, summary, per team/person/feature, feature recap."""
    import io
    import zipfile
    body = []

    def para(text, style=None, bold=False, size=None, color=None, link=None):
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        rpr = ""
        if bold or size or color:
            rpr = "<w:rPr>" + ("<w:b/>" if bold else "") + (f'<w:color w:val="{color}"/>' if color else "") + \
                  (f'<w:sz w:val="{size}"/>' if size else "") + "</w:rPr>"
        runs = text if isinstance(text, list) else [(text, rpr)]
        xml = "".join(f'<w:r>{r or ""}<w:t xml:space="preserve">{_x(t)}</w:t></w:r>' for t, r in runs)
        body.append(f"<w:p>{ppr}{xml}</w:p>")

    t = rep["totals"]
    para("Laporan Delivery Tim", "Title")
    para(f"Periode {rep['period']['label']}", None, color="555555")
    para("Ringkasan", "Heading1")
    para(f"{t['merged']} MR selesai (merged)" + (f", {t['open']} masih berjalan" if t["open"] else "")
         + f", dikerjakan {t['people']} orang.")
    para(" · ".join(f"{KINDS[k]}: {t[k]} MR" for k in KINDS if t[k] or k in ("feature", "bugfix")))
    if rep["features"]:
        para("Fitur yang dikerjakan", "Heading1")
        for f in rep["features"]:
            st = f"{f['merged']} MR selesai" + (f", {f['open']} berjalan" if f["open"] else "")
            para([(f"{KINDS[f['kind']]}: ", '<w:rPr><w:color w:val="555555"/></w:rPr>'),
                  (f["name"], "<w:rPr><w:b/></w:rPr>"), (f" ({st}) · {', '.join(f['people'])}", "")], "ListBullet")
    for team in rep["teams"]:
        para(f"Tim {team['team']}" if team["team"] != "Belum terdaftar" else "Belum terdaftar di tim", "Heading1")
        for p in team["people"]:
            para(f"{p['name']} · {_counts_text(p)}", "Heading2")
            for g in p["groups"]:
                para([(f"{KINDS[g['kind']]}: ", '<w:rPr><w:color w:val="555555"/></w:rPr>'),
                      (g["name"], "<w:rPr><w:b/></w:rPr>")])
                for m in g["mrs"]:
                    state = "selesai" if m["state"] == "merged" else "berjalan"
                    para(f"!{m['iid']} {m['title']} · {m['project']} · {m['kind_label']} · {state} {m['date']}",
                         "ListBullet")
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           + "".join(body) +
           '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" '
           'w:left="1134" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr></w:body></w:document>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
              '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>'
              '<w:sz w:val="21"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="80"/></w:pPr>'
              '</w:pPrDefault></w:docDefaults>'
              '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
              '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/>'
              '<w:pPr><w:spacing w:after="60"/></w:pPr><w:rPr><w:b/><w:sz w:val="40"/></w:rPr></w:style>'
              '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
              '<w:pPr><w:keepNext/><w:spacing w:before="280" w:after="80"/><w:outlineLvl w:val="0"/></w:pPr>'
              '<w:rPr><w:b/><w:color w:val="1F3864"/><w:sz w:val="28"/></w:rPr></w:style>'
              '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>'
              '<w:pPr><w:keepNext/><w:spacing w:before="200" w:after="60"/><w:outlineLvl w:val="1"/></w:pPr>'
              '<w:rPr><w:b/><w:sz w:val="23"/></w:rPr></w:style>'
              '<w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/>'
              '<w:basedOn w:val="Normal"/><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
              '<w:spacing w:after="40"/></w:pPr></w:style></w:styles>')
    numbering = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                 '<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/>'
                 '<w:lvlText w:val="•"/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="567" w:hanging="283"/></w:pPr>'
                 '</w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>')
    ctypes = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
              '<Default Extension="xml" ContentType="application/xml"/>'
              '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
              'wordprocessingml.document.main+xml"/>'
              '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.'
              'wordprocessingml.styles+xml"/>'
              '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.'
              'wordprocessingml.numbering+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>')
    doc_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                'styles" Target="styles.xml"/>'
                '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                'numbering" Target="numbering.xml"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ctypes)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
        z.writestr("word/styles.xml", styles)
        z.writestr("word/numbering.xml", numbering)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
    return buf.getvalue()
