"""Task tracker (Jira or Plane): read the team's work items so the daily review can start from them.

One setting picks the active tracker (`tracker.provider`, switchable from the Daily page). Both providers
return the same normalised item:
  {key, title, state, group, people:[{id, username, name, email}], url, updated, due, project}
`group` is one of todo | doing | review | qa | done | cancelled.
Read-only: MR Pilot never changes anything in Jira or Plane.
"""
import json
import logging
import re
import time
from datetime import datetime

import requests

from .util import short_error

log = logging.getLogger("mr_pilot.tracker")

PROVIDERS = {"jira": "Jira", "plane": "Plane", "none": "Tidak dipakai"}
GROUPS = ("todo", "doing", "review", "qa", "done", "cancelled")


class TrackerError(Exception):
    pass


def state_group(name, category=""):
    """Normalise a workflow state into one of GROUPS (the state name wins for review/QA)."""
    n = (name or "").lower()
    c = (category or "").lower()
    if c in ("cancelled", "canceled") or re.search(r"\b(cancel|cancelled|canceled|won'?t|rejected|dibatalkan)\b", n):
        return "cancelled"
    if c in ("done", "completed") or re.search(r"\b(done|closed|resolved|released|selesai|deployed)\b", n):
        return "done"
    if re.search(r"\b(qa|test(ing)?|sit|uat)\b", n):
        return "qa"
    if re.search(r"review", n):
        return "review"
    if c in ("indeterminate", "started", "in_progress") or re.search(r"progress|doing|develop|sedang", n):
        return "doing"
    return "todo"


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _name_key(s):
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    parts = [p for p in s.split() if p not in ("mas", "mbak", "pak", "bu", "bapak", "ibu", "kak")]
    return " ".join(parts)


def person_keys(p):
    """Every way a tracker person can be recognised (lower-case)."""
    keys = set()
    for k in ("id", "username", "email"):
        v = str(p.get(k) or "").strip().lower()
        if v:
            keys.add(v)
    em = str(p.get("email") or "").strip().lower()
    if "@" in em:
        keys.add(em.split("@")[0])
    nm = _name_key(p.get("name"))
    if nm:
        keys.add(nm)
    return keys


def member_keys(m):
    keys = {str(m.get("username") or "").strip().lower()}
    for v in re.split(r"[,;]", str(m.get("tracker") or "")):
        v = v.strip().lower()
        if v:
            keys.add(v)
            if "@" in v:
                keys.add(v.split("@")[0])
            keys.add(_name_key(v))
    keys.add(_name_key(m.get("name")))
    keys.discard("")
    return keys


def match_members(item, members):
    """Team members an item is assigned to."""
    out = []
    for m in members:
        mk = member_keys(m)
        if any(mk & person_keys(p) for p in item.get("people") or []):
            out.append(m)
    return out


