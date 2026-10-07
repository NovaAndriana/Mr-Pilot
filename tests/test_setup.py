"""setup wizard (non-interactive), doctor, config env defaults, setup-ci helpers."""
import base64
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import mr_pilot.setup_wizard as sw  # noqa: E402
from mr_pilot.config import load_config  # noqa: E402
from mr_pilot.setup_ci import github_set_secrets, gitlab_set_variables, parse_remote  # noqa: E402


class Fake(BaseHTTPRequestHandler):
    calls = []
    gh_key = None

    def log_message(self, *a):
        pass

    def _j(self, code, body):
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def do_GET(self):
        if self.path == "/api/v4/user":
            ok = self.headers.get("PRIVATE-TOKEN") == "glpat-good"
            return self._j(200 if ok else 401, {"username": "nova.andriana"} if ok else {"message": "401"})
        if self.path.endswith("/actions/secrets/public-key"):
            return self._j(200, {"key_id": "kid", "key": Fake.gh_key})
        self._j(404, {})

    def do_PUT(self):
        body = self._body()
        Fake.calls.append(("PUT", self.path, body))
        if "/variables/" in self.path:
            return self._j(404 if "DEPLOY_HOST" in self.path else 200, {"message": "404 Variable Not Found"})
        if "/actions/secrets/" in self.path:
            return self._j(201, {})
        self._j(404, {})

    def do_POST(self):
        Fake.calls.append(("POST", self.path, self._body()))
        self._j(201, {})


def serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_env_roundtrip_keeps_comments():
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, ".env")
    with open(p, "w") as f:
        f.write("# komentar\nGITLAB_TOKEN=old\nOTHER=1\n")
    sw.write_env(p, {"GITLAB_TOKEN": "new", "TEAMS_FLOW_URL": "https://x/y?a=1", "NOTE": "dua kata"})
    txt = open(p).read()
    assert txt.startswith("# komentar\nGITLAB_TOKEN=new\nOTHER=1")
    assert sw.read_env(p) == {"GITLAB_TOKEN": "new", "OTHER": "1", "TEAMS_FLOW_URL": "https://x/y?a=1", "NOTE": "dua kata"}


def test_setup_non_interactive_then_config_and_doctor(monkeypatch, capsys):
    srv, base = serve()
    tmp = tempfile.mkdtemp()
    try:
        for k, v in {"GITLAB_URL": base, "GITLAB_TOKEN": "glpat-good", "TELEGRAM_BOT_TOKEN": "123:abc",
                     "TELEGRAM_CHAT_ID": "4242", "GROQ_API_KEY": "gsk_x", "REVIEW_MODE": "llm",
                     "CODE_QUALITY_ENABLED": "true"}.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(sw, "check_telegram", lambda t: (True, "@mrpilot_bot"))
        monkeypatch.setattr("mr_pilot.ai.AIManager.test", lambda self, n: {"ok": True, "ms": 5})
        assert sw.run_setup(tmp, interactive=False) == 0
        out = capsys.readouterr().out
        assert "Terhubung sebagai @nova.andriana" in out and "Password dashboard dibuat" in out
        assert os.path.exists(os.path.join(tmp, "config.yaml")) and os.path.isdir(os.path.join(tmp, "standards"))
        env = sw.read_env(os.path.join(tmp, ".env"))
        assert env["GITLAB_TOKEN"] == "glpat-good" and env["TEAMS_MODE"] == "telegram_copy"
        assert len(env["DASHBOARD_PASSWORD"]) >= 12
        # fresh process view: only the files on disk
        for k in list(os.environ):
            if k in env:
                monkeypatch.delenv(k, raising=False)
        cfg = load_config(os.path.join(tmp, "config.yaml"))
        assert cfg["gitlab"]["url"] == base and cfg["gitlab"]["verify_ssl"] is True
        assert cfg["telegram"]["chat_id"] == 4242 and cfg["review"]["mode"] == "llm"
        assert cfg["code_quality"]["enabled"] is True and cfg["teams"]["mode"] == "telegram_copy"
        assert cfg["ai"]["providers"]["gemini"]["model"] == "gemini-3.8-flash"
        assert cfg["code_quality"]["standards_dir"] == os.path.join(tmp, "standards")
        from mr_pilot.ai import AIManager
        assert AIManager(cfg).available() == ["groq"]
        assert sw.run_doctor(cfg, test_ai=False) == 0
    finally:
        srv.shutdown()


