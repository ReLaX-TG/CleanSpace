"""CLI commands for the Telegram integration (registered on the Flask app):

    flask --app app setup-telegram     interactive setup (hidden token, one-time code)
    flask --app app test-telegram      send one test message
    flask --app app telegram-worker    deliver queued notifications (keep it running)
    flask --app app disable-telegram   remove the local Telegram settings

The token is never printed. Nothing is sent during setup unless you confirm it.
"""

from __future__ import annotations

import logging
import secrets
import time

import click

import config
import notifications
import telegram_config
from telegram_client import TelegramBot, TelegramError, looks_like_token, redact

# Tests replace this to control the one-time code.
make_setup_code = lambda: "CS-" + secrets.token_hex(4).upper()  # noqa: E731

TEST_MESSAGE = (
    "CleanSpace — Test message\n"
    "Telegram notifications for new demo requests are working."
)


class RedactTokenFilter(logging.Filter):
    """Defence in depth: scrub the token from every log record of the worker process."""

    def __init__(self, token: str):
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message, self._token)
        if cleaned != message:
            record.msg, record.args = cleaned, ()
        record.exc_info = None  # never print tracebacks (they could include request URLs)
        record.exc_text = None
        return True


def _chat_label(chat: dict) -> str:
    name = " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p) or "(no name)"
    username = chat.get("username")
    return f"{name} (@{username})" if username else name


def _explain(exc: TelegramError) -> str:
    hints = {
        "auth": "Telegram rejected this token. Copy it again from @BotFather (/mybots → API Token).",
        "network": "Could not connect to Telegram. Check the internet connection and try again.",
        "timeout": "Telegram did not answer in time. Try again.",
        "rate_limited": f"Telegram asks to wait {exc.retry_after} s before the next request.",
        "forbidden": "The bot cannot write to this chat. Open the chat with the bot, press Start "
                     "(or unblock it) and try again.",
    }
    return hints.get(exc.kind, f"Telegram error: {exc}")


def register(app) -> None:
    @app.cli.command("setup-telegram")
    def setup_telegram():
        """Connect a Telegram bot and choose the chat that receives notifications."""
        try:
            _setup()
        except KeyboardInterrupt:
            raise click.ClickException("Setup cancelled. Nothing was saved.") from None

    @app.cli.command("test-telegram")
    def test_telegram():
        """Send one test message to the configured chat."""
        settings = telegram_config.load()
        if settings is None:
            raise click.ClickException("Telegram is not configured. Run setup-telegram first.")
        click.echo(f"Sending a test message via @{settings.bot_username} to: {settings.chat_label} ...")
        try:
            TelegramBot(settings.token).send_message(settings.chat_id, TEST_MESSAGE)
        except TelegramError as exc:
            raise click.ClickException(f"Test message NOT sent. {_explain(exc)}") from None
        click.echo("Test message sent. Check Telegram.")

    @app.cli.command("telegram-worker")
    @click.option("--once", is_flag=True, help="Process the jobs that are due now, then exit.")
    def telegram_worker(once):
        """Deliver queued notifications. Keep it running in a second terminal window."""
        settings = telegram_config.load()
        if settings is None:
            raise click.ClickException(
                "Telegram is not configured, so there is nothing to deliver. Run setup-telegram first."
            )
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        token_filter = RedactTokenFilter(settings.token)
        for handler in logging.getLogger().handlers:
            handler.addFilter(token_filter)
        click.echo(f"Telegram worker: bot @{settings.bot_username} → {settings.chat_label}. "
                   "Press Ctrl+C to stop.")
        try:
            code = notifications.run_worker(config.DATABASE_PATH, settings, TelegramBot(settings.token),
                                            once=once)
        except KeyboardInterrupt:
            click.echo("Worker stopped.")
            return
        if code == notifications.EXIT_SETTINGS_CHANGED:
            click.echo(
                "Worker stopped: the Telegram settings were removed or changed while it was running.\n"
                "Nothing more will be sent with the old bot token or chat. A message that was already "
                "on its way to Telegram cannot be recalled.\n"
                "If Telegram is still configured, start the worker again to use the new settings."
            )
        if code:
            raise SystemExit(code)

    @app.cli.command("disable-telegram")
    def disable_telegram():
        """Remove the local Telegram settings (queued jobs are kept)."""
        if not telegram_config.is_configured():
            click.echo("Telegram is not configured.")
            return
        if click.confirm("Remove the Telegram settings (bot token and chat)?", default=False):
            telegram_config.remove()
            click.echo("Telegram settings removed. The site and CRM keep working; "
                       "new requests will not be queued for Telegram.")


