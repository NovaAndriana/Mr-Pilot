"""`setup` (interactive config wizard) and `doctor` (connection checks).

Runs inside the Docker container (data folder mounted at /data) or locally.
Writes only <data>/.env (secrets + settings) and creates <data>/config.yaml from the example
the first time, so comments in config.yaml stay intact.
Non-interactive: `setup --non-interactive` takes every answer from environment variables
(same names as in .env), e.g. for a server provisioned by CI."""
import getpass
import os
import re
import secrets
import shutil
import sys
import time

import requests

from .util import short_error

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

C = {"ok": "\033[32m", "bad": "\033[31m", "warn": "\033[33m", "dim": "\033[2m", "b": "\033[1m", "x": "\033[0m"}
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    C = {k: "" for k in C}


def say(msg="", kind=None):
    pre = {"ok": "✅ ", "bad": "❌ ", "warn": "⚠️  "}.get(kind, "")
    print(f"{C.get(kind, '')}{pre}{msg}{C['x'] if kind else ''}")


def h(title):
    print(f"\n{C['b']}── {title} {'─' * max(0, 56 - len(title))}{C['x']}")


# ------------------------------------------------------------------ .env I/O
def read_env(path):
    from .config import read_env_file
    return read_env_file(path)


def write_env(path, updates):
    """Update keys in place, keep other lines/comments, append new keys."""
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    done = set()
    for i, line in enumerate(lines):
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in updates:
            lines[i] = f"{m.group(1)}={_q(updates[m.group(1)])}"
            done.add(m.group(1))
    new = [k for k in updates if k not in done]
    if new:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"# diperbarui oleh setup {time.strftime('%Y-%m-%d %H:%M')}")
        lines += [f"{k}={_q(updates[k])}" for k in new]
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _q(v):
    v = "" if v is None else str(v)
    if not re.search(r"[\s#\"']", v):
        return v
    return f"'{v}'" if '"' in v and "'" not in v else f'"{v}"'


# ------------------------------------------------------------ data folder
# Lines of older config.yaml versions that were never changed by the user -> new defaults.
_MIGRATIONS = [
    (re.compile(r"^(\s*)poll_interval_seconds:\s*120\s*#\s*cek MR baru tiap 2 menit\s*$", re.M),
     r"\1poll_interval_seconds: ${GITLAB_POLL_SECONDS:-60}   # cek MR baru tiap 1 menit (min 30)"),
    (re.compile(r"^(\s*)also_assigned_to_me:\s*(false|true)\s*#.*$", re.M),
     r"\1watch: [reviewer, assignee]     # kirim ke Telegram kalau Anda dijadikan Reviewer ATAU Assignee"),
    (re.compile(r"^(\s*)remove_source_branch:\s*true\s*$", re.M),
     r"\1source_branch: ${MERGE_SOURCE_BRANCH:-ask}   # ask = 2 tombol: Merge (branch tetap) & Merge + hapus branch"),
    (re.compile(r"^(\s*)# bot_then_llm = tunggu komentar bot; kalau tidak muncul dalam wait_minutes, pakai API AI\s*$",
                re.M),
     r"\1# bot_then_llm = pakai komentar bot kalau sudah ada; kalau belum, langsung review pakai API AI"),
]


