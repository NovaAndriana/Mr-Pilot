"""End-to-end: the real `python -m mr_pilot run` process (or the Docker image, E2E_MODE=docker)
against fake GitLab / Telegram / AI / Teams. Run: python -m pytest -q tests/e2e

Each test builds on the previous one (same app process), like a real working day."""
import http.cookiejar as cookiejar
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

from fakes import FakeAI, FakeGitLab, FakeTeams, FakeTelegram  # noqa: E402

MODE = os.environ.get("E2E_MODE", "process")  # process | docker
IMAGE = os.environ.get("E2E_IMAGE", "mr-pilot:e2e")
PASSWORD = "e2e-Dashb0ard-pass"

GO_BAD = (" func Get(id string) {\n+\tdata, _ := repo.Find(id)\n+\tfmt.Println(data)\n"
          "+\tq := \"SELECT * FROM t WHERE id=\" + id\n+\treturn\n")
GO_OK = "+func Sum(a, b int) int {\n+\treturn a + b\n+}\n"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait(cond, timeout=25, msg="kondisi"):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = cond()
            if last:
                return last
        except (KeyError, IndexError, AttributeError):
            pass
        time.sleep(0.15)
    raise AssertionError(f"timeout menunggu {msg} (terakhir: {last!r})")


def review_json(verdict="APPROVE", findings=None, summary="Perubahan rapi dan sesuai deskripsi."):
    return {"verdict": verdict, "summary": summary, "solves": "Menampilkan kuota per user.",
            "changes": ["Response quota per user"], "good_points": ["Test lengkap"],
            "breaking_changes": [], "findings": findings or []}


def ai_router(review_for):
    """Fake AI behaviour: standards prompt -> violations, review prompt -> per-MR verdict."""
    def reply(payload):
        system = payload["messages"][0]["content"]
        user = payload["messages"][1]["content"]
        if "STANDAR TIM" in system:
            return {"violations": [{"rule": "Handler akses repo langsung", "severity": "warning",
                                    "file": "internal/x/usecase.go", "line": 2, "message": "Pindahkan ke usecase",
                                    "confidence": "high"}]}
        m = re.search(r"Judul MR: (.*)", user)
        return review_for((m.group(1) if m else "").strip())
    return reply


# ------------------------------------------------------------------ launcher
class AppProc:
    def __init__(self, data, env):
        self.data, self.env, self.p, self.name = data, env, None, f"mrp-e2e-{os.getpid()}"
        self.out = open(os.path.join(data, "stdout.log"), "ab")

    def start(self):
        if MODE == "docker":
            subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)  # after a kill -9
            os.makedirs(os.path.join(self.data, "home"), exist_ok=True)
            args = ["docker", "run", "--rm", "--name", self.name, "--network", "host",
                    "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{self.data}:/data", "-e", "HOME=/data/home"]
            for k, v in self.env.items():
                if k.startswith(("GITLAB", "TELEGRAM", "DASHBOARD", "REVIEW", "TEAMS", "CODE_", "COMMIT", "MRP_")):
                    args += ["-e", f"{k}={v}"]
            self.p = subprocess.Popen(args + [IMAGE, "run"], stdout=self.out, stderr=subprocess.STDOUT)
        else:
            self.p = subprocess.Popen([sys.executable, "-m", "mr_pilot", "run", "--config",
                                       os.path.join(self.data, "config.yaml")],
                                      cwd=ROOT, env=self.env, stdout=self.out, stderr=subprocess.STDOUT)

    def signal(self, sig):
        if MODE == "docker":
            subprocess.run(["docker", "kill", "--signal", "TERM" if sig == signal.SIGTERM else "KILL", self.name],
                           capture_output=True)
        else:
            self.p.send_signal(sig)

    def wait_exit(self, timeout=40):
        return self.p.wait(timeout)

    def alive(self):
        return self.p and self.p.poll() is None

    def log(self):
        self.out.flush()
        with open(os.path.join(self.data, "stdout.log"), encoding="utf-8", errors="replace") as f:
            return f.read()


# ------------------------------------------------------------------ fixture
class World:
    pass


@pytest.fixture(scope="module")
def w():
    W = World()
    W.gl, W.tg, W.teams = FakeGitLab(), FakeTelegram(), FakeTeams()
    W.verdicts = {}
    W.ai_a = FakeAI(500)  # first provider: broken
    W.ai_b = FakeAI(ai_router(lambda title: review_json(**W.verdicts.get(title, {}))))
    W.data = tempfile.mkdtemp(prefix="mrp-e2e-")
    os.chmod(W.data, 0o777)
    shutil.copy(os.path.join(ROOT, "config.example.yaml"), os.path.join(W.data, "config.yaml"))
    W.port = free_port()
    W.env_file = {
        "GITLAB_URL": W.gl.url, "GITLAB_TOKEN": FakeGitLab.TOKEN, "TELEGRAM_BOT_TOKEN": FakeTelegram.TOKEN,
        "TELEGRAM_CHAT_ID": str(FakeTelegram.CHAT), "TELEGRAM_API_BASE": W.tg.url, "REVIEW_MODE": "llm",
        "TEAMS_MODE": "power_automate", "TEAMS_FLOW_URL": W.teams.url + "/flow?sig=SECRETsig123",
        "CODE_QUALITY_ENABLED": "true", "COMMIT_COMMENTS": "true", "DASHBOARD_PASSWORD": PASSWORD,
        "DASHBOARD_HOST": "127.0.0.1", "DASHBOARD_PORT": str(W.port),
    }
    with open(os.path.join(W.data, ".env"), "w") as f:
        f.write("# e2e\n" + "".join(f"{k}={v}\n" for k, v in W.env_file.items()))
    off = {k: {"enabled": False} for k in ("claude_code", "anthropic", "gemini", "openrouter", "groq", "local")}
    with open(os.path.join(W.data, "ai_overrides.json"), "w") as f:
        json.dump({"providers": {**off,
                                 "openai": {"enabled": True, "base_url": W.ai_a.url + "/v1", "api_key": "sk-proj-aaaaaaaaaaaaaaaaaaaa",
                                            "model": "a-model"},
                                 "ai-b": {"type": "openai", "enabled": True, "base_url": W.ai_b.url + "/v1",
                                          "api_key": "sk-proj-bbbbbbbbbbbbbbbbbbbb", "model": "b-model"}},
                   "order": ["openai", "ai-b"]}, f)
    W.env = {"PATH": os.environ["PATH"], "HOME": W.data, "PYTHONPATH": ROOT, "LANG": "C.UTF-8",
             "PYTHONUNBUFFERED": "1", "MRP_HEALTH_MAX_AGE": "600"}
    W.app = AppProc(W.data, {**W.env, **W.env_file} if MODE == "docker" else W.env)
    yield W
    if W.app.alive():
        W.app.signal(signal.SIGTERM)
        try:
            W.app.wait_exit(30)
        except subprocess.TimeoutExpired:
            W.app.signal(signal.SIGKILL)
    for s in (W.gl, W.tg, W.teams, W.ai_a, W.ai_b):
        s.stop()
    if os.environ.get("E2E_KEEP"):
        print("data:", W.data)


