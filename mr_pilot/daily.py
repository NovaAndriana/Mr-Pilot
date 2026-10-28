"""Daily check: the lead's 09:00 review of every task per team (progress, status, next action, blocker).

- Paste the Teams text you already write -> it is read, tidied up (status names, typos, missing links,
  "belum ada blocker" variants, percentages) and saved per day. Everything can also be filled in by hand.
- Every task is checked against the day before and against GitLab: status that contradicts the progress,
  next action copied from another task, link pointing to another ticket, stale "besok" dates, tasks that
  haven't moved for days, blockers that keep waiting on someone, "Code Review" without an MR, merged MRs
  still marked In Progress.
- "Salin untuk Teams" gives the same format back, cleaned up."""
import difflib
import json
import logging
import re
import time
from datetime import date, timedelta

from .reviewer import extract_json
from .util import short_error

log = logging.getLogger("mr_pilot.daily")

DEFAULT_STATUSES = ["Open", "In Progress", "Code Review", "Ready for QA", "SIT", "UAT", "Done", "Blocked",
                    "On Hold", "Belum dikonfirmasi"]
ADVANCED = {"code review", "ready for qa", "sit", "uat", "done"}
EARLY = {"open", "in progress"}
FINAL = {"done"}
MONTHS = ["januari", "februari", "maret", "april", "mei", "juni", "juli", "agustus", "september", "oktober",
          "november", "desember"]
MONTH_ALIAS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "mei": 5, "may": 5, "jun": 6, "jul": 7, "agu": 8, "agt": 8,
               "aug": 8, "sep": 9, "okt": 10, "oct": 10, "nov": 11, "des": 12, "dec": 12}
FIELDS = {"progress": "progress", "status": "status", "next action": "next_action", "next": "next_action",
          "blocker": "blocker", "kendala": "blocker"}
TYPOS = {"bvelum": "belum", "blm": "belum", "belom": "belum", "pemyesuaian": "penyesuaian",
         "penyesuain": "penyesuaian", "pengherjaan": "pengerjaan", "pengerjaaan": "pengerjaan", "samnpai": "sampai",
         "smpai": "sampai", "kordinasi": "koordinasi", "kordinsi": "koordinasi", "implementsi": "implementasi",
         "impelementasi": "implementasi", "dokumnetasi": "dokumentasi", "konfrimasi": "konfirmasi",
         "selsai": "selesai", "seleasi": "selesai", "testting": "testing", "pengujain": "pengujian",
         "tdk": "tidak", "sdh": "sudah", "udah": "sudah", "dgn": "dengan", "yg": "yang"}

_KEY = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
_DASH_SPLIT = re.compile(r"\s+[—–-]\s+")
NOT_STARTED = re.compile(r"\bbelum\s+(di\s*)?(mulai|dikerjakan|dimulai)\b|\bnot\s+started\b", re.I)
DONE_RE = re.compile(r"^\s*(done|sudah(\s+selesai)?|selesai|implementasi selesai|sudah done|complete[d]?)\b", re.I)
NO_UPDATE = re.compile(r"belum ada (pembaruan|update)|no update", re.I)
START_NEXT = re.compile(r"\bmemulai\s+(implementasi|pengerjaan)\b", re.I)
NONE_BLOCKER = re.compile(r"^\s*(-|–|tidak ada|belum ada|none|aman|nihil|no)\b[^,;]*?$", re.I)
_PCT = re.compile(r"(\d{1,3})\s*%")