def migrate_config(path):
    """Upgrade untouched default lines of an older config.yaml in place (keeps a .bak). Returns changes."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            old = f.read()
    except OSError:
        return 0
    new, n = old, 0
    for rx, rep in _MIGRATIONS:
        new, k = rx.subn(rep, new)
        n += k
    if n:
        try:
            with open(path + ".bak", "w", encoding="utf-8") as f:
                f.write(old)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(new)
            os.replace(tmp, path)
        except OSError:
            return 0  # read-only mount: defaults in code still apply
    return n


def bootstrap_data_dir(data_dir):
    """Create config.yaml, standards/ and home/ in the data folder if missing; upgrade old defaults."""
    os.makedirs(data_dir, exist_ok=True)
    created = []
    cfg = os.path.join(data_dir, "config.yaml")
    if not os.path.exists(cfg):
        shutil.copy(os.path.join(PKG_ROOT, "config.example.yaml"), cfg)
        created.append("config.yaml")
    elif migrate_config(cfg):
        created.append("config.yaml (diperbarui, salinan lama: config.yaml.bak)")
    std = os.path.join(data_dir, "standards")
    src_std = os.path.join(PKG_ROOT, "standards")
    if not os.path.exists(std) and os.path.isdir(src_std) and os.path.abspath(src_std) != os.path.abspath(std):
        shutil.copytree(src_std, std)
        created.append("standards/")
    if os.path.exists("/.dockerenv") or os.environ.get("MRP_IN_DOCKER") == "1":
        os.makedirs(os.path.join(data_dir, "home"), exist_ok=True)  # $HOME for Claude Code in the container
    return created


# --------------------------------------------------------------- prompting
class Asker:
    def __init__(self, env, interactive=True):
        self.env = env
        self.interactive = interactive and sys.stdin.isatty()

    def ask(self, key, prompt, default="", secret=False, optional=False):
        cur = self.env.get(key) or os.environ.get(key) or default
        if not self.interactive:
            return cur
        shown = ("terisi …" + cur[-4:] if len(cur) > 8 else "terisi") if (secret and cur) else cur
        hint = f" [{shown}]" if shown else (" (Enter = lewati)" if optional else "")
        while True:
            val = (getpass.getpass if secret else input)(f"  {prompt}{hint}: ").strip()
            val = val or cur
            if val or optional:
                return val
            say("Wajib diisi.", "warn")

    def yes(self, prompt, default=True):
        if not self.interactive:
            return default
        v = input(f"  {prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not v else v in ("y", "ya", "yes")

    def choose(self, prompt, options, default):
        if not self.interactive:
            return default
        for i, (k, label) in enumerate(options, 1):
            print(f"    {i}. {label}{'  (default)' if k == default else ''}")
        while True:
            v = input(f"  {prompt} [1-{len(options)}]: ").strip()
            if not v:
                return default
            if v.isdigit() and 1 <= int(v) <= len(options):
                return options[int(v) - 1][0]


# ---------------------------------------------------------------- checks
def check_gitlab(url, token, verify=True):
    try:
        r = requests.get(url.rstrip("/") + "/api/v4/user", headers={"PRIVATE-TOKEN": token}, timeout=15, verify=verify)
        if r.status_code == 200:
            return True, "@" + r.json().get("username", "?")
        return False, f"HTTP {r.status_code}" + (" (token salah/kedaluwarsa)" if r.status_code == 401 else "")
    except requests.exceptions.SSLError:
        return False, "SSL_ERROR"
    except Exception as ex:
        return False, short_error(ex, 120)


def tg_base(token):
    return f"{(os.environ.get('TELEGRAM_API_BASE') or 'https://api.telegram.org').rstrip('/')}/bot{token}"


def check_telegram(token):
    try:
        r = requests.post(tg_base(token) + "/getMe", timeout=15).json()
        return (True, "@" + r["result"]["username"]) if r.get("ok") else (False, r.get("description", "token salah"))
    except Exception as ex:
        return False, short_error(ex, 120)  # raw requests errors contain the token in the URL


def detect_chat_id(token, wait=90):
    """Wait for the user to message the bot; return chat id."""
    base = tg_base(token)
    try:
        upd = requests.post(base + "/getUpdates", json={"timeout": 0}, timeout=15).json().get("result", [])
        offset = (upd[-1]["update_id"] + 1) if upd else 0
        end = time.time() + wait
        while time.time() < end:
            res = requests.post(base + "/getUpdates", json={"offset": offset, "timeout": 20}, timeout=35).json()
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                if m.get("chat", {}).get("type") == "private":
                    return m["chat"]["id"], m.get("from", {}).get("first_name", "")
    except Exception:
        pass
    return None, None


# ------------------------------------------------------------------ wizard
AI_KEYS = [
    ("claude_code", "Claude Code (langganan Pro/Max/Team)", "CLAUDE_CODE_OAUTH_TOKEN",
     "Token dari `claude setup-token` (jalankan di PC yang sudah login Claude Code)"),
    ("anthropic", "Anthropic API", "ANTHROPIC_API_KEY", "API key dari console.anthropic.com / platform.claude.com"),
    ("gemini", "Google Gemini", "GEMINI_API_KEY", "API key dari aistudio.google.com"),
    ("openrouter", "OpenRouter", "OPENROUTER_API_KEY", "API key dari openrouter.ai/keys"),
    ("groq", "Groq", "GROQ_API_KEY", "API key dari console.groq.com/keys"),
    ("openai", "OpenAI", "OPENAI_API_KEY", "API key OpenAI"),
]


def run_setup(data_dir, interactive=True):
    from .ai import AIManager
    from .config import load_config

    env_path = os.path.join(data_dir, ".env")
    created = bootstrap_data_dir(data_dir)
    env = read_env(env_path)
    a = Asker(env, interactive)
    upd = {}

    def cur(key, default=""):
        return env.get(key) or os.environ.get(key) or default

    print(f"{C['b']}MR Pilot setup{C['x']}  ·  folder data: {data_dir}")
    if created:
        say("Dibuat: " + ", ".join(created), "ok")

    # 1. GitLab ------------------------------------------------------------
    h("1. GitLab")
    while True:
        url = a.ask("GITLAB_URL", "URL GitLab", "https://code.idas.id")
        token = a.ask("GITLAB_TOKEN", "Personal Access Token (scope: api)", secret=True)
        verify = str(cur("GITLAB_VERIFY_SSL", "true")).lower() != "false"
        ok, info = check_gitlab(url, token, verify)
        if info == "SSL_ERROR":
            say("Sertifikat SSL GitLab tidak dikenali (CA kantor / self-signed / intermediate tidak lengkap).", "warn")
            say("Cara aman: batalkan, jalankan `setup.bat trust-cert` (Windows) atau `./setup.sh trust-cert`, "
                "lalu ulangi setup. Sertifikat disimpan di data/certs.", "warn")
            if a.yes("Atau lewati verifikasi SSL sekarang (kurang aman)?", False):
                verify = False
                ok, info = check_gitlab(url, token, verify)
        if ok:
            say(f"Terhubung sebagai {info}", "ok")
            break
        say(f"Gagal: {info}", "bad")
        if not a.interactive or not a.yes("Coba lagi?", True):
            break
    upd.update(GITLAB_URL=url, GITLAB_TOKEN=token, GITLAB_VERIFY_SSL=str(verify).lower())

    # 2. Telegram ----------------------------------------------------------
    h("2. Telegram")
    print(f"  {C['dim']}Buat bot di @BotFather (/newbot) jika belum punya.{C['x']}")
    tg = a.ask("TELEGRAM_BOT_TOKEN", "Token bot", secret=True)
    ok, info = check_telegram(tg)
    say(f"Bot {info}" if ok else f"Token bot gagal: {info}", "ok" if ok else "bad")
    chat = cur("TELEGRAM_CHAT_ID")
    if ok and a.interactive and (not chat or a.yes(f"Deteksi ulang chat id (sekarang: {chat or '-'})?", not chat)):
        print(f"  Buka Telegram, kirim pesan apa saja ke {info} sekarang… (menunggu 90 detik)")
        cid, name = detect_chat_id(tg)
        if cid:
            chat = str(cid)
            say(f"Chat id {chat} ({name})", "ok")
            try:
                requests.post(tg_base(tg) + "/sendMessage", timeout=15,
                              json={"chat_id": cid, "text": "✅ MR Pilot terhubung. Notifikasi MR akan dikirim ke sini."})
            except Exception:
                pass
        else:
            say("Tidak ada pesan masuk. Isi manual.", "warn")
            chat = a.ask("TELEGRAM_CHAT_ID", "Chat id")
    upd.update(TELEGRAM_BOT_TOKEN=tg, TELEGRAM_CHAT_ID=chat)

    # 3. AI ----------------------------------------------------------------
    h("3. AI")
    print(f"  {C['dim']}Isi satu atau lebih. Enter = lewati. Urutan & detail bisa diubah nanti di dashboard > AI.{C['x']}")
    for _name, label, key, hint in AI_KEYS:
        print(f"  {C['b']}{label}{C['x']} {C['dim']}{hint}{C['x']}")
        v = a.ask(key, key, secret=True, optional=True)
        if v:
            upd[key] = v
    if a.yes("Pakai AI lokal (Ollama / LM Studio)?", cur("LOCAL_AI_ENABLED") == "true"):
        bundled = os.environ.get("MRP_OLLAMA_BUNDLED") == "1"
        url_default = "http://ollama:11434/v1" if bundled else "http://host.docker.internal:11434/v1" \
            if os.path.exists("/.dockerenv") else "http://localhost:11434/v1"
        upd["LOCAL_AI_ENABLED"] = "true"
        upd["LOCAL_AI_URL"] = a.ask("LOCAL_AI_URL", "URL (OpenAI-compatible)", url_default)
        upd["LOCAL_AI_MODEL"] = a.ask("LOCAL_AI_MODEL", "Model", "qwen2.5-coder:14b")
    mode = a.choose("Sumber review MR", [
        ("bot_then_llm", "Tunggu bot AI Review di MR, kalau tidak ada pakai AI di atas"),
        ("llm", "Selalu review pakai AI di atas"),
        ("bot", "Hanya baca bot AI Review yang sudah ada (tanpa API)")],
        cur("REVIEW_MODE", "bot_then_llm"))
    upd["REVIEW_MODE"] = mode

    # 4. Teams ---------------------------------------------------------------
    h("4. Teams (pesan 'sudah di-merge')")
    print(f"  {C['dim']}URL flow Power Automate (lihat README). Enter = lewati, teks dikirim ke Telegram untuk di-copy.{C['x']}")
    flow = a.ask("TEAMS_FLOW_URL", "URL flow", optional=True)
    upd.update(TEAMS_FLOW_URL=flow, TEAMS_MODE="power_automate" if flow else "telegram_copy")

    # 5. Code quality + dashboard -------------------------------------------
    h("5. Code quality & dashboard")
    cq = a.yes("Aktifkan pengecekan standar kode?", str(cur("CODE_QUALITY_ENABLED", "true")) != "false")
    upd["CODE_QUALITY_ENABLED"] = str(cq).lower()
    if cq:
        upd["COMMIT_COMMENTS"] = str(a.yes("Posting warning otomatis di commit GitLab (atas nama Anda)?",
                                           str(cur("COMMIT_COMMENTS", "true")) != "false")).lower()
    pw = cur("DASHBOARD_PASSWORD")
    if not pw or pw.startswith("ganti"):
        pw = secrets.token_urlsafe(12)
        say(f"Password dashboard dibuat: {C['b']}{pw}{C['x']}  (tersimpan di .env)", "ok")
    upd["DASHBOARD_PASSWORD"] = pw
    if os.environ.get("DASHBOARD_PUBLIC_URL") and not env.get("DASHBOARD_PUBLIC_URL"):
        upd["DASHBOARD_PUBLIC_URL"] = os.environ["DASHBOARD_PUBLIC_URL"]

    write_env(env_path, upd)
    say(f"Tersimpan: {env_path}", "ok")

    # 6. Test AI ---------------------------------------------------------------
    for k, v in upd.items():
        os.environ[k] = str(v)
    cfg = load_config(os.path.join(data_dir, "config.yaml"))
    ai = AIManager(cfg)
    ready = ai.available()
    if ready:
        h("6. Tes AI")
        for n in ready[:4]:
            r = ai.test(n)
            say(f"{n}: " + (f"OK ({r['ms']} ms)" if r["ok"] else r["error"]), "ok" if r["ok"] else "bad")
    elif mode != "bot":
        say("Belum ada provider AI yang siap. Review hanya memakai bot di MR sampai AI diisi (dashboard > AI).", "warn")
    print()
    say("Setup selesai.", "ok")
    return 0


# ------------------------------------------------------------------ doctor
def run_doctor(cfg, test_ai=True):
    from .ai import AIManager
    from .standards import Standards
    fails = 0

    def line(ok, name, info):
        nonlocal fails
        fails += 0 if ok else 1
        say(f"{name:<22} {info}", "ok" if ok else "bad")

    print(f"{C['b']}MR Pilot doctor{C['x']}")
    g = cfg["gitlab"]
    ok, info = check_gitlab(g["url"], g["token"], g["verify_ssl"]) if g.get("token") else (False, "GITLAB_TOKEN kosong")
    line(ok, "GitLab", f"{g['url']} {info}")
    t = cfg["telegram"]
    ok, info = check_telegram(t["bot_token"]) if t.get("bot_token") else (False, "TELEGRAM_BOT_TOKEN kosong")
    line(ok and bool(t.get("chat_id")), "Telegram", f"{info}, chat id {t.get('chat_id') or 'kosong'}")
    ai = AIManager(cfg)
    for p in ai.describe()["providers"]:
        if not p["enabled"]:
            continue
        if not p["ready"]:
            say(f"{'AI ' + p['name']:<22} dilewati: {p['reason']}", "warn")
            continue
        if test_ai:
            r = ai.test(p["name"])
            line(r["ok"], f"AI {p['name']}", f"{p['model']} " + (f"{r['ms']} ms" if r["ok"] else r["error"]))
        else:
            line(True, f"AI {p['name']}", f"{p['model']} siap")
    if not ai.available() and cfg["review"]["mode"] != "bot":
        line(False, "AI", "tidak ada provider yang siap")
    tm = cfg["teams"]
    line(tm["mode"] != "power_automate" or bool(tm.get("webhook_url")), "Teams",
         {"power_automate": "Power Automate", "telegram_copy": "salin dari Telegram", "off": "nonaktif"}[tm["mode"]])
    if cfg["code_quality"]["enabled"]:
        std = Standards(cfg["code_quality"])
        line(not std.rule_errors, "Standar kode", f"{len(std.rules)} aturan" +
             (f", error: {'; '.join(std.rule_errors)}" if std.rule_errors else ""))
    d = cfg["dashboard"]
    line(bool(d.get("password")) or d.get("host") in ("127.0.0.1", "localhost"), "Dashboard",
         f"port {d.get('port')}, " + ("password terisi" if d.get("password") else "tanpa password"))
    print()
    say("Semua siap." if not fails else f"{fails} masalah. Perbaiki lalu jalankan doctor lagi.",
        "ok" if not fails else "bad")
    return 1 if fails else 0