def test_doctor_reports_bad_token(monkeypatch, capsys):
    srv, base = serve()
    tmp = tempfile.mkdtemp()
    try:
        sw.bootstrap_data_dir(tmp)
        sw.write_env(os.path.join(tmp, ".env"), {"GITLAB_URL": base, "GITLAB_TOKEN": "bad", "TELEGRAM_BOT_TOKEN": "",
                                                 "REVIEW_MODE": "bot", "DASHBOARD_PASSWORD": "x" * 12})
        for k in ("GITLAB_URL", "GITLAB_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "REVIEW_MODE", "GROQ_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        cfg = load_config(os.path.join(tmp, "config.yaml"))
        assert sw.run_doctor(cfg, test_ai=False) == 1
        out = capsys.readouterr().out
        assert "token salah" in out and "TELEGRAM_BOT_TOKEN kosong" in out
    finally:
        srv.shutdown()


def test_parse_remote():
    assert parse_remote("git@github.com:nova/mr-pilot.git") == ("github", "github.com", "nova/mr-pilot")
    assert parse_remote("https://github.com/Nova/MR-Pilot") == ("github", "github.com", "Nova/MR-Pilot")
    assert parse_remote("https://oauth2:tok@code.idas.id/tools/mr-pilot.git") == ("gitlab", "code.idas.id", "tools/mr-pilot")
    assert parse_remote("ssh://git@code.idas.id:2222/grp/sub/mr-pilot.git") == ("gitlab", "code.idas.id", "grp/sub/mr-pilot")


def test_gitlab_variables_create_or_update():
    srv, base = serve()
    Fake.calls.clear()
    try:
        gitlab_set_variables(base, "tools/mr-pilot", "t", {"DEPLOY_HOST": "10.0.0.5", "DEPLOY_SSH_KEY": "-----BEGIN"})
        kinds = [(m, p.rsplit("/", 1)[-1]) for m, p, _ in Fake.calls]
        assert kinds == [("PUT", "DEPLOY_HOST"), ("POST", "variables"), ("PUT", "DEPLOY_SSH_KEY")]
        assert "/projects/tools%2Fmr-pilot/variables" in Fake.calls[1][1]
        assert parse_qs(Fake.calls[2][2].decode())["variable_type"] == ["file"]
    finally:
        srv.shutdown()


def test_github_secrets_are_sealed(monkeypatch):
    from nacl import encoding, public
    sk = public.PrivateKey.generate()
    Fake.gh_key = sk.public_key.encode(encoding.Base64Encoder()).decode()
    srv, base = serve()
    Fake.calls.clear()
    import mr_pilot.setup_ci as sc
    real_get, real_put = sc.requests.get, sc.requests.put
    monkeypatch.setattr(sc.requests, "get", lambda url, **k: real_get(url.replace("https://api.github.com", base), **k))
    monkeypatch.setattr(sc.requests, "put", lambda url, **k: real_put(url.replace("https://api.github.com", base), **k))
    try:
        github_set_secrets("nova/mr-pilot", "ghp_x", {"DEPLOY_HOST": "10.0.0.5"})
        m, path, body = Fake.calls[-1]
        assert path == "/repos/nova/mr-pilot/actions/secrets/DEPLOY_HOST"
        enc = base64.b64decode(json.loads(body)["encrypted_value"])
        assert public.SealedBox(sk).decrypt(enc) == b"10.0.0.5"
    finally:
        srv.shutdown()