# ------------------------------------------------------------------ helpers
def parse_date(s, default=None):
    s = str(s or "").strip().lower()
    m = re.search(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", s)
    if m:
        name = m.group(2)
        month = MONTHS.index(name) + 1 if name in MONTHS else MONTH_ALIAS.get(name[:3])
        if month:
            try:
                return date(int(m.group(3)), month, int(m.group(1)))
            except ValueError:
                pass
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", s)
    if m:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return default


def date_id(d):
    return f"{d.day} {MONTHS[d.month - 1].capitalize()} {d.year}"


def team_name(s):
    """'MOBILE' -> 'Mobile', 'fe' -> 'FE' (same names as the delivery report)."""
    s = " ".join(str(s or "").split()) or "Tim"
    return s.upper() if len(s) <= 3 else s[0].upper() + s[1:].lower() if s.isupper() or s.islower() else s


def is_none_blocker(text):
    t = (text or "").strip().rstrip(".").strip()
    if t in ("", "-", "–"):
        return True
    return bool(NONE_BLOCKER.match(t)) and not re.search(r"\b(menunggu|tunggu|bergantung|nunggu)\b", t, re.I)


def fix_typos(text):
    """-> (text, [(wrong, right)]) using the built-in list of common slips."""
    fixes = []

    def rep(m):
        w = m.group(0)
        right = TYPOS.get(w.lower())
        if not right:
            return w
        if w[0].isupper():
            right = right[0].upper() + right[1:]
        fixes.append((w, right))
        return right
    return re.sub(r"[A-Za-z]+", rep, text or ""), fixes


def tidy_sentence(text):
    """'tinggal di push' -> 'Tinggal di push.'; '-.' -> '-'."""
    t = " ".join((text or "").split())
    if t in ("-", "-.", "–", "") or t.lower() in ("n/a", "na"):
        return "-"
    if t[0].islower():
        t = t[0].upper() + t[1:]
    if t[-1] not in ".!?:)":
        t += "."
    return t


def pct_of(progress, status=""):
    m = _PCT.search(progress or "")
    if m:
        return max(0, min(100, int(m.group(1))))
    if NOT_STARTED.search(progress or "") or NO_UPDATE.search(progress or ""):
        return 0 if NOT_STARTED.search(progress or "") else None
    if DONE_RE.search(progress or ""):
        return 100
    return None


def split_status(raw, statuses):
    """'In Progres — Testing.' -> ('In Progress', 'Testing', corrected?)"""
    t = (raw or "").strip().rstrip(".").strip()
    if not t:
        return "", "", False
    parts = _DASH_SPLIT.split(t, maxsplit=1)
    base, note = parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")
    lower = {s.lower(): s for s in statuses}
    if base.lower() in lower:
        return lower[base.lower()], note, lower[base.lower()] != base
    m = difflib.get_close_matches(base.lower(), list(lower), n=1, cutoff=0.8)
    if m:
        return lower[m[0]], note, True
    return base, note, False


# ------------------------------------------------------------------ parser
def parse_text(text, statuses=None):
    """Teams text -> {"day": date|None, "tasks": [...], "groups": {grp: {"version"}}, "problems": [...]}.
    Tasks keep the order of the text; subtasks carry "parent" (index into tasks)."""
    statuses = statuses or DEFAULT_STATUSES
    team, grp, pic, day = "", "", "", None
    tasks, groups, problems = [], {}, []
    top = None           # index of the last top-level task (for subtasks)
    cur = None           # index of the task fields are written to
    scope_mode = False
    for n, raw in enumerate((text or "").splitlines(), 1):
        line = raw.replace("\xa0", " ").strip()
        if not line or re.fullmatch(r"[-=_*·]{3,}", line):
            continue
        m = re.match(r"^DAILY\s+CHECK(?:\s+TEAM)?\s*[:\-]?\s*(.*)$", line, re.I)
        if m:
            team, grp, pic, top, cur, scope_mode = team_name(m.group(1)), "", "", None, None, False
            continue
        m = re.match(r"^Tanggal\s*:\s*(.+)$", line, re.I)
        if m:
            day = parse_date(m.group(1), day)
            continue
        m = re.match(r"^\d+\.\s*GROUP\s+(.+)$", line, re.I)
        if m:
            grp, pic, top, cur, scope_mode = m.group(1).strip(), "", None, None, False
            groups.setdefault(grp, {"version": "", "team": team})
            continue
        m = re.match(r"^Versi\s*:\s*(.+)$", line, re.I)
        if m:
            groups.setdefault(grp, {"version": "", "team": team})["version"] = m.group(1).strip()
            continue
        m = re.match(r"^([A-Z])\.\s*(.+)$", line)
        if m:
            scope_mode = False
            pm = re.match(r"^PIC\s*:\s*(.+)$", m.group(2), re.I)
            if pm:
                pic, top, cur = pm.group(1).strip(), None, None
                continue
            # a section without a PIC (e.g. "E. Planning & Analisis — eKYC Dukcapil"): one item of its own
            tasks.append(_new_task(team, grp, "", None, m.group(2).strip(), n, section=True))
            pic, top, cur = "", None, len(tasks) - 1
            continue
        m = re.match(r"^Subtask\s*\d*\s*:\s*(.+)$", line, re.I)
        if m:
            ticket, title = _ticket_title(m.group(1))
            t = _new_task(team, grp, pic, ticket, title, n)
            t["parent"] = top
            if top is None:
                problems.append({"line": n, "msg": "Subtask tanpa task induk, disimpan sebagai task biasa."})
            tasks.append(t)
            cur, scope_mode = len(tasks) - 1, False
            continue
        m = re.match(r"^(\d+)\.\s*(.+)$", line)
        if m and not re.match(r"^[-•*]", line):
            ticket, title = _ticket_title(m.group(2))
            tasks.append(_new_task(team, grp, pic, ticket, title, n))
            top = cur = len(tasks) - 1
            scope_mode = False
            continue
        m = re.match(r"^Link[^:]*:\s*(\S+)", line, re.I)
        if m and cur is not None:
            tasks[cur]["link"] = m.group(1).strip()
            continue
        m = re.match(r"^[-•*]?\s*(Progress|Status|Next\s*Action|Next|Blocker|Kendala)\s*:\s*(.*)$", line, re.I)
        if m:
            if cur is None:
                problems.append({"line": n, "msg": f"Baris di luar task: {line[:60]}"})
                continue
            key = FIELDS[" ".join(m.group(1).lower().split())]
            tasks[cur]["fields"][key] = m.group(2).strip()
            scope_mode = False
            continue
        if line.endswith(":") and cur is not None:
            scope_mode = bool(re.match(r"^scope", line, re.I))
            continue
        m = re.match(r"^[-•*]\s*(.+)$", line)
        if m and cur is not None and scope_mode:
            tasks[cur]["scope"].append(m.group(1).strip().rstrip("."))
            continue
        if cur is not None and tasks[cur]["fields"]:
            last = list(tasks[cur]["fields"])[-1]  # wrapped line: continues the last field
            tasks[cur]["fields"][last] += " " + line
            continue
        problems.append({"line": n, "msg": f"Baris tidak dikenali, dilewati: {line[:60]}"})
    return {"day": day, "tasks": tasks, "groups": groups, "problems": problems}


def _ticket_title(s):
    s = s.strip()
    m = re.match(r"^([A-Za-z][A-Za-z0-9]+-\d+)\s*(?:[—–:\-]\s*)?(.*)$", s)
    if m and _KEY.match(m.group(1).upper()):
        return m.group(1).upper(), (m.group(2) or "").strip() or m.group(1).upper()
    return None, s


def _new_task(team, grp, pic, ticket, title, line, section=False):
    return {"team": team or "Tim", "grp": grp, "pic": pic, "ticket": ticket, "title": title, "link": "",
            "scope": [], "fields": {}, "parent": None, "section": section, "line": line}


# ------------------------------------------------------------------ tidy up
def tidy(parsed, statuses=None, link_base=""):
    """Auto-correct in place. -> list of corrections {i, field, before, after, why}."""
    statuses = statuses or DEFAULT_STATUSES
    out = []
    bases = [re.sub(r"[A-Z][A-Z0-9]+-\d+/?$", "", t["link"]) for t in parsed["tasks"] if t.get("link")]
    base = link_base or (max(set(bases), key=bases.count) if bases else "")
    for i, t in enumerate(parsed["tasks"]):
        f = t["fields"]
        for k in ("progress", "next_action", "blocker"):
            if k not in f:
                continue
            fixed, typos = fix_typos(f[k])
            nice = tidy_sentence(fixed)
            if typos:
                out.append({"i": i, "field": k, "before": f[k], "after": nice,
                            "why": "salah ketik: " + ", ".join(f"{a} → {b}" for a, b in typos)})
            f[k] = nice
        if "status" in f:
            st, note, changed = split_status(fix_typos(f["status"])[0], statuses)
            if changed:
                out.append({"i": i, "field": "status", "before": f["status"],
                            "after": st + (f" — {note}" if note else ""), "why": "nama status disamakan"})
            f["status"], f["status_note"] = st, note
        if "blocker" in f and is_none_blocker(f["blocker"]) and f["blocker"] != "-":
            f["blocker"] = "-"
        if t["ticket"] and not t["link"] and base:
            t["link"] = base + t["ticket"]
            out.append({"i": i, "field": "link", "before": "", "after": t["link"], "why": "link dibuat dari nomor tiket"})
        if f:
            f["pct"] = pct_of(f.get("progress"), f.get("status"))
    return out


AI_FIX_PROMPT = ("Perbaiki HANYA salah ketik dan ejaan bahasa Indonesia pada setiap teks. Jangan mengubah makna, "
                 "jangan menambah informasi, jangan menerjemahkan istilah teknis (MR, API, QA, SIT, deploy, push, "
                 "nama orang, nomor tiket). Kembalikan hanya yang berubah. Jawab HANYA JSON: "
                 '{"items": [{"id": "...", "text": "..."}]}')


def ai_fix(ai, items):
    """items [{id, text}] -> {id: corrected} for small spelling fixes only (big rewrites are dropped)."""
    if not items or not ai:
        return {}
    raw, _ = ai.complete(AI_FIX_PROMPT, json.dumps(items, ensure_ascii=False), "daily", validate=extract_json)
    orig = {x["id"]: x["text"] for x in items}
    out = {}
    for x in extract_json(raw).get("items") or []:
        k, new = str(x.get("id") or ""), str(x.get("text") or "").strip()
        if k in orig and new and new != orig[k] and \
                difflib.SequenceMatcher(None, orig[k].lower(), new.lower()).ratio() >= 0.75:
            out[k] = new
    return out


def ai_polish(ai, parsed):
    """Spelling pass by the AI (optional): only typos/grammar, never the meaning. -> corrections."""
    items = []
    for i, t in enumerate(parsed["tasks"]):
        for k in ("progress", "next_action", "blocker"):
            v = t["fields"].get(k)
            if v and v != "-":
                items.append({"id": f"{i}:{k}", "text": v})
    out = []
    orig = {x["id"]: x["text"] for x in items}
    for k, new in ai_fix(ai, items).items():
        i, field = k.split(":", 1)
        parsed["tasks"][int(i)]["fields"][field] = tidy_sentence(new)
        out.append({"i": int(i), "field": field, "before": orig[k], "after": tidy_sentence(new), "why": "ejaan (AI)"})
    return out


# ------------------------------------------------------------------ checks
def check_entry(task, e, day, prev=None, history=(), mrs=(), dup_with=()):
    """Warnings for one task on one day. history = older entries newest first (same task)."""
    w = []
    st = (e.get("status") or "").lower()
    prog = e.get("progress") or ""
    nxt = e.get("next_action") or ""
    pct = e.get("pct")
    if task.get("ticket") and task.get("link"):
        lk = _KEY.findall(task["link"].upper())
        if lk and lk[-1] != task["ticket"]:
            w.append({"code": "link", "level": "warn",
                      "msg": f"Link mengarah ke {lk[-1]}, bukan {task['ticket']}."})
    if st in ADVANCED and (pct == 0 or NOT_STARTED.search(prog)):
        w.append({"code": "status-vs-progress", "level": "warn",
                  "msg": f"Progress belum dimulai tapi status {e.get('status')}."})
    if st in EARLY and (pct == 100 or DONE_RE.search(prog)):
        w.append({"code": "done-not-moved", "level": "warn",
                  "msg": f"Progress sudah selesai tapi status masih {e.get('status')}; mungkin Code Review / Ready for QA?"})
    if START_NEXT.search(nxt) and ((pct or 0) >= 50 or st in ADVANCED or re.search(r"\bdeploy|selesai\b", prog, re.I)):
        w.append({"code": "next-vs-progress", "level": "warn",
                  "msg": "Next action 'memulai implementasi' padahal progress sudah jalan."})
    for other in dup_with:
        w.append({"code": "dup-next", "level": "warn",
                  "msg": f"Next action sama persis dengan {other}, kemungkinan hasil salin."})
    for m in re.finditer(r"\bbesok\b[^.]*?(\d{1,2}\s+[A-Za-z]+\s+\d{4})", prog + " " + nxt, re.I):
        d = parse_date(m.group(1))
        if d and d <= day:
            w.append({"code": "stale-date", "level": "warn",
                      "msg": f"Tertulis 'besok, {m.group(1)}' di laporan tanggal {date_id(day)}."})
    if NO_UPDATE.search(prog):
        w.append({"code": "no-update", "level": "info", "msg": "Belum ada update progress."})
    same = 0
    for h in history:
        if (h.get("progress"), h.get("status")) == (e.get("progress"), e.get("status")):
            same += 1
        else:
            break
    if same >= 2 and st not in FINAL:
        w.append({"code": "stalled", "level": "warn", "msg": f"Progress dan status tidak berubah {same + 1} hari."})
    if not is_none_blocker(e.get("blocker")):
        days = 1
        for h in history:
            if not is_none_blocker(h.get("blocker")) and _norm(h.get("blocker")) == _norm(e.get("blocker")):
                days += 1
            else:
                break
        if days >= 3:
            who = f" (menunggu {e['waiting_on']})" if e.get("waiting_on") else ""
            w.append({"code": "blocker-long", "level": "escalate",
                      "msg": f"Blocker yang sama sudah {days} hari{who}. Perlu dieskalasi?"})
    if st and st not in {s.lower() for s in e.get("_statuses", [])} and e.get("_statuses"):
        w.append({"code": "unknown-status", "level": "info", "msg": f"Status '{e.get('status')}' belum ada di daftar."})
    # GitLab evidence (MRs that came to the lead)
    open_mrs = [m for m in mrs if m["state"] == "open"]
    merged = [m for m in mrs if m["state"] == "merged"]
    if task.get("ticket"):
        if st == "code review" and not open_mrs and not merged:
            w.append({"code": "review-no-mr", "level": "warn",
                      "msg": "Status Code Review tapi belum ada MR untuk tiket ini yang masuk ke Anda."})
        if merged and st in EARLY and not open_mrs:
            w.append({"code": "merged-still-early", "level": "warn",
                      "msg": f"MR !{merged[-1]['iid']} sudah merged ke {merged[-1]['target']}, status masih {e.get('status')}."})
        for m in open_mrs:
            if m.get("pipeline") == "failed":
                w.append({"code": "pipeline-failed", "level": "info", "msg": f"Pipeline MR !{m['iid']} gagal."})
    return w


def _norm(s):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split())


