"""CLI entry: python -m mr_pilot [--config config.yaml] [command]"""
import argparse
import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

from .config import load_config, require


def setup_logging(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.handlers = [fh, sh]


def main(argv=None):
    p = argparse.ArgumentParser(prog="mr_pilot", description="Auto review + merge MR GitLab via Telegram")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--once", action="store_true", help="cek GitLab sekali lalu keluar")
    p.add_argument("--dry-run", action="store_true", help="tidak kirim apa pun, hanya cetak")
    p.add_argument("--get-chat-id", action="store_true", help="tampilkan chat id Telegram Anda")
    p.add_argument("--test-telegram", action="store_true")
    p.add_argument("--test-teams", action="store_true", help="kirim pesan uji ke flow Teams")
    p.add_argument("--review", metavar="GROUP/PROJECT!IID", help="review satu MR dan cetak hasilnya")
    p.add_argument("--dashboard-only", action="store_true", help="jalankan dashboard saja (tanpa cek GitLab)")
    p.add_argument("--demo", action="store_true", help="dashboard dengan data contoh (tanpa GitLab/Telegram)")
    p.add_argument("--check-standards", metavar="PATH", nargs="+",
                   help="cek file lokal terhadap rules.yaml, mis. --check-standards internal/a.go")
    a = p.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cfg = load_config(a.config)
    setup_logging(cfg["storage"]["log_file"])

    from .telegram_api import Telegram
    if a.get_chat_id:
        require(cfg, "telegram.bot_token")
        tg = Telegram(cfg["telegram"]["bot_token"], 0, cfg["telegram"].get("proxy"))
        print("Kirim pesan apa saja ke bot Anda di Telegram, menunggu 30 detik…")
        for u in tg.get_updates(0, 30):
            m = u.get("message") or {}
            if m:
                print(f"chat_id = {m['chat']['id']}  (dari {m['from'].get('first_name')})")
        return

    if a.test_telegram:
        require(cfg, "telegram.bot_token", "telegram.chat_id")
        Telegram(cfg["telegram"]["bot_token"], cfg["telegram"]["chat_id"],
                 cfg["telegram"].get("proxy")).send("✅ Tes MR Pilot berhasil.")
        print("Terkirim.")
        return

    if a.test_teams:
        from .teams import Teams
        sample = {"iid": 1, "title": "test(IDAS-0000): uji koneksi MR Pilot", "target_branch": "staging",
                  "source_branch": "feat/test", "author": {"name": "Tes Bot", "username": "tes"},
                  "references": {"short": "!1", "full": "idas/repo!1"}, "web_url": "https://example.com"}
        print(Teams(cfg).notify_merged(sample))
        return

    if a.check_standards:
        from .standards import Standards
        std = Standards(cfg["code_quality"])
        for err in std.rule_errors:
            print("⚙️", err)
        total = 0
        for path in a.check_standards:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
            diff = f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + ln for ln in lines)
            for v in std.check_file_diff(path, diff):
                total += 1
                print(f"{path}:{v['line']}  [{v['severity']}] {v['rule']}: {v['message']}")
        print(f"{total} pelanggaran.")
        return

    if a.demo or a.dashboard_only:
        from .dashboard import Dashboard
        from .store import Store
        if a.demo:
            from .demo import seed, simulate
            store = Store(":memory:")
            seed(store)
            simulate(store)
            cfg["code_quality"]["enabled"] = True
        else:
            store = Store(cfg["storage"]["db_path"])
        dash = Dashboard(cfg, store)
        print(f"Dashboard: http://{dash.host}:{dash.port}  (Ctrl+C untuk berhenti)")
        try:
            dash.serve()
        except KeyboardInterrupt:
            pass
        return

    require(cfg, "gitlab.url", "gitlab.token")
    from .app import App

    if a.review:
        from .gitlab_api import GitLab
        from .reviewer import Reviewer, heuristic_flags
        path, iid = a.review.rsplit("!", 1)
        gl = GitLab(cfg["gitlab"]["url"], cfg["gitlab"]["token"], verify=cfg["gitlab"]["verify_ssl"])
        mr = gl.get_mr(path, int(iid))
        rv = Reviewer(cfg, gl).review(mr, first_seen=0)
        print(json.dumps({"review": rv, "flags": heuristic_flags(mr, cfg)}, indent=2, ensure_ascii=False))
        return

    if not a.dry_run:
        require(cfg, "telegram.bot_token", "telegram.chat_id")
    app = App(cfg, dry_run=a.dry_run)
    if a.once or a.dry_run:
        app.poll_gitlab()
        return
    if cfg["dashboard"].get("enabled"):
        from .dashboard import Dashboard
        try:
            d = Dashboard(cfg, app.store)
            d.start_background()
            print(f"Dashboard: http://{d.host}:{d.port}")
        except Exception as ex:
            logging.error("Dashboard tidak bisa dijalankan: %s", ex)
    while True:  # restart loop on unexpected crash
        try:
            app.run_forever()
        except KeyboardInterrupt:
            print("Berhenti.")
            return
        except Exception:
            logging.exception("Crash, restart 30 detik lagi")
            time.sleep(30)


if __name__ == "__main__":
    main()
