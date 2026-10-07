"""Tiny Telegram Bot API client (long polling, no webhook needed)."""
import requests


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token, chat_id, proxy=None):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.s = requests.Session()
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}

    def call(self, method, _http_timeout=30, **params):
        r = self.s.post(f"{self.base}/{method}", json=params, timeout=_http_timeout)
        try:
            data = r.json()
        except ValueError:
            raise TelegramError(f"HTTP {r.status_code}")
        if not data.get("ok"):
            raise TelegramError(data.get("description", "unknown error"))
        return data["result"]

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
                    btns.append({"text": label, "callback_data": val})
            kb.append(btns)
        return {"inline_keyboard": kb}

    def send(self, text, buttons=None, force_reply=False, html=True):
        p = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        if html:
            p["parse_mode"] = "HTML"
        if buttons:
            p["reply_markup"] = self.keyboard(buttons)
        elif force_reply:
            p["reply_markup"] = {"force_reply": True}
        return self.call("sendMessage", **p)["message_id"]

    def edit(self, message_id, text, buttons=None):
        p = {"chat_id": self.chat_id, "message_id": message_id, "text": text,
             "parse_mode": "HTML", "disable_web_page_preview": True,
             "reply_markup": self.keyboard(buttons) if buttons else {"inline_keyboard": []}}
        try:
            self.call("editMessageText", **p)
        except TelegramError as e:
            if "not modified" not in str(e):
                raise

    def answer(self, callback_id, text=""):
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except Exception:
            pass

    def get_updates(self, offset, timeout=25):
        return self.call("getUpdates", _http_timeout=timeout + 15, offset=offset, timeout=timeout,
                         allowed_updates=["message", "callback_query"])
