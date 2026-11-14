"""Telegram notification tests — all Telegram answers are FAKE (no network, no real token).

Run from the project folder:
    .venv\\Scripts\\python -m unittest discover -s tests -v
"""

import json
import logging
import re
import socket
import sqlite3
import threading
import time
import unittest
import urllib.error
import uuid
from contextlib import closing
from datetime import timedelta

import testenv  # noqa: F401  (must be first)
from testenv import TEST_DB

import app as app_module  # noqa: E402
import auth  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import notifications  # noqa: E402
import telegram_cli  # noqa: E402
import telegram_client  # noqa: E402
import telegram_config  # noqa: E402
from helpers import order_payload  # noqa: E402
from telegram_client import TelegramBot  # noqa: E402
from validation import business_today  # noqa: E402

HOST = {"Host": "127.0.0.1:5000"}
FAKE_TOKEN = "1234567890:AAFakeTokenForTestsOnly_xxxxxxxxxxxx"
CHAT_ID = 424242
PASSWORD = "correct horse battery staple"
CSRF_RE = re.compile(r'name="csrf-token" content="([^"]+)"')


def query(sql, params=()):
    with closing(sqlite3.connect(TEST_DB)) as conn, conn:
        return conn.execute(sql, params).fetchall()


class FakeTelegram:
    """Fake transport: scripted answers per API method, records every call."""

    def __init__(self):
        self.calls = []          # (method, payload)
        self.script = {}         # method -> list of answers (last one repeats)

    def on(self, method, *answers):
        self.script[method] = list(answers)
        return self

    def __call__(self, url, payload, timeout):
        assert timeout <= 60, "every call must have a bounded timeout"
        method = url.rsplit("/", 1)[1]
        self.calls.append((method, dict(payload)))
        answers = self.script.get(method) or [(200, {"ok": True, "result": {}})]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def sent(self):
        return [p for m, p in self.calls if m == "sendMessage"]


