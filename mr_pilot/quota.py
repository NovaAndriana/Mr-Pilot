"""AI usage limits. Free tiers cap requests/tokens per day (and per minute): count what MR Pilot used
in the last 24 h, respect what the provider itself reports (rate-limit headers, 429 + retry-after),
skip a provider that is out of quota, and switch to economy mode (skip optional AI calls) at 80%."""
import re
import time

WINDOW = 86400
LOW = 0.8

# Known free-tier limits (Oct 2026). Only used when the user didn't set a limit for the provider.
FREE_DEFAULTS = {
    "groq": {"daily_requests": 1000, "daily_tokens": 200000, "rpm": 30},
}
OPENROUTER_FREE = {"daily_requests": 50, "rpm": 20}


def limits_for(p):
    """{"daily_requests": n|None, "daily_tokens": n|None, "rpm": n|None, "source": "custom"|"free"|None}.
    A user value of 0 means "no limit"."""
    out = {"daily_requests": None, "daily_tokens": None, "rpm": None, "source": None}
    base = dict(FREE_DEFAULTS.get(p.get("type"), {}))
    if p.get("type") == "openrouter" and str(p.get("model") or "").endswith(":free"):
        base = dict(OPENROUTER_FREE)
    if base:
        out.update(base)
        out["source"] = "free"
    custom = False
    for k in ("daily_requests", "daily_tokens", "rpm"):
        v = p.get(k)
        if v in (None, ""):
            continue
        try:
            v = int(v)
        except (TypeError, ValueError):
            continue
        out[k] = v if v > 0 else None
        custom = True
    if custom:
        out["source"] = "custom"
    return out


_DUR = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+)ms)?$")


def parse_reset(v):
    """'2m59.56s' / '6ms' / '17' (seconds) / ISO -> seconds, or None."""
    if v in (None, ""):
        return None
    s = str(v).strip()
    try:
        return max(0.0, float(s))
    except ValueError:
        pass
    m = _DUR.match(s)
    if m and any(m.groups()):
        h, mi, sec, ms = (float(x) if x else 0.0 for x in m.groups())
        return h * 3600 + mi * 60 + sec + ms / 1000
    return None


def headers_meta(headers):
    """Remaining quota as reported by the provider (OpenAI-compatible / Groq / Anthropic headers)."""
    h = {k.lower(): v for k, v in (headers or {}).items()}

    def num(*keys):
        for k in keys:
            if h.get(k) not in (None, ""):
                try:
                    return int(float(h[k]))
                except ValueError:
                    pass
        return None
    return {"remaining_requests": num("x-ratelimit-remaining-requests", "anthropic-ratelimit-requests-remaining",
                                      "x-ratelimit-remaining"),
            "remaining_tokens": num("x-ratelimit-remaining-tokens", "anthropic-ratelimit-tokens-remaining"),
            "reset_requests": parse_reset(h.get("x-ratelimit-reset-requests")),
            "retry_after": parse_reset(h.get("retry-after"))}


_DAILY = re.compile(r"per.?day|daily|RPD|TPD|quota|kuota|exceeded your current|resource.?exhausted", re.I)


