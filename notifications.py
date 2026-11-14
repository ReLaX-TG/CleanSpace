"""Telegram notification worker logic.

Delivery is AT-LEAST-ONCE at best, not exactly-once: if Telegram accepted a message but
the answer was lost (timeout, connection dropped after sending, unreadable answer, worker
crash), a retry can deliver a duplicate. Such jobs are flagged `delivery_uncertain`; the
flag is KEPT even after a later successful attempt, and the CRM warns about it.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import time
from pathlib import Path

import config
import db
import telegram_config
from pricing import format_quantity
from telegram_client import TelegramBot, TelegramError, redact
from telegram_config import TelegramSettings

log = logging.getLogger("cleanspace.telegram")

# Worker exit codes.
EXIT_OK = 0
EXIT_AUTH_FAILED = 2       # Telegram rejected the token
EXIT_SETTINGS_CHANGED = 3  # telegram.json removed or changed while running


def format_pln(grosze: int, currency: str = "PLN") -> str:
    whole, rest = divmod(int(grosze), 100)
    text = f"{whole:,}".replace(",", " ")
    return f"{text}.{rest:02d} {currency}" if rest else f"{text} {currency}"


TELEGRAM_TEXT_LIMIT = 4096   # Telegram's maximum message length
MAX_LISTED_LINES = 8         # longer orders get a compact summary
UNIT_TEXT = {"m2": "m²", "seat": "seats", "item": "pcs"}


def _line_text(item) -> str:
    """'Deep cleaning — 50 m²', 'Single mattress (both sides) — 2 pcs'. No personal data."""
    label = item["label_snapshot"]
    try:
        options = json.loads(item["options_json"] or "{}")
    except ValueError:
        options = {}
    extras = [config.OPTION_LABELS.get(group, {}).get(choice, choice) for group, choice in options.items()]
    if extras:
        label += f" ({', '.join(extras)})"
    quantity = format_quantity(item["quantity_scaled"], item["quantity_decimals"])
    return f"• {label} — {quantity} {UNIT_TEXT.get(item['unit'], item['unit'])}"


def build_message(job, items=None) -> str:
    """Plain text (English), no personal data: no name, email, phone, address, comment
    or internal note, and no links. Long orders are summarised to stay within the limit."""
    items = list(items or [])
    lines = ["CleanSpace — New demo request", f"Request #{job['request_id']}", "Services:"]
    shown = items if len(items) <= MAX_LISTED_LINES else items[:MAX_LISTED_LINES - 1]
    lines += [_line_text(i) for i in shown]
    if len(shown) < len(items):
        lines.append(f"• … and {len(items) - len(shown)} more service(s)")
    lines.append(f"Total: {format_pln(job['total_grosze'], job['currency'])}")
    time_label = config.PREFERRED_TIME_LABELS.get(job["preferred_time"] or "", "")
    when = job["preferred_date"] + (f", {time_label}" if time_label else "")
    lines.append(f"Preferred: {when} (subject to confirmation)")
    text = "\n".join(lines)
    if len(text) > TELEGRAM_TEXT_LIMIT:  # defensive; with ≤ 8 listed lines this cannot happen
        text = text[: TELEGRAM_TEXT_LIMIT - 1] + "…"
    return text


def new_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(3)}"


def _backoff(attempts: int) -> int:
    steps = config.NOTIFY_BACKOFF_SECONDS
    return steps[min(max(attempts, 1) - 1, len(steps) - 1)]


def same_settings(a: TelegramSettings | None, b: TelegramSettings | None) -> bool:
    """True if both exist and point to the same bot token and chat."""
    return a is not None and b is not None and a.token == b.token and a.chat_id == b.chat_id


def process_one(db_path: Path, settings: TelegramSettings, bot: TelegramBot, worker_id: str,
                now_fn=time.time, settings_still_valid=lambda: True) -> str | None:
    """Claim and process one due job. Returns an outcome keyword, or None if the queue is empty.

    Outcomes: "sent", "retry", "postponed", "failed", "auth_failed", "expired",
    "lost_lock", "settings_changed".
    """
    job = db.claim_notification(db_path, worker_id, now_fn(), config.NOTIFY_LEASE_SECONDS)
    if job is None:
        return None
    rid, attempts, max_attempts = job["request_id"], job["attempts"], config.NOTIFY_MAX_ATTEMPTS

    # Re-check right before sending: never use an old token/chat after a change.
    if not settings_still_valid():
        db.release_notification(db_path, job["id"], worker_id, now_fn())
        return "settings_changed"

    def finish(status, **kw):
        ok = db.finish_notification(db_path, job["id"], worker_id, status, now_fn(), **kw)
        if not ok:
            log.warning("Notification for request #%s: lock lost, result not recorded", rid)
        return ok

    if attempts > max_attempts:
        finish("failed", error=f"Gave up after {max_attempts} attempts.")
        log.warning("Notification for request #%s failed: attempts used up", rid)
        return "failed"

    automatic = job["manual_retries"] == 0
    deadline = job["created_at"] + config.NOTIFY_MAX_AGE_SECONDS  # only for automatic sending

    # Never send automatically about old requests (e.g. worker was not running for days).
    if automatic and now_fn() > deadline:
        finish("failed", error="Not sent automatically: the request is older than 24 hours. "
                               "Use “Retry notification” in the CRM to send it anyway.")
        log.info("Notification for request #%s expired (too old)", rid)
        return "expired"

    try:
        items = db.get_items(db_path, [rid])[rid]
        result = bot.send_message(settings.chat_id, build_message(job, items))
    except TelegramError as exc:
        description = redact(exc.description, settings.token)
        uncertain = exc.uncertain

        if exc.kind == "rate_limited":
            # Honour Telegram's retry_after exactly — never retry earlier. The pause is stored
            # in the job (blocked_until), so neither a manual retry nor a worker restart can
            # skip it: claim_notification does not hand out the job before that time.
            delay = int(exc.retry_after or 30)
            not_before = now_fn() + delay
            if automatic and not_before > deadline:
                finish("failed", uncertain=uncertain, blocked_until=not_before, error=(
                    f"Rate limited by Telegram (429): asked to wait {delay} s, which is past the "
                    "24-hour limit for automatic sending. Not sent. “Retry notification” in the "
                    "CRM can queue it now; it is sent only after Telegram's pause ends."))
                log.info("Notification for request #%s: 429 wait exceeds job lifetime, not sent", rid)
                return "failed"
            # A 429 means Telegram did not accept the message, so it does not use up an attempt.
            db.finish_notification(db_path, job["id"], worker_id, "pending", now_fn(),
                                   error=f"Rate limited by Telegram (429): next attempt after {delay} s.",
                                   next_attempt_at=not_before, attempts_delta=-1,
                                   blocked_until=not_before)
            log.info("Notification for request #%s rate limited, next attempt in %ss", rid, delay)
            return "postponed"

        if exc.permanent:
            messages = {
                "auth": "Telegram rejected the bot token (HTTP {s}). Run setup-telegram again, then retry.",
                "forbidden": "The bot cannot write to this chat (HTTP {s}): {d} "
                             "Unblock the bot or press Start in Telegram, then retry.",
                "bad_request": "Telegram refused the message (HTTP {s}): {d}",
            }
            error = messages[exc.kind].format(s=exc.status, d=description)
            finish("failed", error=error)
            log.warning("Notification for request #%s failed permanently: %s", rid, exc.kind)
            return "auth_failed" if exc.kind == "auth" else "failed"

        # Temporary problems: timeout, connection lost, unreadable answer, network, 5xx, conflict.
        prefix = "Delivery unknown" if uncertain else "Temporary problem"
        error = f"{prefix} ({exc.kind}): {description}"
        if attempts >= max_attempts:
            finish("failed", error=error + f" Gave up after {max_attempts} attempts.", uncertain=uncertain)
            log.warning("Notification for request #%s failed after %s attempts (%s)", rid, attempts, exc.kind)
            return "failed"
        delay = _backoff(attempts)
        finish("pending", error=error, next_attempt_at=now_fn() + delay, uncertain=uncertain)
        log.info("Notification for request #%s: %s, retry in %ss", rid, exc.kind, delay)
        return "retry"

    message_id = result.get("message_id") if isinstance(result, dict) else None
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        message_id = None
    # delivery_uncertain is deliberately NOT cleared: an earlier attempt may also have arrived.
    if not finish("sent", error="", message_id=message_id):
        return "lost_lock"
    log.info("Notification for request #%s sent (attempt %s)", rid, attempts)
    return "sent"


def run_worker(db_path: Path, settings: TelegramSettings, bot: TelegramBot, *, once: bool = False,
               sleep=time.sleep, should_stop=lambda: False, load_settings=None) -> int:
    """Process the queue until stopped. Returns a process exit code.

    Before each job the settings file is read again. If it was removed or now points to
    another token/chat, the worker stops (EXIT_SETTINGS_CHANGED) instead of sending with
    the old settings. A request that is already on its way to Telegram cannot be recalled.
    once=True: process every job that is due now, then return (used by tests).
    """
    load_settings = load_settings or telegram_config.load
    worker_id = new_worker_id()

    def still_valid() -> bool:
        return same_settings(settings, load_settings())

    log.info("Telegram worker started (bot @%s)", settings.bot_username)
    while not should_stop():
        if not still_valid():
            current = load_settings()
            reason = "removed (integration disabled)" if current is None else "changed"
            log.warning("Telegram settings were %s. Worker stopped; nothing more is sent with the "
                        "old settings. Start the worker again if needed.", reason)
            return EXIT_SETTINGS_CHANGED
        outcome = process_one(db_path, settings, bot, worker_id, settings_still_valid=still_valid)
        if outcome == "settings_changed":
            continue  # the check at the top of the loop stops the worker with an explanation
        if outcome == "auth_failed":
            log.error("Telegram rejected the bot token. Worker stopped. Run setup-telegram again.")
            return EXIT_AUTH_FAILED
        if outcome is None:
            if once:
                return EXIT_OK
            sleep(config.NOTIFY_POLL_SECONDS)
    return EXIT_OK
