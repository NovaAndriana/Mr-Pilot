"""Free-tier usage limits (quota) and the natural GitLab comment voice."""
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mr_pilot import voice  # noqa: E402
from mr_pilot.ai import AIManager  # noqa: E402
from mr_pilot.config import DEFAULTS, _merge  # noqa: E402
from mr_pilot.quota import QuotaGuard, fmt_num, headers_meta, limits_for, parse_reset  # noqa: E402
from mr_pilot.standards import Standards  # noqa: E402
from mr_pilot.store import Store  # noqa: E402


# ------------------------------------------------------------------ limits
def test_limits_free_defaults_and_custom():
    assert limits_for({"type": "groq"})["daily_requests"] == 1000
    assert limits_for({"type": "groq"})["source"] == "free"
    orf = limits_for({"type": "openrouter", "model": "meta-llama/llama-3.3-70b-instruct:free"})
    assert orf["daily_requests"] == 50 and orf["rpm"] == 20
    assert limits_for({"type": "openrouter", "model": "anthropic/claude"})["daily_requests"] is None
    c = limits_for({"type": "groq", "daily_requests": "10", "daily_tokens": 0, "rpm": ""})
    assert c["daily_requests"] == 10 and c["daily_tokens"] is None and c["rpm"] == 30 and c["source"] == "custom"
    assert limits_for({"type": "anthropic"}) == {"daily_requests": None, "daily_tokens": None, "rpm": None,
                                                 "source": None}


def test_parse_reset_and_headers():
    assert parse_reset("2m59.5s") == 179.5 and parse_reset("6ms") == 0.006 and parse_reset("17") == 17
    assert parse_reset("1h") == 3600 and parse_reset("") is None and parse_reset("soon") is None
    m = headers_meta({"X-RateLimit-Remaining-Requests": "12", "x-ratelimit-remaining-tokens": "900",
                      "Retry-After": "30"})
    assert m["remaining_requests"] == 12 and m["remaining_tokens"] == 900 and m["retry_after"] == 30
    assert fmt_num(200000) == "200rb" and fmt_num(1500) == "1.5rb" and fmt_num(2_000_000) == "2jt"


def _shift(store, sql, params):
    with store.lock:
        store.db.execute(sql, params)
        store.db.commit()


def _calls(store, name, n, tokens=100, ago=10):
    for _ in range(n):
        store.add_ai_call(name, "review", True, 0.1, "", tokens_in=tokens, tokens_out=0)
    if ago:
        _shift(store, "UPDATE ai_calls SET ts = ts - ? WHERE provider=?", (ago, name))


def test_state_levels_window_and_cooldown():
    store = Store(":memory:")
    q = QuotaGuard(store)
    p = {"name": "g", "type": "groq", "daily_requests": 10}
    assert q.state(p)["level"] == "ok"
    _calls(store, "g", 8)
    st = q.state(p)
    assert st["level"] == "low" and round(st["ratio"], 1) == 0.8 and st["usage"]["tokens"] == 800
    _calls(store, "g", 2)
    assert q.state(p)["level"] == "exhausted" and "harian" in q.state(p)["why"]
    _shift(store, "UPDATE ai_calls SET ts = ts - 90000", ())  # older than 24h -> not counted any more
    assert q.state(p)["level"] == "ok"
    # provider says 429 with retry-after -> cooldown
    until = q.record_failure("g", 429, "rate limit", retry_after=120)
    assert until and q.state(p)["level"] == "exhausted" and q.cooldown("g")["reason"].startswith("terlalu")
    assert q.cooldown("g", now=until + 1) is None and q.state(p)["level"] == "ok"
    assert q.record_failure("g", 500, "boom") is None


def test_daily_quota_message_alerts_once():
    store, sent = Store(":memory:"), []
    q = QuotaGuard(store, sent.append)
    q.record_failure("or", 429, "Rate limit exceeded: free-models-per-day")
    q.record_failure("or", 429, "Rate limit exceeded: free-models-per-day")
    assert len(sent) == 1 and "or" in sent[0] and "kuota" in sent[0]
    assert q.cooldown("or")["until"] - time.time() > 5 * 3600


def test_minute_wait():
    store = Store(":memory:")
    q = QuotaGuard(store)
    p = {"name": "g", "type": "groq", "rpm": 3}
    _calls(store, "g", 2, ago=0)
    assert q.minute_wait(p) == 0
    _calls(store, "g", 1, ago=0)
    assert 0 < q.minute_wait(p) <= 60


