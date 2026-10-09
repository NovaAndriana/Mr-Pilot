"""CLI entry: python -m mr_pilot [--config config.yaml] [run|setup|doctor|setup-ci|demo] [opsi]"""
import argparse
import json
import logging
import os
import faulthandler
import signal
import sys
import time
from logging.handlers import RotatingFileHandler

from . import __version__
from .config import ConfigError, load_config, require
from .util import RedactingFilter


def setup_logging(path):
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5, encoding="utf-8"))
    except OSError as ex:  # read-only folder: keep logging to stdout (docker logs)
        print(f"Log file {path} tidak bisa ditulis ({ex}); log hanya ke layar.", file=sys.stderr)
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(RedactingFilter())  # token/API key never reach the log
    root.handlers = handlers
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_health(config_path):
    """Exit 0 when the main loop wrote its heartbeat recently (used by Docker HEALTHCHECK)."""
    data_dir = os.path.dirname(os.path.abspath(config_path))
    if not os.path.exists(config_path):
        print("menunggu setup")
        return 0
    hb = os.path.join(data_dir, ".heartbeat")
    try:
        age = time.time() - os.path.getmtime(hb)
    except OSError:
        print("belum ada heartbeat")
        return 1
    limit = int(os.environ.get("MRP_HEALTH_MAX_AGE", "600"))
    print(f"heartbeat {int(age)} detik lalu (batas {limit})")
    return 0 if age < limit else 1


def cmd_reset(cfg, data_dir, a):
    """Hapus riwayat (MR, aktivitas, pelanggaran, statistik AI, log). .env, config, standar, AI tetap."""
    import glob
    from .store import Store
    hb = os.path.join(data_dir, ".heartbeat")
    try:
        running = time.time() - os.path.getmtime(hb) < 90
    except OSError:
        running = False
    if running and not a.force:
        print("MR Pilot sepertinya masih berjalan. Hentikan dulu (setup.bat stop / ./setup.sh stop),\n"
              "atau pakai `setup.bat reset` yang menghentikan dan menyalakan ulang otomatis.")
        return 1
    db = cfg["storage"]["db_path"]
    log_file = cfg["storage"]["log_file"]
    logs = sorted(glob.glob(log_file + "*"))
    store = Store(db) if os.path.exists(db) else None
    counts = store.counts() if store else {}
    print("Yang akan DIHAPUS:")
    print(f"  - Riwayat MR             : {counts.get('mrs', 0)}")
    print(f"  - Aktivitas / events     : {counts.get('events', 0)}")
    print(f"  - Pelanggaran standar    : {counts.get('violations', 0)}")
    print(f"  - Statistik panggilan AI : {counts.get('ai_calls', 0)}")
    print(f"  - File log               : {len(logs)} file ({os.path.dirname(log_file)})")
    print("Yang TETAP: data/.env (token & API key), config.yaml, standards/, pengaturan AI, password dashboard.")
    if a.all:
        print("--all: penanda komentar yang sudah diposting ke GitLab ikut dihapus "
              "(warning di commit lama bisa diposting ulang).")
    if not a.yes:
        try:
            ans = input("Ketik RESET untuk melanjutkan: ").strip()
        except EOFError:
            ans = ""
        if ans != "RESET":
            print("Dibatalkan, tidak ada yang dihapus.")
            return 1
    deleted = store.reset(everything=a.all) if store else {}
    if store:
        store.db.close()
    gone = 0
    for f in logs + [hb]:
        try:
            os.remove(f)
            gone += f in logs
        except OSError:
            pass
    print(f"Selesai: {sum(v for k, v in deleted.items() if k != 'kv')} baris riwayat dan {gone} file log dihapus.")
    print("MR yang masih terbuka dan di-assign ke Anda akan dikirim ulang sebagai kartu baru saat MR Pilot jalan.")
    return 0


