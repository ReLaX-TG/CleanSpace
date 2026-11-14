"""Admin CRM tests: auth, CSRF, throttling, list/search/pagination, updates, conflicts,
CLI password command and schema migration.

Run from the project folder:
    .venv\\Scripts\\python -m unittest discover -s tests -v
"""

import json
import logging
import re
import shutil
import sqlite3
import time
import unittest
import uuid
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import testenv  # noqa: F401  (must be first)
from testenv import INSTANCE, ROOT, TEST_DB

import app as app_module  # noqa: E402
import auth  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
from helpers import insert_order, order_payload  # noqa: E402
from pricing import price_order  # noqa: E402
from validation import business_today, validate_order  # noqa: E402

HOST = {"Host": "127.0.0.1:5000"}
PASSWORD = "correct horse battery staple"
CSRF_RE = re.compile(r'name="csrf-token" content="([^"]+)"')


def query(sql, params=()):
    with closing(sqlite3.connect(TEST_DB)) as conn, conn:
        return conn.execute(sql, params).fetchall()


def insert_request(name="Test Person", email="test@example.com", comment="", created_at=None,
                   status="new", cleaning_type="deep", area=50, items=None, **extra):
    """Insert an order directly (fast) through the real validation + pricing code."""
    rid = insert_order(name=name, email=email, comment=comment,
                       items=items or [{"service": cleaning_type, "quantity": area}], **extra)
    if created_at or status != "new":
        query("UPDATE requests SET created_at = COALESCE(?, created_at), status = ? WHERE id = ?",
              (created_at, status, rid))
    return rid


class AdminTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app_module.app

    def setUp(self):
        for table in ("notifications", "request_items", "requests"):
            query(f"DELETE FROM {table}")
        query("DELETE FROM login_failures")
        auth.save_admin_password(PASSWORD)
        self.client = self.app.test_client()

    # ---------------------------------------------------------------- helpers
    def csrf(self, path="/admin/login"):
        with self.client.get(path, headers=HOST) as r:
            match = CSRF_RE.search(r.get_data(as_text=True))
        self.assertIsNotNone(match, f"no CSRF token on {path}")
        return match.group(1)

    def login(self, password=PASSWORD):
        token = self.csrf()
        return self.client.post("/admin/login", headers=HOST,
                                data={"password": password, "csrf_token": token})

    def logged_in_csrf(self):
        r = self.login()
        self.assertEqual(r.status_code, 303)
        r.close()
        return self.csrf("/admin")

    def patch(self, request_id, body, token):
        headers = dict(HOST)
        if token is not None:
            headers["X-CSRFToken"] = token
        return self.client.patch(f"/admin/api/requests/{request_id}", json=body, headers=headers)

    def status_of(self, path):
        with self.client.get(path, headers=HOST) as r:
            return r.status_code


