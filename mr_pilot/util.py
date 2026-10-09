"""Shared helpers: secret redaction (logs, events, Telegram), small HTTP retry session."""
import glob
import logging
import os
import re
import ssl

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Secrets that can appear inside URLs or error messages.
_PATTERNS = [
    (re.compile(r"(bot)\d{5,}:[A-Za-z0-9_-]{20,}"), r"\1<telegram-token>"),          # Telegram bot token in URL
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), "<telegram-token>"),               # bare Telegram token
    (re.compile(r"glpat-[A-Za-z0-9_\-]{10,}"), "glpat-<redacted>"),                  # GitLab PAT
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"), "sk-ant-<redacted>"),                # Anthropic keys / OAuth
    (re.compile(r"\bsk-(or-|proj-)?[A-Za-z0-9_\-]{16,}"), "sk-<redacted>"),          # OpenAI / OpenRouter
    (re.compile(r"\bgsk_[A-Za-z0-9]{16,}"), "gsk_<redacted>"),                       # Groq
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), "AIza<redacted>"),                    # Google API key
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "gh_<redacted>"),                  # GitHub tokens
    (re.compile(r"([?&](?:sig|key|token|access_token|private_token)=)[^&\s'\"]+", re.I), r"\1<redacted>"),
    (re.compile(r"(PRIVATE-TOKEN['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", re.I), r"\1<redacted>"),
    (re.compile(r"(Authorization['\"]?\s*[:=]\s*['\"]?(?:Bearer\s+)?)[^'\"\s,}]+", re.I), r"\1<redacted>"),
]


def redact(text):
    """Remove tokens/keys from any string before it is logged, stored or sent."""
    if text is None:
        return ""
    s = str(text)
    for rx, rep in _PATTERNS:
        s = rx.sub(rep, s)
    return s


class RedactingFilter(logging.Filter):
    """Applied to every log handler: formats the record, then scrubs secrets (incl. tracebacks)."""

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        record.msg, record.args = redact(msg), None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def retry_session(total=3, backoff=1.0, methods=("GET", "HEAD")):
    """requests.Session that retries idempotent calls on connection errors / 429 / 5xx."""
    s = requests.Session()
    retry = Retry(total=total, connect=total, read=total, status=total, backoff_factor=backoff,
                  status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset(methods),
                  respect_retry_after_header=True, raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def short_error(ex, limit=300):
    """Human-sized, secret-free error text."""
    if isinstance(ex, requests.ConnectionError):
        msg = "tidak bisa terhubung"
    elif isinstance(ex, requests.Timeout):
        msg = "timeout"
    elif isinstance(ex, requests.HTTPError) and ex.response is not None:
        msg = f"HTTP {ex.response.status_code}"
        try:
            body = ex.response.json()
            detail = body.get("message") or body.get("error") or body.get("description")
            if detail:
                msg += f": {detail}"
        except Exception:
            pass
    else:
        msg = str(ex) or ex.__class__.__name__
    return redact(msg)[:limit]


# ------------------------------------------------------------- extra CAs
CERT_GLOBS = ("*.crt", "*.pem", "*.cer")


def _cert_text(path):
    """PEM text of a certificate file (PEM or DER). None if it isn't a usable certificate."""
    with open(path, "rb") as f:
        raw = f.read()
    if b"-----BEGIN CERTIFICATE-----" in raw:
        text = raw.decode("utf-8-sig", "replace")
    else:
        try:
            text = ssl.DER_cert_to_PEM_cert(raw)
        except Exception:
            return None
    try:
        ssl.create_default_context().load_verify_locations(cadata=text)
    except ssl.SSLError:
        return None
    return text.strip() + "\n"


def install_extra_cas(data_dir, log=None):
    """Trust the certificates in <data>/certs (company CA, GitLab's intermediate) for every HTTPS call:
    writes <data>/.ca-bundle.pem = public roots + extras and points requests / Python / Node at it.
    Returns (bundle_path, used_files, skipped_files) or (None, [], []) when there is nothing to add."""
    log = log or logging.getLogger("mr_pilot")
    files = sorted({p for g in CERT_GLOBS for p in glob.glob(os.path.join(data_dir, "certs", g))})
    if not files:
        return None, [], []
    used, skipped, extras = [], [], []
    for f in files:
        text = _cert_text(f)
        if text:
            used.append(os.path.basename(f))
            extras.append(text)
        else:
            skipped.append(os.path.basename(f))
    if skipped:
        log.warning("File sertifikat dilewati (bukan sertifikat yang valid): %s", ", ".join(skipped))
    if not extras:
        return None, [], skipped
    import certifi
    with open(certifi.where(), encoding="utf-8") as f:
        roots = f.read()
    bundle = os.path.join(data_dir, ".ca-bundle.pem")
    extra_only = os.path.join(data_dir, ".ca-extra.pem")
    for path, body in ((bundle, roots.rstrip() + "\n" + "".join(extras)), (extra_only, "".join(extras))):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        os.replace(tmp, path)
    os.environ["REQUESTS_CA_BUNDLE"] = bundle   # requests (GitLab, Telegram, AI, Teams)
    os.environ["SSL_CERT_FILE"] = bundle        # Python ssl default context
    os.environ["NODE_EXTRA_CA_CERTS"] = extra_only  # Claude Code CLI (Node)
    log.info("Sertifikat tambahan dipercaya: %s", ", ".join(used))
    return bundle, used, skipped


def ssl_hint(ex):
    """Friendly one-liner for TLS failures, else None."""
    text = str(ex)
    if isinstance(ex, requests.exceptions.SSLError) or "CERTIFICATE_VERIFY_FAILED" in text:
        host = re.search(r"host='([^']+)'", text)
        host = host.group(1) if host else "server"
        return (f"Sertifikat SSL {host} tidak dipercaya (CA kantor / sertifikat intermediate tidak lengkap). "
                f"Jalankan `setup.bat trust-cert` (Windows) atau `./setup.sh trust-cert` lalu restart. "
                f"Darurat: GITLAB_VERIFY_SSL=false di data/.env.")
    return None