# --------------------------------------------------------- AIManager + HTTP
class API(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, body, headers=None):
        b = json.dumps(body).encode()
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        if self.path.startswith("/limited"):
            return self._json(429, {"error": {"message": "Rate limit reached: requests per day (RPD)"}},
                              {"retry-after": "7200"})
        return self._json(200, {"choices": [{"message": {"content": '{"ok": true}'}}],
                                "usage": {"prompt_tokens": 1200, "completion_tokens": 300}},
                          {"x-ratelimit-remaining-requests": "999"})


def _mgr(base, tmp, **a_extra):
    cfg = _merge(DEFAULTS, {})
    cfg["ai"]["providers"] = {"a": {"type": "openrouter", "api_key": "x", "base_url": base + "/limited/v1", **a_extra},
                              "b": {"type": "openrouter", "api_key": "y", "base_url": base + "/v1"}}
    cfg["ai"]["order"] = ["a", "b"]
    cfg["ai"]["tasks"] = {}
    cfg["_base_dir"] = tmp
    store = Store(":memory:")
    return AIManager(cfg, store), store


def test_manager_skips_exhausted_logs_tokens_and_alerts():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), API)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        ai, store = _mgr(base, tempfile.mkdtemp())
        sent = []
        ai.notify = sent.append
        assert ai.complete("s", "u")[1] == "b"  # a -> 429 per day -> cooldown, b answers
        assert ai.quota.cooldown("a") and any("a</b>" in t for t in sent)
        n_before = len(store.query("SELECT * FROM ai_calls WHERE provider='a'"))
        assert ai.complete("s", "u")[1] == "b"
        assert len(store.query("SELECT * FROM ai_calls WHERE provider='a'")) == n_before, "a dilewati, tidak dipanggil"
        st = ai.quota.state(ai.providers["b"])
        assert st["usage"]["tokens"] == 3000 and st["reported"]["remaining_requests"] == 999
        d = {p["name"]: p for p in ai.describe()["providers"]}
        assert d["a"]["quota"]["level"] == "exhausted" and d["b"]["quota"]["level"] == "ok"
        rep = ai.quota_report()
        assert "a" in rep and "b" in rep
        stats = {r["provider"]: r for r in store.ai_stats()["by_provider"]}
        assert stats["b"]["tokens_in"] == 2400 and stats["b"]["tokens_out"] == 600
    finally:
        srv.shutdown()


def test_economy_mode_and_daily_limit_setting():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), API)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        tmp = tempfile.mkdtemp()
        ai, store = _mgr(base, tmp)
        ai.save_overrides({"providers": {"a": {"enabled": False}, "b": {"daily_requests": 5}}})
        sent = []
        ai.notify = sent.append
        assert not ai.economy("review")
        _calls(store, "b", 4)
        assert ai.economy("review"), "80% terpakai -> mode hemat"
        ai.complete("s", "u")
        assert any("80%" in t and "Mode hemat" in t for t in sent)
        try:
            ai.complete("s", "u")
            raise AssertionError("harus gagal: kuota habis")
        except Exception as ex:
            assert "batas harian" in str(ex)
        assert any("Semua provider AI habis" in t for t in sent)
        assert ai.economy("review")
    finally:
        srv.shutdown()


# -------------------------------------------------------------------- voice
TELLS = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]|<sub>|\*\*Code Standard|Saran review|otomatis|\bAI\b|—")


def test_standard_comment_natural_and_detailed():
    v = {"rule": "go-sql-concat", "severity": "error", "path": "a.go", "line": 3, "source": "rule",
         "message": "Query SQL di sini disusun dengan menggabungkan string, jadi rawan SQL injection.",
         "why": "Isi seperti `1 OR 1=1` ikut dieksekusi.", "fix": "Pakai parameter query.",
         "snippet": 'q := "SELECT " + id'}
    inline = voice.standard_comment(v)
    assert "go-sql-concat" not in inline and not TELLS.search(inline)
    assert "rawan SQL injection" in inline and "1 OR 1=1" in inline and "parameter query" in inline
    assert "merge" in inline.split("\n\n")[-1], "error ditutup dengan permintaan perbaikan"
    outside = voice.standard_comment(v, inline=False)
    assert outside.startswith("Di `a.go:3`: query SQL") and 'q := "SELECT " + id' in outside
    nit = voice.standard_comment({**v, "severity": "info", "message": "TODO ini belum ada nomor tiketnya."})
    assert nit.startswith("nit: todo ini") or nit.startswith("nit: TODO"), nit
    assert voice.standard_comment(v, footer="Tim QA").endswith("<sub>Tim QA</sub>")