class AccessTests(AdminTestCase):
    def test_unauthenticated_pages_redirect_and_api_reveals_nothing(self):
        rid = insert_request(name="Hidden Person", email="hidden@example.com", comment="secret")
        for path in ("/admin", "/admin/", f"/admin/requests/{rid}"):
            with self.client.get(path, headers=HOST) as r:
                self.assertEqual(r.status_code, 302, path)
                self.assertIn("/admin/login", r.headers["Location"])
                self.assertNotIn(b"Hidden Person", r.data)
        for path in ("/admin/api/requests", f"/admin/api/requests/{rid}", "/admin/api/requests?q=hidden"):
            with self.client.get(path, headers=HOST) as r:
                self.assertEqual(r.status_code, 401, path)
                self.assertEqual(r.get_json()["error"], "unauthorized")
                for secret in (b"Hidden", b"hidden@", b"secret"):
                    self.assertNotIn(secret, r.data)

    def test_unauthenticated_patch_rejected_and_row_unchanged(self):
        rid = insert_request()
        # Without CSRF token -> CSRF failure.
        r = self.patch(rid, {"status": "completed", "adminNote": "x", "version": 1}, token=None)
        self.assertEqual(r.status_code, 400)
        # With a valid CSRF token from an anonymous session -> still not logged in.
        token = self.csrf()
        r = self.patch(rid, {"status": "completed", "adminNote": "x", "version": 1}, token=token)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(query("SELECT status, admin_note, version FROM requests")[0], ("new", "", 1))

    def test_correct_and_wrong_password(self):
        r = self.login("wrong password 123")
        self.assertEqual(r.status_code, 401)
        self.assertIn(b"Incorrect password", r.data)
        r.close()
        self.assertEqual(self.status_of("/admin/api/requests"), 401)

        r = self.login()
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["Location"].endswith("/admin"))
        r.close()
        self.assertEqual(self.status_of("/admin/api/requests"), 200)
        self.assertEqual(self.status_of("/admin"), 200)

    def test_session_cookie_flags(self):
        r = self.login()
        cookies = [c for c in r.headers.getlist("Set-Cookie") if c.startswith(config.SESSION_COOKIE_NAME + "=")]
        r.close()
        self.assertTrue(cookies)
        cookie = cookies[-1]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn("Secure", cookie)  # local HTTP: Secure would block the cookie
        self.assertIn("Expires=", cookie)  # permanent session with a limited lifetime

    def test_login_requires_csrf(self):
        r = self.client.post("/admin/login", headers=HOST, data={"password": PASSWORD})
        self.assertEqual(r.status_code, 400)
        r.close()
        r = self.client.post("/admin/login", headers=HOST,
                             data={"password": PASSWORD, "csrf_token": "forged"})
        self.assertEqual(r.status_code, 400)
        r.close()
        self.assertEqual(self.status_of("/admin/api/requests"), 401)

    def test_logout_is_post_only_and_needs_csrf(self):
        token = self.logged_in_csrf()
        # GET is not a logout route (404/405), and it does not end the session.
        self.assertIn(self.status_of("/admin/logout"), (404, 405))
        self.assertEqual(self.status_of("/admin/api/requests"), 200)
        r = self.client.post("/admin/logout", headers=HOST)  # no CSRF
        self.assertEqual(r.status_code, 400)
        r.close()
        self.assertEqual(self.status_of("/admin/api/requests"), 200)  # still logged in

        r = self.client.post("/admin/logout", headers=HOST, data={"csrf_token": token})
        self.assertEqual(r.status_code, 303)
        r.close()
        self.assertEqual(self.status_of("/admin/api/requests"), 401)
        self.assertEqual(self.status_of("/admin"), 302)

    def test_throttling_and_expiry(self):
        for _ in range(config.LOGIN_MAX_FAILURES):
            r = self.login("wrong password 123")
            r.close()
        # Even the correct password is refused while throttled.
        r = self.login()
        self.assertEqual(r.status_code, 429)
        self.assertIn(b"Too many failed attempts", r.data)
        r.close()
        self.assertEqual(self.status_of("/admin/api/requests"), 401)

        # Simulate the window passing: failures expire, no permanent lockout.
        query("UPDATE login_failures SET failed_at = failed_at - ?", (config.LOGIN_WINDOW_SECONDS + 1,))
        r = self.login()
        self.assertEqual(r.status_code, 303)
        r.close()
        self.assertEqual(query("SELECT COUNT(*) FROM login_failures")[0][0], 0)

    def test_password_not_configured(self):
        config.ADMIN_CREDENTIALS_FILE.unlink()
        with self.client.get("/admin/login", headers=HOST) as r:
            self.assertIn(b"set-admin-password", r.data)
        token = self.csrf()
        r = self.client.post("/admin/login", headers=HOST, data={"password": "anything", "csrf_token": token})
        self.assertEqual(r.status_code, 503)
        r.close()

    def test_password_change_logs_out_existing_sessions(self):
        self.logged_in_csrf()
        self.assertEqual(self.status_of("/admin/api/requests"), 200)
        auth.save_admin_password("another long password 456")
        self.assertEqual(self.status_of("/admin/api/requests"), 401)

    def test_credentials_file_has_hash_only(self):
        text = config.ADMIN_CREDENTIALS_FILE.read_text(encoding="utf-8")
        self.assertNotIn(PASSWORD, text)
        self.assertTrue(json.loads(text)["password_hash"].startswith("scrypt:"))
        self.assertFalse(str(config.ADMIN_CREDENTIALS_FILE).startswith(str(config.STATIC_DIR)))
        self.assertEqual(self.status_of("/admin_credentials.json"), 404)
        self.assertEqual(self.status_of("/secret_key"), 404)

    def test_no_secrets_or_personal_data_in_logs(self):
        rid = insert_request(name="Logname Unique", email="log.unique@example.com")
        with self.assertLogs("cleanspace", level="INFO") as logs:
            r = self.login("Wrong-Password-XYZ")
            r.close()
            token = self.logged_in_csrf()
            r = self.patch(rid, {"status": "in_progress", "adminNote": "note-secret-777", "version": 1}, token)
            r.close()
            with self.client.get("/admin/api/requests?q=log.unique@example.com", headers=HOST):
                pass
        text = "\n".join(logs.output)
        for secret in ("Wrong-Password-XYZ", PASSWORD, "note-secret-777", "Logname", "log.unique"):
            self.assertNotIn(secret, text)
        self.assertIn("Admin login failed", text)
        self.assertIn("updated by admin", text)