def cek(W):
    n = len(W.tg.sent)
    W.tg.say("/cek")
    wait(lambda: W.tg.find("Selesai cek GitLab", n), 40, "/cek selesai")


def db_status(W, key):
    con = sqlite3.connect(os.path.join(W.data, "mr_pilot.db"), timeout=10)
    try:
        row = con.execute("SELECT status FROM mrs WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def card(W, iid, since=0):
    return wait(lambda: next((m for m in reversed(W.tg.sent[since:])
                              if f"!{iid}" in W.tg.messages[m]["text"] and any("Merge" in b for b in W.tg.buttons(m))), None),
                40, f"kartu !{iid}")


def http(W, path, method="GET", body=None, headers=None, opener=None):
    req = urllib.request.Request(f"http://127.0.0.1:{W.port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with (opener or W.opener).open(req, timeout=10) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


# ===================================================================== tests
def test_01_startup(w):
    w.gl.add_mr(375, "feat(IDAS-5323): show per user quota usage", "a" * 40,
                description="## Reasons for Change\nKuota per user.\n## Changes\n- quota object",
                files={"internal/x/usecase.go": GO_BAD})
    w.app.start()
    wait(lambda: w.tg.find("MR Pilot aktif"), 40, "pesan startup")
    wait(lambda: os.path.exists(os.path.join(w.data, ".heartbeat")), 20, "heartbeat")
    w.jar = cookiejar.CookieJar()
    w.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(w.jar), NoRedirect)
    code, body, _ = wait(lambda: (lambda r: r if r[0] == 200 else None)(http(w, "/healthz")), 30, "healthz")
    h = json.loads(body)
    assert h["ok"] and h["version"]


def test_02_new_mr_card_review_quality(w):
    mid = card(w, 375)
    text = w.tg.messages[mid]["text"]
    for needle in ("Dewi Lestari", "Masalah yang diselesaikan", "Approve", "Standar kode", "go-sql-concat"):
        assert needle in text, needle
    assert set(w.tg.buttons(mid)) == {"✅ Merge", "❌ Tolak", "🗑️ Merge + hapus branch", "🔁 Review ulang", "🔗 Buka MR"}
    # AI: provider A broken -> fell back to B
    assert w.ai_a.calls and w.ai_b.calls
    # code standard warnings on the commit (file + line), summary note, commit status
    lines = sorted((c[1].get("path"), c[1].get("line")) for c in w.gl.commit_comments if c[1].get("line"))
    assert ("internal/x/usecase.go", 2) in lines and ("internal/x/usecase.go", 3) in lines
    assert any("Code Standard" in n["body"] for n in w.gl.notes_by_me(375))
    assert w.gl.statuses and w.gl.statuses[-1][1]["name"] == "code-standard"
    assert w.gl.discussions, "temuan AI inline"


def test_03_no_duplicate_on_repoll(w):
    n_cards = sum(1 for m in w.tg.sent if "!375" in w.tg.messages[m]["text"] and w.tg.buttons(m))
    n_comments = len(w.gl.commit_comments)
    cek(w)
    cek(w)
    assert sum(1 for m in w.tg.sent if "!375" in w.tg.messages[m]["text"] and w.tg.buttons(m)) == n_cards
    assert len(w.gl.commit_comments) == n_comments, "warning tidak boleh diposting ulang"


def test_04_merge_waits_for_pipeline_then_merges(w):
    mid = w.card375 = card(w, 375)
    w.gl.mrs[(7, 375)]["head_pipeline"] = {"status": "running"}
    n = len(w.tg.sent)
    w.tg.press(mid, w.tg.buttons(mid)["✅ Merge"])
    wait(lambda: w.tg.find("Pipeline masih", n), 20, "pesan pipeline running")
    assert not w.gl.merges
    w.gl.mrs[(7, 375)]["head_pipeline"] = {"status": "success"}
    n = len(w.tg.sent)
    w.tg.press(mid, w.tg.buttons(mid)["✅ Merge"])
    # built-in rule flagged the SQL concat as an error -> explicit confirmation
    conf = wait(lambda: w.tg.find("Yakin tetap merge", n), 20, "konfirmasi standar kode")
    assert "error" in w.tg.messages[conf]["text"] and not w.gl.merges
    w.tg.press(conf, w.tg.buttons(conf)["⚠️ Ya, tetap merge"])
    wait(lambda: w.gl.merges, 20, "merge di GitLab")
    assert w.gl.approvals and w.gl.approvals[-1][1] == "a" * 40
    assert w.gl.merges[-1][1]["sha"] == "a" * 40
    wait(lambda: "Merged" in w.tg.messages[mid]["text"], 20, "kartu merged")
    assert not w.tg.buttons(mid), "tombol hilang setelah merge"
    wait(lambda: w.teams.posts, 10, "pesan Teams")
    t = w.teams.posts[-1]["text"]
    assert "!375" in t or "IDAS-5323" in t
    assert "terkirim" in w.tg.messages[mid]["text"]


def test_05_double_tap_after_merge_is_ignored(w):
    mid = w.card375
    merges = len(w.gl.merges)
    w.tg.press(mid, "m|7|375")
    wait(lambda: any("sudah di-merge" in (a[1] or "") for a in w.tg.answers), 15, "jawaban sudah di-merge")
    assert len(w.gl.merges) == merges


def test_06_request_changes_needs_confirmation(w):
    w.verdicts["fix: reject empty phases"] = {"verdict": "REQUEST_CHANGES",
                                              "findings": [{"severity": "blocker", "title": "Overflow",
                                                            "file": "internal/y.go", "line": 2,
                                                            "evidence": "return a + b"}]}
    w.gl.add_mr(381, "fix: reject empty phases", "b" * 40, files={"internal/y.go": GO_OK},
                author=("Bima Saputra", "bima.s"))
    n = len(w.tg.sent)
    cek(w)
    mid = card(w, 381, n)
    assert "Request changes" in w.tg.messages[mid]["text"]
    n = len(w.tg.sent)
    w.tg.press(mid, "m|7|381")
    conf = wait(lambda: w.tg.find("Yakin tetap merge", n), 15, "konfirmasi")
    assert not any(k == (7, 381) for k, _ in w.gl.merges)
    w.tg.press(conf, w.tg.buttons(conf)["⚠️ Ya, tetap merge"])
    wait(lambda: any(k == (7, 381) for k, _ in w.gl.merges), 20, "merge setelah konfirmasi")


def test_07_reject_posts_comment(w):
    w.gl.add_mr(390, "feat: halaman kuota", "c" * 40, files={"web/src/a.tsx": "+const a: any = 1\n"},
                author=("Sinta Maharani", "sinta.m"))
    n = len(w.tg.sent)
    cek(w)
    mid = card(w, 390, n)
    n = len(w.tg.sent)
    w.tg.press(mid, "x|7|390")
    prompt = wait(lambda: w.tg.find("Tulis komentar", n), 15, "prompt tolak")
    w.tg.say("Tolong ganti any & tambah test <segera>", reply_to=prompt)
    wait(lambda: any("ganti any" in nn["body"] for nn in w.gl.notes_by_me(390)), 15, "komentar di MR")
    wait(lambda: "Ditolak" in w.tg.messages[mid]["text"], 15, "kartu ditolak")
    # replying again to the same prompt does nothing more
    before = len(w.gl.notes_by_me(390))
    w.tg.say("lagi", reply_to=prompt)
    time.sleep(2)
    assert len(w.gl.notes_by_me(390)) == before


def test_08_new_commit_replaces_card_and_merge_on_stale_sha(w):
    w.gl.add_mr(400, "feat: estimator", "d" * 40, files={"internal/est.go": GO_OK}, author=("Rizky", "rizky.p"))
    n = len(w.tg.sent)
    cek(w)
    old = card(w, 400, n)
    w.gl.push(400, "e" * 40, {"internal/est.go": GO_OK})
    n = len(w.tg.sent)
    cek(w)
    new = card(w, 400, n)
    assert new != old and "diperbarui" in w.tg.messages[new]["text"]
    assert "Diganti review terbaru" in w.tg.messages[old]["text"] and not w.tg.buttons(old)
    # another push right before tapping Merge: must re-review, not merge
    w.gl.push(400, "f" * 40, {"internal/est.go": GO_OK})
    n = len(w.tg.sent)
    w.tg.press(new, "m|7|400")
    wait(lambda: w.tg.find("Ada commit baru", n), 15, "deteksi commit baru")
    assert not any(k == (7, 400) for k, _ in w.gl.merges)


def test_09_conflict_and_merge_api_errors(w):
    w.gl.add_mr(410, "fix: conflict", "1" * 40, files={"a.go": GO_OK}, conflicts=True)
    n = len(w.tg.sent)
    cek(w)
    mid = card(w, 410, n)
    n = len(w.tg.sent)
    w.tg.press(mid, "m|7|410")
    wait(lambda: w.tg.find("conflict", n), 15, "pesan conflict")
    # GitLab refuses (405: approvals required)
    w.gl.mrs[(7, 410)]["has_conflicts"] = False
    w.gl.merge_behaviour[(7, 410)] = lambda h, mr, body: h._json(405, {"message": "405 Method Not Allowed"})
    n = len(w.tg.sent)
    w.tg.press(mid, "m|7|410")
    wait(lambda: w.tg.find("HTTP 405", n), 15, "pesan 405")
    assert "approval" in w.tg.messages[w.tg.find("HTTP 405", n)]["text"]
    # status back to notified -> can retry, and this time GitLab accepts
    w.gl.merge_behaviour.pop((7, 410))
    w.tg.press(mid, "m|7|410")
    wait(lambda: any(k == (7, 410) for k, _ in w.gl.merges), 15, "merge setelah retry")


def test_10_merge_connection_drop_but_merged(w):
    """Network drops during PUT /merge although GitLab merged: must report success, not failure."""
    w.gl.add_mr(420, "fix: drop", "2" * 40, files={"a.go": GO_OK})
    n = len(w.tg.sent)
    cek(w)
    mid = card(w, 420, n)

    def merged_then_drop(h, mr, body):
        mr["state"] = "merged"
        w.gl.merges.append(((7, 420), body))
        h._drop()
        return True
    w.gl.merge_behaviour[(7, 420)] = merged_then_drop
    w.tg.press(mid, "m|7|420")
    wait(lambda: "Merged" in w.tg.messages[mid]["text"], 30, "kartu merged walau koneksi putus")
    assert sum(1 for k, _ in w.gl.merges if k == (7, 420)) == 1


def test_11_unauthorized_user_cannot_merge(w):
    w.gl.add_mr(430, "fix: unauthorized", "3" * 40, files={"a.go": GO_OK})
    n = len(w.tg.sent)
    cek(w)
    mid = card(w, 430, n)
    w.tg.press(mid, "m|7|430", user=999)
    wait(lambda: any(a[1] == "Tidak diizinkan" for a in w.tg.answers), 15, "ditolak")
    time.sleep(1)
    assert not any(k == (7, 430) for k, _ in w.gl.merges)


def test_12_telegram_outage_card_is_retried(w):
    w.gl.add_mr(440, "fix: outage", "4" * 40, files={"a.go": GO_OK})
    base = w.tg.log.count("sendMessage")
    w.tg.fault("POST", "sendMessage", 502, times=40)  # Telegram down for every send attempt
    w.tg.say("/cek")
    wait(lambda: w.tg.log.count("sendMessage") - base >= 4, 60, "percobaan kirim")
    wait(lambda: db_status(w, "7:440") == "notify_failed", 60, "status notify_failed (kartu tidak hilang)")
    w.tg.faults.clear()
    n = len(w.tg.sent)
    cek(w)
    card(w, 440, n)


def test_13_telegram_429_and_html_fallback(w):
    w.gl.add_mr(450, "fix: rate limit", "5" * 40, files={"a.go": GO_OK})
    w.tg.fault("POST", "sendMessage", (429, 2), times=1)
    n = len(w.tg.sent)
    cek(w)
    card(w, 450, n)
    # Telegram rejects the HTML once -> message still arrives as plain text
    w.tg.reject_html_once = True
    n = len(w.tg.sent)
    w.tg.say("/status")
    mid = wait(lambda: w.tg.find("Menunggu keputusan", n), 20, "status plain text")
    assert w.tg.messages[mid]["parse_mode"] is None


def test_14_long_card_fits_telegram_limit(w):
    w.verdicts["feat: huge"] = {"verdict": "NEEDS_ATTENTION", "summary": "x" * 1400,
                                "findings": [{"severity": "minor", "title": "t" * 190, "detail": "<b>d</b> & " * 60,
                                              "file": "f.go"}] * 8}
    w.gl.add_mr(460, "feat: huge", "6" * 40, files={f"pkg/f{i}.go": GO_BAD for i in range(30)},
                description="## Changes\n" + "".join(f"- perubahan {i} " + "y" * 150 + "\n" for i in range(12)))
    n = len(w.tg.sent)
    cek(w)
    mid = card(w, 460, n)
    assert len(w.tg.messages[mid]["text"]) <= 4096
    assert not w.tg.rejected_html, w.tg.rejected_html[-1][0] + "\n" + w.tg.rejected_html[-1][1][-1500:]
    assert w.tg.messages[mid]["parse_mode"] == "HTML"


def test_15_gitlab_transient_500_is_retried(w):
    w.gl.add_mr(470, "fix: transient", "7" * 40, files={"a.go": GO_OK})
    w.gl.fault("GET", r"^/api/v4/merge_requests$", 502, times=2)
    n = len(w.tg.sent)
    cek(w)
    card(w, 470, n)


def test_16_closed_outside_and_unassigned(w):
    mid = card(w, 470)
    w.gl.mrs[(7, 470)]["state"] = "merged"
    cek(w)
    wait(lambda: "di luar MR Pilot" in w.tg.messages[mid]["text"], 15, "kartu merged di luar")
    w.gl.add_mr(480, "fix: reviewer removed", "8" * 40, files={"a.go": GO_OK})
    n = len(w.tg.sent)
    cek(w)
    mid2 = card(w, 480, n)
    w.gl.mrs[(7, 480)]["reviewers"] = [{"username": "someone.else"}]
    cek(w)
    wait(lambda: "tidak lagi reviewer" in w.tg.messages[mid2]["text"], 15, "kartu unassigned")
    assert not w.tg.buttons(mid2)


def test_17_teams_template_typo_and_teams_down(w):
    cfgp = os.path.join(w.data, "config.yaml")
    w.gl.add_mr(490, "fix: teams", "9" * 40, files={"a.go": GO_OK})
    w.teams.fault("POST", "flow", 500, times=5)
    n = len(w.tg.sent)
    cek(w)
    mid = card(w, 490, n)
    n = len(w.tg.sent)
    w.tg.press(mid, "m|7|490")
    wait(lambda: "Merged" in w.tg.messages[mid]["text"], 20, "merged meski Teams gagal")
    assert "gagal" in w.tg.messages[mid]["text"]
    wait(lambda: any("fix: teams" in t and "<b>" not in t and "MR baru" not in t for t in w.tg.texts(n)), 15, "teks Teams untuk di-copy")
    assert os.path.exists(cfgp)
    w.teams.faults.clear()


def login(w, password):
    """POST /login with a fresh client; returns (status, Location, Set-Cookie)."""
    anon = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(f"http://127.0.0.1:{w.port}/login", method="POST",
                                 data=urllib.parse.urlencode({"password": password}).encode())
    try:
        r = anon.open(req, timeout=15)
        return r.status, r.headers.get("Location"), r.headers.get("Set-Cookie")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location"), e.headers.get("Set-Cookie")


def test_18_dashboard_login_lockout_and_api(w):
    # right password works
    code, loc, cookie = login(w, PASSWORD)
    assert code in (302, 303) and loc == "/" and "mrp_session=" in cookie and "HttpOnly" in cookie
    assert "SameSite" in cookie
    w.session = {"Cookie": cookie.split(";")[0]}
    # wrong password x10 -> locked out, even with the right one
    for _ in range(10):
        code, loc, cookie = login(w, "salah")
        assert code in (302, 303) and loc == "/login?e=1" and not cookie
    code, loc, cookie = login(w, PASSWORD)
    assert loc == "/login?e=2" and not cookie, "harus terkunci"
    anon = urllib.request.build_opener(NoRedirect)
    assert http(w, "/api/summary", opener=anon)[0] == 401
    assert http(w, "/healthz", opener=anon)[0] == 200


def test_19_dashboard_data_sse_and_headers(w):
    hdr = w.session  # real cookie from the successful login in test_18 (lockout doesn't revoke sessions)
    code, body, headers = http(w, "/api/summary", headers=hdr)
    assert code == 200
    s = json.loads(body)
    assert s["kpi"]["merged_7d"] >= 4 and s["user"] == "nova.andriana"
    assert all(FakeTelegram.TOKEN not in json.dumps(ev) for ev in s["events"])
    code, body, headers = http(w, "/", headers=hdr)
    assert code == 200 and "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    q = json.loads(http(w, "/api/quality?days=abc", headers=hdr)[1])
    assert q["kpi"]["total"] > 0
    ai = json.loads(http(w, "/api/ai", headers=hdr)[1])
    assert {p["name"]: p for p in ai["providers"]}["ai-b"]["ready"]
    assert all("sk-proj" not in json.dumps(p) for p in ai["providers"])
    st = {r["provider"]: r for r in ai["stats"]["by_provider"]}
    assert st["openai"]["ok"] == 0 and st["ai-b"]["ok"] >= 1
    # invalid AI settings rejected
    code, body, _ = http(w, "/api/ai", "PUT", {"providers": {"ai-b": {"timeout": "abc"}}}, {**hdr, "X-MRPilot": "1"})
    assert code == 400 and "Timeout" in json.loads(body)["error"]
    # SSE delivers a new event live
    req = urllib.request.Request(f"http://127.0.0.1:{w.port}/api/stream", headers=hdr)
    stream = urllib.request.urlopen(req, timeout=20)
    assert b"hello" in stream.readline() + stream.readline()
    w.gl.add_mr(495, "fix: live event", "95" * 20, files={"a.go": GO_OK})
    w.tg.say("/cek")
    got = b""
    end = time.time() + 20
    while time.time() < end and b"event: activity" not in got:
        got += stream.readline()
    stream.close()
    assert b"event: activity" in got


def test_20_ai_garbage_json_falls_back(w):
    w.ai_a.reply = "Berikut review saya: {tidak valid"  # provider A answers, but with broken JSON
    w.gl.add_mr(500, "fix: garbage", "a5" * 20, files={"a.go": GO_OK})
    calls_b = len(w.ai_b.calls)
    n = len(w.tg.sent)
    cek(w)
    mid = card(w, 500, n)
    assert len(w.ai_b.calls) > calls_b
    assert "Review AI gagal" not in w.tg.messages[mid]["text"]


def test_21_secrets_never_logged(w):
    w.tg.fault("POST", "getUpdates", "drop", times=3)  # requests error message would contain the bot URL
    wait(lambda: all(f[3] == 0 for f in w.tg.faults), 70, "getUpdates putus 3x")
    w.tg.faults.clear()
    wait(lambda: "Telegram: getUpdates: tidak bisa terhubung" in w.app.log(), 15, "error Telegram tercatat")
    wait(lambda: "Telegram tersambung lagi" in w.app.log(), 70, "Telegram pulih")
    assert w.app.log().count("Telegram: getUpdates") == 1, "gangguan beruntun cukup dicatat sekali"
    cek(w)  # bot recovers after the outage
    log_all = w.app.log()
    files = [os.path.join(w.data, "logs", f) for f in os.listdir(os.path.join(w.data, "logs"))]
    for fp in files:
        with open(fp, encoding="utf-8", errors="replace") as f:
            log_all += f.read()
    for secret in (FakeTelegram.TOKEN, FakeGitLab.TOKEN, "SECRETsig123", "sk-proj-aaaaaaaaaaaaaaaaaaaa"):
        assert secret not in log_all, f"rahasia bocor di log: {secret[:8]}"


def test_22_graceful_stop_during_merge_finishes_merge(w):
    w.gl.add_mr(510, "fix: slow merge", "b5" * 20, files={"a.go": GO_OK})
    n = len(w.tg.sent)
    cek(w)
    mid = card(w, 510, n)
    w.gl.fault("PUT", r"/merge_requests/510/merge$", ("sleep", 4), times=1)
    w.tg.press(mid, "m|7|510")
    wait(lambda: any("/merge_requests/510/merge" in p for m_, p in w.gl.log if m_ == "PUT"), 15, "merge dimulai")
    w.app.signal(signal.SIGTERM)
    code = w.app.wait_exit(40)
    assert code == 0, w.app.log()[-2000:]
    assert any(k == (7, 510) for k, _ in w.gl.merges)
    assert "Merged" in w.tg.messages[mid]["text"], "merge harus selesai sebelum berhenti"


def test_23_restart_no_duplicates_and_kill_during_merge_recovers(w):
    cards_before = sum(1 for m in w.tg.sent if w.tg.buttons(m))
    comments_before = len(w.gl.commit_comments)
    w.app.start()
    wait(lambda: w.tg.texts().count(next(t for t in w.tg.texts() if "MR Pilot aktif" in t)) >= 2, 40, "start ke-2")
    cek(w)
    assert sum(1 for m in w.tg.sent if w.tg.buttons(m)) == cards_before, "tidak ada kartu duplikat"
    assert len(w.gl.commit_comments) == comments_before
    # kill -9 in the middle of a merge, GitLab finished it
    w.gl.add_mr(520, "fix: kill", "c5" * 20, files={"a.go": GO_OK})
    n = len(w.tg.sent)
    cek(w)
    mid = card(w, 520, n)

    def merge_and_hang(h, mr, body):
        mr["state"] = "merged"
        w.gl.merges.append(((7, 520), body))
        time.sleep(30)
        return h._json(200, dict(mr))
    w.gl.merge_behaviour[(7, 520)] = merge_and_hang
    w.tg.press(mid, "m|7|520")
    wait(lambda: any(k == (7, 520) for k, _ in w.gl.merges), 15, "merge berjalan")
    time.sleep(0.5)
    w.app.signal(signal.SIGKILL)
    w.app.wait_exit(20)
    w.gl.merge_behaviour.pop((7, 520))
    w.app.start()
    wait(lambda: "dipulihkan" in w.tg.messages[mid]["text"], 40, "pemulihan setelah kill")


def test_24_conflict_409_backoff_and_health(w):
    w.tg.conflict = True  # another MR Pilot polls the same bot
    wait(lambda: "dipakai MR Pilot lain" in w.app.log(), 60, "peringatan 409")
    p0 = w.tg.log.count("getUpdates")
    time.sleep(12)
    assert w.tg.log.count("getUpdates") - p0 <= 1, "harus backoff 30 detik, bukan spam"
    assert w.app.log().count("dipakai MR Pilot lain") == 1, "peringatan cukup sekali"
    w.tg.conflict = False
    n = len(w.tg.sent)
    w.tg.say("/cek")
    wait(lambda: w.tg.find("Selesai cek GitLab", n), 70, "bot pulih setelah konflik selesai")
    env = {**w.env, "MRP_HEALTH_MAX_AGE": "600"}
    r = subprocess.run([sys.executable, "-m", "mr_pilot", "health", "--config", os.path.join(w.data, "config.yaml")],
                       cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_25_invalid_config_exits_cleanly(w):
    bad = tempfile.mkdtemp()
    shutil.copy(os.path.join(ROOT, "config.example.yaml"), os.path.join(bad, "config.yaml"))
    with open(os.path.join(bad, ".env"), "w") as f:
        f.write("TELEGRAM_CHAT_ID=@channel\nGITLAB_URL=code.idas.id\n")
    r = subprocess.run([sys.executable, "-m", "mr_pilot", "run", "--config", os.path.join(bad, "config.yaml")],
                       cwd=ROOT, env=w.env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "Config tidak valid" in r.stderr and "Traceback" not in r.stderr


def test_26_dashboard_writes_and_bot_commands(w):
    hdr = {**w.session, "X-MRPilot": "1"}
    std_dir = os.path.join(w.data, "standards")
    rules_p = os.path.join(std_dir, "rules.yaml")
    # CSRF guard, path tricks, bad bodies
    assert http(w, "/api/standards/rules.yaml", "PUT", {"content": "rules: []"}, w.session)[0] == 403
    assert http(w, "/api/ai", "PUT", {"providers": {}}, {"X-MRPilot": "1"},
                opener=urllib.request.build_opener(NoRedirect))[0] == 401
    for bad in ("/api/standards/../x.md", "/api/standards/.hidden.md", "/api/standards/a%2Fb.md",
                "/api/standards/run.sh"):
        assert http(w, bad, "PUT", {"content": "x"}, hdr)[0] in (400, 404), bad
    assert not os.path.exists(os.path.join(w.data, "x.md")) and not os.path.exists(os.path.join(std_dir, ".hidden.md"))
    assert http(w, "/api/standards/rules.yaml", "PUT", {"content": "rules: [::"}, hdr)[0] == 400
    assert http(w, "/api/standards/rules.yaml", "PUT", {"content": 123}, hdr)[0] == 400
    req = urllib.request.Request(f"http://127.0.0.1:{w.port}/api/ai", method="PUT", data=b"[1,2]",
                                 headers={**hdr, "Content-Type": "application/json"})
    try:
        w.opener.open(req, timeout=10)
        raise AssertionError("list body harus ditolak")
    except urllib.error.HTTPError as ex:
        assert ex.code == 400
    # a new team rule saved from the dashboard is enforced on the next commit
    with open(rules_p, encoding="utf-8") as f:
        rules = f.read()
    rules += ("\n  - id: e2e-no-println\n    stacks: [\"*\"]\n    severity: warning\n"
              "    pattern: 'fmt\\.Println'\n    message: Pakai logger, bukan fmt.Println.\n")
    code, body, _ = http(w, "/api/standards/rules.yaml", "PUT", {"content": rules}, hdr)
    assert code == 200, body
    assert os.path.exists(rules_p + ".bak")
    assert os.stat(rules_p).st_mode & 0o044, "file standar harus tetap bisa dibaca dari host"
    t = json.loads(http(w, "/api/standards/test", "POST", {"path": "a.go", "code": "fmt.Println(1)"}, hdr)[1])
    assert any(v["rule"] == "e2e-no-println" for v in t["violations"]), t
    w.verdicts["fix: println"] = {"verdict": "NEEDS_ATTENTION"}
    w.gl.add_mr(530, "fix: println", "d5" * 20, files={"cmd/a.go": "+func main() {\n+\tfmt.Println(1)\n+}\n"})
    n = len(w.tg.sent)
    cek(w)
    mid = card(w, 530, n)
    wait(lambda: "e2e-no-println" in json.dumps(w.gl.commit_comments), 20, "warning aturan baru di commit")
    # valid AI change is saved
    code, body, _ = http(w, "/api/ai", "PUT", {"providers": {"ai-b": {"timeout": 90}}}, hdr)
    assert code == 200, body
    with open(os.path.join(w.data, "ai_overrides.json")) as f:
        assert json.load(f)["providers"]["ai-b"]["timeout"] == 90
    # MR turned into Draft after the card was sent: merge refused
    w.gl.mrs[(7, 530)]["draft"] = w.gl.mrs[(7, 530)]["work_in_progress"] = True
    n = len(w.tg.sent)
    w.tg.press(mid, "m|7|530")
    wait(lambda: w.tg.find("masih Draft", n), 15, "tolak merge Draft")
    assert not any(k == (7, 530) for k, _ in w.gl.merges)
    w.gl.mrs[(7, 530)]["draft"] = w.gl.mrs[(7, 530)]["work_in_progress"] = False
    # re-review button replaces the card
    n = len(w.tg.sent)
    w.tg.press(mid, "rr|7|530")
    new = card(w, 530, n)
    assert new != mid and "Review ulang" in w.tg.messages[new]["text"]
    wait(lambda: not w.tg.buttons(mid), 10, "kartu lama tanpa tombol")
    # needs confirmation -> Batal cancels
    n = len(w.tg.sent)
    w.tg.press(new, "m|7|530")
    conf = wait(lambda: w.tg.find("Yakin tetap merge", n), 15, "konfirmasi")
    w.tg.press(conf, w.tg.buttons(conf)["Batal"])
    wait(lambda: "dibatalkan" in w.tg.messages[conf]["text"], 15, "batal")
    assert not any(k == (7, 530) for k, _ in w.gl.merges)
    # reject without comment ("-")
    n = len(w.tg.sent)
    notes = len(w.gl.notes_by_me(530))
    w.tg.press(new, "x|7|530")
    prompt = wait(lambda: w.tg.find("Tulis komentar", n), 15, "prompt tolak")
    w.tg.say("-", reply_to=prompt)
    wait(lambda: "Ditolak" in w.tg.messages[new]["text"], 15, "kartu ditolak")
    assert len(w.gl.notes_by_me(530)) == notes
    assert db_status(w, "7:530") == "rejected"
    # unknown/forged callback data is harmless
    w.tg.press(new, "zz|7|530")
    w.tg.press(new, "m|x|y")
    w.tg.press(new, "m|7|99999")
    wait(lambda: sum(1 for a in w.tg.answers[-3:] if a[1]) == 3, 15, "jawaban untuk tombol aneh")
    # commands
    n = len(w.tg.sent)
    w.tg.say("/help")
    wait(lambda: w.tg.find("/status", n), 15, "/help")
    n = len(w.tg.sent)
    w.tg.say("/status")
    st = wait(lambda: w.tg.find("Menunggu keputusan", n) or w.tg.find("Tidak ada MR", n), 15, "/status")
    assert "!530" not in w.tg.messages[st]["text"], "MR yang ditolak tidak boleh ada di /status"
    assert w.app.alive()


def test_27_assignee_reviewer_added_later_and_draft(w):
    """Every MR where Nova becomes Reviewer OR Assignee reaches Telegram on the next poll."""
    # assignee only (not reviewer)
    w.gl.add_mr(540, "feat: assignee only", "e5" * 20, reviewers=(), assignees=("nova.andriana",),
                files={"a.go": GO_OK})
    # reviewer + assignee at once: one card, not two
    w.gl.add_mr(541, "feat: both roles", "e6" * 20, assignees=("nova.andriana",), files={"a.go": GO_OK})
    # someone else's MR: no card
    w.gl.add_mr(542, "feat: not mine", "e7" * 20, reviewers=("budi",), assignees=("budi",), files={"a.go": GO_OK})
    n = len(w.tg.sent)
    cek(w)
    c540 = card(w, 540, n)
    assert "Anda: Assignee" in w.tg.messages[c540]["text"]
    c541 = card(w, 541, n)
    assert "Anda: Reviewer, Assignee" in w.tg.messages[c541]["text"]
    assert sum(1 for m in w.tg.sent[n:] if "!541" in w.tg.messages[m]["text"] and w.tg.buttons(m)) == 1
    assert not any("!542" in t for t in w.tg.texts(n))
    # Nova gets added to 542 later as assignee -> card on the next poll
    w.gl.mrs[(7, 542)]["assignees"].append({"username": "nova.andriana"})
    n = len(w.tg.sent)
    cek(w)
    card(w, 542, n)
    # removed as assignee -> card closed; added back -> new card
    w.gl.mrs[(7, 540)]["assignees"] = []
    cek(w)
    wait(lambda: "tidak lagi reviewer" in w.tg.messages[c540]["text"], 15, "kartu ditutup saat di-unassign")
    w.gl.mrs[(7, 540)]["assignees"] = [{"username": "nova.andriana"}]
    n = len(w.tg.sent)
    cek(w)
    card(w, 540, n)
    # Draft is held back, then sent as soon as it is marked Ready
    w.gl.add_mr(543, "feat: draft dulu", "e8" * 20, draft=True, files={"a.go": GO_OK})
    n = len(w.tg.sent)
    cek(w)
    assert not any("!543" in t for t in w.tg.texts(n))
    w.gl.mrs[(7, 543)]["draft"] = w.gl.mrs[(7, 543)]["work_in_progress"] = False
    n = len(w.tg.sent)
    cek(w)
    card(w, 543, n)


def test_28_polls_on_its_own_and_migrates_old_config(w):
    """No /cek: the poll loop alone delivers the card (poll interval from config), and an old
    config.yaml (also_assigned_to_me: false, 2-minute poll) is upgraded on start."""
    cfgp = os.path.join(w.data, "config.yaml")
    with open(cfgp, encoding="utf-8") as f:
        s = f.read()
    s = re.sub(r"(?m)^(\s*)poll_interval_seconds:.*$", r"\1poll_interval_seconds: 120      # cek MR baru tiap 2 menit", s)
    s = re.sub(r"(?m)^(\s*)watch:.*$", r"\1also_assigned_to_me: false      # true = MR yang assignee-nya Anda juga ikut dicek", s)
    with open(cfgp, "w", encoding="utf-8") as f:
        f.write(s)
    w.app.signal(signal.SIGTERM)
    assert w.app.wait_exit(40) == 0
    w.env["GITLAB_POLL_SECONDS"] = "30"
    if MODE == "docker":
        w.app.env["GITLAB_POLL_SECONDS"] = "30"
    w.app.start()
    wait(lambda: "cek tiap 30s" in w.app.log(), 60, "start dengan poll 30 detik")
    assert "config.yaml (diperbarui" in w.app.log()
    with open(cfgp, encoding="utf-8") as f:
        s = f.read()
    assert "watch: [reviewer, assignee]" in s and "also_assigned_to_me" not in s
    assert os.path.exists(cfgp + ".bak")
    time.sleep(2)  # let the first poll after start finish
    w.gl.add_mr(550, "feat: tanpa cek manual", "f5" * 20, reviewers=(), assignees=("nova.andriana",),
                files={"a.go": GO_OK})
    n = len(w.tg.sent)
    mid = wait(lambda: next((m for m in w.tg.sent[n:] if "!550" in w.tg.messages[m]["text"]
                             and any("Merge" in b for b in w.tg.buttons(m))), None), 75, "kartu otomatis tanpa /cek")
    assert "Anda: Assignee" in w.tg.messages[mid]["text"]


def test_29_source_branch_kept_unless_chosen(w):
    """Merge keeps the source branch (even if the MR's own "Delete source branch" box is ticked);
    deleting happens only with the explicit "Merge + hapus branch" button, also via confirmation."""
    w.gl.add_mr(560, "fix: keep branch", "a6" * 20, delete_branch_checkbox=True, files={"a.go": GO_OK})
    w.gl.add_mr(561, "fix: hapus branch", "a7" * 20, files={"a.go": GO_OK})
    w.verdicts["fix: hapus lewat konfirmasi"] = {"verdict": "NEEDS_ATTENTION"}
    w.gl.add_mr(562, "fix: hapus lewat konfirmasi", "a8" * 20, files={"a.go": GO_OK})
    n = len(w.tg.sent)
    cek(w)
    c560, c561, c562 = card(w, 560, n), card(w, 561, n), card(w, 562, n)
    # 1) plain Merge -> branch kept, MR checkbox switched off first
    w.tg.press(c560, w.tg.buttons(c560)["✅ Merge"])
    wait(lambda: "Merged" in w.tg.messages[c560]["text"], 20, "merge 560")
    body = next(b for k, b in w.gl.merges if k == (7, 560))
    assert body["should_remove_source_branch"] is False
    assert w.gl.mrs[(7, 560)]["force_remove_source_branch"] is False
    assert "feat/560" not in w.gl.deleted_branches
    assert "dipertahankan" in w.tg.messages[c560]["text"]
    # 2) explicit "Merge + hapus branch"
    w.tg.press(c561, w.tg.buttons(c561)["🗑️ Merge + hapus branch"])
    wait(lambda: "Merged" in w.tg.messages[c561]["text"], 20, "merge 561")
    assert "feat/561" in w.gl.deleted_branches and "dihapus" in w.tg.messages[c561]["text"]
    # 3) delete choice survives the confirmation step
    n = len(w.tg.sent)
    w.tg.press(c562, w.tg.buttons(c562)["🗑️ Merge + hapus branch"])
    conf = wait(lambda: w.tg.find("Yakin tetap merge", n), 15, "konfirmasi 562")
    btn = next(b for b in w.tg.buttons(conf) if "hapus branch" in b)
    w.tg.press(conf, w.tg.buttons(conf)[btn])
    wait(lambda: "Merged" in w.tg.messages[c562]["text"], 20, "merge 562")
    assert "feat/562" in w.gl.deleted_branches


def test_30_smarter_review_context_evidence_and_second_pass(w):
    """The AI gets line-numbered diff + full file + commits + team standards; a finding quoting code that
    does not exist is marked unverified; the skeptical second pass removes a false positive."""
    title = "feat: review pintar"
    w.gl.add_mr(570, title, "b6" * 20, files={"internal/q/usecase.go": GO_BAD},
                commit_msg="feat(IDAS-77): kuota per user")
    seen = {}
    old_reply = w.ai_b.reply

    def reply(payload):
        system, user = payload["messages"][0]["content"], payload["messages"][1]["content"]
        if "reviewer kedua yang skeptis" in system:
            seen["verify"] = user
            return {"checks": [{"id": 0, "valid": True, "severity": "blocker"},
                               {"id": 1, "valid": False, "reason": "sudah ditangani"}]}
        if "Judul MR: " + title in user and "Checklist" in system:
            seen["system"], seen["user"] = system, user
            return {"verdict": "REQUEST_CHANGES", "risk": "high", "summary": "Ada SQL injection.",
                    "tests": "Tidak ada test untuk query baru.", "questions": ["Kenapa error repo.Find diabaikan?"],
                    "findings": [
                        {"severity": "blocker", "category": "security", "file": "internal/q/usecase.go", "line": 9,
                         "title": "SQL injection", "evidence": 'q := "SELECT * FROM t WHERE id=" + id',
                         "detail": "id dari user digabung ke query.", "suggestion": "Pakai placeholder ? / $1"},
                        {"severity": "major", "file": "internal/q/usecase.go", "line": 2, "title": "Error diabaikan",
                         "evidence": "data, _ := repo.Find(id)"},
                        {"severity": "major", "file": "internal/q/usecase.go", "line": 3, "title": "Panic di nil",
                         "evidence": "data.Items[0].Name"}]}
        return old_reply(payload) if callable(old_reply) else old_reply
    w.ai_b.reply = reply
    try:
        n = len(w.tg.sent)
        cek(w)
        mid = card(w, 570, n)
    finally:
        w.ai_b.reply = old_reply
    u = seen["user"]
    assert "angka kiri = nomor baris" in u and "    4 +\tq := " in u
    assert "isi lengkap setelah perubahan" in u and "feat(IDAS-77): kuota per user" in u
    assert ("internal/q/usecase.go", "b6" * 20) in w.gl.raw_requests       # file read at the MR's commit
    assert "Standar tim" in seen["system"] and "Checklist" in seen["system"]
    assert "SQL injection" in seen["verify"]
    t = w.tg.messages[mid]["text"]
    assert "SQL injection" in t and "usecase.go:4" in t                    # line corrected 9 -> 4
    assert "💡 Pakai placeholder" in t and "Risiko: tinggi" in t and "Request changes" in t
    assert "<b>Error diabaikan</b>" not in t                                # AI finding dropped by second pass
    assert "Panic di nil" in t and "belum terbukti" in t                    # quote not in code
    assert "Kenapa error repo.Find diabaikan?" in t and "🧪 Tidak ada test" in t


def test_31_logout_revokes_session(w):
    # this address is locked out since test_18; use the session obtained before the lockout
    hdr = dict(w.session)
    # "no-referrer" would make browsers send Origin: null on the logout form (seen in a real browser)
    assert http(w, "/", headers=hdr)[2]["Referrer-Policy"] == "same-origin"
    assert http(w, "/api/summary", headers=hdr)[0] == 200
    assert json.loads(http(w, "/api/summary", headers=hdr)[1])["auth"] is True
    # cross-site form post is refused
    req = urllib.request.Request(f"http://127.0.0.1:{w.port}/logout", data=b"", method="POST",
                                 headers={**hdr, "Origin": "http://evil.example"})
    try:
        urllib.request.build_opener(NoRedirect).open(req, timeout=10)
        raise AssertionError("logout lintas situs harus ditolak")
    except urllib.error.HTTPError as ex:
        assert ex.code == 403
    assert http(w, "/api/summary", headers=hdr)[0] == 200
    # real logout: cookie cleared, back to login, old token no longer works
    req = urllib.request.Request(f"http://127.0.0.1:{w.port}/logout", data=b"", method="POST",
                                 headers={**hdr, "Origin": f"http://127.0.0.1:{w.port}"})
    try:
        urllib.request.build_opener(NoRedirect).open(req, timeout=10)
        raise AssertionError("harus redirect")
    except urllib.error.HTTPError as ex:
        assert ex.code == 303 and ex.headers["Location"] == "/login?e=3"
        assert "Max-Age=0" in ex.headers["Set-Cookie"]
    assert http(w, "/api/summary", headers=hdr)[0] == 401, "token lama harus dicabut"


def test_32_reset_clears_history_keeps_keys(w):
    cfgp = os.path.join(w.data, "config.yaml")
    envp = os.path.join(w.data, ".env")
    with open(envp, "rb") as f:
        env_before = f.read()
    run = lambda *args, inp="": subprocess.run(  # noqa: E731
        [sys.executable, "-m", "mr_pilot", "reset", "--config", cfgp, *args], cwd=ROOT,
        env=w.env, input=inp, capture_output=True, text=True, timeout=60)
    assert db_status(w, "7:540") == "notified"
    # refuses while MR Pilot is running
    r = run("--yes")
    assert r.returncode == 1 and "masih berjalan" in r.stdout
    w.app.signal(signal.SIGTERM)
    assert w.app.wait_exit(40) == 0
    # cancel = nothing deleted
    r = run(inp="tidak\n")
    assert r.returncode == 1 and "Dibatalkan" in r.stdout and db_status(w, "7:540") == "notified"
    comments = len(w.gl.commit_comments)
    r = run(inp="RESET\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Riwayat MR" in r.stdout and "data/.env (token & API key)" in r.stdout
    con = sqlite3.connect(os.path.join(w.data, "mr_pilot.db"))
    try:
        assert [con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("mrs", "events", "violations", "ai_calls")] == [0, 0, 0, 0]
        assert con.execute("SELECT COUNT(*) FROM kv WHERE k LIKE 'cq_%'").fetchone()[0] > 0
    finally:
        con.close()
    assert not os.listdir(os.path.join(w.data, "logs")), "log lama harus terhapus"
    with open(envp, "rb") as f:
        assert f.read() == env_before, ".env (token) tidak boleh berubah"
    assert os.path.exists(cfgp) and os.path.isdir(os.path.join(w.data, "standards"))
    # start again: still-open assigned MRs come back as fresh cards, no duplicate GitLab comments
    n = len(w.tg.sent)
    w.app.start()
    wait(lambda: w.tg.find("MR Pilot aktif", n), 40, "start setelah reset")
    cek(w)
    card(w, 540, n)
    assert len(w.gl.commit_comments) == comments, "komentar commit tidak boleh diposting ulang"
