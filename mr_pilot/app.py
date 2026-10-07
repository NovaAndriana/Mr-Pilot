"""Main loop: poll GitLab -> review -> Telegram buttons -> merge -> Teams."""
import logging
import time
from datetime import datetime

from .ai import AIManager
from .formatting import VERDICT_LABEL, build_messages, e, review_buttons
from .gitlab_api import GitLab
from .quality import CodeQuality
from .reviewer import Reviewer, heuristic_flags
from .store import Store
from .teams import Teams
from .telegram_api import Telegram

log = logging.getLogger("mr_pilot")

DONE = ("merged", "rejected", "closed")


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


class App:
    def __init__(self, cfg, dry_run=False, gl=None, tg=None, store=None):
        self.cfg = cfg
        self.dry = dry_run
        g, t = cfg["gitlab"], cfg["telegram"]
        self.gl = gl or GitLab(g["url"], g["token"], verify=g["verify_ssl"])
        self.tg = tg or Telegram(t["bot_token"], t["chat_id"], proxy=t.get("proxy"))
        # dry-run uses a throwaway DB so a test run doesn't mark MRs as "already notified"
        self.store = store or Store(":memory:" if dry_run else cfg["storage"]["db_path"])
        self.allowed = set(t["allowed_user_ids"])
        self.ai = AIManager(cfg, self.store)
        self.reviewer = Reviewer(cfg, self.gl, self.ai)
        self.teams = Teams(cfg)
        self.quality = CodeQuality(cfg, self.gl, self.store, cfg.get("_base_dir", "."), self.ai)
        self.username = g.get("username") or None
        self.last_poll = 0.0

    # ------------------------------------------------------------- lifecycle
    def ensure_user(self):
        if not self.username:
            self.username = self.gl.me()["username"]
        if self.store.kv_get("gitlab_user") != self.username:
            self.store.kv_set("gitlab_user", self.username)
        return self.username

    def event(self, type_, title, detail="", key=None, level="info"):
        try:
            self.store.add_event(type_, title, detail, key, level)
        except Exception:
            log.exception("gagal simpan event")

    def run_forever(self):
        self.ensure_user()
        self.event("system", "MR Pilot aktif", f"Memantau MR untuk @{self.username}")
        interval = int(self.cfg["gitlab"]["poll_interval_seconds"])
        log.info("MR Pilot jalan untuk @%s (cek tiap %ss)", self.username, interval)
        if self.cfg["telegram"].get("startup_message"):
            self.safe_send(f"🟢 MR Pilot aktif, memantau MR untuk <b>@{e(self.username)}</b>.")
        while True:
            if time.time() - self.last_poll >= interval:
                self.last_poll = time.time()
                try:
                    self.poll_gitlab()
                    self.store.kv_set("last_poll_ok", time.time())
                except Exception as ex:
                    log.exception("Gagal cek GitLab")
                    self.store.kv_set("last_poll_error", f"{time.strftime('%H:%M')} {ex}")
                    self.event("error", "Gagal cek GitLab", str(ex)[:300], level="error")
            try:
                self.handle_updates(timeout=max(1, min(25, interval)))
            except Exception:
                log.exception("Gagal ambil update Telegram")
                time.sleep(5)

    def safe_send(self, text, **kw):
        if self.dry:
            print("[TELEGRAM]", text)
            return 0
        try:
            return self.tg.send(text, **kw)
        except Exception:
            log.exception("Gagal kirim Telegram")
            return 0

    def safe_edit(self, msg_id, text, buttons=None):
        if self.dry or not msg_id:
            print("[TELEGRAM EDIT]", text)
            return
        try:
            self.tg.edit(msg_id, text, buttons)
        except Exception:
            log.exception("Gagal edit pesan Telegram")
            self.safe_send(text)

    # --------------------------------------------------------------- GitLab
    def poll_gitlab(self):
        g = self.cfg["gitlab"]
        me = self.ensure_user()
        mrs = self.gl.list_review_mrs(me, also_assigned=g.get("also_assigned_to_me"))
        projects = set(g.get("projects") or [])
        seen = set()
        for m in mrs:
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
            if rec and rec["sha"] == m["sha"] and rec["status"] not in ("waiting_bot", "error"):
                continue
            if rec and rec["status"] == "error" and (rec.get("attempts") or 0) >= 3 and rec["sha"] == m["sha"]:
                continue
            try:
                self.process(m["project_id"], m["iid"], rec)
            except Exception as ex:
                log.exception("Gagal proses MR %s", key)
                self.store.upsert(key, project_id=m["project_id"], iid=m["iid"], sha=m["sha"],
                                  title=m.get("title"), web_url=m.get("web_url"), status="error",
                                  attempts=(rec.get("attempts") or 0) + 1 if rec else 1)
                if not rec or (rec.get("attempts") or 0) == 2:
                    self.safe_send(f"⚠️ Gagal memproses {e(m.get('web_url'))}: {e(ex)}")
                self.event("error", f"Gagal memproses !{m['iid']}", str(ex)[:300], key, "error")
        self.sync_closed(seen)

    def process(self, pid, iid, rec=None, force_llm=False, header=None):
        key = mr_key(pid, iid)
        mr = self.gl.get_mr(pid, iid)
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
            except Exception as ex:
                log.exception("Code quality gagal untuk %s", key)
                self.event("error", f"Cek standar kode gagal !{iid}", str(ex)[:300], key, "error")
        if header is None:
            is_update = bool(rec and rec.get("tg_msg_id") and not same_sha)
            header = "🔄 MR diperbarui (ada commit baru)" if is_update else "🔔 MR baru untuk direview"
        # retire the old Telegram card for this MR
        if rec and rec.get("tg_msg_id") and rec.get("tg_text"):
            self.safe_edit(rec["tg_msg_id"], rec["tg_text"] + "\n\n<i>↪️ Diganti review terbaru di bawah.</i>")
        detail, text = build_messages(mr, review, heuristic_flags(mr, self.cfg), header, quality)
        if detail:
            self.safe_send(detail)
        msg_id = self.safe_send(text, buttons=review_buttons(pid, iid, mr["web_url"]))
        self.store.upsert(key, status="notified", review=review, tg_msg_id=msg_id,
                          tg_text=text, attempts=0, verdict=review["verdict"], quality=quality, **base)
        self.event("review", f"Review !{iid} selesai: {VERDICT_LABEL.get(review['verdict'], review['verdict'])}",
                   review.get("summary", "")[:300], key,
                   {"APPROVE": "success", "REQUEST_CHANGES": "error"}.get(review["verdict"], "warning"))
        log.info("MR %s dinotifikasi (verdict %s)", key, review["verdict"])

    def sync_closed(self, open_keys):
        """MRs merged/closed outside the bot: update their Telegram card."""
        for rec in self.store.by_status("notified", "waiting_bot", "error"):
            key = mr_key(rec["project_id"], rec["iid"])
            if key in open_keys:
                continue
            try:
                mr = self.gl.get_mr(rec["project_id"], rec["iid"])
            except Exception:
                continue
            if mr.get("state") == "opened":
                continue  # e.g. reviewer changed / became draft; keep as is
            state = mr.get("state")
            self.store.upsert(key, status="merged" if state == "merged" else "closed", decided_at=time.time())
            self.event("closed", f"!{rec['iid']} {state} di luar MR Pilot", rec.get("title") or "", key)
            if rec.get("tg_msg_id") and rec.get("tg_text"):
                self.safe_edit(rec["tg_msg_id"], rec["tg_text"] +
                               f"\n\n<i>ℹ️ MR sudah {e(state)} di luar MR Pilot ({now_str()}).</i>")

    # -------------------------------------------------------------- Telegram
    def handle_updates(self, timeout=25):
        offset = int(self.store.kv_get("tg_offset", 0))
        for u in self.tg.get_updates(offset, timeout):
            self.store.kv_set("tg_offset", u["update_id"] + 1)
            try:
                if "callback_query" in u:
                    self.on_callback(u["callback_query"])
                elif "message" in u:
                    self.on_message(u["message"])
            except Exception as ex:
                log.exception("Gagal proses update Telegram")
                self.safe_send(f"⚠️ Error: {e(ex)}")

    def on_callback(self, cb):
        if (cb.get("from") or {}).get("id") not in self.allowed:
            self.tg.answer(cb["id"], "Tidak diizinkan")
            return
        action, pid, iid = cb.get("data", "").split("|")
        pid, iid = int(pid), int(iid)
        rec = self.store.get(mr_key(pid, iid))
        if not rec:
            self.tg.answer(cb["id"], "Data MR tidak ditemukan")
            return
        if action == "m":
            self.tg.answer(cb["id"], "Memproses merge…")
            self.do_merge(rec, confirmed=False)
        elif action == "mf":
            self.tg.answer(cb["id"], "Merge…")
            self.do_merge(rec, confirmed=True, confirm_msg_id=cb["message"]["message_id"])
        elif action == "c":
            self.tg.answer(cb["id"], "Dibatalkan")
            self.safe_edit(cb["message"]["message_id"], "Merge dibatalkan.")
        elif action == "x":
            self.tg.answer(cb["id"])
            self.ask_reject_reason(rec)
        elif action == "rr":
            self.tg.answer(cb["id"], "Review ulang…")
            self.process(pid, iid, rec, force_llm=self.reviewer.llm_available(),
                         header="🔁 Review ulang")

    def on_message(self, msg):
        if (msg.get("from") or {}).get("id") not in self.allowed:
            return
        text = (msg.get("text") or "").strip()
        reply = msg.get("reply_to_message")
        if reply:
            key = self.store.kv_get(f"reject:{reply['message_id']}")
            if key:
                self.store.kv_del(f"reject:{reply['message_id']}")
                self.finish_reject(self.store.get(key), text)
                return
        cmd = text.split()[0].split("@")[0].lower() if text else ""
        if cmd in ("/start", "/help"):
            self.safe_send("<b>MR Pilot</b>\n/status: MR yang menunggu keputusan\n"
                           "/cek: cek GitLab sekarang\n\nKartu MR punya tombol Merge, Tolak, "
                           "Review ulang, dan Buka MR.")
        elif cmd == "/status":
            pend = self.store.by_status("notified", "waiting_bot", "error")
            if not pend:
                self.safe_send("Tidak ada MR yang menunggu. 👍")
            else:
                lines = ["<b>Menunggu keputusan:</b>"]
                for r in pend:
                    v = (r.get("review") or {}).get("verdict", "")
                    lines.append(f"• <a href=\"{e(r['web_url'])}\">!{r['iid']}</a> {e(r['title'])} "
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
        if st in ("running", "pending", "created"):
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
        if reasons and not confirmed:
            self.safe_send(f"<b>!{iid}</b> " + "\n".join(e(r) for r in reasons) + "\nYakin tetap merge?",
                           buttons=[[("⚠️ Ya, tetap merge", f"mf|{pid}|{iid}"),
                                     ("Batal", f"c|{pid}|{iid}")]])
            return
        if confirm_msg_id:
            self.safe_edit(confirm_msg_id, f"Merge !{iid} diproses…")

        mr = self.gl.get_mr(pid, iid)
        key = mr_key(pid, iid)
        if mr.get("state") != "opened":
            self.store.upsert(key, status="merged" if mr.get("state") == "merged" else "closed")
            self.safe_edit(rec["tg_msg_id"], rec["tg_text"] + f"\n\n<i>ℹ️ MR sudah {e(mr.get('state'))}.</i>")
            return
        if mr["sha"] != rec["sha"]:
            self.safe_send(f"🔄 Ada commit baru di !{iid} sejak direview. Saya review ulang dulu.")
            self.process(pid, iid, rec)
            return
        if mr.get("has_conflicts"):
            self.safe_send(f"⛔ !{iid} punya conflict, tidak bisa di-merge.")
            return
        ok, reason = self.pipeline_ok(mr)
        if not ok:
            self.safe_send(f"⏸️ !{iid}: {reason}")
            return
        if self.dry:
            print(f"[DRY RUN] akan merge {key}")
            return

        if mcfg["approve_before_merge"]:
            try:
                r = self.gl.approve(pid, iid, mr["sha"])
                if r.status_code not in (200, 201, 401):
                    log.info("approve !%s -> HTTP %s", iid, r.status_code)
            except Exception:
                log.exception("approve gagal (diabaikan)")

        self.store.upsert(key, status="merging")
        r = self.gl.merge(pid, iid, sha=mr["sha"], remove_source_branch=mcfg["remove_source_branch"],
                          squash=mcfg["squash"])
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code != 200 or body.get("state") != "merged":
            self.store.upsert(key, status="notified")
            msg = body.get("message") if isinstance(body, dict) else body
            if r.status_code == 200:
                msg = f"state = {body.get('state')} (mungkin merge train / auto-merge)"
            self.safe_send(f"⛔ Merge !{iid} gagal (HTTP {r.status_code}): {e(msg)}")
            self.event("error", f"Merge !{iid} gagal", f"HTTP {r.status_code}: {msg}", key, "error")
            return

        self.store.upsert(key, status="merged", decided_at=time.time())
        status, text = self.teams.notify_merged(body or mr)
        teams_line = {"sent": "Teams: terkirim ✅", "off": "Teams: nonaktif",
                      "copy": "Teams: salin pesan di bawah"}.get(status, f"Teams: {status} ⚠️")
        self.safe_edit(rec["tg_msg_id"], rec["tg_text"] +
                       f"\n\n<b>✅ Merged</b> ke <code>{e(mr['target_branch'])}</code> · {now_str()} · {e(teams_line)}")
        if status != "sent" and status != "off":
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

    def finish_reject(self, rec, text):
        if not rec:
            return
        key = mr_key(rec["project_id"], rec["iid"])
        posted = False
        if text and text != "-":
            if self.dry:
                print("[DRY RUN] komentar MR:", text)
            else:
                self.gl.add_note(rec["project_id"], rec["iid"], text)
            posted = True
        self.store.upsert(key, status="rejected", decided_at=time.time())
        self.event("rejected", f"!{rec['iid']} ditolak", text if posted else "", key, "warning")
        self.safe_edit(rec["tg_msg_id"], rec["tg_text"] +
                       f"\n\n<b>❌ Ditolak</b> · {now_str()}" + (" · komentar terkirim" if posted else ""))