class ListTests(AdminTestCase):
    def setUp(self):
        super().setUp()
        self.token = self.logged_in_csrf()

    def get(self, qs=""):
        with self.client.get(f"/admin/api/requests{qs}", headers=HOST) as r:
            return r.status_code, r.get_json()

    def test_empty_list(self):
        code, data = self.get()
        self.assertEqual(code, 200)
        self.assertEqual((data["items"], data["total"], data["totalPages"]), ([], 0, 1))
        self.assertEqual(data["counts"]["all"], 0)

    def test_newest_first_pagination_and_counts(self):
        ids = []
        for i in range(45):
            status = ["new", "in_progress", "completed", "cancelled"][i % 4]
            ids.append(insert_request(name=f"Person {i:02d}", email=f"p{i}@example.com",
                                      created_at=f"2026-11-01T10:{i:02d}:00+00:00", status=status))
        code, p1 = self.get()
        self.assertEqual(code, 200)
        self.assertEqual(len(p1["items"]), 20)
        self.assertEqual(p1["items"][0]["id"], ids[-1])  # newest first
        self.assertEqual([x["id"] for x in p1["items"]], sorted([x["id"] for x in p1["items"]], reverse=True))
        self.assertEqual((p1["total"], p1["totalPages"]), (45, 3))
        _, p3 = self.get("?page=3")
        self.assertEqual(len(p3["items"]), 5)
        _, p99 = self.get("?page=99")  # out of range -> clamps to the last page
        self.assertEqual(p99["page"], 3)

        # Counts are global: identical with and without filters.
        expected = {"new": 12, "in_progress": 11, "completed": 11, "cancelled": 11, "all": 45}
        _, filtered = self.get("?status=completed&q=Person%201")
        self.assertEqual(p1["counts"], expected)
        self.assertEqual(filtered["counts"], expected)
        self.assertTrue(all(x["status"] == "completed" for x in filtered["items"]))

    def test_search_by_number_name_email(self):
        a = insert_request(name="Łukasz Nowak", email="lukasz@example.com")
        b = insert_request(name="Anna Kowalska", email="anna.k@example.org")
        insert_request(name="Zoe", email="zoe@example.net")
        self.assertEqual([x["id"] for x in self.get(f"?q={b}")[1]["items"]], [b])
        self.assertEqual([x["id"] for x in self.get(f"?q=%23{a}")[1]["items"]], [a])
        self.assertEqual([x["id"] for x in self.get("?q=%C5%81UK")[1]["items"]], [a])  # "ŁUK", Unicode case
        self.assertEqual([x["id"] for x in self.get("?q=EXAMPLE.ORG")[1]["items"]], [b])
        code, none = self.get("?q=nobody-here")
        self.assertEqual((code, none["total"], none["items"]), (200, 0, []))
        # SQL wildcard characters are plain text, not patterns.
        self.assertEqual(self.get("?q=%25")[1]["total"], 0)
        self.assertEqual(self.get("?q=_")[1]["total"], 0)

    def test_invalid_filters(self):
        self.assertEqual(self.get("?status=deleted")[0], 400)
        self.assertEqual(self.get("?page=abc")[0], 400)
        self.assertEqual(self.get("?page=0")[0], 400)
        self.assertEqual(self.get("?q=" + "x" * 121)[0], 400)

    def test_list_item_fields(self):
        insert_request(name="<b>Bold</b>", email="x@example.com", comment="<script>alert(1)</script>")
        item = self.get()[1]["items"][0]
        for key in ("id", "createdAt", "name", "items", "totalGrosze", "agreedTotalGrosze", "preferredDate",
                    "preferredTime", "status", "statusLabel"):
            self.assertIn(key, item)
        self.assertEqual(item["items"][0]["service"], "deep")   # stable code; the page translates it
        self.assertEqual(item["status"], "new")
        self.assertEqual(item["name"], "<b>Bold</b>")  # raw text; the page renders it with textContent
        self.assertNotIn("email", item)  # email only in details
        self.assertNotIn("comment", item)