def cmd_password(cfg, data_dir, a):
    """Tampilkan / ganti password dashboard. Sumbernya DASHBOARD_PASSWORD di <data>/.env."""
    import secrets
    from .setup_wizard import read_env, write_env
    env_path = os.path.join(data_dir, ".env")
    if a.reset or a.set_password:
        new = a.set_password or secrets.token_urlsafe(12)
        if len(new) < 8:
            print("Password minimal 8 karakter.")
            return 2
        write_env(env_path, {"DASHBOARD_PASSWORD": new})
        print(f"Password dashboard baru : {new}")
        print("Disimpan di             :", env_path)
        print("Restart MR Pilot agar dipakai (setup.bat restart / ./setup.sh restart).")
        return 0
    in_file = read_env(env_path).get("DASHBOARD_PASSWORD", "")
    active = str(cfg["dashboard"].get("password") or "")
    print(f"File .env             : {env_path}")
    print(f"Password dashboard    : {active or '(kosong)'}")
    if in_file and in_file != active:
        print("PERINGATAN: nilai di .env berbeda dengan yang terbaca. Ada variabel environment "
              "DASHBOARD_PASSWORD lain yang menimpanya (cek docker-compose.yml / env sistem).")
    print("Kalau baru diubah, restart MR Pilot dulu (setup.bat restart / ./setup.sh restart).")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="mr_pilot", description="Auto review + merge MR GitLab via Telegram")
    p.add_argument("command", nargs="?", default="run",
                   choices=["run", "setup", "doctor", "setup-ci", "demo", "dashboard", "password", "health",
                            "reset"],
                   help="run (default) | setup: wizard konfigurasi | doctor: cek koneksi | "
                        "setup-ci: pasang CI/CD + deploy | demo: dashboard data contoh | "
                        "reset: hapus riwayat & log (token/.env tetap)")
    p.add_argument("--config", default=os.environ.get("MRP_CONFIG", "config.yaml"))
    p.add_argument("--version", action="version", version=f"MR Pilot {__version__}")
    p.add_argument("--non-interactive", action="store_true", help="setup/setup-ci: ambil jawaban dari env")
    p.add_argument("--src", default=os.environ.get("MRP_SRC", "."), help="setup-ci: folder repo (default .)")
    p.add_argument("--no-ai-test", action="store_true", help="doctor: jangan panggil AI")
    p.add_argument("--reset", action="store_true", help="password: buat password dashboard baru")
    p.add_argument("--set", dest="set_password", metavar="PASSWORD", help="password: pakai password ini")
    p.add_argument("--yes", action="store_true", help="reset: tanpa konfirmasi")
    p.add_argument("--all", action="store_true", help="reset: hapus juga penanda komentar GitLab")
    p.add_argument("--force", action="store_true", help="reset: jalankan walau MR Pilot terdeteksi masih jalan")
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
    if a.command == "health":
        sys.exit(cmd_health(a.config))
    data_dir = os.path.dirname(os.path.abspath(a.config))
    if a.demo:
        a.command = "demo"
    elif a.dashboard_only:
        a.command = "dashboard"

    if a.command == "setup":
        from .setup_wizard import run_setup
        try:
            sys.exit(run_setup(data_dir, interactive=not a.non_interactive))
        except (KeyboardInterrupt, EOFError):
            print("\nSetup dibatalkan.")
            sys.exit(130)
    if a.command == "setup-ci":
        from .setup_ci import run_setup_ci
        try:
            sys.exit(run_setup_ci(os.path.abspath(a.src), data_dir, interactive=not a.non_interactive))
        except (KeyboardInterrupt, EOFError):
            print("\nDibatalkan.")
            sys.exit(130)

    if not os.path.exists(a.config):
        if a.command == "demo":
            from .setup_wizard import PKG_ROOT
            a.config = os.path.join(PKG_ROOT, "config.example.yaml")
        else:
            print(f"Config {a.config} belum ada. Jalankan setup dulu:\n"
                  "  Docker : ./setup.sh   (Windows: setup.bat)\n"
                  "  Lokal  : python -m mr_pilot setup")
            if os.environ.get("MRP_IN_DOCKER") == "1" and a.command == "run":
                while not os.path.exists(a.config):  # don't crash-loop the container; wait for setup
                    time.sleep(10)
            else:
                sys.exit(2)
    from .setup_wizard import bootstrap_data_dir
    if a.command == "run" and os.path.basename(a.config) == "config.yaml":
        for item in bootstrap_data_dir(data_dir):  # standards/ & home/ on first start, old defaults upgraded
            print(f"[setup] {item}")
    try:
        cfg = load_config(a.config)
    except ConfigError as ex:
        print(f"Config tidak valid: {ex}\nPerbaiki {a.config} / .env lalu jalankan lagi (atau jalankan setup).",
              file=sys.stderr)
        if os.environ.get("MRP_IN_DOCKER") == "1" and a.command == "run":
            time.sleep(30)  # restart policy: don't spin
        sys.exit(2)
    if a.command == "reset":  # before logging opens (and locks, on Windows) the log file
        sys.exit(cmd_reset(cfg, data_dir, a))
    if a.command == "demo":
        import tempfile
        cfg["storage"]["log_file"] = os.path.join(tempfile.gettempdir(), "mr-pilot-demo.log")
    setup_logging(cfg["storage"]["log_file"])

    if a.command == "password":
        sys.exit(cmd_password(cfg, data_dir, a))

    if a.command == "doctor":
        from .setup_wizard import run_doctor
        sys.exit(run_doctor(cfg, test_ai=not a.no_ai_test))

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

    if a.command == "demo":
        a.demo = True
    if a.command == "dashboard":
        a.dashboard_only = True
    if a.demo or a.dashboard_only:
        from .dashboard import Dashboard
        from .store import Store
        if a.demo:
            from .demo import seed, simulate
            import tempfile
            store = Store(":memory:")
            seed(store)
            simulate(store)
            cfg["code_quality"]["enabled"] = True
            cfg["_base_dir"] = tempfile.mkdtemp(prefix="mrp-demo-")  # AI overrides don't touch real config
            if not cfg["dashboard"].get("password") and cfg["dashboard"]["host"] not in ("127.0.0.1", "localhost"):
                cfg["dashboard"]["password"] = "demo"
                print("Password demo: demo")
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
            d = Dashboard(cfg, app.store, app.ai)
            d.start_background()
            print(f"Dashboard: http://{d.host}:{d.port}")
        except Exception as ex:
            logging.error("Dashboard tidak bisa dijalankan: %s", ex)
    from .app import Stop
    # docker stop / Ctrl+C: finish a running merge first, then exit cleanly
    if hasattr(signal, "SIGUSR1"):  # `kill -USR1 <pid>` dumps all thread stacks to the log (debug hangs)
        try:
            faulthandler.register(signal.SIGUSR1, all_threads=True)
        except Exception:
            pass
    signal.signal(signal.SIGTERM, app.request_stop)
    signal.signal(signal.SIGINT, app.request_stop)
    while not app.stop_requested:  # restart loop on unexpected crash
        try:
            app.run_forever()
        except (Stop, KeyboardInterrupt):
            break
        except Exception:
            logging.exception("Crash, mulai ulang 30 detik lagi")
            try:
                for _ in range(30):
                    if app.stop_requested:
                        break
                    time.sleep(1)
            except (Stop, KeyboardInterrupt):
                break
    try:  # clean stop: no stale heartbeat (reset/health must not think we're still running)
        os.remove(app.heartbeat_path)
    except OSError:
        pass
    logging.info("MR Pilot berhenti.")


if __name__ == "__main__":
    main()
