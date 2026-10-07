"""AI provider manager tests (fake HTTP server + fake `claude` CLI)."""
import json
import os
import stat
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mr_pilot.ai import AIManager  # noqa: E402
from mr_pilot.config import DEFAULTS, _merge  # noqa: E402
from mr_pilot.store import Store  # noqa: E402


class FakeAPI(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def _json(self, code, body):
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.endswith("/models"):
            if "gemini" in self.path:
                return self._json(200, {"models": [{"name": "models/gemini-x", "supportedGenerationMethods": ["generateContent"]},
                                                   {"name": "models/embed", "supportedGenerationMethods": ["embedContent"]}]})
            return self._json(200, {"data": [{"id": "m-b"}, {"id": "m-a"}]})
        self._json(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeAPI.seen.append((self.path, dict(self.headers), body))
        if self.path.startswith("/down"):
            return self._json(503, {"error": {"message": "overloaded"}})
        if self.path.endswith("/chat/completions"):
            return self._json(200, {"choices": [{"message": {"content": '{"ok": true, "via": "%s"}' % body["model"]}}]})
        if self.path.endswith("/v1/messages"):
            return self._json(200, {"content": [{"type": "text", "text": '{"ok": true, "via": "anthropic"}'}]})
        if ":generateContent" in self.path:
            return self._json(200, {"candidates": [{"content": {"parts": [{"text": '{"ok": true, "via": "gemini"}'}]}}]})
        self._json(404, {})


def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def cfg_with(providers, order, tmp, tasks=None):
    cfg = _merge(DEFAULTS, {})
    cfg["ai"]["providers"] = providers
    cfg["ai"]["order"] = order
    cfg["ai"]["tasks"] = tasks or {}
    cfg["_base_dir"] = tmp
    return cfg


def test_fallback_order_and_usage_log():
    srv, base = server()
    tmp = tempfile.mkdtemp()
    try:
        cfg = cfg_with({
            "groq": {"type": "groq", "api_key": "g", "base_url": base + "/down", "model": "llama"},
            "openrouter": {"type": "openrouter", "api_key": "o", "base_url": base + "/v1", "model": "anthropic/x"},
            "gemini": {"type": "gemini", "enabled": "auto"},  # no key -> skipped
        }, ["gemini", "groq", "openrouter"], tmp)
        store = Store(":memory:")
        ai = AIManager(cfg, store)
        assert ai.available() == ["groq", "openrouter"]
        text, prov = ai.complete("sys", "user")
        assert prov == "openrouter" and json.loads(text)["via"] == "anthropic/x"
        path, headers, body = FakeAPI.seen[-1]
        assert headers["Authorization"] == "Bearer o" and headers["X-Title"] == "MR Pilot"
        assert body["messages"][0] == {"role": "system", "content": "sys"}
        st = {r["provider"]: r for r in store.ai_stats()["by_provider"]}
        assert st["groq"]["ok"] == 0 and st["openrouter"]["ok"] == 1
        assert "overloaded" in store.ai_stats()["recent_errors"][0]["error"]
    finally:
        srv.shutdown()


def test_anthropic_gemini_and_task_routing():
    srv, base = server()
    tmp = tempfile.mkdtemp()
    try:
        cfg = cfg_with({"anthropic": {"type": "anthropic", "api_key": "a", "base_url": base},
                        "gemini": {"type": "gemini", "api_key": "k", "base_url": base + "/gemini", "model": "gemini-x"}},
                       ["anthropic", "gemini"], tmp, tasks={"standards": "gemini"})
        ai = AIManager(cfg)
        assert ai.complete("s", "u", "review")[1] == "anthropic"
        assert ai.complete("s", "u", "standards")[1] == "gemini"
        path, headers, body = FakeAPI.seen[-1]
        assert path == "/gemini/models/gemini-x:generateContent" and headers["x-goog-api-key"] == "k"
        assert body["systemInstruction"]["parts"][0]["text"] == "s"
        assert ai.list_models("gemini") == ["gemini-x"]
        assert ai.list_models("anthropic") == ["m-b", "m-a"]
    finally:
        srv.shutdown()


def test_overrides_from_dashboard_mask_keys_and_persist():
    srv, base = server()
    tmp = tempfile.mkdtemp()
    try:
        cfg = cfg_with({"groq": {"type": "groq", "enabled": "auto"},
                        "local": {"type": "local", "enabled": "auto", "base_url": base + "/v1"}}, ["groq", "local"], tmp)
        ai = AIManager(cfg)
        d = {p["name"]: p for p in ai.describe()["providers"]}
        assert not d["groq"]["ready"] and d["groq"]["reason"] == "API key belum diisi"
        assert not d["local"]["enabled"]  # local auto = off until enabled
        ai.save_overrides({"providers": {"groq": {"api_key": "gsk_secret_1234", "model": "openai/gpt-oss-20b"},
                                         "local": {"enabled": True, "model": "qwen"}},
                           "order": ["local", "groq"], "tasks": {"review": "groq", "bogus": "x"}})
        d = {p["name"]: p for p in ai.describe()["providers"]}
        assert d["groq"]["key_set"] and d["groq"]["key_hint"] == "…1234" and "api_key" not in d["groq"]
        assert d["local"]["enabled"] and ai.order == ["local", "groq"] and ai.tasks == {"review": "groq"}
        assert ai.complete("s", "u", "x")[1] == "local"
        # a second manager (e.g. after restart) reads the same overrides file
        ai2 = AIManager(cfg)
        assert ai2.providers["groq"]["api_key"] == "gsk_secret_1234" and ai2.chain("review")[0] == "groq"
        ai2.save_overrides({"providers": {"groq": {"clear_key": True}}})
        assert not ai2.providers["groq"].get("api_key")
        # add a custom provider (second OpenRouter account)
        ai2.save_overrides({"providers": {"or-kantor": {"type": "openrouter", "api_key": "x", "base_url": base + "/v1"}}})
        assert "or-kantor" in ai2.order and ai2.test("or-kantor")["ok"]
    finally:
        srv.shutdown()


def test_claude_code_cli(monkeypatch):
    tmp = tempfile.mkdtemp()
    fake = os.path.join(tmp, "claude")
    with open(fake, "w") as f:
        f.write("#!/usr/bin/env python3\nimport sys, json, os\n"
                "data = sys.stdin.read()\n"
                "ok = '=== INSTRUKSI ===' in data and '--output-format' in sys.argv and 'dontAsk' in sys.argv\n"
                "print(json.dumps({'type': 'result', 'is_error': not ok, 'result': json.dumps({'argv': sys.argv[1:], "
                "'tok': os.environ.get('CLAUDE_CODE_OAUTH_TOKEN'), 'key': os.environ.get('ANTHROPIC_API_KEY')})}))\n")
    os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-should-be-removed")
    cfg = cfg_with({"cc": {"type": "claude_code", "cli_path": fake, "api_key": "sk-ant-oat01-token", "model": "opus"}},
                   ["cc"], tmp)
    ai = AIManager(cfg)
    text, prov = ai.complete("instruksi", "diff")
    out = json.loads(text)
    assert prov == "cc" and out["tok"] == "sk-ant-oat01-token" and out["key"] is None
    assert "--bare" not in out["argv"] and out["argv"][out["argv"].index("--model") + 1] == "opus"
    # with a Console API key -> bare mode + ANTHROPIC_API_KEY
    ai.save_overrides({"providers": {"cc": {"api_key": "sk-ant-api03-xyz"}}})
    out = json.loads(ai.complete("i", "d")[0])
    assert "--bare" in out["argv"] and out["key"] == "sk-ant-api03-xyz"