class UpdateTests(AdminTestCase):
    def setUp(self):
        super().setUp()
        self.token = self.logged_in_csrf()
        self.rid = insert_request(name="Original Name", email="orig@example.com", comment="<i>hi</i>",
                                  cleaning_type="deep", area=50)

    def detail(self):
        with self.client.get(f"/admin/api/requests/{self.rid}", headers=HOST) as r:
            self.assertEqual(r.status_code, 200)
            return r.get_json()["request"]

    def test_detail_contains_private_fields(self):
        d = self.detail()
        self.assertEqual((d["email"], d["comment"], d["adminNote"]), ("orig@example.com", "<i>hi</i>", ""))
        self.assertEqual(self.status_of("/admin/api/requests/999999"), 404)

    def test_update_status_and_note_only(self):
        r = self.patch(self.rid, {
            "status": "in_progress", "adminNote": "  Call on Monday  ", "version": 1,
            # Attempts to change customer data or price must be ignored:
            "name": "Hacked", "email": "hacked@example.com", "totalGrosze": 1, "area": 999,
        }, self.token)
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()["request"]
        r.close()
        self.assertEqual((body["status"], body["adminNote"], body["version"]), ("in_progress", "Call on Monday", 2))
        row = query("SELECT name, email, phone, street, total_grosze, subtotal_grosze, comment, status, admin_note, "
                    "version, updated_at IS NOT NULL FROM requests WHERE id = ?", (self.rid,))[0]
        self.assertEqual(row, ("Original Name", "orig@example.com", "+48 600 000 000", "Testowa 1", 40000, 40000,
                               "<i>hi</i>", "in_progress", "Call on Monday", 2, 1))
        self.assertEqual(query("SELECT quantity_scaled, line_total_grosze FROM request_items WHERE request_id = ?",
                               (self.rid,)), [(50, 40000)])

    def test_invalid_status_and_long_note_not_written(self):
        cases = [
            ({"status": "deleted", "adminNote": "x", "version": 1}, "status"),
            ({"status": "", "adminNote": "x", "version": 1}, "status"),
            ({"status": "completed", "adminNote": "x" * 2001, "version": 1}, "adminNote"),
            ({"status": "completed", "adminNote": 5, "version": 1}, "adminNote"),
            ({"status": "completed", "adminNote": "x", "version": True}, "version"),
            ({"status": "completed", "adminNote": "x", "version": "1"}, "version"),
            ({"status": "completed", "adminNote": "x"}, "version"),
        ]
        for body, field in cases:
            with self.subTest(field=field, body=str(body)[:60]):
                r = self.patch(self.rid, body, self.token)
                self.assertEqual(r.status_code, 422)
                self.assertIn(field, r.get_json()["fields"])
                r.close()
        self.assertEqual(query("SELECT status, admin_note, version FROM requests")[0], ("new", "", 1))

    def test_note_limit_edge(self):
        r = self.patch(self.rid, {"status": "new", "adminNote": "n" * 2000, "version": 1}, self.token)
        self.assertEqual(r.status_code, 200)
        r.close()
        self.assertEqual(len(query("SELECT admin_note FROM requests")[0][0]), 2000)

    def test_csrf_required_for_changes(self):
        body = {"status": "completed", "adminNote": "x", "version": 1}
        for token in (None, "", "forged-token"):
            with self.subTest(token=token):
                r = self.patch(self.rid, body, token)
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.get_json()["error"], "csrf_failed")
                r.close()
        self.assertEqual(query("SELECT status, version FROM requests")[0], ("new", 1))

    def test_two_tabs_conflict(self):
        # Both "tabs" loaded version 1.
        first = self.patch(self.rid, {"status": "in_progress", "adminNote": "from tab A", "version": 1}, self.token)
        self.assertEqual(first.status_code, 200)
        first.close()
        second = self.patch(self.rid, {"status": "cancelled", "adminNote": "from tab B", "version": 1}, self.token)
        self.assertEqual(second.status_code, 409)
        current = second.get_json()["current"]
        second.close()
        self.assertEqual((current["status"], current["adminNote"], current["version"]),
                         ("in_progress", "from tab A", 2))
        # Tab A's change is intact.
        self.assertEqual(query("SELECT status, admin_note, version FROM requests")[0],
                         ("in_progress", "from tab A", 2))
        # After loading the latest version, tab B can save.
        third = self.patch(self.rid, {"status": "cancelled", "adminNote": "from tab B", "version": 2}, self.token)
        self.assertEqual(third.status_code, 200)
        third.close()

    def test_update_missing_request(self):
        r = self.patch(999999, {"status": "new", "adminNote": "", "version": 1}, self.token)
        self.assertEqual(r.status_code, 404)
        r.close()

    # ---------------------------------------------------------------- stage 6
    def history(self):
        with self.client.get(f"/admin/api/requests/{self.rid}", headers=HOST) as r:
            return r.get_json()["history"]

    def send(self, body):
        r = self.patch(self.rid, body, self.token)
        data = r.get_json()
        r.close()
        return r.status_code, data

    def test_detail_shows_items_breakdown_contact_and_time(self):
        d = self.detail()
        self.assertEqual(d["items"][0]["service"], "deep")
        self.assertEqual((d["items"][0]["quantity"], d["items"][0]["unitRateGrosze"], d["items"][0]["lineTotalGrosze"]),
                         ("50", 800, 40000))
        self.assertEqual((d["subtotalGrosze"], d["minimumTopupGrosze"], d["totalGrosze"]), (40000, 0, 40000))
        self.assertEqual((d["phone"], d["city"], d["street"], d["preferredTime"]),
                         ("+48 600 000 000", "Kraków", "Testowa 1", "morning"))
        self.assertEqual((d["agreedDate"], d["agreedTime"], d["agreedTotalGrosze"]), (None, None, None))
        self.assertEqual(self.history(), [])

    def test_history_records_each_change_with_old_and_new_values(self):
        code, body = self.send({"version": 1, "status": "in_progress", "adminNote": "First note"})
        self.assertEqual(code, 200, body)
        code, body = self.send({"version": 2, "agreedDate": "2026-12-05", "agreedTime": "09:30"})
        self.assertEqual(code, 200, body)
        code, body = self.send({"version": 3, "agreedTotal": "420,50", "priceReason": "Extra balcony"})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["request"]["agreedTotalGrosze"], 42050)
        self.assertEqual(body["request"]["totalGrosze"], 40000)      # original estimate stays visible
        h = {e["action"]: e for e in self.history()}
        self.assertEqual(set(h), {"status_changed", "note_changed", "schedule_changed", "price_adjusted"})
        self.assertEqual(h["status_changed"]["changes"], [{"field": "status", "from": "new", "to": "in_progress"}])
        self.assertEqual(h["note_changed"]["changes"][0]["to"], "First note")
        self.assertEqual({c["field"] for c in h["schedule_changed"]["changes"]}, {"agreed_date", "agreed_time"})
        self.assertEqual(h["price_adjusted"]["changes"], [{"field": "agreed_total_grosze", "from": None, "to": 42050}])
        self.assertEqual(h["price_adjusted"]["reason"], "Extra balcony")
        self.assertTrue(all(e["actor"] == "admin" and e["createdAt"] for e in h.values()))
        # Preferred (customer) date/time stay unchanged.
        self.assertEqual(query("SELECT preferred_time FROM requests")[0][0], "morning")

    def test_extreme_agreed_totals_are_422_not_500(self):
        """Regression: "1e999999" raised decimal.Overflow in _parse_money (HTTP 500)."""
        snapshot = lambda: (query("SELECT * FROM requests"), query("SELECT * FROM request_history"),  # noqa: E731
                            query("SELECT * FROM notifications"))
        before = snapshot()
        reason = "valid reason"
        invalid = ["1e999999", "-1e999999", "1e-999999", "9E+999999", "1e2", "NaN", "nan", "Infinity",
                   "-Infinity", "inf", float("inf"), float("-inf"), float("nan"), 1.5, False, True,
                   "0x10", "١٢", "300.555", "1" * 13]
        for value in invalid:
            with self.subTest(value=repr(value)):
                status, data = self.send({"version": 1, "agreedTotal": value, "priceReason": reason})
                self.assertEqual(status, 422, data)
                self.assertEqual(data["fieldCodes"]["agreedTotal"]["code"], "agreed_total_invalid")
        out_of_range = ["0", "-5", "-0.01", "100000.01", "999999999999"]
        for value in out_of_range:
            with self.subTest(value=value):
                status, data = self.send({"version": 1, "agreedTotal": value, "priceReason": reason})
                self.assertEqual(status, 422, data)
                self.assertEqual(data["fieldCodes"]["agreedTotal"]["code"], "agreed_total_range")
        self.assertEqual(snapshot(), before)   # nothing changed: request, history, queue
        # Boundary and ordinary valid values still work (comma, spaces, whole numbers).
        version = 1
        for value, grosze in [("0.01", 1), ("100000", 100_000_00), ("100 000,00", 100_000_00),
                              ("420,50", 42050), (350, 35000), ("  99.9 ", 9990)]:
            with self.subTest(value=value):
                status, data = self.send({"version": version, "agreedTotal": value, "priceReason": reason})
                self.assertEqual(status, 200, data)
                self.assertEqual(data["request"]["agreedTotalGrosze"], grosze)
                version = data["request"]["version"] if not data.get("unchanged") else version

    def test_price_change_needs_reason_and_valid_amount(self):
        for body, field, code in [
            ({"agreedTotal": "300"}, "priceReason", "price_reason_required"),
            ({"agreedTotal": "300", "priceReason": "ok"}, "priceReason", "price_reason_length"),
            ({"agreedTotal": "300.555", "priceReason": "valid reason"}, "agreedTotal", "agreed_total_invalid"),
            ({"agreedTotal": "0", "priceReason": "valid reason"}, "agreedTotal", "agreed_total_range"),
            ({"agreedTotal": "abc", "priceReason": "valid reason"}, "agreedTotal", "agreed_total_invalid"),
            ({"agreedTotal": True, "priceReason": "valid reason"}, "agreedTotal", "agreed_total_invalid"),
            ({"agreedDate": "2026-02-30"}, "agreedDate", "agreed_date_invalid"),
            ({"agreedTime": "25:00"}, "agreedTime", "agreed_time_invalid"),
            ({"agreedTime": "9:30"}, "agreedTime", "agreed_time_invalid"),
        ]:
            with self.subTest(body=body):
                status, data = self.send({"version": 1, **body})
                self.assertEqual(status, 422)
                self.assertEqual(data["fieldCodes"][field]["code"], code)
        self.assertEqual(query("SELECT agreed_total_grosze, agreed_date, version FROM requests")[0], (None, None, 1))
        self.assertEqual(self.history(), [])
        # Clearing an agreed price also needs a reason and is recorded.
        self.send({"version": 1, "agreedTotal": "350", "priceReason": "Discount agreed"})
        status, data = self.send({"version": 2, "agreedTotal": None})
        self.assertEqual(data["fieldCodes"]["priceReason"]["code"], "price_reason_required")
        status, data = self.send({"version": 2, "agreedTotal": "", "priceReason": "Back to estimate"})
        self.assertEqual((status, data["request"]["agreedTotalGrosze"]), (200, None))
        self.assertEqual(len([e for e in self.history() if e["action"] == "price_adjusted"]), 2)

    def test_unchanged_save_writes_no_history(self):
        status, data = self.send({"version": 1, "status": "new", "adminNote": ""})
        self.assertEqual((status, data["unchanged"], data["request"]["version"]), (200, True, 1))
        self.assertEqual(self.history(), [])

    def test_conflict_writes_no_history(self):
        self.send({"version": 1, "status": "in_progress"})
        status, _ = self.send({"version": 1, "status": "cancelled", "agreedTime": "10:00"})
        self.assertEqual(status, 409)
        self.assertEqual([e["action"] for e in self.history()], ["status_changed"])

    def test_history_is_append_only_in_the_database(self):
        self.send({"version": 1, "status": "completed"})
        with self.assertRaises(sqlite3.DatabaseError):
            query("UPDATE request_history SET action = 'x'")
        with self.assertRaises(sqlite3.DatabaseError):
            query("DELETE FROM request_history")
        self.assertEqual(len(self.history()), 1)

    def test_history_requires_login(self):
        anon = self.app.test_client()
        with anon.get(f"/admin/api/requests/{self.rid}", headers=HOST) as r:
            self.assertEqual(r.status_code, 401)
            self.assertNotIn(b"history", r.data)

    def test_migrated_request_shows_missing_data_as_not_provided(self):
        query("UPDATE requests SET phone = '', city = '', street = '', preferred_time = '' WHERE id = ?", (self.rid,))
        d = self.detail()
        self.assertEqual((d["phone"], d["city"], d["street"], d["preferredTime"]), (None, None, None, None))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.runner = app_module.app.test_cli_runner()

    def test_sets_hash_with_hidden_confirmed_input(self):
        result = self.runner.invoke(args=["set-admin-password"], input="a-new-long-password\na-new-long-password\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("a-new-long-password", result.output)  # input is not echoed
        self.assertTrue(auth.verify_password("a-new-long-password"))
        self.assertNotIn("a-new-long-password", config.ADMIN_CREDENTIALS_FILE.read_text(encoding="utf-8"))

    def test_rejects_short_and_mismatched(self):
        auth.save_admin_password(PASSWORD)
        result = self.runner.invoke(args=["set-admin-password"], input="short\nshort\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("too short", result.output)
        result = self.runner.invoke(args=["set-admin-password"], input="long-password-one\nlong-password-two\n")
        self.assertNotEqual(result.exit_code, 0)  # click asks again, input runs out -> abort
        self.assertTrue(auth.verify_password(PASSWORD))  # unchanged


V0_SCHEMA = """
CREATE TABLE requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, request_token TEXT NOT NULL UNIQUE, payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL, cleaning_type TEXT NOT NULL,
    area_sqm INTEGER NOT NULL CHECK (typeof(area_sqm) = 'integer'),
    rate_per_sqm_grosze INTEGER NOT NULL CHECK (typeof(rate_per_sqm_grosze) = 'integer'),
    total_grosze INTEGER NOT NULL CHECK (typeof(total_grosze) = 'integer'),
    currency TEXT NOT NULL, name TEXT NOT NULL, email TEXT NOT NULL, preferred_date TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new'))
);
"""


class MigrationTests(unittest.TestCase):
    def make_v0(self, path: Path, rows: int):
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.executescript(V0_SCHEMA)
            for i in range(rows):
                conn.execute(
                    "INSERT INTO requests (request_token, payload_hash, created_at, cleaning_type, area_sqm, "
                    "rate_per_sqm_grosze, total_grosze, currency, name, email, preferred_date, comment) "
                    "VALUES (?, 'h', '2026-11-12T10:00:00+00:00', 'deep', 50, 800, 40000, 'PLN', ?, ?, "
                    "'2026-12-01', '')",
                    (str(uuid.uuid4()), f"N{i}", f"e{i}@example.com"),
                )
            conn.execute("DELETE FROM requests WHERE id = ?", (rows,))  # a deleted id must never be reused

    def test_v0_to_v2_keeps_rows_and_is_repeatable(self):
        path = INSTANCE / "migrate-me.db"
        backups = INSTANCE / "mig-backups"
        self.make_v0(path, 5)

        backup = db.init_db(path, backups)
        self.assertIsNotNone(backup)
        self.assertTrue(backup.exists())
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 4)
            self.assertEqual(conn.execute("SELECT DISTINCT status, admin_note, version FROM requests").fetchall(),
                             [("new", "", 1)])
        with closing(sqlite3.connect(backup)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 4)

        # Running again does nothing and makes no new backup.
        self.assertIsNone(db.init_db(path, backups))
        self.assertEqual(len(list(backups.iterdir())), 1)

        # Every old request became an order with exactly one line, totals unchanged.
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*), SUM(line_total_grosze), MIN(service), MAX(quantity_scaled) FROM request_items"
            ).fetchone(), (4, 160000, "deep", 50))
            self.assertEqual(conn.execute(
                "SELECT DISTINCT hash_version, phone, preferred_time, subtotal_grosze, minimum_topup_grosze "
                "FROM requests").fetchall(), [(1, "", "", 40000, 0)])

        # New rows continue after the highest id ever used (5 was deleted).
        clean, errors = validate_order(order_payload(items=[{"service": "standard", "quantity": 30}]))
        self.assertFalse(errors)
        row, _ = db.save_order(path, clean, price_order(clean["items"]))
        self.assertEqual(row["id"], 6)
        # New statuses are accepted after migration, with history.
        result, _ = db.update_request(path, 1, {"status": "completed", "admin_note": "done"}, 1)
        self.assertEqual(result, "updated")

    def test_v3_to_v4_keeps_queue_minimum_and_tokens(self):
        """A stage-3 database: below-minimum request (top-up), queue job, CRM edits."""
        path = INSTANCE / "v3.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(db.REQUESTS_V2_SQL.format(name="requests"))
            for sql in db.NOTIFICATIONS_SQL:
                conn.execute(sql)
            conn.execute("INSERT INTO requests (request_token, payload_hash, created_at, cleaning_type, area_sqm, "
                         "rate_per_sqm_grosze, total_grosze, currency, name, email, preferred_date, comment, status, "
                         "admin_note, version) VALUES ('tok-1', 'h1', '2026-11-12T10:00:00+00:00', 'standard', 20, 500, "
                         "15000, 'PLN', 'A', 'a@example.com', '2026-12-01', 'c', 'in_progress', 'note', 3)")
            conn.execute("INSERT INTO notifications (request_id, status, next_attempt_at, created_at, updated_at) "
                         "VALUES (1, 'sent', 0, 0, 0)")
            conn.execute("PRAGMA user_version = 3")
        self.assertIsNotNone(db.init_db(path, INSTANCE / "v3-backups"))
        with closing(sqlite3.connect(path)) as conn:
            row = conn.execute("SELECT id, request_token, payload_hash, hash_version, total_grosze, subtotal_grosze, "
                               "minimum_topup_grosze, status, admin_note, version FROM requests").fetchone()
            self.assertEqual(row, (1, "tok-1", "h1", 1, 15000, 10000, 5000, "in_progress", "note", 3))
            self.assertEqual(conn.execute("SELECT request_id, status FROM notifications").fetchall(), [(1, "sent")])
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("SELECT unit_rate_grosze, line_total_grosze FROM request_items").fetchall(),
                             [(500, 10000)])

    def test_v4_to_v5_adds_pause_and_restores_it_from_old_429_errors(self):
        """A stage-4 database (no blocked_until): data kept, 429 pauses restored, backup made."""
        path = INSTANCE / "v4.db"
        db.init_db(path)
        for i in range(3):
            clean, errors = validate_order(order_payload(items=[{"service": "standard", "quantity": 30 + i}]))
            self.assertFalse(errors)
            db.save_order(path, clean, price_order(clean["items"]), notify=True)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("UPDATE notifications SET status = 'failed', updated_at = 1000, last_error = "
                         "'Rate limited by Telegram (429): asked to wait 90000 s, which is past the 24-hour limit' "
                         "WHERE request_id = 1")
            conn.execute("UPDATE notifications SET status = 'pending', updated_at = 2000, next_attempt_at = 2030, "
                         "last_error = 'Rate limited by Telegram (429): next attempt after 30 s.' WHERE request_id = 2")
            conn.execute("UPDATE notifications SET status = 'failed', updated_at = 3000, "
                         "last_error = 'Telegram refused the message (HTTP 400): chat not found' WHERE request_id = 3")
            conn.execute("ALTER TABLE notifications DROP COLUMN blocked_until")  # the v4 table had no such column
            conn.execute("PRAGMA user_version = 4")
            before = conn.execute("SELECT id, request_token, total_grosze, status FROM requests ORDER BY id").fetchall()
            jobs_before = conn.execute("SELECT id, request_id, status, attempts, next_attempt_at, last_error "
                                       "FROM notifications ORDER BY id").fetchall()

        backup = db.init_db(path, INSTANCE / "v4-backups")
        self.assertIsNotNone(backup)
        with closing(sqlite3.connect(backup)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(conn.execute("SELECT id, request_token, total_grosze, status FROM requests "
                                          "ORDER BY id").fetchall(), before)
            self.assertEqual(conn.execute("SELECT id, request_id, status, attempts, next_attempt_at, last_error "
                                          "FROM notifications ORDER BY id").fetchall(), jobs_before)
            self.assertEqual(conn.execute("SELECT request_id, blocked_until FROM notifications ORDER BY request_id")
                             .fetchall(), [(1, 91000.0), (2, 2030.0), (3, 0.0)])
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertIsNone(db.init_db(path, INSTANCE / "v4-backups"))  # second run: no-op

    def test_real_backup_copy_migrates_without_loss(self):
        """Migrates a COPY of the pre-CRM backup of the real database, if present."""
        source = ROOT / "_backup" / "2026-11-13-before-crm" / "cleanspace.db"
        if not source.exists():
            self.skipTest("pre-CRM backup not present")
        copy = INSTANCE / "real-copy.db"
        shutil.copyfile(source, copy)
        with closing(sqlite3.connect(copy)) as conn:
            before = conn.execute(
                "SELECT id, request_token, created_at, cleaning_type, area_sqm, total_grosze, name, email, "
                "preferred_date, comment, status FROM requests ORDER BY id").fetchall()
        db.init_db(copy, INSTANCE / "real-copy-backups")
        with closing(sqlite3.connect(copy)) as conn:
            after = conn.execute(
                "SELECT id, request_token, created_at, cleaning_type, area_sqm, total_grosze, name, email, "
                "preferred_date, comment, status FROM requests ORDER BY id").fetchall()
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertGreater(len(before), 0)
        self.assertEqual(before, after)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