# ------------------------------------------------------------------ service
class Daily:
    def __init__(self, cfg, store, ai=None):
        self.cfg = cfg.get("daily") or {}
        self.store, self.ai = store, ai

    # settings
    def statuses(self):
        try:
            v = json.loads(self.store.kv_get("daily_statuses") or "null")
            if isinstance(v, list) and v:
                return [str(x) for x in v]
        except ValueError:
            pass
        return list(self.cfg.get("statuses") or DEFAULT_STATUSES)

    def save_statuses(self, items):
        clean = []
        for s in items or []:
            s = " ".join(str(s).split())[:40]
            if s and s.lower() not in {x.lower() for x in clean}:
                clean.append(s)
        if not clean:
            raise ValueError("daftar status tidak boleh kosong")
        self.store.kv_set("daily_statuses", json.dumps(clean, ensure_ascii=False))
        return clean

    def groups_meta(self):
        try:
            return json.loads(self.store.kv_get("daily_groups") or "{}")
        except ValueError:
            return {}

    def link_base(self):
        return self.store.kv_get("daily_link_base") or self.cfg.get("link_base") or ""

    # ------------------------------------------------------------ import
    def preview(self, text, use_ai=False):
        parsed = parse_text(text, self.statuses())
        corr = tidy(parsed, self.statuses(), self.link_base())
        note = ""
        if use_ai:
            try:
                corr += ai_polish(self.ai, parsed)
            except Exception as ex:
                note = f"AI tidak bisa merapikan ejaan ({short_error(ex)}); koreksi bawaan tetap dipakai."
        day = parsed["day"] or date.today()
        for c in corr:
            t = parsed["tasks"][c["i"]]
            c["task"] = t["ticket"] or t["title"]
        return {"day": day.isoformat(), "day_label": date_id(day), "day_from_text": bool(parsed["day"]),
                "tasks": parsed["tasks"], "groups": parsed["groups"], "corrections": corr,
                "problems": parsed["problems"], "note": note}

    def import_text(self, text, day=None, use_ai=False):
        pv = self.preview(text, use_ai)
        d = day or pv["day"]
        bases = [re.sub(r"[A-Z][A-Z0-9]+-\d+/?$", "", t["link"]) for t in pv["tasks"] if t.get("link")]
        if bases and not self.store.kv_get("daily_link_base"):
            self.store.kv_set("daily_link_base", max(set(bases), key=bases.count))
        meta = self.groups_meta()
        for g, m in pv["groups"].items():
            meta[g] = {**meta.get(g, {}), **{k: v for k, v in m.items() if v}}
        self.store.kv_set("daily_groups", json.dumps(meta, ensure_ascii=False))
        ids = []
        team_rank = {}
        for i, t in enumerate(pv["tasks"]):
            team_rank.setdefault(t["team"], len(team_rank))
            parent_id = ids[t["parent"]] if t.get("parent") is not None and t["parent"] < len(ids) else None
            tid = self.save_task({"ticket": t["ticket"], "title": t["title"], "link": t["link"], "team": t["team"],
                                  "grp": t["grp"], "pic": t["pic"], "parent_id": parent_id, "scope": t["scope"],
                                  "ord": team_rank[t["team"]] * 10000 + i}, match=True)
            ids.append(tid)
            f = t["fields"]
            if f or not any(x.get("parent") == i for x in pv["tasks"]):
                self.save_entry(tid, d, {"progress": f.get("progress", ""), "pct": f.get("pct"),
                                         "status": f.get("status", ""), "status_note": f.get("status_note", ""),
                                         "next_action": f.get("next_action", "-"), "blocker": f.get("blocker", "-")})
        self.store.add_event("daily", f"Daily check {date_id(date.fromisoformat(d))} diimpor",
                             f"{len(ids)} task, {len(pv['corrections'])} koreksi otomatis")
        return {**self.day(d), "corrections": pv["corrections"], "problems": pv["problems"], "note": pv["note"]}

    # ------------------------------------------------------------ edits
    def _find(self, t):
        if t.get("ticket"):
            r = self.store.query("SELECT id FROM daily_tasks WHERE ticket=? ORDER BY id DESC LIMIT 1", (t["ticket"],))
        else:
            r = self.store.query("SELECT id FROM daily_tasks WHERE ticket IS NULL AND lower(title)=lower(?) "
                                 "AND team=? ORDER BY id DESC LIMIT 1", (t.get("title") or "", t.get("team") or ""))
        return r[0]["id"] if r else None

    def save_task(self, t, match=False):
        now = time.time()
        title = " ".join(str(t.get("title") or "").split())[:300]
        ticket = (str(t.get("ticket") or "").strip().upper() or None)
        if ticket and not _KEY.fullmatch(ticket):
            raise ValueError("nomor tiket tidak valid, contoh IDAS-5392")
        if not title and not ticket:
            raise ValueError("judul atau nomor tiket wajib diisi")
        link = str(t.get("link") or "").strip()
        if ticket and not link and self.link_base():
            link = self.link_base() + ticket
        vals = {"ticket": ticket, "title": title or ticket, "link": link,
                "team": team_name(t.get("team"))[:40], "grp": str(t.get("grp") or "").strip()[:120],
                "pic": str(t.get("pic") or "").strip()[:80], "parent_id": t.get("parent_id"),
                "scope": json.dumps([str(x)[:200] for x in (t.get("scope") or [])], ensure_ascii=False)}
        tid = t.get("id") or (self._find({**t, "ticket": ticket, "title": title}) if match else None)
        if tid:
            sets = ", ".join(f"{k}=?" for k in vals)
            params = list(vals.values())
            if t.get("ord") is not None:
                sets += ", ord=?"
                params.append(t["ord"])
            self.store.execute(f"UPDATE daily_tasks SET {sets}, active=1, updated_at=? WHERE id=?", (*params, now, tid))
            return int(tid)
        ord_ = t.get("ord")
        if ord_ is None:
            r = self.store.query("SELECT MAX(ord) m FROM daily_tasks WHERE team=?", (vals["team"],))
            ord_ = (r[0]["m"] or 0) + 1
        self.store.execute("INSERT INTO daily_tasks(ticket, title, link, team, grp, pic, parent_id, scope, ord, "
                           "active, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,1,?,?)",
                           (*vals.values(), ord_, now, now))
        return self.store.query("SELECT MAX(id) id FROM daily_tasks")[0]["id"]

    def save_entry(self, task_id, day, f, use_ai=False):
        """Upsert one day's entry; text is auto-corrected. -> [{field, before, after, why}]."""
        if not self.store.query("SELECT 1 FROM daily_tasks WHERE id=?", (task_id,)):
            raise ValueError("task tidak ditemukan")
        date.fromisoformat(day)
        cur = (self.store.query("SELECT * FROM daily_entries WHERE task_id=? AND day=?", (task_id, day)) or [{}])[0]
        e = {k: cur.get(k) for k in ("progress", "pct", "status", "status_note", "next_action", "blocker", "waiting_on")}
        for k in e:
            if k in f and f[k] is not None:
                e[k] = f[k]
        if "status" in f and f["status"] is not None:
            st, note, _ = split_status(str(f["status"]), self.statuses())
            e["status"] = st
            if note:
                e["status_note"] = note
            elif "status_note" in f:
                e["status_note"] = f["status_note"]
        corr = []
        if "status" in f and f["status"] is not None and str(f["status"]).strip():
            shown = e["status"] + (f" — {e['status_note']}" if e.get("status_note") else "")
            raw = str(f["status"]).strip().rstrip(".")
            if raw.lower() != shown.lower() and raw.split(" — ")[0].strip().lower() != e["status"].lower():
                corr.append({"field": "status", "before": raw, "after": e["status"], "why": "nama status disamakan"})
        texts = {}
        for k in ("progress", "next_action", "blocker"):
            if k in f and f[k] is not None:
                fixed, typos = fix_typos(str(f[k]))
                e[k] = tidy_sentence(fixed)
                texts[k] = e[k]
                if typos:
                    corr.append({"field": k, "before": str(f[k]), "after": e[k],
                                 "why": "salah ketik: " + ", ".join(f"{a} → {b}" for a, b in typos)})
        if use_ai and self.ai:
            try:
                fixed = ai_fix(self.ai, [{"id": k, "text": v} for k, v in texts.items() if v and v != "-"])
            except Exception as ex:
                log.info("AI ejaan gagal: %s", short_error(ex))
                fixed = {}
            for k, new in fixed.items():
                before = e[k]
                e[k] = tidy_sentence(new)
                corr.append({"field": k, "before": before, "after": e[k], "why": "ejaan (AI)"})
        if e.get("blocker") and is_none_blocker(e["blocker"]):
            e["blocker"] = "-"
        if "pct" in f and f["pct"] not in (None, ""):
            e["pct"] = max(0, min(100, int(f["pct"])))
        elif "progress" in f:
            e["pct"] = pct_of(e.get("progress"))
        self.store.execute("INSERT INTO daily_entries(task_id, day, progress, pct, status, status_note, next_action, "
                           "blocker, waiting_on, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id, day) "
                           "DO UPDATE SET progress=excluded.progress, pct=excluded.pct, status=excluded.status, "
                           "status_note=excluded.status_note, next_action=excluded.next_action, "
                           "blocker=excluded.blocker, waiting_on=excluded.waiting_on, updated_at=excluded.updated_at",
                           (task_id, day, e.get("progress") or "", e.get("pct"), e.get("status") or "",
                            e.get("status_note") or "", e.get("next_action") or "-", e.get("blocker") or "-",
                            (e.get("waiting_on") or "")[:80], time.time()))
        return corr

    def remove_entry(self, task_id, day):
        self.store.execute("DELETE FROM daily_entries WHERE task_id=? AND day=?", (task_id, day))

    def archive(self, task_id, active=False):
        self.store.execute("UPDATE daily_tasks SET active=? WHERE id=?", (1 if active else 0, task_id))

    def carry_over(self, day):
        """Start `day` from the latest earlier day: every active task not Done comes along (same text, so
        what nobody updates shows up as unchanged)."""
        prev = self.store.query("SELECT MAX(day) d FROM daily_entries WHERE day < ?", (day,))[0]["d"]
        if not prev:
            return 0
        rows = self.store.query("SELECT e.* FROM daily_entries e JOIN daily_tasks t ON t.id=e.task_id "
                                "WHERE e.day=? AND t.active=1", (prev,))
        n = 0
        for r in rows:
            if (r.get("status") or "").lower() in FINAL:
                continue
            if self.store.query("SELECT 1 FROM daily_entries WHERE task_id=? AND day=?", (r["task_id"], day)):
                continue
            self.store.execute("INSERT INTO daily_entries(task_id, day, progress, pct, status, status_note, "
                               "next_action, blocker, waiting_on, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (r["task_id"], day, r["progress"], r["pct"], r["status"], r["status_note"],
                                r["next_action"], r["blocker"], r["waiting_on"], time.time()))
            n += 1
        return n

    # ------------------------------------------------------------ view
    def _mrs_by_ticket(self, tickets):
        out = {t: [] for t in tickets}
        if not tickets:
            return out
        for r in self.store.query("SELECT iid, title, web_url, status, source_branch, target_branch, pipeline, "
                                  "updated_at FROM mrs"):
            hay = f"{r.get('title') or ''} {r.get('source_branch') or ''}".upper().replace("_", "-")
            for t in set(_KEY.findall(hay)):
                if t in out:
                    st = r.get("status")
                    out[t].append({"iid": r["iid"], "url": r.get("web_url"), "title": r.get("title"),
                                   "state": "merged" if st == "merged" else ("closed" if st in ("closed", "rejected")
                                                                            else "open"),
                                   "pipeline": r.get("pipeline"), "target": r.get("target_branch") or "",
                                   "updated": r.get("updated_at")})
        return out

    def day(self, day=None):
        day = day or date.today().isoformat()
        d = date.fromisoformat(day)
        statuses = self.statuses()
        rows = self.store.query("SELECT t.*, e.progress, e.pct, e.status, e.status_note, e.next_action, e.blocker, "
                                "e.waiting_on, e.updated_at AS e_updated FROM daily_entries e JOIN daily_tasks t "
                                "ON t.id=e.task_id WHERE e.day=? ORDER BY t.ord, t.id", (day,))
        # parents without an entry that day still show (their subtasks have one)
        have = {r["id"] for r in rows}
        for pid in {r["parent_id"] for r in rows if r.get("parent_id")} - have:
            p = self.store.query("SELECT * FROM daily_tasks WHERE id=?", (pid,))
            if p:
                rows.append({**p[0], "_header_only": True})
        hist_rows = self.store.query("SELECT * FROM daily_entries WHERE day < ? AND day >= ? ORDER BY day DESC",
                                     (day, (d - timedelta(days=21)).isoformat()))
        hist = {}
        for h in hist_rows:
            hist.setdefault(h["task_id"], []).append(h)
        mrs = self._mrs_by_ticket({r["ticket"] for r in rows if r.get("ticket")})
        nexts = {}
        for r in rows:
            n = _norm(r.get("next_action"))
            if len(n) >= 25:
                nexts.setdefault(n, []).append(r.get("ticket") or r.get("title"))
        tasks, warn_count, blockers, escalate = [], 0, [], []
        for r in rows:
            r["scope"] = json.loads(r.get("scope") or "[]")
            if r.get("_header_only"):
                r["warnings"], r["mrs"] = [], mrs.get(r.get("ticket"), [])
                tasks.append(r)
                continue
            me = r.get("ticket") or r.get("title")
            dup = [x for x in nexts.get(_norm(r.get("next_action")), []) if x != me]
            e = {**r, "_statuses": statuses}
            r["mrs"] = mrs.get(r.get("ticket"), [])
            r["warnings"] = check_entry(r, e, d, history=hist.get(r["id"], []), mrs=r["mrs"], dup_with=dup)
            prev = (hist.get(r["id"]) or [None])[0]
            r["changed"] = None if not prev else any(
                (prev.get(k) or "") != (r.get(k) or "") for k in ("progress", "status", "next_action", "blocker"))
            warn_count += sum(1 for w in r["warnings"] if w["level"] in ("warn", "escalate"))
            if not is_none_blocker(r.get("blocker")):
                blockers.append({"task": me, "pic": r.get("pic"), "team": r.get("team"), "blocker": r["blocker"],
                                 "waiting_on": r.get("waiting_on")})
            escalate += [{"task": me, "pic": r.get("pic"), "msg": w["msg"]} for w in r["warnings"]
                         if w["level"] == "escalate"]
            tasks.append(r)
        tasks.sort(key=lambda t: (t.get("ord") or 0, t["id"]))
        prev_day = self.store.query("SELECT MAX(day) d FROM daily_entries WHERE day < ?", (day,))[0]["d"]
        next_day = self.store.query("SELECT MIN(day) d FROM daily_entries WHERE day > ?", (day,))[0]["d"]
        teams = self._tree(tasks)
        done = sum(1 for t in tasks if (t.get("status") or "").lower() in FINAL)
        return {"day": day, "day_label": date_id(d), "prev_day": prev_day, "next_day": next_day,
                "statuses": statuses, "teams": teams, "groups_meta": self.groups_meta(),
                "link_base": self.link_base(),
                "summary": {"tasks": sum(1 for t in tasks if not t.get("_header_only")), "warnings": warn_count,
                            "blockers": len(blockers), "done": done,
                            "unchanged": sum(1 for t in tasks if t.get("changed") is False)},
                "blockers": blockers, "escalate": escalate,
                "pics": sorted({t["pic"] for t in tasks if t.get("pic")}),
                "team_names": [t["team"] for t in teams],
                "group_names": sorted({t["grp"] for t in tasks if t.get("grp")})}

    @staticmethod
    def _tree(tasks):
        """team -> group -> blocks (a PIC with tasks, or a section item) -> tasks with their subtasks."""
        by_id = {t["id"]: t for t in tasks}
        for t in tasks:
            t["subtasks"] = []
        roots = []
        for t in tasks:
            p = by_id.get(t.get("parent_id"))
            (p["subtasks"] if p else roots).append(t)
        teams = []
        for t in roots:
            team = next((x for x in teams if x["team"] == t["team"]), None)
            if not team:
                team = {"team": t["team"], "groups": []}
                teams.append(team)
            g = next((x for x in team["groups"] if x["name"] == (t.get("grp") or "")), None)
            if not g:
                g = {"name": t.get("grp") or "", "blocks": []}
                team["groups"].append(g)
            if not t.get("pic"):
                g["blocks"].append({"kind": "section", "name": t["title"], "tasks": [t]})
                continue
            b = next((x for x in g["blocks"] if x["kind"] == "pic" and x["name"] == t["pic"]), None)
            if not b:
                b = {"kind": "pic", "name": t["pic"], "tasks": []}
                g["blocks"].append(b)
            b["tasks"].append(t)
        return teams

    # ------------------------------------------------------------ export
    def teams_text(self, day=None, team=None):
        """The Teams format the lead already uses, cleaned up."""
        v = self.day(day)
        meta = v["groups_meta"]
        out, gno = [], 0
        teams = [t for t in v["teams"] if not team or t["team"] == team]
        for ti, tm in enumerate(teams):
            if ti:
                out += ["", "---", ""]
            out += [f"DAILY CHECK TEAM {tm['team']}", f"Tanggal: {v['day_label']}", ""]
            for g in tm["groups"]:
                gno += 1
                if g["name"]:
                    out.append(f"{gno}. GROUP {g['name']}")
                    ver = (meta.get(g["name"]) or {}).get("version")
                    if ver:
                        out.append(f"Versi: {ver}")
                    out.append("")
                for bi, b in enumerate(g["blocks"]):
                    letter = chr(ord("A") + bi % 26)
                    if b["kind"] == "pic":
                        out += [f"{letter}. PIC: {b['name']}", ""]
                        for n, t in enumerate(b["tasks"], 1):
                            out += _task_lines(t, f"{n}. ")
                    else:
                        t = b["tasks"][0]
                        out += [f"{letter}. {t['title']}", ""]
                        if t["scope"]:
                            out += ["Scope:"] + [f"- {s}." for s in t["scope"]] + [""]
                        out += _field_lines(t) + [""]
        return "\n".join(out).rstrip() + "\n"


