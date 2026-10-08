"""Send the 'already merged' note to Teams as the user, via a Power Automate flow."""
import html
import logging
import random
import re
import time

import requests

from .util import short_error

log = logging.getLogger("mr_pilot.teams")
DEFAULT_TEMPLATE = "MR {ref} ({title}) sudah saya merge ke {target_branch}."

_JIRA = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


class _Safe(dict):
    def __missing__(self, key):
        return ""


def build_context(mr):
    author = mr.get("author") or {}
    name = author.get("name") or author.get("username") or ""
    refs = mr.get("references") or {}
    full = refs.get("full") or ""
    jira = _JIRA.search(mr.get("title") or "") or _JIRA.search((mr.get("source_branch") or "").upper())
    jira = jira.group(1) if jira else ""
    return {
        "ref": refs.get("short") or f"!{mr.get('iid')}",
        "full_ref": full,
        "project": full.split("!")[0] if full else "",
        "iid": mr.get("iid"),
        "title": mr.get("title", ""),
        "jira": jira,
        "topic": jira or mr.get("title", ""),
        "author_name": name,
        "author_first": name.split()[0] if name else "",
        "author_username": author.get("username", ""),
        "source_branch": mr.get("source_branch", ""),
        "target_branch": mr.get("target_branch", ""),
        "url": mr.get("web_url", ""),
    }


def render(template, ctx):
    try:
        text = str(template).format_map(_Safe(ctx))
    except (ValueError, KeyError, IndexError, AttributeError) as ex:
        # salah ketik di template (mis. "{title" atau "{a.b}") tidak boleh menggagalkan proses setelah merge
        log.warning("Template Teams tidak valid (%s): %r. Pakai template bawaan.", ex, template)
        text = DEFAULT_TEMPLATE.format_map(_Safe(ctx))
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


class Teams:
    def __init__(self, cfg):
        self.cfg = cfg["teams"]

    def compose(self, mr):
        templates = [t for t in (self.cfg.get("templates") or []) if isinstance(t, str) and t.strip()]
        return render(random.choice(templates) if templates else DEFAULT_TEMPLATE, build_context(mr))

    def notify_merged(self, mr):
        """Returns (status, text). status: sent | copy | off | 'gagal: ...'"""
        text = self.compose(mr)
        mode = self.cfg["mode"]
        if mode == "off":
            return "off", text
        if mode == "telegram_copy" or not self.cfg.get("webhook_url"):
            return "copy", text
        payload = {"text": text,
                   "text_html": html.escape(text).replace("\n", "<br>"),
                   "mr_url": mr.get("web_url", ""), "mr_title": mr.get("title", "")}
        last = ""
        for attempt in range(2):  # retry once only when the flow clearly did not accept the message
            try:
                r = requests.post(self.cfg["webhook_url"], json=payload, timeout=30)
            except requests.ConnectionError as ex:
                last = short_error(ex)
                time.sleep(2)
                continue
            except Exception as ex:  # timeout: may have been delivered, don't risk a duplicate post
                return f"gagal: {short_error(ex)}", text
            if r.status_code in (200, 201, 202):
                return "sent", text
            last = f"HTTP {r.status_code}"
            if r.status_code not in (429, 500, 502, 503, 504):
                break
            time.sleep(3)
        return f"gagal: {last}", text
