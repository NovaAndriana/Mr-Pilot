"""Main loop: poll GitLab -> review -> Telegram buttons -> merge -> Teams."""
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime

from .ai import AIManager
from .formatting import VERDICT_LABEL, build_messages, e, review_buttons
from .gitlab_api import GitLab
from .quality import CodeQuality
from .reviewer import Reviewer, heuristic_flags
from .store import Store
from .teams import Teams
from .telegram_api import Telegram, TelegramError
from .util import redact, short_error

log = logging.getLogger("mr_pilot")

DONE = ("merged", "rejected", "closed")
# statuses that are retried on the next poll even when the head sha did not change
RETRY = ("waiting_bot", "error", "notify_failed", "unassigned", "closed")
PENDING = ("notified", "waiting_bot", "error", "notify_failed", "merging")


def mr_key(pid, iid):
    return f"{pid}:{iid}"


def now_str():
    return datetime.now().strftime("%d/%m %H:%M")


def mr_meta(mr):
    """MR fields kept for the dashboard."""
    author = mr.get("author") or {}
    hp = mr.get("head_pipeline") or mr.get("pipeline") or {}
    return dict(project=((mr.get("references") or {}).get("full") or "").split("!")[0],
                author=author.get("name"), author_username=author.get("username"),
                source_branch=mr.get("source_branch"), target_branch=mr.get("target_branch"),
                pipeline=hp.get("status") or "none", mr_created_at=mr.get("created_at"))


class Stop(BaseException):
    """Raised (outside critical sections) when SIGTERM/SIGINT asks the app to stop.
    BaseException (like KeyboardInterrupt) so generic `except Exception` handlers can't swallow it."""