def test_finding_comment_and_humanize():
    f = {"severity": "blocker", "file": "a.go", "line": 3, "title": "SQL injection",
         "detail": "Catatan: `id` dari query string langsung digabung — jadi `?id=1 OR 1=1` membaca semua baris 🚨",
         "suggestion": "Pakai placeholder `?`.", "evidence": "x", "fix_code": "y"}
    body = voice.finding_comment(f)
    assert not TELLS.search(body) and "Catatan:" not in body and "SQL injection" not in body
    assert "```suggestion:-0+0\ny\n```" in body and "?id=1 OR 1=1" in body
    assert voice.finding_comment({**f, "severity": "minor"}).startswith("nit: ")
    assert "Kira-kira seperti ini" in voice.finding_comment(f, inline=False)
    assert voice.finding_comment(f, inline=False).startswith("Di `a.go:3`:")


def test_summary_note_groups_and_clean_update():
    vs = [{"severity": "error", "path": "a.go", "line": 3, "message": "SQL injection."},
          {"severity": "warning", "path": "b.ts", "line": 9, "message": "Masih ada console.log."},
          {"severity": "info", "path": "", "line": None, "message": "Checklist belum dicentang."}]
    note = voice.summary_note("abcdef1234", vs)
    assert "`abcdef12`" in note and note.index("Perlu diperbaiki") < note.index("Sebaiknya") < note.index("Catatan kecil")
    assert not TELLS.search(note) and "|" not in note
    assert voice.summary_note("abc", [], had_before=False) is None
    assert "sudah beres" in voice.summary_note("abc", [], had_before=True)


def test_pipeline_comment():
    c = voice.pipeline_comment({"name": "test", "stage": "test", "web_url": "http://gl/j/1"},
                               {"cause": "TestX gagal.", "where": "x_test.go:4", "fix": "Perbaiki hitungan.",
                                "code_problem": True})
    assert "gagal di job `test`" in c and "x_test.go:4" in c and "http://gl/j/1" in c and not TELLS.search(c)


# --------------------------------------------------------------- rules file
def test_bundled_rules_have_why_and_fix_and_reach_violations():
    std = Standards({"standards_dir": os.path.join(ROOT, "standards"), "rules_file": "rules.yaml",
                     "stacks": {"go": {"paths": ["**/*.go", "*.go"]}}})
    assert std.rules and all(r["why"] and r["fix"] for r in std.rules)
    vs = std.check_file_diff("internal/a.go", '@@ -1,0 +1,1 @@\n+\tq := "SELECT * FROM t WHERE id=" + id\n')
    v = [x for x in vs if x["rule"] == "go-sql-concat"][0]
    assert "SQL injection" in v["message"] and v["why"] and "?" in v["fix"]


def test_old_rules_and_footer_are_migrated(tmp_path):
    from mr_pilot.setup_wizard import _OLD_RULES_SHA, migrate_config, migrate_rules
    import subprocess
    try:
        old = subprocess.run(["git", "-C", ROOT, "show", "c39a39f:standards/rules.yaml"], capture_output=True,
                             check=True).stdout
    except Exception:
        old = None
    std = tmp_path / "standards"
    std.mkdir()
    if old is not None:
        assert hashlib.sha256(old).hexdigest() in _OLD_RULES_SHA
        (std / "rules.yaml").write_bytes(old.replace(b"\n", b"\r\n"))  # Windows checkout
        assert migrate_rules(str(std))
        assert b"why:" in (std / "rules.yaml").read_bytes() and (std / "rules.yaml.bak").exists()
    (std / "rules.yaml").write_text("rules: []  # punya saya\n")
    assert not migrate_rules(str(std)), "file yang sudah diubah user tidak disentuh"
    cfg = tmp_path / "config.yaml"
    shutil.copy(os.path.join(ROOT, "config.example.yaml"), cfg)
    txt = cfg.read_text().replace('comment_footer: ""            # teks kecil di bawah komentar GitLab; kosong = tanpa footer',
                                  'comment_footer: "Pengecekan standar kode otomatis"')
    cfg.write_text(txt)
    assert migrate_config(str(cfg)) >= 1 and 'comment_footer: ""' in cfg.read_text()