OK_SEND = (200, {"ok": True, "result": {"message_id": 77}})
UNAUTHORIZED = (401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
BLOCKED = (403, {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"})
CHAT_NOT_FOUND = (400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})


def rate_limited(seconds):
    return (429, {"ok": False, "error_code": 429, "description": f"Too Many Requests: retry after {seconds}",
                  "parameters": {"retry_after": seconds}})


def payload(**overrides):
    """Order with made-up personal data that must NEVER appear in Telegram messages."""
    data = order_payload(name="Private Person", email="private.person@example.com", phone="+48 611 222 333",
                         city="Privatecity", street="Privatestreet 5", apartment="Apt9",
                         comment="private comment")
    data.update(overrides)
    return data


class TelegramTestCase(unittest.TestCase):
    def setUp(self):
        query("DELETE FROM notifications")
        query("DELETE FROM request_items")
        query("DELETE FROM requests")
        query("DELETE FROM login_failures")
        telegram_config.save(FAKE_TOKEN, CHAT_ID, "Test Admin (@test_admin)", "cleanspace_test_bot")
        self.fake = FakeTelegram()
        self._orig_transport = telegram_client.default_transport
        telegram_client.default_transport = self.fake  # nothing can reach the real Telegram
        self.client = app_module.app.test_client()
        self.settings = telegram_config.load()
        self.bot = TelegramBot(FAKE_TOKEN, transport=self.fake)

    def tearDown(self):
        telegram_client.default_transport = self._orig_transport
        telegram_config.remove()

    def create(self, data=None):
        r = self.client.post("/api/requests", json=data or payload(), headers=HOST)
        body = r.get_json()
        r.close()
        return r.status_code, body

    def job(self):
        rows = query("SELECT status, attempts, next_attempt_at, last_error, delivery_uncertain, "
                     "telegram_message_id, manual_retries, id FROM notifications")
        return rows[0] if rows else None

    def process(self, now=None):
        now_fn = (lambda: now) if now is not None else time.time
        return notifications.process_one(TEST_DB, self.settings, self.bot, "worker-test", now_fn=now_fn)


class DeliveryTests(TelegramTestCase):
    def test_success_plain_text_without_personal_data(self):
        code, body = self.create()
        self.assertEqual(code, 201)
        self.assertEqual(self.fake.calls, [])  # saving a request never calls Telegram
        self.assertEqual(self.job()[0], "pending")

        self.fake.on("sendMessage", OK_SEND)
        self.assertEqual(self.process(), "sent")
        status, attempts, _, error, uncertain, message_id, _, _ = self.job()
        self.assertEqual((status, attempts, error, uncertain, message_id), ("sent", 1, "", 0, 77))

        sent = self.fake.sent()
        self.assertEqual(len(sent), 1)
        msg = sent[0]
        self.assertNotIn("parse_mode", msg)
        self.assertEqual(msg["chat_id"], CHAT_ID)
        self.assertEqual(msg["text"].splitlines(), [
            "CleanSpace — New demo request",
            f"Request #{body['id']}",
            "Services:",
            "• Deep cleaning — 50 m²",
            "Total: 400 PLN",
            f"Preferred: {payload()['date']}, Morning (subject to confirmation)",
        ])
        for private in ("Private Person", "private.person", "611 222", "Privatecity", "Privatestreet", "Apt9",
                        "private comment", "127.0.0.1", "http"):
            self.assertNotIn(private, msg["text"])

        self.assertIsNone(self.process())  # nothing left; sent jobs are never resent

    def test_multi_service_message(self):
        items = [{"service": "deep", "quantity": 50}, {"service": "sofa", "quantity": 3},
                 {"service": "carpet", "quantity": "6.5"},
                 {"service": "mattress_single", "quantity": 2, "options": {"sides": "both"}}]
        code, body = self.create(payload(items=items, time="evening"))
        self.assertEqual(code, 201, body)
        self.fake.on("sendMessage", OK_SEND)
        self.assertEqual(self.process(), "sent")
        text = self.fake.sent()[0]["text"]
        self.assertEqual(text.splitlines()[2:8], [
            "Services:",
            "• Deep cleaning — 50 m²",
            "• Sofa cleaning — 3 seats",
            "• Carpet cleaning — 6.5 m²",
            "• Single mattress (both sides) — 2 pcs",
            "Total: 962.50 PLN",
        ])
        self.assertIn("Evening (subject to confirmation)", text)

    def test_long_order_gets_compact_summary(self):
        items = [{"service": "deep", "quantity": 50}, {"service": "standard", "quantity": 30},
                 {"service": "moveout", "quantity": 40}, {"service": "carpet", "quantity": 2},
                 {"service": "fitted_carpet", "quantity": 10}, {"service": "sofa", "quantity": 3},
                 {"service": "armchair", "quantity": 2}, {"service": "upholstered_chair", "quantity": 6},
                 {"service": "mattress_single", "quantity": 1, "options": {"sides": "one"}},
                 {"service": "mattress_double", "quantity": 1, "options": {"sides": "both"}}]
        self.create(payload(items=items))
        self.fake.on("sendMessage", OK_SEND)
        self.process()
        text = self.fake.sent()[0]["text"]
        self.assertIn("• … and 3 more service(s)", text)
        self.assertEqual(sum(1 for line in text.splitlines() if line.startswith("• ")), 8)
        self.assertLessEqual(len(text), notifications.TELEGRAM_TEXT_LIMIT)
        self.assertIn("Total:", text)

    def test_invalid_token_stops_without_endless_retries(self):
        for _ in range(3):
            self.create()
        self.fake.on("sendMessage", UNAUTHORIZED)
        code = notifications.run_worker(TEST_DB, self.settings, self.bot, once=True, sleep=lambda s: None)
        self.assertEqual(code, 2)  # worker stops
        self.assertEqual(len(self.fake.sent()), 1)  # only ONE call, not one per job or retry loop
        rows = query("SELECT status, attempts, last_error FROM notifications ORDER BY id")
        failed = [r for r in rows if r[0] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0][1], 1)
        self.assertIn("rejected the bot token", failed[0][2])

    def test_bot_blocked_by_user_is_permanent(self):
        self.create()
        self.fake.on("sendMessage", BLOCKED)
        self.assertEqual(self.process(), "failed")
        status, attempts, _, error, *_ = self.job()
        self.assertEqual((status, attempts), ("failed", 1))
        self.assertIn("blocked", error)
        self.assertIsNone(self.process())  # not retried automatically
        self.assertEqual(len(self.fake.sent()), 1)

    def test_chat_not_found_is_permanent(self):
        self.create()
        self.fake.on("sendMessage", CHAT_NOT_FOUND)
        self.assertEqual(self.process(), "failed")
        self.assertIn("chat not found", self.job()[3])

    def test_timeout_retries_with_backoff_then_gives_up(self):
        self.create()
        self.fake.on("sendMessage", socket.timeout("timed out"))
        now = time.time()
        self.assertEqual(self.process(now), "retry")
        status, attempts, next_at, error, uncertain, *_ = self.job()
        self.assertEqual((status, attempts, uncertain), ("pending", 1, 1))
        self.assertAlmostEqual(next_at, now + config.NOTIFY_BACKOFF_SECONDS[0], delta=1)
        self.assertIn("delivery is unknown", error)

        self.assertIsNone(self.process(now + 1))  # not due yet: no call
        self.assertEqual(len(self.fake.sent()), 1)

        t = now
        outcomes = []
        for _ in range(config.NOTIFY_MAX_ATTEMPTS - 1):
            t = self.job()[2] + 0.1
            outcomes.append(self.process(t))
        self.assertEqual(outcomes[-1], "failed")
        status, attempts, *_ = self.job()
        self.assertEqual((status, attempts), ("failed", config.NOTIFY_MAX_ATTEMPTS))
        self.assertEqual(len(self.fake.sent()), config.NOTIFY_MAX_ATTEMPTS)  # bounded
        self.assertIsNone(self.process(t + 10_000))

    def test_network_error_text_is_token_free(self):
        self.create()
        # urllib errors can mention the URL — which contains the token.
        self.fake.on("sendMessage", urllib.error.URLError(
            f"failed for https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"))
        self.assertEqual(self.process(), "retry")
        self.assertNotIn(FAKE_TOKEN, self.job()[3])
        self.assertNotIn(FAKE_TOKEN.split(":")[1], self.job()[3])

    def test_429_respects_retry_after(self):
        self.create()
        self.fake.on("sendMessage", rate_limited(7), OK_SEND)
        now = time.time()
        self.assertEqual(self.process(now), "postponed")
        status, attempts, next_at, error, *_ = self.job()
        # Telegram did not accept it, so no attempt is used up.
        self.assertEqual((status, attempts), ("pending", 0))
        self.assertAlmostEqual(next_at, now + 7, delta=0.01)
        self.assertIn("after 7 s", error)
        self.assertIsNone(self.process(now + 6.9))  # never earlier than Telegram asked
        self.assertEqual(len(self.fake.sent()), 1)
        self.assertEqual(self.process(now + 7.1), "sent")

    def test_429_long_wait_is_not_shortened(self):
        """Regression: retry_after used to be cut to min(retry_after, 3600)."""
        self.create()
        self.fake.on("sendMessage", rate_limited(7200))
        now = time.time()
        self.assertEqual(self.process(now), "postponed")
        self.assertAlmostEqual(self.job()[2], now + 7200, delta=0.01)
        self.assertIsNone(self.process(now + 3601))   # the old cap would have retried here
        self.assertIsNone(self.process(now + 7199.9))
        self.assertEqual(len(self.fake.sent()), 1)

    def test_429_beyond_job_lifetime_fails_with_explanation(self):
        self.create()
        self.fake.on("sendMessage", rate_limited(10 ** 7))
        now = time.time()
        self.assertEqual(self.process(now), "failed")
        status, _, _, error, *_ = self.job()
        self.assertEqual(status, "failed")
        self.assertIn("past the 24-hour limit", error)
        self.assertIn(str(10 ** 7), error)
        self.assertEqual(len(self.fake.sent()), 1)  # not sent earlier, not retried

    def test_429_after_manual_retry_waits_the_full_time(self):
        self.create()
        self.fake.on("sendMessage", BLOCKED)
        self.process()
        rid = query("SELECT request_id FROM notifications")[0][0]
        db.retry_notification(TEST_DB, rid, time.time())
        self.fake.on("sendMessage", rate_limited(10 ** 6))
        now = time.time()
        self.assertEqual(self.process(now), "postponed")  # manual jobs have no 24 h limit
        self.assertAlmostEqual(self.job()[2], now + 10 ** 6, delta=0.01)

    def test_repeated_request_creates_one_notification(self):
        data = payload()
        self.assertEqual(self.create(data)[0], 201)
        self.assertEqual(self.create(data)[0], 200)  # same idempotency token
        self.assertEqual(self.create(data)[0], 200)
        self.assertEqual(query("SELECT COUNT(*) FROM requests")[0][0], 1)
        self.assertEqual(query("SELECT COUNT(*) FROM notifications")[0][0], 1)

    def test_request_saved_even_if_telegram_is_down(self):
        self.fake.on("sendMessage", urllib.error.URLError("no route to host"))
        code, body = self.create()
        self.assertEqual(code, 201)
        self.assertIsInstance(body["id"], int)
        self.assertEqual(self.fake.calls, [])

    def test_old_requests_are_not_sent(self):
        telegram_config.remove()
        self.create()  # created while NOT configured -> no job
        telegram_config.save(FAKE_TOKEN, CHAT_ID, "x", "bot")
        self.assertEqual(query("SELECT COUNT(*) FROM notifications")[0][0], 0)
        self.assertEqual(notifications.run_worker(TEST_DB, self.settings, self.bot, once=True), 0)
        self.assertEqual(self.fake.calls, [])

    def test_stale_pending_job_expires_but_manual_retry_sends(self):
        self.create()
        old = time.time() - config.NOTIFY_MAX_AGE_SECONDS - 60
        query("UPDATE notifications SET created_at = ?", (old,))
        self.assertEqual(self.process(), "expired")
        self.assertEqual(self.job()[0], "failed")
        self.assertEqual(self.fake.calls, [])
        rid = query("SELECT request_id FROM notifications")[0][0]
        self.assertEqual(db.retry_notification(TEST_DB, rid, time.time())[0], "queued")
        self.fake.on("sendMessage", OK_SEND)
        self.assertEqual(self.process(), "sent")


class TelegramPauseTests(TelegramTestCase):
    """Regression: a manual retry used to skip Telegram's 429 pause (retry_after).

    Time is controlled: every call gets an explicit `now`, so no real waiting is needed.
    """

    PAUSE = 90_000  # > 24 h: the automatic job fails, the pause must still hold

    def blocked_until(self):
        return query("SELECT blocked_until FROM notifications")[0][0]

    def fail_with_long_pause(self):
        _, body = self.create()
        self.fake.on("sendMessage", rate_limited(self.PAUSE), OK_SEND)
        now = time.time()
        self.assertEqual(self.process(now), "failed")
        self.assertEqual(len(self.fake.sent()), 1)
        self.assertAlmostEqual(self.blocked_until(), now + self.PAUSE, delta=0.01)
        return body["id"], now

    def test_manual_retry_does_not_skip_the_pause(self):
        rid, now = self.fail_with_long_pause()
        result, row = db.retry_notification(TEST_DB, rid, now + 1)
        self.assertEqual(result, "queued")
        self.assertEqual(row["status"], "pending")
        # Queued now, but due only when the pause ends.
        self.assertAlmostEqual(row["next_attempt_at"], now + self.PAUSE, delta=0.01)
        for t in (now + 1, now + 2, now + 3600, now + self.PAUSE - 0.1):
            self.assertIsNone(self.process(t), t - now)
        self.assertEqual(len(self.fake.sent()), 1)          # no extra send during the pause
        self.assertEqual(self.process(now + self.PAUSE + 0.1), "sent")
        self.assertEqual(len(self.fake.sent()), 2)

    def test_repeated_retry_click_keeps_the_pause(self):
        rid, now = self.fail_with_long_pause()
        self.assertEqual(db.retry_notification(TEST_DB, rid, now + 1)[0], "queued")
        before = query("SELECT next_attempt_at, blocked_until, manual_retries FROM notifications")[0]
        self.assertEqual(db.retry_notification(TEST_DB, rid, now + 2)[0], "not_failed")
        self.assertEqual(query("SELECT next_attempt_at, blocked_until, manual_retries FROM notifications")[0], before)
        self.assertIsNone(self.process(now + 3))
        self.assertEqual(len(self.fake.sent()), 1)

    def test_pause_survives_restart_and_forced_due_time(self):
        rid, now = self.fail_with_long_pause()
        db.retry_notification(TEST_DB, rid, now + 1)
        # Server restart: init_db runs again; the pause is stored in the database.
        db.init_db(TEST_DB)
        self.assertAlmostEqual(self.blocked_until(), now + self.PAUSE, delta=0.01)
        # Even if the due time were reset (e.g. a released job), claiming checks the pause itself.
        query("UPDATE notifications SET next_attempt_at = 0")
        other_worker = notifications.process_one(TEST_DB, self.settings, self.bot, "worker-after-restart",
                                                 now_fn=lambda: now + 10)
        self.assertIsNone(other_worker)
        self.assertIsNone(db.claim_notification(TEST_DB, "w2", now + 10, 60))
        # A real worker run (real clock) sends nothing either.
        self.assertEqual(notifications.run_worker(TEST_DB, self.settings, self.bot, once=True), 0)
        self.assertEqual(len(self.fake.sent()), 1)
        self.assertEqual(self.process(now + self.PAUSE + 1), "sent")

    def test_short_pause_still_postpones_and_keeps_limits(self):
        """A 429 inside the 24 h window: pending, no attempt used, pause stored too."""
        self.create()
        self.fake.on("sendMessage", rate_limited(30), OK_SEND)
        now = time.time()
        self.assertEqual(self.process(now), "postponed")
        status, attempts, next_at, *_ = self.job()
        self.assertEqual((status, attempts), ("pending", 0))
        self.assertAlmostEqual(self.blocked_until(), now + 30, delta=0.01)
        self.assertIsNone(self.process(now + 29.9))
        self.assertEqual(self.process(now + 30.1), "sent")

    def test_pause_never_shortened_by_a_later_shorter_value(self):
        rid, now = self.fail_with_long_pause()
        db.retry_notification(TEST_DB, rid, now + 1)
        job = db.claim_notification(TEST_DB, "w", now + self.PAUSE + 1, 60)
        self.assertIsNotNone(job)
        # Recording a smaller pause value must not move the stored one backwards.
        self.assertTrue(db.finish_notification(TEST_DB, job["id"], "w", "pending", now + self.PAUSE + 2,
                                               blocked_until=now + 5, next_attempt_at=now + 5))
        self.assertAlmostEqual(self.blocked_until(), now + self.PAUSE, delta=0.01)


class QueueTests(TelegramTestCase):
    def test_queue_survives_restart(self):
        self.create()
        self.create()
        # "Restart": run the startup migration again and use a fresh app/client.
        self.assertIsNone(db.init_db(TEST_DB, None))
        self.assertEqual(query("SELECT COUNT(*) FROM notifications WHERE status = 'pending'")[0][0], 2)
        self.fake.on("sendMessage", OK_SEND)
        self.assertEqual(notifications.run_worker(TEST_DB, self.settings, self.bot, once=True), 0)
        self.assertEqual(query("SELECT COUNT(*) FROM notifications WHERE status = 'sent'")[0][0], 2)

    def test_lease_prevents_double_claim_and_recovers_after_crash(self):
        self.create()
        now = time.time()
        job_a = db.claim_notification(TEST_DB, "worker-A", now, config.NOTIFY_LEASE_SECONDS)
        self.assertIsNotNone(job_a)
        # Worker B cannot take it while A's lease is valid.
        self.assertIsNone(db.claim_notification(TEST_DB, "worker-B", now + 30, config.NOTIFY_LEASE_SECONDS))
        # A "crashes". After the lease expires B takes it, flagged as possibly delivered.
        job_b = db.claim_notification(TEST_DB, "worker-B", now + config.NOTIFY_LEASE_SECONDS + 1,
                                      config.NOTIFY_LEASE_SECONDS)
        self.assertIsNotNone(job_b)
        self.assertEqual(job_b["attempts"], 2)
        self.assertEqual(job_b["delivery_uncertain"], 1)
        # A comes back late: its result is NOT recorded (fencing by locked_by).
        self.assertFalse(db.finish_notification(TEST_DB, job_a["id"], "worker-A", "sent", now + 70))
        self.assertTrue(db.finish_notification(TEST_DB, job_b["id"], "worker-B", "sent", now + 71))

    def test_two_workers_racing_claim_once(self):
        for trial in range(10):
            query("DELETE FROM notifications")
            self.create()
            barrier = threading.Barrier(4)
            results = []

            def worker(name):
                barrier.wait()
                results.append(db.claim_notification(TEST_DB, name, time.time(), 60))

            threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sum(r is not None for r in results), 1, f"trial {trial}")

    def test_manual_retry_reuses_the_same_job(self):
        self.create()
        self.fake.on("sendMessage", BLOCKED)
        self.process()
        job_id = self.job()[7]
        rid = query("SELECT request_id FROM notifications")[0][0]
        result, row = db.retry_notification(TEST_DB, rid, time.time())
        self.assertEqual(result, "queued")
        self.assertEqual((row["id"], row["status"], row["attempts"], row["manual_retries"]),
                         (job_id, "pending", 0, 1))
        self.assertEqual(query("SELECT COUNT(*) FROM notifications")[0][0], 1)
        self.assertEqual(db.retry_notification(TEST_DB, rid, time.time())[0], "not_failed")