def _task_lines(t, prefix):
    head = f"{t['ticket']} — {t['title']}" if t.get("ticket") else t["title"]
    lines = [f"{prefix}{head}"]
    if t.get("link"):
        lines.append(f"Link: {t['link']}")
    lines.append("")
    if t.get("scope"):
        lines += ["Scope:"] + [f"- {s}." for s in t["scope"]] + [""]
    for i, s in enumerate(t.get("subtasks") or [], 1):
        sh = f"{s['ticket']} — {s['title']}" if s.get("ticket") else s["title"]
        lines.append(f"Subtask {i}: {sh}")
        if s.get("link"):
            lines.append(f"Link: {s['link']}")
        lines += [""] + _field_lines(s) + [""]
    if not t.get("_header_only"):
        lines += _field_lines(t) + [""]
    return lines


def _field_lines(t):
    st = (t.get("status") or "-") + (f" — {t['status_note']}" if t.get("status_note") else "")
    blocker = t.get("blocker") or "-"
    if is_none_blocker(blocker):
        blocker = "Tidak ada."
    elif t.get("waiting_on") and t["waiting_on"].lower() not in blocker.lower():
        blocker = blocker.rstrip(".") + f" (menunggu {t['waiting_on']})."
    return [f"- Progress: {t.get('progress') or '-'}",
            f"- Status: {st}{'' if st.endswith('.') or st == '-' else '.'}",
            f"- Next Action: {t.get('next_action') or '-'}",
            f"- Blocker: {blocker}"]
