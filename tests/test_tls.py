"""Real TLS: a local HTTPS 'GitLab' signed by a private CA (like a company CA / SSL inspection)."""
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mr_pilot.gitlab_api import GitLab  # noqa: E402
from mr_pilot.util import install_extra_cas, ssl_hint  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="openssl CLI tidak ada")


def _run(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    d = tmp_path_factory.mktemp("pki")
    _run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.crt", "-days", "2",
         "-subj", "/CN=IDAS Test Root CA", "-addext", "basicConstraints=critical,CA:TRUE",
         "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-addext", "subjectKeyIdentifier=hash", cwd=d)
    _run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "srv.key", "-out", "srv.csr", "-subj", "/CN=localhost",
         cwd=d)
    (d / "ext.cnf").write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=CA:FALSE\n"
                               "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
                               "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n")
    _run("x509", "-req", "-in", "srv.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial", "-out",
         "srv.crt", "-days", "2", "-extfile", "ext.cnf", cwd=d)
    _run("x509", "-in", "ca.crt", "-outform", "DER", "-out", "ca.cer", cwd=d)  # Windows export format

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps({"username": "nova.andriana"} if self.path.startswith("/api/v4/user") else []).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(d / "srv.crt", d / "srv.key")
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"dir": d, "url": f"https://localhost:{srv.server_address[1]}"}
    srv.shutdown()


@pytest.fixture
def clean_env(monkeypatch):
    for k in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "CURL_CA_BUNDLE"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _gl(url, verify=True):
    gl = GitLab(url, "glpat-x", verify=verify, timeout=5)
    gl.s.mount("https://", requests.adapters.HTTPAdapter())  # no retry backoff in tests
    return gl


def test_untrusted_ca_fails_with_clear_hint(pki, clean_env):
    with pytest.raises(requests.exceptions.SSLError) as ei:
        _gl(pki["url"]).me()
    assert "trust-cert" in ssl_hint(ei.value) and "localhost" in ssl_hint(ei.value)


@pytest.mark.parametrize("name", ["idas-ca.crt", "idas-ca.cer"])
def test_cert_in_data_certs_is_trusted(pki, clean_env, tmp_path, name):
    (tmp_path / "certs").mkdir()
    shutil.copy(pki["dir"] / ("ca.crt" if name.endswith(".crt") else "ca.cer"), tmp_path / "certs" / name)
    (tmp_path / "certs" / "junk.pem").write_text("bukan sertifikat")
    bundle, used, skipped = install_extra_cas(str(tmp_path))
    assert used == [name] and skipped == ["junk.pem"] and os.environ["REQUESTS_CA_BUNDLE"] == bundle
    assert _gl(pki["url"]).me()["username"] == "nova.andriana"
    with open(os.environ["NODE_EXTRA_CA_CERTS"]) as f:
        assert f.read().count("BEGIN CERTIFICATE") == 1
    # public sites keep working: the public roots are still in the bundle
    assert open(bundle).read().count("BEGIN CERTIFICATE") > 50


def test_no_certs_dir_changes_nothing(clean_env, tmp_path):
    assert install_extra_cas(str(tmp_path)) == (None, [], [])
    assert "REQUESTS_CA_BUNDLE" not in os.environ


def test_verify_false_is_honoured_even_with_ca_bundle_env(pki, clean_env, tmp_path):
    # requests lets REQUESTS_CA_BUNDLE override a session-level verify=False; the client must not
    other = tmp_path / "other.pem"
    import certifi
    shutil.copy(certifi.where(), other)
    clean_env.setenv("REQUESTS_CA_BUNDLE", str(other))
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert _gl(pki["url"], verify=False).me()["username"] == "nova.andriana"
    with pytest.raises(requests.exceptions.SSLError):
        _gl(pki["url"], verify=True).me()