class NotConfiguredTests(TelegramTestCase):
    def setUp(self):
        super().setUp()
        telegram_config.remove()

    def test_site_saves_without_queueing(self):
        code, body = self.create()
        self.assertEqual(code, 201)
        self.assertEqual(query("SELECT COUNT(*) FROM notifications")[0][0], 0)
        self.assertEqual(self.fake.calls, [])

    def test_cli_commands_explain_missing_setup(self):
        runner = app_module.app.test_cli_runner()
        for command in ("telegram-worker", "test-telegram"):
            result = runner.invoke(args=[command])
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("not configured", result.output)
        self.assertEqual(self.fake.calls, [])

    def test_invalid_config_file_counts_as_not_configured(self):
        config.TELEGRAM_CONFIG_FILE.write_text('{"bot_token": "nope", "chat_id": 1}', encoding="utf-8")
        self.assertFalse(telegram_config.is_configured())
        self.assertEqual(self.create()[0], 201)


class CrmTests(TelegramTestCase):
    def setUp(self):
        super().setUp()
        auth.save_admin_password(PASSWORD)

    def login(self):
        with self.client.get("/admin/login", headers=HOST) as r:
            token = CSRF_RE.search(r.get_data(as_text=True)).group(1)
        r = self.client.post("/admin/login", headers=HOST, data={"password": PASSWORD, "csrf_token": token})
        r.close()
        with self.client.get("/admin", headers=HOST) as r:
            return CSRF_RE.search(r.get_data(as_text=True)).group(1)

    def notification(self, rid):
        with self.client.get(f"/admin/api/requests/{rid}", headers=HOST) as r:
            return r.get_json()["notification"]

    def retry(self, rid, csrf):
        headers = dict(HOST)
        if csrf is not None:
            headers["X-CSRFToken"] = csrf
        r = self.client.post(f"/admin/api/requests/{rid}/notification/retry", json={}, headers=headers)
        body = r.get_json()
        r.close()
        return r.status_code, body

    def test_states_shown_separately_from_request_status(self):
        csrf = self.login()
        telegram_config.remove()
        _, old = self.create()
        self.assertEqual(self.notification(old["id"])["state"], "not_configured")
        telegram_config.save(FAKE_TOKEN, CHAT_ID, "x", "bot")
        _, new = self.create()
        n = self.notification(new["id"])
        self.assertEqual((n["state"], n["label"], n["attempts"]), ("pending", "Pending", 0))
        db.claim_notification(TEST_DB, "w", time.time(), 60)
        self.assertEqual(self.notification(new["id"])["label"], "Sending")
        # Cleaning status is independent:
        with self.client.get(f"/admin/api/requests/{new['id']}", headers=HOST) as r:
            self.assertEqual(r.get_json()["request"]["status"], "new")
        del csrf

    def test_manual_retry_is_protected(self):
        _, body = self.create()
        rid = body["id"]
        self.fake.on("sendMessage", BLOCKED)
        self.process()

        # Not logged in -> 401 (with CSRF token from an anonymous session) / 400 (without).
        self.assertEqual(self.retry(rid, None)[0], 400)
        with self.client.get("/admin/login", headers=HOST) as r:
            anon = CSRF_RE.search(r.get_data(as_text=True)).group(1)
        self.assertEqual(self.retry(rid, anon)[0], 401)

        csrf = self.login()
        self.assertEqual(self.retry(rid, None)[0], 400)            # missing CSRF
        self.assertEqual(self.retry(rid, "forged")[0], 400)        # wrong CSRF
        self.assertEqual(self.job()[0], "failed")                  # unchanged so far

        code, body = self.retry(rid, csrf)
        self.assertEqual(code, 200)
        self.assertEqual(body["notification"]["state"], "pending")
        self.assertEqual(self.job()[0], "pending")
        code, body = self.retry(rid, csrf)                          # only failed jobs
        self.assertEqual((code, body["error"]), (409, "not_failed"))
        self.assertEqual(self.retry(999999, csrf)[0], 404)

        telegram_config.remove()
        _, other = self.create()
        self.assertEqual(self.retry(other["id"], csrf)[1]["error"], "no_notification")

    def test_retry_during_telegram_pause_is_queued_but_waits(self):
        _, body = self.create()
        rid = body["id"]
        self.fake.on("sendMessage", rate_limited(90_000))
        self.process()
        csrf = self.login()
        n = self.notification(rid)
        self.assertEqual(n["state"], "failed")
        self.assertIsNotNone(n["blockedUntil"])
        self.assertTrue(n["canRetry"])

        code, body = self.retry(rid, csrf)
        self.assertEqual(code, 200)
        self.assertTrue(body["waitsForPause"])
        self.assertEqual(body["notification"]["state"], "pending")
        self.assertEqual(body["notification"]["blockedUntil"], n["blockedUntil"])
        self.assertEqual(body["notification"]["nextAttemptAt"], n["blockedUntil"])  # not "now"
        code, again = self.retry(rid, csrf)                       # second click: refused by the server
        self.assertEqual((code, again["error"]), (409, "not_failed"))
        self.assertIsNone(self.process())                         # worker: still paused
        self.assertEqual(len(self.fake.sent()), 1)

    def test_retry_without_pause_is_not_marked_waiting(self):
        _, body = self.create()
        self.fake.on("sendMessage", BLOCKED)
        self.process()
        csrf = self.login()
        code, res = self.retry(body["id"], csrf)
        self.assertEqual(code, 200)
        self.assertFalse(res["waitsForPause"])
        self.assertIsNone(res["notification"]["blockedUntil"])

    def test_get_is_not_a_retry(self):
        _, body = self.create()
        self.fake.on("sendMessage", BLOCKED)
        self.process()
        self.login()
        with self.client.get(f"/admin/api/requests/{body['id']}/notification/retry", headers=HOST) as r:
            self.assertIn(r.status_code, (404, 405))
        self.assertEqual(self.job()[0], "failed")


