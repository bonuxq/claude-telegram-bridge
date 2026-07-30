"""Minimal Telegram Bot API client.

Only the handful of methods the bridge actually needs, on urllib, so that
neither the daemon nor the hook client pulls a single third-party package.
"""

import json
import urllib.error
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(RuntimeError):
    def __init__(self, method, code, description):
        super().__init__(f"{method}: {code} {description}")
        self.method = method
        self.code = code
        self.description = description


class Bot:
    def __init__(self, token, timeout=20):
        self.token = token
        self.timeout = timeout

    def call(self, method, _read_timeout=None, **params):
        # `_read_timeout` is the socket timeout; it is kept out of the payload
        # because Telegram's own `timeout` field (getUpdates) shares the name.
        payload = json.dumps(
            {k: v for k, v in params.items() if v is not None}
        ).encode("utf-8")
        req = urllib.request.Request(
            API.format(token=self.token, method=method),
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(
                req, timeout=_read_timeout or self.timeout
            ) as r:
                body = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:
                raise TelegramError(method, e.code, e.reason) from None
            raise TelegramError(
                method, body.get("error_code", e.code), body.get("description", "")
            ) from None
        if not body.get("ok"):
            raise TelegramError(
                method, body.get("error_code"), body.get("description", "")
            )
        return body["result"]

    # -- methods used by the bridge -------------------------------------

    def get_updates(self, offset=None, long_poll=50):
        # Read timeout must outlive the long poll or urllib kills it first.
        return self.call(
            "getUpdates",
            _read_timeout=long_poll + 20,
            offset=offset,
            timeout=long_poll,
            allowed_updates=["message", "callback_query"],
        )

    def send_message(
        self, chat_id, text, thread_id=None, markup=None, html=True, silent=False
    ):
        return self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            message_thread_id=thread_id,
            parse_mode="HTML" if html else None,
            reply_markup=markup,
            disable_notification=silent or None,
            link_preview_options={"is_disabled": True},
        )

    def pin_message(self, chat_id, message_id):
        return self.call(
            "pinChatMessage",
            chat_id=chat_id,
            message_id=message_id,
            disable_notification=True,
        )

    def edit_message(self, chat_id, message_id, text, markup=None, html=True):
        return self.call(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML" if html else None,
            reply_markup=markup,
            link_preview_options={"is_disabled": True},
        )

    def create_topic(self, chat_id, name):
        return self.call("createForumTopic", chat_id=chat_id, name=name)

    def answer_callback(self, callback_id, text=None):
        return self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    def get_me(self):
        return self.call("getMe")