class QuotaGuard:
    def __init__(self, store, notify=None):
        self.store = store
        self.notify = notify  # callable(text) -> Telegram

    # --------------------------------------------------------------- usage
    def usage(self, name, now=None):
        now = now or time.time()
        if self.store is None:
            return {"requests": 0, "tokens": 0, "last_minute": 0}
        r = self.store.query("SELECT COUNT(*) n, COALESCE(SUM(tokens_in + tokens_out), 0) t FROM ai_calls "
                             "WHERE provider=? AND task!='test' AND ts>=?", (name, now - WINDOW))[0]
        m = self.store.query("SELECT COUNT(*) n FROM ai_calls WHERE provider=? AND ts>=?", (name, now - 60))[0]
        return {"requests": r["n"], "tokens": r["t"], "last_minute": m["n"]}

    def cooldown(self, name, now=None):
        if self.store is None:
            return None
        v = self.store.kv_get(f"ai_cooldown:{name}")
        if not v:
            return None
        until, _, why = str(v).partition("|")
        try:
            until = float(until)
        except ValueError:
            return None
        if until <= (now or time.time()):
            self.store.kv_del(f"ai_cooldown:{name}")
            return None
        return {"until": until, "reason": why}

    def state(self, p, now=None):
        """{"level": ok|low|exhausted, "ratio": 0..1|None, "usage": {...}, "limits": {...}, "why": str}"""
        now = now or time.time()
        name = p["name"]
        lim = limits_for(p)
        use = self.usage(name, now)
        cd = self.cooldown(name, now)
        ratios = []
        if lim["daily_requests"]:
            ratios.append(use["requests"] / lim["daily_requests"])
        if lim["daily_tokens"]:
            ratios.append(use["tokens"] / lim["daily_tokens"])
        hdr = self._headers(name)
        if hdr.get("remaining_requests") is not None and hdr.get("at", 0) > now - 3600:
            if hdr["remaining_requests"] <= 0:
                ratios.append(1.0)
        ratio = max(ratios) if ratios else None
        level, why = "ok", ""
        if cd:
            level, why = "exhausted", cd["reason"]
        elif ratio is not None and ratio >= 1:
            level, why = "exhausted", "batas harian tercapai"
        elif ratio is not None and ratio >= LOW:
            level = "low"
        return {"level": level, "ratio": ratio, "usage": use, "limits": lim, "why": why,
                "cooldown_until": cd["until"] if cd else None, "reported": hdr}

    def minute_wait(self, p, now=None):
        """Seconds to wait before the per-minute limit allows another call (0 = go)."""
        lim = limits_for(p)
        if not lim["rpm"] or self.store is None:
            return 0
        now = now or time.time()
        rows = self.store.query("SELECT ts FROM ai_calls WHERE provider=? AND ts>=? ORDER BY ts",
                                (p["name"], now - 60))
        if len(rows) < lim["rpm"]:
            return 0
        return max(0.0, rows[0]["ts"] + 60 - now)

    # ------------------------------------------------------------- signals
    def _headers(self, name):
        import json
        try:
            return json.loads(self.store.kv_get(f"ai_hdr:{name}") or "{}") if self.store is not None else {}
        except ValueError:
            return {}

    def record_headers(self, name, meta):
        if self.store is None or not meta:
            return
        import json
        keep = {k: v for k, v in meta.items() if k in ("remaining_requests", "remaining_tokens") and v is not None}
        if keep:
            keep["at"] = time.time()
            self.store.kv_set(f"ai_hdr:{name}", json.dumps(keep))

    def record_failure(self, name, status, message="", retry_after=None, now=None):
        """429 (or 403/402 quota): put the provider on cooldown so the next calls skip it."""
        if self.store is None or status not in (402, 403, 429):
            return None
        now = now or time.time()
        daily = bool(_DAILY.search(message or "")) or status == 402
        if retry_after:
            secs = float(retry_after)
        elif daily:
            secs = 6 * 3600
        else:
            secs = 60
        secs = max(10.0, min(secs, 24 * 3600))
        why = "kuota/limit provider habis" if daily or status == 402 else "terlalu banyak request (429)"
        self.store.kv_set(f"ai_cooldown:{name}", f"{now + secs}|{why}")
        if secs >= 600:
            self.alert(name, "exhausted", f"⛔ AI <b>{name}</b>: {why}. Dilewati sampai "
                                          f"{time.strftime('%H:%M', time.localtime(now + secs))}; "
                                          f"MR Pilot memakai provider berikutnya.")
        return now + secs

    def alert(self, name, level, text, every=WINDOW):
        """Telegram notice at most once per `every` seconds per provider+level."""
        if not self.notify or self.store is None:
            return False
        key = f"ai_alert:{name}:{level}"
        last = float(self.store.kv_get(key, 0) or 0)
        if time.time() - last < every:
            return False
        self.store.kv_set(key, time.time())
        try:
            self.notify(text)
        except Exception:
            pass
        return True


def fmt_num(n):
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}jt".replace(".0jt", "jt")
    if n >= 1000:
        return f"{n / 1000:.0f}rb" if n >= 10000 else f"{n / 1000:.1f}rb".replace(".0rb", "rb")
    return str(n)
