"""Minimal Telegram Bot API client (standard library only).

- Every request has a bounded timeout.
- Errors are classified so the worker can decide: retry later, or stop.
- The bot token is part of the request URL, so it is NEVER put into exception
  messages, logs or return values: every error text passes through `redact()`.
- The HTTP layer is injectable (`transport`) so tests use fake responses and never
  contact Telegram.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

import config

TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,64}$")
# Anything that looks like a bot token, even one we do not know about.
_ANY_TOKEN_RE = re.compile(r"\d{5,15}:[A-Za-z0-9_-]{30,64}")


def looks_like_token(value: str) -> bool:
    return bool(TOKEN_RE.match(value or ""))


def redact(text: str, token: str | None = None) -> str:
    """Remove the token (and anything token-shaped) from a text."""
    text = str(text)
    if token:
        text = text.replace(token, "<token>")
    text = _ANY_TOKEN_RE.sub("<token>", text)
    return text[:300]


class TelegramError(Exception):
    """A classified, token-free Telegram error.

    kind:
      "auth"         401/404 — token invalid or revoked (do not retry)
      "forbidden"    403 — bot blocked by the user / cannot write to the chat (do not retry)
      "bad_request"  400 — e.g. chat not found (do not retry)
      "conflict"     409 — webhook is active, getUpdates not allowed
      "rate_limited" 429 — retry after `retry_after` seconds (Telegram did NOT accept it)
      "timeout"         no answer in time — the message MAY have been delivered
      "connection_lost" connection dropped after the request was (maybe) sent — MAY have been delivered
      "bad_response"    an answer arrived but cannot be read/confirmed — MAY have been delivered
      "network"         could not connect at all (refused, DNS) — the request never left
      "server"          Telegram answered with a readable 5xx error — not accepted
    """

    PERMANENT = {"auth", "forbidden", "bad_request"}
    # Cases where we cannot know whether Telegram accepted the message.
    UNCERTAIN = {"timeout", "connection_lost", "bad_response"}

    def __init__(self, kind: str, description: str, status: int | None = None,
                 retry_after: int | None = None):
        super().__init__(description)
        self.kind = kind
        self.description = description
        self.status = status
        self.retry_after = retry_after

    @property
    def permanent(self) -> bool:
        return self.kind in self.PERMANENT

    @property
    def uncertain(self) -> bool:
        return self.kind in self.UNCERTAIN

    def __str__(self) -> str:
        code = f" (HTTP {self.status})" if self.status else ""
        return f"{self.kind}{code}: {self.description}"


def urllib_transport(url: str, payload: dict, timeout: float) -> tuple[int, dict | None]:
    """Real HTTP transport. Returns (status, parsed JSON or None)."""
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:  # Telegram answers errors with JSON bodies
        status, raw = exc.code, exc.read()
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, None


# Tests replace this with a fake.
default_transport = urllib_transport


class TelegramBot:
    def __init__(self, token: str, transport=None, timeout: float | None = None):
        self._token = token
        self._transport = transport or default_transport
        self._timeout = timeout or config.TELEGRAM_HTTP_TIMEOUT

    def _call(self, method: str, params: dict | None = None, http_timeout: float | None = None) -> object:
        url = f"{config.TELEGRAM_API_BASE}/bot{self._token}/{method}"
        try:
            status, body = self._transport(url, params or {}, http_timeout or self._timeout)
        except (socket.timeout, TimeoutError):
            raise TelegramError("timeout", "No answer from Telegram in time; delivery is unknown.") from None
        except urllib.error.URLError as exc:
            # urllib wraps errors raised while CONNECTING / SENDING the request.
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise TelegramError("timeout", "No answer from Telegram in time; delivery is unknown.") from None
            if isinstance(reason, (ConnectionRefusedError, socket.gaierror, ssl.SSLCertVerificationError)):
                # Failed before the request left this computer: certainly not delivered.
                raise TelegramError("network", redact(f"Could not connect: {reason}", self._token)) from None
            raise TelegramError("connection_lost", redact(
                f"Connection broke while sending ({reason}); delivery is unknown.", self._token)) from None
        except (OSError, http.client.HTTPException) as exc:
            # Raised while READING the answer (reset, remote closed, incomplete read):
            # the request was sent, so Telegram may have accepted it.
            raise TelegramError("connection_lost", redact(
                f"Connection broke before the answer was received ({type(exc).__name__}); "
                "delivery is unknown.", self._token)) from None

        if isinstance(body, dict) and body.get("ok") is True:
            return body.get("result")

        if not isinstance(body, dict) or "ok" not in body:
            if status in (401, 403, 404):
                pass  # clear refusals even without a JSON body; classified below
            else:
                # 2xx/5xx with an unreadable body (e.g. a proxy page): cannot confirm either way.
                raise TelegramError("bad_response",
                                    f"Unreadable answer from Telegram (HTTP {status}); delivery is unknown.",
                                    status)

        description = ""
        retry_after = None
        if isinstance(body, dict):
            description = redact(body.get("description", ""), self._token)
            retry_after = (body.get("parameters") or {}).get("retry_after")
        if status == 429:
            if not isinstance(retry_after, int) or isinstance(retry_after, bool) or retry_after < 1:
                retry_after = 30
            raise TelegramError("rate_limited", description or "Too many requests.", status, retry_after)
        if status in (401, 404):
            raise TelegramError("auth", description or "Telegram rejected the bot token.", status)
        if status == 403:
            raise TelegramError("forbidden", description or "The bot cannot write to this chat.", status)
        if status == 409:
            raise TelegramError("conflict", description or "Conflict with a webhook.", status)
        if status == 400:
            raise TelegramError("bad_request", description or "Bad request.", status)
        raise TelegramError("server", description or "Unexpected answer from Telegram.", status)

    # --- API methods used by CleanSpace ------------------------------------
    def get_me(self) -> dict:
        return self._call("getMe")

    def get_webhook_info(self) -> dict:
        return self._call("getWebhookInfo")

    def get_updates(self, offset: int | None, long_poll: int) -> list:
        params = {"timeout": long_poll, "allowed_updates": json.dumps(["message"])}
        if offset is not None:
            params["offset"] = offset
        # HTTP timeout = long-poll duration + normal timeout, still bounded.
        return self._call("getUpdates", params, http_timeout=long_poll + self._timeout)

    def send_message(self, chat_id: int, text: str) -> dict:
        # Plain text: no parse_mode, so nothing in the text is interpreted as markup.
        return self._call("sendMessage", {"chat_id": chat_id, "text": text,
                                          "disable_web_page_preview": "true"})
