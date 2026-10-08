"""Shared helpers: secret redaction (logs, events, Telegram), small HTTP retry session."""
import logging
import re

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