def _setup() -> None:
    existing = telegram_config.load()
    if existing and not click.confirm(
        f"Telegram is already configured (bot @{existing.bot_username} → {existing.chat_label}). Replace it?",
        default=False,
    ):
        click.echo("Nothing changed.")
        return

    click.echo("Paste the bot token from @BotFather. It will not be shown on screen.")
    token = click.prompt("Bot token", hide_input=True).strip()
    if not looks_like_token(token):
        raise click.ClickException("This does not look like a bot token (expected digits:letters). Nothing was saved.")

    bot = TelegramBot(token)
    try:
        me = bot.get_me()
    except TelegramError as exc:
        raise click.ClickException(_explain(exc)) from None
    username = (me or {}).get("username", "")
    click.echo(f"Token accepted by Telegram. Bot: @{username}")

    try:
        webhook = bot.get_webhook_info() or {}
    except TelegramError as exc:
        raise click.ClickException(_explain(exc)) from None
    if webhook.get("url"):
        raise click.ClickException(
            "This bot has a webhook configured. Telegram does not allow reading messages with "
            "getUpdates while a webhook is active, so the chat cannot be detected.\n"
            "CleanSpace will NOT remove the webhook automatically, because another service may "
            "depend on it. Use a separate bot for CleanSpace, or remove the webhook yourself if "
            "you are sure it is not needed, then run setup-telegram again. Nothing was saved."
        )

    code = make_setup_code()
    click.echo("")
    click.echo(f"In Telegram, open a PRIVATE chat with @{username} and send this code as a message:")
    click.echo("")
    click.echo(f"    {code}")
    click.echo("")
    click.echo(f"Waiting up to {config.TELEGRAM_SETUP_WAIT_SECONDS // 60} minutes... (Ctrl+C to cancel)")

    chat = _wait_for_code(bot, code)
    if chat is None:
        raise click.ClickException("The code was not received in time. Nothing was saved. Run setup-telegram again.")

    label = _chat_label(chat)
    click.echo("")
    click.echo("Code received from this private chat:")
    click.echo(f"    {label}, chat id {chat['id']}")
    if not click.confirm("Send CleanSpace notifications to this chat?", default=False):
        raise click.ClickException("Not confirmed. Nothing was saved.")

    telegram_config.save(token, int(chat["id"]), label, username)
    click.echo(f"Saved to {config.TELEGRAM_CONFIG_FILE} (keep this file private; it is in .gitignore).")
    click.echo("New requests will now be queued for Telegram. Start the worker to deliver them:")
    click.echo("    .venv\\Scripts\\flask --app app telegram-worker")

    if click.confirm("Send a test message now?", default=False):
        try:
            bot.send_message(int(chat["id"]), TEST_MESSAGE)
            click.echo("Test message sent. Check Telegram.")
        except TelegramError as exc:
            click.echo(f"Test message NOT sent. {_explain(exc)}")
    else:
        click.echo("No test message sent. You can send one later with: .venv\\Scripts\\flask --app app test-telegram")


def _wait_for_code(bot: TelegramBot, code: str) -> dict | None:
    """Poll getUpdates until a PRIVATE message with exactly `code` arrives."""
    deadline = time.monotonic() + config.TELEGRAM_SETUP_WAIT_SECONDS
    offset = None
    warned_group = False
    while time.monotonic() < deadline:
        try:
            updates = bot.get_updates(offset, config.TELEGRAM_LONG_POLL_SECONDS)
        except TelegramError as exc:
            if exc.kind == "conflict":
                raise click.ClickException(
                    "Telegram reports a conflict: a webhook or another program is reading this bot's "
                    "updates. Stop it (CleanSpace will not change it) and run setup-telegram again."
                ) from None
            if exc.kind in ("timeout", "network", "server"):
                time.sleep(2)
                continue
            if exc.kind == "rate_limited":
                wait = int(exc.retry_after or 5)  # never ask Telegram again earlier than it said
                if time.monotonic() + wait > deadline:
                    raise click.ClickException(
                        f"Telegram asks to wait {wait} s before the next request, which is longer than "
                        "setup waits. Nothing was saved. Run setup-telegram again later."
                    ) from None
                time.sleep(wait)
                continue
            raise click.ClickException(_explain(exc)) from None

        for update in updates or []:
            if isinstance(update.get("update_id"), int):
                offset = update["update_id"] + 1
            message = update.get("message") or {}
            text = (message.get("text") or "").strip()
            chat = message.get("chat") or {}
            if text.upper() != code.upper():
                continue  # other messages are ignored — we never pick a chat automatically
            if chat.get("type") != "private":
                if not warned_group:
                    click.echo("The code arrived from a group or channel and was ignored. "
                               "Please send it in a private chat with the bot.")
                    warned_group = True
                continue
            if isinstance(chat.get("id"), int):
                return chat
    return None