class App:
    def __init__(self, cfg, dry_run=False, gl=None, tg=None, store=None):
        self.cfg = cfg
        self.dry = dry_run
        g, t = cfg["gitlab"], cfg["telegram"]
        self.gl = gl or GitLab(g["url"], g["token"], verify=g["verify_ssl"])
        self.tg = tg or Telegram(t["bot_token"], t["chat_id"], proxy=t.get("proxy"), api_base=t.get("api_base"))
        # dry-run uses a throwaway DB so a test run doesn't mark MRs as "already notified"
        self.store = store or Store(":memory:" if dry_run else cfg["storage"]["db_path"])
        self.allowed = set(t["allowed_user_ids"])
        self.ai = AIManager(cfg, self.store)
        self.reviewer = Reviewer(cfg, self.gl, self.ai)
        self.teams = Teams(cfg)
        self.quality = CodeQuality(cfg, self.gl, self.store, cfg.get("_base_dir", "."), self.ai)
        self.username = g.get("username") or None
        self.last_poll = 0.0
        self.last_prune = 0.0
        self.stop_requested = False
        self._critical = 0
        self._tg_backoff_until = 0.0
        self._tg_warned = 0.0
        self._tg_fail = 0
        self._tg_fail_logged = 0.0
        self.heartbeat_path = os.path.join(cfg.get("_base_dir", "."), ".heartbeat")

    # ------------------------------------------------------------- lifecycle
    @contextmanager
    def critical(self):
        """Work that must not be cut in half by SIGTERM (merge, posting a decision)."""
        self._critical += 1
        try:
            yield
        finally:
            self._critical -= 1

    def request_stop(self, *_):
        self.stop_requested = True
        if not self._critical:
            raise Stop()

    def watch(self):
        w = self.cfg["gitlab"].get("watch") or ["reviewer", "assignee"]
        if self.cfg["gitlab"].get("also_assigned_to_me") and "assignee" not in w:
            w = list(w) + ["assignee"]
        return w

    def my_roles(self, mr):
        """Which watched roles I have on this MR: ['Reviewer', 'Assignee']."""
        me, w, roles = self.username, self.watch(), []
        if "reviewer" in w and any(r.get("username") == me for r in (mr.get("reviewers") or [])):
            roles.append("Reviewer")
        if "assignee" in w and (any(a.get("username") == me for a in (mr.get("assignees") or []))
                                or (mr.get("assignee") or {}).get("username") == me):
            roles.append("Assignee")
        return roles

    def ensure_user(self):
        if not self.username:
            self.username = self.gl.me()["username"]
        if self.store.kv_get("gitlab_user") != self.username:
            self.store.kv_set("gitlab_user", self.username)
        return self.username

    def event(self, type_, title, detail="", key=None, level="info"):
        try:
            self.store.add_event(type_, redact(title), redact(detail), key, level)
        except Exception:
            log.exception("gagal simpan event")

    def heartbeat(self):
        now = time.time()
        if now - getattr(self, "_hb_last", 0) < 10:  # every loop pass; one fsync'd write per 10s is plenty
            return
        self._hb_last = now
        try:
            with open(self.heartbeat_path, "w") as f:
                f.write(str(int(time.time())))
        except OSError:
            pass
        self.store.kv_set("heartbeat", time.time())

    def recover(self):
        """After a crash/kill: MRs left in 'merging' are re-checked against GitLab."""
        for rec in self.store.by_status("merging"):
            key = mr_key(rec["project_id"], rec["iid"])
            try:
                mr = self.gl.get_mr(rec["project_id"], rec["iid"])
            except Exception as ex:
                log.warning("recover %s: %s", key, short_error(ex))
                continue
            state = mr.get("state")
            if state == "merged":
                self.store.upsert(key, status="merged", decided_at=time.time())
                self.event("merged", f"!{rec['iid']} ternyata sudah merged (dipulihkan setelah restart)", "", key,
                           "success")
                self._finish_card(rec, f"<b>✅ Merged</b> · {now_str()} · <i>dipulihkan setelah restart</i>")
            elif state == "closed":
                self.store.upsert(key, status="closed", decided_at=time.time())
            else:
                self.store.upsert(key, status="notified")
                self.safe_send(f"⚠️ Merge !{rec['iid']} terputus (MR Pilot sempat berhenti). "
                               f"MR masih terbuka, silakan tap Merge lagi.")

    def run_forever(self):
        self.ensure_user()
        self.recover()
        self.event("system", "MR Pilot aktif", f"Memantau MR untuk @{self.username}")
        interval = int(self.cfg["gitlab"]["poll_interval_seconds"])
        log.info("MR Pilot jalan untuk @%s (cek tiap %ss)", self.username, interval)
        if self.cfg["telegram"].get("startup_message"):
            self.safe_send(f"🟢 MR Pilot aktif, memantau MR untuk <b>@{e(self.username)}</b>.")
        while not self.stop_requested:
            self.heartbeat()
            if time.time() - self.last_poll >= interval:
                self.last_poll = time.time()
                try:
                    self.poll_gitlab()
                    self.store.kv_set("last_poll_ok", time.time())
                    self.store.kv_del("last_poll_error")
                except Stop:
                    raise
                except Exception as ex:
                    msg = short_error(ex)
                    log.warning("Gagal cek GitLab: %s", msg)
                    log.debug("detail", exc_info=True)
                    self.store.kv_set("last_poll_error", f"{time.strftime('%H:%M')} {msg}")
                    self.event("error", "Gagal cek GitLab", msg, level="error")
            if time.time() - self.last_prune > 86400:
                self.last_prune = time.time()
                try:
                    n = self.store.prune()
                    if n:
                        log.info("Pembersihan data lama: %s baris", n)
                except Exception:
                    log.exception("prune gagal")
            self.poll_telegram(interval)

    def poll_telegram(self, interval):
        if time.time() < self._tg_backoff_until:
            time.sleep(max(0.0, min(5, self._tg_backoff_until - time.time())))
            return
        try:
            self.handle_updates(timeout=max(1, min(25, interval)))
        except Stop:
            raise
        except TelegramError as ex:
            if ex.is_conflict:
                # same bot token polled elsewhere (e.g. PC + server): explain once, back off
                if time.time() - self._tg_warned > 600:
                    self._tg_warned = time.time()
                    log.error("Bot Telegram yang sama sedang dipakai MR Pilot lain (409). Matikan salah satu: "
                              "PC atau server. Tombol Telegram tidak akan berfungsi di instance ini.")
                    self.event("error", "Bot Telegram dipakai instance lain",
                               "Matikan MR Pilot di PC atau di server; hanya satu yang boleh jalan.", level="error")
                self._tg_backoff_until = time.time() + 30
            else:
                self._tg_failed(str(ex))
        except Exception as ex:
            self._tg_failed(short_error(ex))
        else:
            if self._tg_fail:
                log.info("Telegram tersambung lagi setelah %s kali gagal.", self._tg_fail)
                self._tg_fail = 0

    def _tg_failed(self, msg):
        """Network/API trouble: exponential backoff (5s..60s), log the first failure and then every 10 min."""
        self._tg_fail += 1
        if self._tg_fail == 1 or time.time() - self._tg_fail_logged > 600:
            self._tg_fail_logged = time.time()
            log.warning("Telegram: %s (dicoba ulang otomatis)", msg)
        self._tg_backoff_until = time.time() + min(5 * 2 ** (self._tg_fail - 1), 60)

    # ------------------------------------------------------------- Telegram io
    def safe_send(self, text, **kw):
        if self.dry:
            print("[TELEGRAM]", text)
            return 0
        try:
            return self.tg.send(text, **kw)
        except Exception as ex:
            log.warning("Gagal kirim Telegram: %s", short_error(ex))
            return 0

    def safe_edit(self, msg_id, text, buttons=None):
        if self.dry:
            print("[TELEGRAM EDIT]", text)
            return
        if not msg_id:  # original card never arrived: send the new state instead
            self.safe_send(text, buttons=buttons) if buttons else self.safe_send(text)
            return
        try:
            self.tg.edit(msg_id, text, buttons)
        except Exception as ex:
            log.warning("Gagal edit pesan Telegram (%s), kirim pesan baru", short_error(ex))
            self.safe_send(text)

    def _finish_card(self, rec, line):
        """Append the final state to the MR card (buttons removed) and remember the new text."""
        text = (rec.get("tg_text") or f"<b>!{rec['iid']}</b> {e(rec.get('title'))}") + "\n\n" + line
        self.safe_edit(rec.get("tg_msg_id"), text)
        self.store.upsert(mr_key(rec["project_id"], rec["iid"]), tg_text=text)

    # --------------------------------------------------------------- GitLab
    def poll_gitlab(self):
        g = self.cfg["gitlab"]
        me = self.ensure_user()
        mrs = self.gl.list_review_mrs(me, watch=self.watch())
        projects = set(g.get("projects") or [])
        seen = set()
        for m in mrs:
            if self.stop_requested:
                break
            if g.get("skip_draft") and (m.get("draft") or m.get("work_in_progress")):
                continue
            if (m.get("author") or {}).get("username") == me:
                continue
            proj = ((m.get("references") or {}).get("full") or "").split("!")[0]
            if projects and proj not in projects:
                continue
            key = mr_key(m["project_id"], m["iid"])
            seen.add(key)
            rec = self.store.get(key)
            if rec and rec["sha"] == m["sha"]:
                if rec["status"] == "merging":
                    continue
                if rec["status"] not in RETRY:
                    continue
                if rec["status"] in ("error", "notify_failed") and (rec.get("attempts") or 0) >= 5:
                    continue
            try:
                self.process(m["project_id"], m["iid"], rec)
            except Stop:
                raise
            except Exception as ex:
                msg = short_error(ex)
                log.warning("Gagal proses MR %s: %s", key, msg)
                log.debug("detail", exc_info=True)
                attempts = ((rec.get("attempts") or 0) + 1) if rec and rec["sha"] == m["sha"] else 1
                self.store.upsert(key, project_id=m["project_id"], iid=m["iid"], sha=m["sha"],
                                  title=m.get("title"), web_url=m.get("web_url"), status="error",
                                  attempts=attempts)
                if attempts in (1, 5):
                    self.safe_send(f"⚠️ Gagal memproses {e(m.get('web_url'))}: {e(msg)}"
                                   + ("\n<i>Berhenti mencoba sampai ada commit baru.</i>" if attempts == 5 else ""))
                self.event("error", f"Gagal memproses !{m['iid']}", msg, key, "error")
        self.sync_closed(seen)

    def process(self, pid, iid, rec=None, force_llm=False, header=None):
        key = mr_key(pid, iid)
        mr = self.gl.get_mr(pid, iid)
        if mr.get("state") != "opened":
            self.store.upsert(key, status="merged" if mr.get("state") == "merged" else "closed",
                              decided_at=time.time())
            return
        same_sha = bool(rec and rec.get("sha") == mr["sha"])
        first_seen = rec["first_seen"] if same_sha and rec.get("first_seen") else time.time()
        review = self.reviewer.review(mr, first_seen, force_llm=force_llm)
        base = dict(project_id=pid, iid=iid, sha=mr["sha"], title=mr.get("title"),
                    web_url=mr.get("web_url"), first_seen=first_seen, **mr_meta(mr))
        if not rec:
            self.event("mr_new", f"MR baru !{iid}: {mr.get('title')}",
                       f"oleh {base['author']} → {base['target_branch']}", key)
        elif not same_sha:
            self.event("mr_updated", f"Commit baru di !{iid}", mr.get("title") or "", key)
        if review is None:
            if not rec or rec.get("status") != "waiting_bot" or not same_sha:
                self.event("waiting", f"!{iid} menunggu komentar bot review", "", key)
                # tell the lead right away; the full card follows when the bot review is in
                roles = self.my_roles(mr)
                self.safe_send(f"🕒 <b>MR {'diperbarui' if rec and not same_sha else 'baru'}</b> "
                               f"<a href=\"{e(mr.get('web_url'), True)}\">!{iid}</a> {e(mr.get('title'))}\n"
                               f"👤 {e(base['author'])}" + (f" · Anda: {e(', '.join(roles))}" if roles else "")
                               + f"\n<i>Menunggu komentar bot AI review (maks {int(self.cfg['review']['bot']['wait_minutes'])}"
                               f" menit). Kartu dengan tombol Merge/Tolak menyusul.</i>")
            self.store.upsert(key, status="waiting_bot", **base)
            log.info("MR %s menunggu komentar bot review", key)
            return
        quality = None
        if self.quality.enabled:
            try:
                quality = self.quality.run(mr, dry=self.dry)
                c = quality["counts"]
                lvl = "error" if c["error"] else ("warning" if c["warning"] else "success")
                self.event("quality", f"Standar kode !{iid}: {c['error']} error, {c['warning']} warning, "
                           f"{c['info']} info", f"{quality['posted']} warning diposting ke GitLab", key, lvl)
            except Stop:
                raise
            except Exception as ex:
                log.warning("Code quality gagal untuk %s: %s", key, short_error(ex))
                self.event("error", f"Cek standar kode gagal !{iid}", short_error(ex), key, "error")
        if header is None:
            is_update = bool(rec and rec.get("tg_msg_id") and not same_sha)
            header = "🔄 MR diperbarui (ada commit baru)" if is_update else "🔔 MR baru untuk direview"
            roles = self.my_roles(mr)
            if roles:
                header += f" · Anda: {', '.join(roles)}"
        # retire the old Telegram card for this MR (buttons removed)
        if rec and rec.get("tg_msg_id") and rec.get("tg_text") and rec.get("status") not in ("notify_failed",):
            self.safe_edit(rec["tg_msg_id"], rec["tg_text"] + "\n\n<i>↪️ Diganti review terbaru di bawah.</i>")
        detail, text = build_messages(mr, review, heuristic_flags(mr, self.cfg), header, quality)
        if detail:
            self.safe_send(detail)
        msg_id = self.safe_send(text, buttons=review_buttons(pid, iid, mr["web_url"]))
        if not msg_id and not self.dry:
            # Telegram unreachable: keep the review, retry the card on the next poll
            attempts = ((rec.get("attempts") or 0) + 1) if same_sha and rec else 1
            self.store.upsert(key, status="notify_failed", review=review, tg_msg_id=0, tg_text=text,
                              attempts=attempts, verdict=review["verdict"], quality=quality, **base)
            self.event("error", f"Kartu !{iid} gagal dikirim ke Telegram", "dicoba lagi otomatis", key, "error")
            return
        self.store.upsert(key, status="notified", review=review, tg_msg_id=msg_id,
                          tg_text=text, attempts=0, verdict=review["verdict"], quality=quality, **base)
        self.event("review", f"Review !{iid} selesai: {VERDICT_LABEL.get(review['verdict'], review['verdict'])}",
                   review.get("summary", "")[:300], key,
                   {"APPROVE": "success", "REQUEST_CHANGES": "error"}.get(review["verdict"], "warning"))
        log.info("MR %s dinotifikasi (verdict %s)", key, review["verdict"])

    def sync_closed(self, open_keys):
        """MRs merged/closed outside the bot, or where I'm no longer reviewer: update card and status."""
        for rec in self.store.by_status("notified", "waiting_bot", "error", "notify_failed"):
            key = mr_key(rec["project_id"], rec["iid"])
            if key in open_keys:
                continue
            try:
                mr = self.gl.get_mr(rec["project_id"], rec["iid"])
            except Exception:
                continue
            state = mr.get("state")
            if state == "opened":
                still_mine = bool(self.my_roles(mr))
                if not still_mine and ("reviewers" in mr or "assignees" in mr):
                    self.store.upsert(key, status="unassigned")
                    self.event("closed", f"!{rec['iid']} tidak lagi di-assign ke Anda", rec.get("title") or "", key)
                    self._finish_card(rec, "<i>ℹ️ Anda tidak lagi reviewer MR ini.</i>")
                continue  # draft / filtered: keep as is
            self.store.upsert(key, status="merged" if state == "merged" else "closed", decided_at=time.time())
            self.event("closed", f"!{rec['iid']} {state} di luar MR Pilot", rec.get("title") or "", key)
            if rec.get("tg_text"):
                self._finish_card(rec, f"<i>ℹ️ MR sudah {e(state)} di luar MR Pilot ({now_str()}).</i>")

    # -------------------------------------------------------------- Telegram
    def handle_updates(self, timeout=25):
        offset = int(self.store.kv_get("tg_offset", 0) or 0)
        for u in self.tg.get_updates(offset, timeout):
            # at-most-once: never re-run a Merge tap after a crash
            self.store.kv_set("tg_offset", u["update_id"] + 1)
            try:
                if "callback_query" in u:
                    self.on_callback(u["callback_query"])
                elif "message" in u:
                    self.on_message(u["message"])
            except Stop:
                raise
            except Exception as ex:
                log.exception("Gagal proses update Telegram")
                self.safe_send(f"⚠️ Error: {e(short_error(ex))}")

    def on_callback(self, cb):
        cid = cb.get("id")
        if (cb.get("from") or {}).get("id") not in self.allowed:
            self.tg.answer(cid, "Tidak diizinkan")
            log.warning("Tombol ditekan oleh user tidak dikenal: %s", (cb.get("from") or {}).get("id"))
            return
        try:
            action, pid, iid = (cb.get("data") or "").split("|")
            pid, iid = int(pid), int(iid)
        except ValueError:
            self.tg.answer(cid, "Tombol tidak dikenal")
            return
        msg_id = (cb.get("message") or {}).get("message_id")
        rec = self.store.get(mr_key(pid, iid))
        if not rec:
            self.tg.answer(cid, "Data MR tidak ditemukan")
            return
        if action in ("m", "mf", "x") and rec["status"] in DONE + ("merging",):
            label = {"merged": "sudah di-merge", "rejected": "sudah ditolak", "closed": "sudah ditutup",
                     "merging": "sedang diproses"}[rec["status"]]
            self.tg.answer(cid, f"MR ini {label}")
            return
        if action == "m":
            self.tg.answer(cid, "Memproses merge…")
            self.do_merge(rec, confirmed=False)
        elif action == "mf":
            self.tg.answer(cid, "Merge…")
            self.do_merge(rec, confirmed=True, confirm_msg_id=msg_id)
        elif action == "c":
            self.tg.answer(cid, "Dibatalkan")
            self.safe_edit(msg_id, "Merge dibatalkan.")
        elif action == "x":
            self.tg.answer(cid)
            self.ask_reject_reason(rec)
        elif action == "rr":
            self.tg.answer(cid, "Review ulang…")
            self.process(pid, iid, rec, force_llm=self.reviewer.llm_available(), header="🔁 Review ulang")
        else:
            self.tg.answer(cid, "Tombol tidak dikenal")

    def on_message(self, msg):
        if (msg.get("from") or {}).get("id") not in self.allowed:
            return
        text = (msg.get("text") or "").strip()
        reply = msg.get("reply_to_message")
        if reply:
            key = self.store.kv_get(f"reject:{reply.get('message_id')}")
            if key:
                self.finish_reject(self.store.get(key), text, reply.get("message_id"))
                return
        cmd = text.split()[0].split("@")[0].lower() if text else ""
        if cmd in ("/start", "/help"):
            self.safe_send("<b>MR Pilot</b>\n/status: MR yang menunggu keputusan\n"
                           "/cek: cek GitLab sekarang\n\nKartu MR punya tombol Merge, Tolak, "
                           "Review ulang, dan Buka MR.")
        elif cmd == "/status":
            pend = self.store.by_status("notified", "waiting_bot", "error", "notify_failed")
            if not pend:
                self.safe_send("Tidak ada MR yang menunggu. 👍")
            else:
                lines = ["<b>Menunggu keputusan:</b>"]
                for r in pend[:40]:
                    v = (r.get("review") or {}).get("verdict", "")
                    lines.append(f"• <a href=\"{e(r['web_url'], True)}\">!{r['iid']}</a> {e(r['title'])} "
                                 f"[{e(r['status'])}{' · ' + VERDICT_LABEL.get(v, v) if v else ''}]")
                self.safe_send("\n".join(lines))
        elif cmd == "/cek":
            self.last_poll = time.time()
            self.poll_gitlab()
            self.safe_send("Selesai cek GitLab.")

    # ----------------------------------------------------------------- merge
    def pipeline_ok(self, mr):
        m = self.cfg["merge"]
        hp = mr.get("head_pipeline") or mr.get("pipeline")
        if not hp:
            return bool(m["allow_no_pipeline"]), "MR tidak punya pipeline."
        st = hp.get("status")
        if st == "success" or not m["require_pipeline_success"]:
            return True, ""
        if st in ("running", "pending", "created", "waiting_for_resource", "preparing", "scheduled"):
            return False, f"Pipeline masih <b>{e(st)}</b>. Tap Merge lagi setelah selesai."
        return False, f"Pipeline <b>{e(st)}</b>, merge ditahan."

    def do_merge(self, rec, confirmed=False, confirm_msg_id=None):
        pid, iid = rec["project_id"], rec["iid"]
        mcfg = self.cfg["merge"]
        verdict = (rec.get("review") or {}).get("verdict", "UNKNOWN")
        reasons = []
        if mcfg["confirm_if_not_approved"] and verdict != "APPROVE":
            reasons.append(f"Verdict review: {VERDICT_LABEL.get(verdict, verdict)}")
        q_err = ((rec.get("quality") or {}).get("counts") or {}).get("error", 0)
        if mcfg.get("confirm_if_quality_errors") and q_err:
            reasons.append(f"Standar kode: ❌ {q_err} error")

        mr = self.gl.get_mr(pid, iid)
        key = mr_key(pid, iid)
        if mr.get("state") != "opened":
            self.store.upsert(key, status="merged" if mr.get("state") == "merged" else "closed",
                              decided_at=time.time())
            self._finish_card(rec, f"<i>ℹ️ MR sudah {e(mr.get('state'))}.</i>")
            return
        if mr["sha"] != rec["sha"]:
            self.safe_send(f"🔄 Ada commit baru di !{iid} sejak direview. Saya review ulang dulu.")
            self.process(pid, iid, rec)
            return
        if mr.get("has_conflicts"):
            self.safe_send(f"⛔ !{iid} punya conflict, tidak bisa di-merge.")
            return
        if mr.get("draft") or mr.get("work_in_progress"):
            self.safe_send(f"⛔ !{iid} masih Draft, tidak bisa di-merge.")
            return
        ok, reason = self.pipeline_ok(mr)
        if not ok:
            self.safe_send(f"⏸️ !{iid}: {reason}")
            return
        # blocking checks first, then ask: no point confirming a merge that cannot happen yet
        if reasons and not confirmed:
            self.safe_send(f"<b>!{iid}</b> " + "\n".join(e(r) for r in reasons) + "\nYakin tetap merge?",
                           buttons=[[("⚠️ Ya, tetap merge", f"mf|{pid}|{iid}"),
                                     ("Batal", f"c|{pid}|{iid}")]])
            return
        if confirm_msg_id:
            self.safe_edit(confirm_msg_id, f"Merge !{iid} diproses…")
        if self.dry:
            print(f"[DRY RUN] akan merge {key}")
            return

        with self.critical():
            if mcfg["approve_before_merge"]:
                try:
                    r = self.gl.approve(pid, iid, mr["sha"])
                    if r.status_code not in (200, 201, 401):
                        log.info("approve !%s -> HTTP %s", iid, r.status_code)
                except Exception as ex:
                    log.warning("approve gagal (diabaikan): %s", short_error(ex))

            self.store.upsert(key, status="merging")
            try:
                r = self.gl.merge(pid, iid, sha=mr["sha"], remove_source_branch=mcfg["remove_source_branch"],
                                  squash=mcfg["squash"])
            except Exception as ex:
                # the request may or may not have reached GitLab: check the real state instead of guessing
                log.warning("merge !%s: %s", iid, short_error(ex))
                try:
                    state = self.gl.get_mr(pid, iid).get("state")
                except Exception:
                    state = None
                if state != "merged":
                    self.store.upsert(key, status="notified")
                    self.safe_send(f"⛔ Merge !{iid} gagal: {e(short_error(ex))}. Silakan coba lagi.")
                    self.event("error", f"Merge !{iid} gagal", short_error(ex), key, "error")
                    return
                body = self.gl.get_mr(pid, iid)
            else:
                try:
                    body = r.json()
                except ValueError:
                    body = {}
                if not isinstance(body, dict):
                    body = {}
                if r.status_code != 200 or body.get("state") != "merged":
                    self.store.upsert(key, status="notified")
                    msg = body.get("message") or body.get("error") or ""
                    if isinstance(msg, (list, dict)):
                        msg = str(msg)
                    if r.status_code == 200:
                        msg = f"state = {body.get('state')} (mungkin merge train / auto-merge)"
                    hint = {401: "token tidak punya akses", 403: "Anda tidak punya izin merge di project ini",
                            405: "MR belum bisa di-merge (approval wajib, diskusi belum resolved, atau draft)",
                            406: "ada conflict / branch tidak bisa di-merge", 409: "SHA berubah, ada commit baru",
                            422: "branch tidak bisa di-merge"}.get(r.status_code, "")
                    self.safe_send(f"⛔ Merge !{iid} gagal (HTTP {r.status_code}): {e(msg)}"
                                   + (f"\n<i>{e(hint)}</i>" if hint else ""))
                    self.event("error", f"Merge !{iid} gagal", f"HTTP {r.status_code}: {msg}", key, "error")
                    return

            self.store.upsert(key, status="merged", decided_at=time.time())
            status, text = self.teams.notify_merged({**mr, **body})
            teams_line = {"sent": "Teams: terkirim ✅", "off": "Teams: nonaktif",
                          "copy": "Teams: salin pesan di bawah"}.get(status, f"Teams: {status} ⚠️")
            self._finish_card(self.store.get(key) or rec,
                              f"<b>✅ Merged</b> ke <code>{e(mr['target_branch'])}</code> · {now_str()} · {e(teams_line)}")
            if status not in ("sent", "off"):
                self.safe_send(text, html=False)
            self.event("merged", f"!{iid} di-merge ke {mr['target_branch']}",
                       f"{mr.get('title')} · {teams_line}", key, "success")
            log.info("MR %s merged; teams=%s", key, status)

    # ---------------------------------------------------------------- reject
    def ask_reject_reason(self, rec):
        msg_id = self.safe_send(
            f"Tulis komentar untuk <b>!{rec['iid']}</b> {e(rec.get('title'))}\n"
            f"(akan diposting di MR atas nama Anda). Balas <b>-</b> kalau tidak perlu komentar.",
            force_reply=True)
        if msg_id:
            self.store.kv_set(f"reject:{msg_id}", mr_key(rec["project_id"], rec["iid"]))

    def finish_reject(self, rec, text, prompt_id=None):
        if not rec:
            return
        key = mr_key(rec["project_id"], rec["iid"])
        if rec["status"] in DONE:
            self.safe_send(f"MR !{rec['iid']} sudah {e(rec['status'])}, komentar tidak dikirim.")
            self.store.kv_del(f"reject:{prompt_id}")
            return
        posted = False
        with self.critical():
            if text and text != "-":
                if self.dry:
                    print("[DRY RUN] komentar MR:", text)
                else:
                    try:
                        self.gl.add_note(rec["project_id"], rec["iid"], text)
                    except Exception as ex:
                        # keep the prompt so the user can simply reply again
                        self.safe_send(f"⛔ Komentar ke !{rec['iid']} gagal dikirim: {e(short_error(ex))}. "
                                       f"Balas lagi pesan tadi untuk mencoba ulang.")
                        return
                posted = True
            self.store.kv_del(f"reject:{prompt_id}")
            self.store.upsert(key, status="rejected", decided_at=time.time())
            self.event("rejected", f"!{rec['iid']} ditolak", text if posted else "", key, "warning")
            self._finish_card(rec, f"<b>❌ Ditolak</b> · {now_str()}" + (" · komentar terkirim" if posted else ""))
