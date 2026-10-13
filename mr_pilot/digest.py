"""Morning digest (what needs you today) and reminders for MRs that have been waiting too long."""
import logging
import time

from .formatting import VERDICT_LABEL, e, review_buttons

log = logging.getLogger("mr_pilot.digest")

DAYS = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
VERDICT_ICON = {"APPROVE": "✅", "NEEDS_ATTENTION": "⚠️", "REQUEST_CHANGES": "❌"}
PIPE_ICON = {"success": "🟢", "failed": "🔴", "running": "🔵", "pending": "⚪", "canceled": "⚫"}


def _hhmm(s, default):
    try:
        h, m = str(s or default).split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        h, m = default.split(":")
        return int(h) * 60 + int(m)


def age_text(seconds):
    h = int(seconds // 3600)
    if h >= 48:
        return f"{h // 24} hari"
    if h >= 1:
        return f"{h} jam"
    return f"{max(1, int(seconds // 60))} mnt"


class Digest:
    def __init__(self, cfg, store, send):
        self.d = cfg.get("digest") or {}
        self.r = cfg.get("reminders") or {}
        self.sb_mode = (cfg.get("merge") or {}).get("source_branch", "ask")
        self.store, self.send = store, send

    # ------------------------------------------------------------ schedule
    def _workday(self, lt, weekdays_only):
        return not weekdays_only or lt.tm_wday < 5

    def tick(self, now=None):
        now = now or time.time()
        lt = time.localtime(now)
        sent = None
        if self.d.get("enabled", True) and self._workday(lt, self.d.get("weekdays_only", True)):
            today = time.strftime("%Y-%m-%d", lt)
            if lt.tm_hour * 60 + lt.tm_min >= _hhmm(self.d.get("time"), "08:30") \
                    and self.store.kv_get("digest_last") != today:
                self.store.kv_set("digest_last", today)
                sent = self.send_digest(now)
        self.remind(now)
        return sent

    # -------------------------------------------------------------- digest
    def build(self, now=None):
        now = now or time.time()
        lt = time.localtime(now)
        waiting = self.store.by_status("notified", "notify_failed")
        waiting.sort(key=lambda r: r.get("notified_at") or r.get("first_seen") or now)
        bot = self.store.by_status("waiting_bot")
        errors = self.store.by_status("error")
        day_ago = now - 86400
        recent = [r for r in self.store.by_status("merged", "rejected") if (r.get("decided_at") or 0) >= day_ago]
        head = f"☀️ <b>Ringkasan · {DAYS[lt.tm_wday]} {lt.tm_mday} {MONTHS[lt.tm_mon - 1]}</b>"
        lines = [head, ""]
        if not waiting and not bot and not errors:
            lines.append("Tidak ada MR yang menunggu Anda. ✨")
        if waiting:
            lines.append(f"<b>Menunggu keputusan Anda: {len(waiting)}</b>")
            for r in waiting[:15]:
                age = age_text(now - (r.get("notified_at") or r.get("first_seen") or now))
                v = (r.get("review") or {}).get("verdict") or r.get("verdict")
                lines.append(f"{VERDICT_ICON.get(v, '•')} <a href=\"{e(r.get('web_url'), True)}\">!{r['iid']}</a> "
                             f"{e((r.get('title') or '')[:70])} · {e(r.get('author') or '-')} · {age}"
                             f"{' ' + PIPE_ICON[r['pipeline']] if r.get('pipeline') in ('failed',) else ''}")
            if len(waiting) > 15:
                lines.append(f"…dan {len(waiting) - 15} lainnya (/status)")
        red = [r for r in waiting + bot if r.get("pipeline") == "failed"]
        if red:
            lines += ["", f"🔴 <b>Pipeline merah: {len(red)}</b> " +
                      ", ".join(f"!{r['iid']}" for r in red[:10])]
        if bot:
            lines += ["", f"🕒 Menunggu komentar bot review: {len(bot)}"]
        if errors:
            lines += ["", "⚠️ Gagal diproses: " + ", ".join(f"!{r['iid']}" for r in errors[:10])]
        if recent:
            m = sum(1 for r in recent if r["status"] == "merged")
            lines += ["", f"24 jam terakhir: {m} merged, {len(recent) - m} ditolak."]
        return "\n".join(lines)

    def send_digest(self, now=None):
        text = self.build(now)
        self.send(text)
        return text

    # ----------------------------------------------------------- reminders
    def _active_hours(self, lt):
        start = _hhmm(self.r.get("active_from"), "08:00")
        end = _hhmm(self.r.get("active_until"), "20:00")
        cur = lt.tm_hour * 60 + lt.tm_min
        return start <= cur < end

    def remind(self, now=None):
        if not self.r.get("enabled", True):
            return []
        now = now or time.time()
        lt = time.localtime(now)
        if not self._workday(lt, self.r.get("weekdays_only", True)) or not self._active_hours(lt):
            return []
        after = float(self.r.get("after_hours", 24)) * 3600
        every = float(self.r.get("repeat_hours", 24)) * 3600
        sent = []
        for r in self.store.by_status("notified"):
            since = r.get("notified_at") or r.get("first_seen") or now
            if now - since < after:
                continue
            key = f"{r['project_id']}:{r['iid']}"
            last = float(self.store.kv_get(f"remind:{key}:{r.get('sha')}", 0) or 0)
            if now - last < every:
                continue
            self.store.kv_set(f"remind:{key}:{r.get('sha')}", now)
            v = (r.get("review") or {}).get("verdict") or r.get("verdict")
            self.send(f"⏰ <a href=\"{e(r.get('web_url'), True)}\">!{r['iid']}</a> menunggu keputusan Anda "
                      f"<b>{age_text(now - since)}</b>\n{e(r.get('title'))}\n👤 {e(r.get('author') or '-')}"
                      f" · {e(VERDICT_LABEL.get(v, v or '-'))}",
                      buttons=review_buttons(r["project_id"], r["iid"], r.get("web_url") or "", self.sb_mode))
            sent.append(r["iid"])
        return sent