def _get(session, url, headers, params=None, verify=True, what="tracker"):
    for attempt in (1, 2):
        try:
            r = session.get(url, headers=headers, params=params, timeout=(10, 40), verify=verify)
        except requests.exceptions.SSLError:
            raise TrackerError(f"Sertifikat {what} tidak dipercaya (taruh file .pem di data/certs atau matikan verify_ssl)") from None
        except requests.exceptions.RequestException as ex:
            raise TrackerError(f"Tidak bisa terhubung ke {what}: {short_error(ex)}") from None
        if r.status_code == 429 and attempt == 1:
            time.sleep(min(float(r.headers.get("Retry-After") or 3), 10))
            continue
        break
    if r.status_code in (401, 403):
        raise TrackerError(f"{what} menolak kunci/token (HTTP {r.status_code}). Cek API key dan izin aksesnya.")
    if r.status_code == 404:
        raise TrackerError(f"{what}: alamat tidak ditemukan (HTTP 404). Cek url / workspace / project.")
    if r.status_code == 429:
        raise TrackerError(f"{what}: terlalu banyak request (rate limit). Coba lagi sebentar lagi.")
    if r.status_code >= 400:
        raise TrackerError(f"{what} membalas HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise TrackerError(f"{what} membalas bukan JSON; cek apakah alamatnya API, bukan halaman web.") from None


# ------------------------------------------------------------------ Jira
class Jira:
    label = "Jira"

    def __init__(self, cfg, session):
        self.c, self.s = cfg or {}, session
        self.url = str(self.c.get("url") or "").rstrip("/")
        self.verify = self.c.get("verify_ssl", True) is not False

    def configured(self):
        return bool(self.url and self.c.get("token"))

    def missing(self):
        return [k for k, v in (("jira.url", self.url), ("jira.token", self.c.get("token"))) if not v]

    def _flavor(self):
        f = str(self.c.get("flavor") or "auto").lower()
        if f == "auto":
            return "cloud" if "atlassian.net" in self.url else "server"
        return f

    def _headers(self):
        auth = str(self.c.get("auth") or "auto").lower()
        if auth == "auto":
            auth = "basic" if self.c.get("email") else "bearer"
        if auth == "basic":
            import base64
            raw = f"{self.c.get('email') or ''}:{self.c.get('token') or ''}".encode()
            return {"Authorization": "Basic " + base64.b64encode(raw).decode(), "Accept": "application/json"}
        return {"Authorization": f"Bearer {self.c.get('token')}", "Accept": "application/json"}

    def jql(self, lookback_days):
        if self.c.get("jql"):
            return str(self.c["jql"])
        projects = [str(p).strip() for p in (self.c.get("projects") or []) if str(p).strip()]
        where = [f"project in ({','.join(projects)})"] if projects else []
        where.append(f"(statusCategory != Done OR resolutiondate >= -{int(lookback_days)}d)")
        return " AND ".join(where) + " ORDER BY updated DESC"

    def fetch(self, lookback_days=7, max_items=2000):
        flavor, headers = self._flavor(), self._headers()
        fields = "summary,status,assignee,updated,duedate,project"
        out, start, token = [], 0, None
        while len(out) < max_items:
            if flavor == "cloud":
                params = {"jql": self.jql(lookback_days), "fields": fields, "maxResults": 100}
                if token:
                    params["nextPageToken"] = token
                data = _get(self.s, f"{self.url}/rest/api/3/search/jql", headers, params, self.verify, "Jira")
            else:
                params = {"jql": self.jql(lookback_days), "fields": fields, "maxResults": 100, "startAt": start}
                data = _get(self.s, f"{self.url}/rest/api/2/search", headers, params, self.verify, "Jira")
            issues = data.get("issues") or []
            for i in issues:
                out.append(self._item(i))
            if flavor == "cloud":
                token = data.get("nextPageToken")
                if not issues or data.get("isLast", True) or not token:
                    break
            else:
                start += len(issues)
                if not issues or start >= int(data.get("total") or 0):
                    break
        return out

    def _item(self, i):
        f = i.get("fields") or {}
        st = f.get("status") or {}
        cat = (st.get("statusCategory") or {}).get("key") or ""
        a = f.get("assignee") or {}
        people = []
        if a:
            people.append({"id": a.get("accountId") or a.get("key") or "", "username": a.get("name") or "",
                           "name": a.get("displayName") or "", "email": a.get("emailAddress") or ""})
        return {"key": i.get("key"), "title": f.get("summary") or "", "state": st.get("name") or "",
                "group": state_group(st.get("name"), {"new": "", "indeterminate": "started", "done": "done"}.get(cat, "")),
                "people": people, "url": f"{self.url}/browse/{i.get('key')}", "updated": _ts(f.get("updated")),
                "due": f.get("duedate") or "", "project": (f.get("project") or {}).get("name") or ""}

    def ping(self):
        data = _get(self.s, f"{self.url}/rest/api/{'3' if self._flavor() == 'cloud' else '2'}/myself",
                    self._headers(), None, self.verify, "Jira")
        return data.get("displayName") or data.get("name") or "ok"


# ------------------------------------------------------------------ Plane
class Plane:
    label = "Plane"

    def __init__(self, cfg, session):
        self.c, self.s = cfg or {}, session
        self.url = str(self.c.get("url") or "").rstrip("/")
        self.web = str(self.c.get("web_url") or "").rstrip("/") or self.url
        self.ws = str(self.c.get("workspace") or "").strip().strip("/")
        self.verify = self.c.get("verify_ssl", True) is not False
        self._members = None

    def configured(self):
        return bool(self.url and self.ws and self.c.get("api_key"))

    def missing(self):
        return [k for k, v in (("plane.url", self.url), ("plane.workspace", self.ws),
                               ("plane.api_key", self.c.get("api_key"))) if not v]

    def _h(self):
        return {"X-API-Key": str(self.c.get("api_key")), "Accept": "application/json"}

    def _base(self):
        return f"{self.url}/api/v1/workspaces/{self.ws}"

    @staticmethod
    def _results(data):
        return data if isinstance(data, list) else (data.get("results") or [])

    def _paged(self, url, params=None, max_pages=40):
        params, out, cursor = dict(params or {}), [], None
        for _ in range(max_pages):
            if cursor:
                params["cursor"] = cursor
            data = _get(self.s, url, self._h(), params, self.verify, "Plane")
            out += self._results(data)
            if isinstance(data, list) or not data.get("next_page_results") or not data.get("next_cursor"):
                break
            cursor = data["next_cursor"]
        return out

    def projects(self):
        wanted = {str(p).strip().lower() for p in (self.c.get("projects") or []) if str(p).strip()}
        allp = self._paged(f"{self._base()}/projects/", {"per_page": 100})
        if not wanted:
            return allp
        return [p for p in allp if wanted & {str(p.get("id") or "").lower(), str(p.get("identifier") or "").lower(),
                                             str(p.get("name") or "").lower()}]

    def members(self):
        if self._members is None:
            self._members = {}
            try:
                for m in self._paged(f"{self._base()}/members/", {"per_page": 100}):
                    m = m.get("member") or m
                    if m.get("id"):
                        self._members[str(m["id"])] = m
            except TrackerError as ex:
                log.info("Plane: daftar member tidak terbaca (%s); memakai data assignee dari work item", ex)
        return self._members

    def _person(self, a):
        if isinstance(a, str):
            a = self.members().get(a) or {"id": a}
        name = a.get("display_name") or " ".join(x for x in (a.get("first_name"), a.get("last_name")) if x)
        return {"id": str(a.get("id") or ""), "username": a.get("display_name") or "", "name": name,
                "email": a.get("email") or ""}

    def fetch(self, lookback_days=7, max_items=3000):
        out, cutoff = [], time.time() - lookback_days * 86400
        for p in self.projects():
            pid, ident = p.get("id"), p.get("identifier") or ""
            states = {}
            params = {"expand": "assignees,state", "per_page": 100, "order_by": "-updated_at"}
            try:
                rows = self._paged(f"{self._base()}/projects/{pid}/work-items/", params)
            except TrackerError as ex:
                if "404" not in str(ex) and "tidak ditemukan" not in str(ex):
                    raise
                rows = self._paged(f"{self._base()}/projects/{pid}/issues/", params)  # older Plane versions
            for w in rows:
                st = w.get("state")
                if isinstance(st, str):
                    if not states:
                        states = {str(s.get("id")): s for s in self._paged(
                            f"{self._base()}/projects/{pid}/states/", {"per_page": 100})}
                    st = states.get(st) or {}
                st = st or {}
                group = state_group(st.get("name"), st.get("group"))
                upd = _ts(w.get("updated_at")) or _ts(w.get("created_at"))
                if group in ("done", "cancelled") and upd and upd < cutoff:
                    continue
                key = f"{ident}-{w.get('sequence_id')}"
                out.append({"key": key, "title": w.get("name") or "", "state": st.get("name") or "", "group": group,
                            "people": [self._person(a) for a in (w.get("assignees") or w.get("assignee_ids") or [])],
                            "url": f"{self.web}/{self.ws}/browse/{key}/", "updated": upd,
                            "due": w.get("target_date") or "", "project": p.get("name") or ident})
                if len(out) >= max_items:
                    return out
        return out

    def ping(self):
        n = len(self._paged(f"{self._base()}/projects/", {"per_page": 100}, max_pages=1))
        return f"{n} project"


# ------------------------------------------------------------------ facade
class Tracker:
    def __init__(self, cfg, store, session=None):
        self.cfg = cfg.get("tracker") or {}
        self.store = store
        self.session = session or requests.Session()
        self._cache = {}

    def provider(self):
        v = (self.store.kv_get("tracker_provider") or self.cfg.get("provider") or "jira").lower()
        return v if v in PROVIDERS else "jira"

    def set_provider(self, v):
        v = str(v or "").lower()
        if v not in PROVIDERS:
            raise ValueError("tracker harus jira | plane | none")
        self.store.kv_set("tracker_provider", v)
        self._cache.clear()

    def _impl(self, name=None):
        name = name or self.provider()
        if name == "jira":
            return Jira(self.cfg.get("jira"), self.session)
        if name == "plane":
            return Plane(self.cfg.get("plane"), self.session)
        return None

    def label(self):
        return PROVIDERS[self.provider()]

    def status(self):
        out = []
        for k in ("jira", "plane"):
            impl = self._impl(k)
            out.append({"id": k, "label": PROVIDERS[k], "configured": impl.configured(), "missing": impl.missing()})
        out.append({"id": "none", "label": PROVIDERS["none"], "configured": True, "missing": []})
        cur = self.provider()
        return {"provider": cur, "label": PROVIDERS[cur], "providers": out,
                "enabled": cur != "none" and next(p["configured"] for p in out if p["id"] == cur)}

    def test(self):
        cur = self.provider()
        impl = self._impl(cur)
        if impl is None:
            return {"ok": False, "provider": cur, "message": "Tracker dimatikan (provider: none)."}
        if not impl.configured():
            return {"ok": False, "provider": cur,
                    "message": f"{impl.label} belum lengkap: isi {', '.join(impl.missing())} di config / .env."}
        try:
            who = impl.ping()
        except TrackerError as ex:
            return {"ok": False, "provider": cur, "message": str(ex)}
        except Exception as ex:
            return {"ok": False, "provider": cur, "message": f"Gagal: {short_error(ex)}"}
        return {"ok": True, "provider": cur, "message": f"Tersambung ke {impl.label} ({who})."}

    def items(self, force=False):
        cur = self.provider()
        impl = self._impl(cur)
        if impl is None or not impl.configured():
            return []
        ttl = float(self.cfg.get("cache_minutes") or 5) * 60
        hit = self._cache.get(cur)
        if hit and not force and time.time() - hit[0] < ttl:
            return [dict(i) for i in hit[1]]
        items = impl.fetch(int(self.cfg.get("lookback_days") or 7))
        self._cache[cur] = (time.time(), items)
        return [dict(i) for i in items]

    def team_items(self, members, force=False):
        """[(item, [members])] for items assigned to someone on the team."""
        out = []
        for it in self.items(force):
            who = match_members(it, members)
            if who:
                out.append((it, who))
        return out

    def snapshot(self, item):
        """Small record kept per ticket so the Daily page can compare it with what the PIC reported."""
        return json.dumps({"provider": self.provider(), "state": item.get("state"), "group": item.get("group"),
                           "people": [p.get("name") or p.get("username") for p in item.get("people") or []],
                           "url": item.get("url"), "updated": item.get("updated"), "at": time.time()},
                          ensure_ascii=False)