class SetupCliTests(TelegramTestCase):
    def setUp(self):
        super().setUp()
        telegram_config.remove()
        self.runner = app_module.app.test_cli_runner()
        self._orig_code = telegram_cli.make_setup_code
        telegram_cli.make_setup_code = lambda: "CS-ABCD1234"
        self.fake.on("getMe", (200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "cleanspace_test_bot"}}))
        self.fake.on("getWebhookInfo", (200, {"ok": True, "result": {"url": ""}}))
        self.fake.on("getUpdates", (200, {"ok": True, "result": [
            {"update_id": 10, "message": {"text": "hello", "chat": {"id": 111, "type": "private", "first_name": "Stranger"}}},
            {"update_id": 11, "message": {"text": "CS-ABCD1234", "chat": {"id": -222, "type": "group", "title": "Some group"}}},
            {"update_id": 12, "message": {"text": " cs-abcd1234 ", "chat": {"id": 333, "type": "private",
                                                                          "first_name": "Owner", "username": "owner"}}},
        ]}))

    def tearDown(self):
        telegram_cli.make_setup_code = self._orig_code
        super().tearDown()

    def run_setup(self, answers):
        return self.runner.invoke(args=["setup-telegram"], input=answers)

    def test_finds_exact_private_chat_and_sends_nothing_without_consent(self):
        result = self.run_setup(f"{FAKE_TOKEN}\ny\nn\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn(FAKE_TOKEN, result.output)
        self.assertNotIn(FAKE_TOKEN.split(":")[1], result.output)
        self.assertIn("Owner (@owner), chat id 333", result.output)
        self.assertIn("group or channel and was ignored", result.output)
        settings = telegram_config.load()
        self.assertEqual((settings.chat_id, settings.bot_username), (333, "cleanspace_test_bot"))
        self.assertEqual(self.fake.sent(), [])  # no test message without explicit "y"
        self.assertNotIn("deleteWebhook", [m for m, _ in self.fake.calls])

    def test_test_message_only_after_confirmation(self):
        self.fake.on("sendMessage", OK_SEND)
        result = self.run_setup(f"{FAKE_TOKEN}\ny\ny\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(self.fake.sent()), 1)
        self.assertEqual(self.fake.sent()[0]["chat_id"], 333)

    def test_declined_recipient_saves_nothing(self):
        result = self.run_setup(f"{FAKE_TOKEN}\nn\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(telegram_config.is_configured())

    def test_webhook_conflict_stops_without_deleting(self):
        self.fake.on("getWebhookInfo", (200, {"ok": True, "result": {"url": "https://example.com/hook"}}))
        result = self.run_setup(f"{FAKE_TOKEN}\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("webhook", result.output)
        self.assertIn("will NOT remove", result.output)
        self.assertNotIn("https://example.com/hook", result.output)
        self.assertFalse(telegram_config.is_configured())
        methods = [m for m, _ in self.fake.calls]
        self.assertNotIn("deleteWebhook", methods)
        self.assertNotIn("getUpdates", methods)

    def test_bad_tokens_are_rejected(self):
        result = self.run_setup("not-a-token\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(self.fake.calls, [])
        self.fake.on("getMe", UNAUTHORIZED)
        result = self.run_setup(f"{FAKE_TOKEN}\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("rejected this token", result.output)
        self.assertNotIn(FAKE_TOKEN, result.output)
        self.assertFalse(telegram_config.is_configured())

    def test_code_not_received(self):
        self.fake.on("getUpdates", (200, {"ok": True, "result": [
            {"update_id": 1, "message": {"text": "hi", "chat": {"id": 5, "type": "private"}}}]}))
        orig = config.TELEGRAM_SETUP_WAIT_SECONDS
        config.TELEGRAM_SETUP_WAIT_SECONDS = 0.05
        try:
            result = self.run_setup(f"{FAKE_TOKEN}\n")
        finally:
            config.TELEGRAM_SETUP_WAIT_SECONDS = orig
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("not received", result.output)
        self.assertFalse(telegram_config.is_configured())  # never picked chat 5 automatically

    def test_test_telegram_command(self):
        telegram_config.save(FAKE_TOKEN, CHAT_ID, "Test Admin", "bot")
        self.fake.on("sendMessage", OK_SEND)
        result = self.runner.invoke(args=["test-telegram"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Test message sent", result.output)
        self.assertEqual(len(self.fake.sent()), 1)
        self.fake.on("sendMessage", BLOCKED)
        result = self.runner.invoke(args=["test-telegram"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("NOT sent", result.output)
        self.assertNotIn(FAKE_TOKEN, result.output)

    def test_worker_command_once(self):
        telegram_config.save(FAKE_TOKEN, CHAT_ID, "Test Admin", "bot")
        self.create()
        self.fake.on("sendMessage", OK_SEND)
        result = self.runner.invoke(args=["telegram-worker", "--once"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn(FAKE_TOKEN, result.output)
        self.assertEqual(self.job()[0], "sent")


class SecretTests(TelegramTestCase):
    def test_no_token_or_personal_data_in_logs_db_or_api(self):
        auth.save_admin_password(PASSWORD)
        scenarios = [OK_SEND, UNAUTHORIZED, BLOCKED, rate_limited(3), socket.timeout("t"),
                     urllib.error.URLError(f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage refused"),
                     (500, {"ok": False, "description": f"boom bot{FAKE_TOKEN}"}), (502, None)]
        with self.assertLogs("cleanspace", level="INFO") as logs:
            for answer in scenarios:
                query("DELETE FROM notifications")
                self.create()
                self.fake.on("sendMessage", answer)
                self.process()
        text = "\n".join(logs.output)
        stored = json.dumps([list(r) for r in query("SELECT * FROM notifications")])
        for secret in (FAKE_TOKEN, FAKE_TOKEN.split(":")[1], "Private Person", "private.person", "private comment"):
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, stored)

    def test_redact_filter_and_repr(self):
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "url=%s", (f"/bot{FAKE_TOKEN}/getMe",), None)
        telegram_cli.RedactTokenFilter(FAKE_TOKEN).filter(record)
        self.assertNotIn(FAKE_TOKEN, record.getMessage())
        self.assertNotIn(FAKE_TOKEN, repr(telegram_config.load()))
        err = telegram_client.TelegramError("auth", telegram_client.redact(f"bad {FAKE_TOKEN}"), 401)
        self.assertNotIn(FAKE_TOKEN, str(err))

    def test_config_file_location(self):
        self.assertFalse(str(config.TELEGRAM_CONFIG_FILE).startswith(str(config.STATIC_DIR)))
        with self.client.get("/telegram.json", headers=HOST) as r:
            self.assertEqual(r.status_code, 404)
        with open(config.BASE_DIR / ".gitignore", encoding="utf-8") as fh:
            self.assertIn("instance/", fh.read())


class UncertainDeliveryTests(TelegramTestCase):
    """Regression: delivery_uncertain used to be set for timeouts only."""

    def outcome_for(self, answer):
        query("DELETE FROM notifications")
        self.create()
        self.fake.on("sendMessage", answer)
        self.process()
        status, _, _, error, uncertain, *_ = self.job()
        return status, uncertain, error

    def test_cases_where_telegram_may_have_accepted(self):
        cases = {
            "timeout": socket.timeout("timed out"),
            "reset after sending": ConnectionResetError(10054, "reset by peer"),
            "remote closed": __import__("http.client").client.RemoteDisconnected("closed"),
            "incomplete read": __import__("http.client").client.IncompleteRead(b"{\"ok\":"),
            "broken while sending": urllib.error.URLError(ConnectionResetError(10054, "reset")),
            "unreadable 200": (200, None),
            "200 without ok": (200, {"result": {"message_id": 1}}),
            "proxy 502 page": (502, None),
        }
        for name, answer in cases.items():
            with self.subTest(name):
                status, uncertain, error = self.outcome_for(answer)
                self.assertEqual((status, uncertain), ("pending", 1), error)
                self.assertIn("Delivery unknown", error)

    def test_cases_where_telegram_certainly_did_not_accept(self):
        cases = {
            "connection refused": urllib.error.URLError(ConnectionRefusedError(10061, "refused")),
            "dns failure": urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed")),
            "readable 500": (500, {"ok": False, "error_code": 500, "description": "Internal Server Error"}),
            "429": rate_limited(5),
            "403": BLOCKED,
        }
        for name, answer in cases.items():
            with self.subTest(name):
                _, uncertain, error = self.outcome_for(answer)
                self.assertEqual(uncertain, 0, error)

    def test_warning_is_kept_after_later_success(self):
        auth.save_admin_password(PASSWORD)
        _, body = self.create()
        self.fake.on("sendMessage", ConnectionResetError(10054, "reset"), OK_SEND)
        now = time.time()
        self.assertEqual(self.process(now), "retry")
        self.assertEqual(self.process(now + 10_000), "sent")
        status, attempts, _, error, uncertain, *_ = self.job()
        self.assertEqual((status, attempts, uncertain), ("sent", 2, 1))
        # The CRM still reports the possible duplicate.
        with self.client.get("/admin/login", headers=HOST) as r:
            token = CSRF_RE.search(r.get_data(as_text=True)).group(1)
        self.client.post("/admin/login", headers=HOST, data={"password": PASSWORD, "csrf_token": token}).close()
        with self.client.get(f"/admin/api/requests/{body['id']}", headers=HOST) as r:
            n = r.get_json()["notification"]
        self.assertEqual((n["state"], n["deliveryUncertain"]), ("sent", True))

    def test_manual_retry_keeps_warning(self):
        self.create()
        self.fake.on("sendMessage", socket.timeout("t"))
        t = time.time()
        for i in range(config.NOTIFY_MAX_ATTEMPTS):
            self.process(t + 10_000 * (i + 1))
        self.assertEqual(self.job()[0], "failed")
        rid = query("SELECT request_id FROM notifications")[0][0]
        db.retry_notification(TEST_DB, rid, time.time())
        self.fake.on("sendMessage", OK_SEND)
        self.process()
        self.assertEqual((self.job()[0], self.job()[4]), ("sent", 1))


class SettingsChangeTests(TelegramTestCase):
    """Regression: the worker used to keep the settings loaded at start forever."""

    def test_stops_when_settings_file_removed(self):
        self.create()
        self.create()
        self.fake.on("sendMessage", OK_SEND)
        sent_before_removal = []

        def transport(url, payload, timeout):
            answer = self.fake(url, payload, timeout)
            sent_before_removal.append(1)
            telegram_config.remove()  # admin disables Telegram while the worker runs
            return answer

        bot = TelegramBot(FAKE_TOKEN, transport=transport)
        loops = {"n": 0}

        def guard():  # safety net: a broken test must fail, not hang forever
            loops["n"] += 1
            return loops["n"] > 50

        code = notifications.run_worker(TEST_DB, self.settings, bot, sleep=lambda s: None, should_stop=guard)
        self.assertEqual(code, notifications.EXIT_SETTINGS_CHANGED)
        self.assertEqual(len(self.fake.sent()), 1)  # the second job was NOT sent
        rows = query("SELECT status, attempts FROM notifications ORDER BY id")
        self.assertEqual(rows, [("sent", 1), ("pending", 0)])

    def test_stops_when_token_or_chat_changes(self):
        for change in ({"token": "1234567890:AAAnotherFakeTokenForTests_yyyyyyyyy"}, {"chat_id": 999}):
            with self.subTest(change=change):
                query("DELETE FROM notifications")
                telegram_config.save(FAKE_TOKEN, CHAT_ID, "x", "bot")
                self.create()
                telegram_config.save(change.get("token", FAKE_TOKEN), change.get("chat_id", CHAT_ID), "x", "bot")
                self.fake.calls.clear()
                code = notifications.run_worker(TEST_DB, self.settings, self.bot, once=True)
                self.assertEqual(code, notifications.EXIT_SETTINGS_CHANGED)
                self.assertEqual(self.fake.sent(), [])  # nothing with the OLD token/chat
                self.assertEqual(self.job()[:2], ("pending", 0))

    def test_change_between_check_and_send_releases_job(self):
        self.create()
        code = notifications.process_one(TEST_DB, self.settings, self.bot, "w",
                                         settings_still_valid=lambda: False)
        self.assertEqual(code, "settings_changed")
        self.assertEqual(self.fake.sent(), [])
        status, attempts, next_at, *_ = self.job()
        self.assertEqual((status, attempts), ("pending", 0))  # no attempt used, not locked

    def test_cli_explains_the_stop(self):
        self.create()
        runner = app_module.app.test_cli_runner()
        orig_load = telegram_config.load
        calls = {"n": 0}

        def load_then_vanish():
            calls["n"] += 1
            return orig_load() if calls["n"] == 1 else None  # present at start, removed later

        telegram_config.load = load_then_vanish
        try:
            result = runner.invoke(args=["telegram-worker", "--once"])
        finally:
            telegram_config.load = orig_load
        self.assertEqual(result.exit_code, notifications.EXIT_SETTINGS_CHANGED, result.output)
        self.assertIn("removed or changed", result.output)
        self.assertIn("cannot be recalled", result.output)
        self.assertEqual(self.fake.sent(), [])


class RealTransportTests(unittest.TestCase):
    """The real urllib transport against a LOCAL fake HTTP server (never api.telegram.org)."""

    @classmethod
    def setUpClass(cls):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self.server.last_body = self.rfile.read(length).decode()
                self.server.last_path = self.path
                method = self.path.rsplit("/", 1)[1]
                if method == "slow":
                    time.sleep(1.5)
                if method == "reset":
                    self.close_connection = True
                    return  # request received, connection closed without any answer
                if method == "garbage":
                    raw = b"<html>not json</html>"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                answers = {
                    "getMe": (200, {"ok": True, "result": {"username": "local_bot"}}),
                    "unauth": (401, {"ok": False, "error_code": 401, "description": "Unauthorized"}),
                    "limited": (429, {"ok": False, "description": "Too Many Requests",
                                      "parameters": {"retry_after": 9}}),
                    "broken": (502, None),
                    "slow": (200, {"ok": True, "result": {}}),
                    "sendMessage": (200, {"ok": True, "result": {"message_id": 5}}),
                    "error500": (500, {"ok": False, "error_code": 500, "description": "Internal Server Error"}),
                }
                status, body = answers.get(method, (404, {"ok": False, "description": "Not Found"}))
                raw = b"<html>bad gateway</html>" if body is None else json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
                    pass  # the client already gave up (timeout test)

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls._orig_base = config.TELEGRAM_API_BASE
        config.TELEGRAM_API_BASE = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        config.TELEGRAM_API_BASE = cls._orig_base
        cls.server.shutdown()
        cls.server.server_close()

    def bot(self, timeout=5):
        return TelegramBot(FAKE_TOKEN, transport=telegram_client.urllib_transport, timeout=timeout)

    def test_ok_and_form_encoding(self):
        self.assertEqual(self.bot().get_me()["username"], "local_bot")
        self.assertEqual(self.server.last_path, f"/bot{FAKE_TOKEN}/getMe")

    def test_http_errors_are_classified_and_token_free(self):
        bot = self.bot()
        cases = {"unauth": ("auth", None), "limited": ("rate_limited", 9),
                 "broken": ("bad_response", None), "missing": ("auth", None),
                 "garbage": ("bad_response", None), "reset": ("connection_lost", None),
                 "error500": ("server", None)}
        for method, (kind, retry) in cases.items():
            with self.subTest(method=method):
                with self.assertRaises(telegram_client.TelegramError) as ctx:
                    bot._call(method)
                self.assertEqual(ctx.exception.kind, kind)
                self.assertEqual(ctx.exception.retry_after, retry)
                self.assertNotIn(FAKE_TOKEN, str(ctx.exception))
                self.assertIsNone(ctx.exception.__cause__)  # no chained urllib error with the URL

    def test_timeout_is_bounded(self):
        started = time.monotonic()
        with self.assertRaises(telegram_client.TelegramError) as ctx:
            self.bot(timeout=0.5)._call("slow")
        self.assertEqual(ctx.exception.kind, "timeout")
        self.assertLess(time.monotonic() - started, 1.4)

    def test_send_message_has_no_parse_mode(self):
        result = self.bot().send_message(1, "a <b>b</b>")
        self.assertEqual(result["message_id"], 5)
        self.assertNotIn("parse_mode", self.server.last_body)
        self.assertIn("text=a+%3Cb%3Eb%3C%2Fb%3E", self.server.last_body)

    def test_connection_refused_is_network_error(self):
        orig = config.TELEGRAM_API_BASE
        config.TELEGRAM_API_BASE = "http://127.0.0.1:9"  # nothing listens here
        try:
            with self.assertRaises(telegram_client.TelegramError) as ctx:
                self.bot(timeout=2).get_me()
        finally:
            config.TELEGRAM_API_BASE = orig
        self.assertIn(ctx.exception.kind, ("network", "timeout"))
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