def test_app_process_logs_one_line_then_recovers_after_trust_cert(pki, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(os.path.join(ROOT, "config.example.yaml"), data / "config.yaml")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    (data / ".env").write_text(
        f"GITLAB_URL={pki['url']}\nGITLAB_TOKEN=glpat-test12345678\nTELEGRAM_BOT_TOKEN=123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
        f"TELEGRAM_CHAT_ID=1\nTELEGRAM_API_BASE=http://127.0.0.1:9\nDASHBOARD_PORT={port}\nDASHBOARD_HOST=127.0.0.1\n"
        "DASHBOARD_PASSWORD=pw-12345678\nREVIEW_MODE=llm\n")
    env = {"PATH": os.environ["PATH"], "HOME": str(data), "PYTHONPATH": ROOT, "PYTHONUNBUFFERED": "1"}

    def start():
        out = open(data / "out.log", "ab")
        return subprocess.Popen([sys.executable, "-m", "mr_pilot", "run", "--config", str(data / "config.yaml")],
                                cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT)

    def log():
        return (data / "out.log").read_text(errors="replace")

    def wait(cond, t=60):
        end = time.time() + t
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.3)
        raise AssertionError(log()[-3000:])

    p = start()
    try:
        wait(lambda: "tidak dipercaya" in log())
        assert "setup.bat trust-cert" in log() and "Traceback" not in log()
    finally:
        p.terminate()
        p.wait(30)
    (data / "certs").mkdir()
    shutil.copy(pki["dir"] / "ca.cer", data / "certs" / "localhost-chain.cer")
    p = start()
    try:
        wait(lambda: "MR Pilot jalan untuk @nova.andriana" in log())
        assert "localhost-chain.cer" in log()
    finally:
        p.terminate()
        p.wait(30)


def _serve(pki, full_chain):
    """HTTPS server on localhost sending only the leaf (like a misconfigured server) or leaf+CA."""
    d = pki["dir"]
    cert = d / ("full.crt" if full_chain else "srv.crt")
    if full_chain:
        cert.write_text((d / "srv.crt").read_text() + (d / "ca.crt").read_text())
    srv = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, d / "srv.key")
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"https://localhost:{srv.server_address[1]}"


def _workdir(tmp_path, script):
    (tmp_path / "data").mkdir()
    shutil.copy(os.path.join(ROOT, script), tmp_path / script)
    return tmp_path


def _trusted_by_python(data_dir, url, pki):
    bundle, used, _ = install_extra_cas(str(data_dir))
    try:
        _gl(url).me()
    except requests.exceptions.SSLError:
        return False
    except requests.exceptions.HTTPError:
        pass  # TLS handshake verified; this minimal server just doesn't implement the API
    return bool(used)


@pytest.mark.skipif(not shutil.which("bash"), reason="bash tidak ada")
def test_setup_sh_trust_cert_saves_issuer(pki, clean_env, tmp_path):
    srv, url = _serve(pki, full_chain=True)
    try:
        wd = _workdir(tmp_path, "setup.sh")
        env = {**os.environ, "PATH": os.environ["PATH"]}
        r = subprocess.run(["bash", "setup.sh", "trust-cert", f"--url={url}"], cwd=wd, env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr
        pem = (wd / "data" / "certs" / "localhost-chain.pem").read_text()
        assert pem.count("BEGIN CERTIFICATE") == 1  # the CA only, not the server's own certificate
        assert _trusted_by_python(wd / "data", url, pki), "Python harus percaya server setelah trust-cert"
    finally:
        srv.shutdown()


@pytest.mark.skipif(not shutil.which("bash"), reason="bash tidak ada")
def test_setup_sh_trust_cert_explains_when_issuer_unknown(pki, clean_env, tmp_path):
    srv, url = _serve(pki, full_chain=False)
    try:
        wd = _workdir(tmp_path, "setup.sh")
        r = subprocess.run(["bash", "setup.sh", "trust-cert", f"--url={url}"], cwd=wd,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 1 and "tim IT" in r.stdout and "GITLAB_VERIFY_SSL=false" in r.stdout
        assert not (wd / "data" / "certs" / "localhost-chain.pem").exists()
    finally:
        srv.shutdown()


PWSH = shutil.which("pwsh") or os.environ.get("PWSH")


@pytest.mark.skipif(not PWSH, reason="pwsh tidak ada")
def test_setup_ps1_trust_cert_uses_os_trust_store(pki, clean_env, tmp_path):
    # Server sends ONLY its own certificate; the OS trust store knows the CA (like Windows knows the
    # company CA). trust-cert must export the CA so Python in Docker trusts the server too.
    srv, url = _serve(pki, full_chain=False)
    try:
        wd = _workdir(tmp_path, "setup.ps1")
        import certifi
        store = tmp_path / "os-store.pem"
        store.write_text(open(certifi.where()).read() + (pki["dir"] / "ca.crt").read_text())
        env = {**os.environ, "SSL_CERT_FILE": str(store), "PATH": "/usr/bin:/bin"}
        r = subprocess.run([PWSH, "-NoProfile", "-File", "setup.ps1", "trust-cert", "-Url", url], cwd=wd,
                           env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr
        pem = (wd / "data" / "certs" / "localhost-chain.pem").read_text()
        assert pem.count("BEGIN CERTIFICATE") == 1 and "IDAS Test Root CA" in pem
        assert _trusted_by_python(wd / "data", url, pki), "Python harus percaya server setelah trust-cert"
    finally:
        srv.shutdown()
