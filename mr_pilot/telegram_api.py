"""Telegram Bot API client (long polling, no webhook needed).

Robustness: retries on 429 (retry_after) and transient 5xx/network errors, falls back to plain text
when Telegram can't parse the HTML, keeps messages under the 4096-char limit, and never puts the
bot token in exception messages."""
import html
import logging
import re
import time

import requests

from .util import redact, short_error

log = logging.getLogger("mr_pilot.telegram")

MAX_LEN = 4096


class TelegramError(Exception):
    def __init__(self, description, code=None, retry_after=None):
        super().__init__(redact(description))
        self.code = code
        self.retry_after = retry_after

    @property
    def is_conflict(self):  # another process is polling the same bot
        return self.code == 409

    @property
    def is_parse_error(self):
        return self.code == 400 and "parse" in str(self).lower()


def strip_html(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def fit(text, limit=MAX_LEN):
    """Cut on a line boundary so HTML tags stay balanced."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 30].rsplit("\n", 1)[0]
    return cut + "\n…(dipotong)"


class Telegram:
    def __init__(self, token, chat_id, proxy=None, api_base="https://api.telegram.org"):
        self.base = f"{(api_base or 'https://api.telegram.org').rstrip('/')}/bot{token}"
        self.chat_id = chat_id
        self.s = requests.Session()
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}

    def call(self, method, _http_timeout=30, _retries=3, **params):
        for attempt in range(_retries + 1):
            try:
                r = self.s.post(f"{self.base}/{method}", json=params, timeout=_http_timeout)
            except requests.RequestException as ex:
                if attempt < _retries:
                    time.sleep(min(2 ** attempt, 10))
                    continue
                # requests puts the full URL (with the token) in the message -> short, redacted text only
                raise TelegramError(f"{method}: {short_error(ex)}") from None
            try:
                data = r.json()
            except ValueError:
                data = {"ok": False, "description": f"HTTP {r.status_code}", "error_code": r.status_code}
            if data.get("ok"):
                return data["result"]
            code = data.get("error_code") or r.status_code
            retry_after = (data.get("parameters") or {}).get("retry_after")
            if code == 429 and attempt < _retries:
                wait = min(int(retry_after or 5), 60)
                log.warning("Telegram rate limit, tunggu %ss", wait)
                time.sleep(wait)
                continue
            if code >= 500 and attempt < _retries:
                time.sleep(min(2 ** attempt, 10))
                continue
            raise TelegramError(data.get("description", "unknown error"), code, retry_after)
        raise TelegramError("gagal setelah beberapa percobaan")

    @staticmethod
    def keyboard(rows):
        """rows = [[(label, 'callback_data' | 'url:https://...'), ...], ...]"""
        kb = []
        for row in rows:
            btns = []
            for label, val in row:
                if val.startswith("url:"):
                    btns.append({"text": label, "url": val[4:]})
                else:
                    btns.append({"text": label, "callback_data": val[:64]})
            kb.append(btns)
        return {"inline_keyboard": kb}

    def send(self, text, buttons=None, force_reply=False, html=True):
        p = {"chat_id": self.chat_id, "text": fit(text), "disable_web_page_preview": True}
        if html:
            p["parse_mode"] = "HTML"
        if buttons:
            p["reply_markup"] = self.keyboard(buttons)
        elif force_reply:
            p["reply_markup"] = {"force_reply": True}
        try:
            return self.call("sendMessage", **p)["message_id"]
        except TelegramError as ex:
            if html and ex.is_parse_error:
                log.warning("HTML ditolak Telegram (%s), kirim sebagai teks biasa", ex)
                p.pop("parse_mode")
                p["text"] = fit(strip_html(text))
                return self.call("sendMessage", **p)["message_id"]
            raise

    def edit(self, message_id, text, buttons=None):
        p = {"chat_id": self.chat_id, "message_id": message_id, "text": fit(text),
             "parse_mode": "HTML", "disable_web_page_preview": True,
             "reply_markup": self.keyboard(buttons) if buttons else {"inline_keyboard": []}}
        try:
            self.call("editMessageText", **p)
        except TelegramError as ex:
            if "not modified" in str(ex):
                return
            if ex.is_parse_error:
                p.pop("parse_mode")
                p["text"] = fit(strip_html(text))
                self.call("editMessageText", **p)
                return
            raise

    def answer(self, callback_id, text=""):
        try:
            self.call("answerCallbackQuery", _retries=0, callback_query_id=callback_id, text=(text or "")[:190])
        except Exception:
            pass

    def get_updates(self, offset, timeout=25):
        return self.call("getUpdates", _http_timeout=timeout + 15, _retries=0, offset=offset, timeout=timeout,
                         allowed_updates=["message", "callback_query"])

    def get_me(self):
        return self.call("getMe", _retries=1)
