"""Public API tests: catalog, multi-service orders, pricing, validation, idempotency,
legacy replay after the v4 migration. Uses a temporary database (tests/testenv.py).

Run from the project folder:
    .venv\\Scripts\\python -m unittest discover -s tests -v
"""

import json
import logging
import sqlite3
import unittest
import uuid
from contextlib import closing
from datetime import date, timedelta
from unittest import mock

import testenv  # noqa: F401  (must be first: points config at a temporary folder)
from testenv import TEST_DB

import app as app_module  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import pricing  # noqa: E402
from helpers import order_payload  # noqa: E402
from validation import business_today  # noqa: E402

HOST = {"Host": "127.0.0.1:5000"}


def query(sql: str, params=()):
    with closing(sqlite3.connect(TEST_DB)) as conn, conn:
        return conn.execute(sql, params).fetchall()


def count_rows() -> int:
    return query("SELECT COUNT(*) FROM requests")[0][0]


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()

    def setUp(self):
        for table in ("notifications", "request_items", "requests"):
            query(f"DELETE FROM {table}")

    def post(self, payload):
        r = self.client.post("/api/requests", json=payload, headers=HOST)
        body = r.get_json()
        r.close()
        return r.status_code, body

    def status_of(self, path, headers=HOST):
        with self.client.get(path, headers=headers) as r:
            return r.status_code


class CatalogTests(ApiTestCase):
    def test_catalog_comes_from_config(self):
        with self.client.get("/api/pricing", headers=HOST) as r:
            self.assertEqual(r.status_code, 200)
            body = r.get_json()
        self.assertEqual(body["minimumOrderGrosze"], config.MINIMUM_ORDER_GROSZE)
        self.assertEqual(body["maxItems"], config.MAX_ORDER_ITEMS)
        services = {s["id"]: s for s in body["services"]}
        self.assertEqual(set(services), set(config.SERVICES))
        # Existing room rates and range are kept.
        for sid, rate in (("standard", 500), ("deep", 800), ("moveout", 1000)):
            self.assertEqual((services[sid]["rateGrosze"], services[sid]["min"], services[sid]["max"],
                              services[sid]["unit"], services[sid]["decimals"]), (rate, "20", "300", "m2", 0))
        self.assertEqual((services["carpet"]["decimals"], services["carpet"]["step"]), (1, "0.1"))
        self.assertEqual(services["sofa"]["unit"], "seat")
        self.assertEqual({c["id"] for c in services["mattress_single"]["option"]["choices"]}, {"one", "both"})
        self.assertEqual(body["preferredTimes"], ["morning", "afternoon", "evening", "flexible"])
        self.assertEqual(body["timezone"], "Europe/Warsaw")

    def test_static_site_served(self):
        with self.client.get("/", headers=HOST) as r:
            self.assertEqual(r.status_code, 200)
            self.assertIn(b'<h1 id="hero-title"><span class="hero-title-line" data-i18n="hero.title1">'
                          b'A clean home.</span> <span', r.data)
            self.assertIn(b">More time for you.</span>", r.data)
            self.assertIn(b'<html lang="en">', r.data)  # English by default, no auto-detection
        for path in ("/script.js", "/i18n/i18n.js", "/i18n/en.js", "/i18n/uk.js", "/img/hero-interior.webp"):
            self.assertEqual(self.status_of(path), 200, path)
        with self.client.get("/img/hero-interior.webp", headers=HOST) as r:
            self.assertEqual(r.mimetype, "image/webp")


class PricingTests(ApiTestCase):
    def test_rounding_rule_half_up_integers_only(self):
        self.assertEqual(pricing.line_total(1205, 63, 1), 7592)   # 7591.5 -> 7592
        self.assertEqual(pricing.line_total(1205, 61, 1), 7351)   # 7350.5 -> 7351
        self.assertEqual(pricing.line_total(1204, 63, 1), 7585)   # 7585.2 -> 7585
        self.assertEqual(pricing.line_total(1500, 65, 1), 9750)   # 6.5 m² × 15 PLN
        self.assertEqual(pricing.line_total(800, 50, 0), 40000)

    def test_single_service_order(self):
        code, body = self.post(order_payload(items=[{"service": "deep", "quantity": 50}]))
        self.assertEqual(code, 201, body)
        self.assertEqual((body["subtotalGrosze"], body["minimumTopupGrosze"], body["totalGrosze"]), (40000, 0, 40000))
        self.assertEqual(body["items"][0]["unitRateGrosze"], 800)

    def test_multi_service_order_and_snapshot(self):
        items = [{"service": "deep", "quantity": 50}, {"service": "sofa", "quantity": 3},
                 {"service": "carpet", "quantity": "6.5"},
                 {"service": "mattress_double", "quantity": 2, "options": {"sides": "both"}}]
        code, body = self.post(order_payload(items=items))
        self.assertEqual(code, 201, body)
        expected = [40000, 16500, 9750, 44000]
        self.assertEqual([i["lineTotalGrosze"] for i in body["items"]], expected)
        self.assertEqual(body["subtotalGrosze"], sum(expected))
        self.assertEqual(body["totalGrosze"], sum(expected))
        rows = query("SELECT service, unit, quantity_scaled, quantity_decimals, options_json, unit_rate_grosze, "
                     "line_total_grosze, typeof(line_total_grosze) FROM request_items ORDER BY position")
        self.assertEqual(rows[2], ("carpet", "m2", 65, 1, "{}", 1500, 9750, "integer"))
        self.assertEqual(rows[3][4], '{"sides": "both"}')

    def test_minimum_applied_once_per_order(self):
        items = [{"service": "carpet", "quantity": 1}, {"service": "upholstered_chair", "quantity": 1}]
        code, body = self.post(order_payload(items=items))
        self.assertEqual(code, 201)
        self.assertEqual((body["subtotalGrosze"], body["minimumTopupGrosze"], body["totalGrosze"]),
                         (4000, 11000, 15000))
        code, body = self.post(order_payload(items=[{"service": "standard", "quantity": 20},
                                                    {"service": "armchair", "quantity": 1}]))
        self.assertEqual((body["subtotalGrosze"], body["minimumTopupGrosze"]), (17000, 0))  # 100 + 70 > 150

    def test_client_prices_are_ignored(self):
        payload = order_payload(items=[{"service": "deep", "quantity": 50, "unitRateGrosze": 1,
                                        "lineTotalGrosze": 1, "price": 1}],
                                totalGrosze=1, subtotalGrosze=1, minimumTopupGrosze=0, price="1 PLN")
        code, body = self.post(payload)
        self.assertEqual(code, 201)
        self.assertEqual(body["totalGrosze"], 40000)
        self.assertEqual(query("SELECT total_grosze FROM requests")[0][0], 40000)

    def test_old_estimates_do_not_change_when_rates_change(self):
        code, body = self.post(order_payload(items=[{"service": "carpet", "quantity": "10"}]))
        self.assertEqual(body["totalGrosze"], 15000)
        with mock.patch.dict(config.SERVICES["carpet"], {"rate_grosze": 99_00}):
            self.client.application  # noqa: B018 (keep the patch active for the next calls)
            code2, body2 = self.post(order_payload(items=[{"service": "carpet", "quantity": "10"}]))
            self.assertEqual(body2["totalGrosze"], 99000)
        rows = query("SELECT r.total_grosze, i.unit_rate_grosze FROM requests r JOIN request_items i "
                     "ON i.request_id = r.id ORDER BY r.id")
        self.assertEqual(rows, [(15000, 1500), (99000, 9900)])  # the first one is unchanged


class ValidationTests(ApiTestCase):
    def assert_rejected(self, payload, field, code=None):
        status, body = self.post(payload)
        self.assertEqual(status, 422, body)
        self.assertEqual(body["error"], "validation_error")
        self.assertIn(field, body["fields"], body["fields"])
        self.assertIn(field, body["fieldCodes"])
        if code:
            self.assertEqual(body["fieldCodes"][field]["code"], code)

    def test_item_errors(self):
        cases = [
            ([], "items", "items_required"),
            ("deep", "items", "items_required"),
            ([{"service": "luxury", "quantity": 50}], "items.0.service", "service_unknown"),
            ([{"service": "deep", "quantity": True}], "items.0.quantity", "quantity_number"),
            ([{"service": "deep", "quantity": "abc"}], "items.0.quantity", "quantity_number"),
            ([{"service": "deep", "quantity": "NaN"}], "items.0.quantity", "quantity_number"),
            ([{"service": "deep", "quantity": "Infinity"}], "items.0.quantity", "quantity_number"),
            ([{"service": "deep", "quantity": 50.5}], "items.0.quantity", "quantity_whole"),
            ([{"service": "deep", "quantity": 19}], "items.0.quantity", "quantity_range"),
            ([{"service": "deep", "quantity": 301}], "items.0.quantity", "quantity_range"),
            ([{"service": "carpet", "quantity": "6.25"}], "items.0.quantity", "quantity_decimals"),
            ([{"service": "carpet", "quantity": "0.5"}], "items.0.quantity", "quantity_range"),
            ([{"service": "sofa", "quantity": "2.5"}], "items.0.quantity", "quantity_whole"),
            ([{"service": "sofa", "quantity": -1}], "items.0.quantity", "quantity_range"),
            ([{"service": "mattress_single", "quantity": 1}], "items.0.options", "option_invalid"),
            ([{"service": "mattress_single", "quantity": 1, "options": {"sides": "three"}}], "items.0.options", "option_invalid"),
            ([{"service": "deep", "quantity": 50, "options": {"sides": "one"}}], "items.0.options", "option_invalid"),
            ([{"service": "deep", "quantity": 50}, {"service": "deep", "quantity": 30}], "items.1.service", "item_duplicate"),
            ([{"service": "carpet", "quantity": 1}] * 0 + [{"service": s, "quantity": 1} for s in ("sofa",) * 1]
             + [{"service": "deep", "quantity": 20 + i} for i in range(20)], "items", "items_too_many"),
            (["deep"], "items.0", "item_invalid"),
        ]
        for items, field, code in cases:
            with self.subTest(items=str(items)[:60]):
                self.assert_rejected(order_payload(items=items), field, code)
        self.assertEqual(count_rows(), 0)

    def test_extreme_numbers_are_422_not_500(self):
        """Regression: carpet quantity "1e999999" raised decimal.Overflow (HTTP 500)."""
        existing = self.post(order_payload())[1]["id"]   # must stay exactly as it is
        snapshot = lambda: (query("SELECT * FROM requests"), query("SELECT * FROM request_items"),  # noqa: E731
                            query("SELECT * FROM notifications"))
        before = snapshot()
        invalid = ["1e999999", "-1e999999", "1e-999999", "9E+999999", "1e1", "NaN", "-nan", "Infinity",
                   "-Infinity", "inf", "sNaN", True, False, None, "0x10", "١", "", " ", "6.", ".5", "1" * 13,
                   [1], {"v": 1}]
        for value in invalid:
            with self.subTest(value=repr(value)):
                self.assert_rejected(order_payload(items=[{"service": "carpet", "quantity": value}]),
                                     "items.0.quantity", "quantity_number")
        for value in ["0.9", "60.1", "-1", "0", "999999999999"]:
            with self.subTest(value=value):
                self.assert_rejected(order_payload(items=[{"service": "carpet", "quantity": value}]),
                                     "items.0.quantity", "quantity_range")
        self.assert_rejected(order_payload(items=[{"service": "carpet", "quantity": "6.25"}]),
                             "items.0.quantity", "quantity_decimals")
        self.assertEqual(snapshot(), before)
        self.assertEqual(count_rows(), 1)
        del existing

    def test_extreme_json_numbers_are_422_not_500(self):
        """Raw JSON numbers: 1e999999 -> inf, -1e999999 -> -inf, 1e-999999 -> 0.0, NaN/Infinity literals."""
        for raw, code in [("1e999999", "quantity_number"), ("-1e999999", "quantity_number"),
                          ("NaN", "quantity_number"), ("Infinity", "quantity_number"),
                          ("-Infinity", "quantity_number"), ("1e-999999", "quantity_range"),
                          # 1e400 -> inf; 1E5 is a valid JSON number (100000.0) -> outside the range.
                          ("1e400", "quantity_number"), ("1E5", "quantity_range")]:
            with self.subTest(raw=raw):
                body = json.dumps(order_payload(items=[{"service": "carpet", "quantity": 0}]))
                body = body.replace('"quantity": 0', f'"quantity": {raw}')
                r = self.client.post("/api/requests", data=body, content_type="application/json", headers=HOST)
                data = r.get_json()
                r.close()
                self.assertEqual(r.status_code, 422, data)
                self.assertEqual(data["fieldCodes"]["items.0.quantity"]["code"], code)
        self.assertEqual(count_rows(), 0)

    def test_boundary_and_ordinary_quantities_still_accepted(self):
        for value, scaled in [("1", 10), ("60", 600), ("1.0", 10), ("59.9", 599), (6.5, 65), (60, 600),
                              ("  6.5 ", 65), ("+6.5", 65), ("006.5", 65)]:
            with self.subTest(value=value):
                self.assertEqual(pricing.parse_quantity("carpet", value), (scaled, 1))
        status, body = self.post(order_payload(items=[{"service": "carpet", "quantity": "60"},
                                                      {"service": "sofa", "quantity": 10}]))
        self.assertEqual(status, 201, body)

    def test_valid_quantities(self):
        for items in ([{"service": "carpet", "quantity": "60"}], [{"service": "carpet", "quantity": 6.5}],
                      [{"service": "fitted_carpet", "quantity": "5.0"}], [{"service": "deep", "quantity": "50"}],
                      [{"service": "mattress_single", "quantity": 1, "options": {"sides": "one"}},
                       {"service": "mattress_single", "quantity": 1, "options": {"sides": "both"}}]):
            with self.subTest(items=items):
                status, body = self.post(order_payload(items=items))
                self.assertEqual(status, 201, body)

    def test_contact_and_time_errors(self):
        yesterday = (business_today() - timedelta(days=1)).isoformat()
        cases = {
            "name": ["", " a ", "x" * 81, "Line\nbreak", 5],
            "email": ["", "not-an-email", "a@b", "a" * 110 + "@example.com"],
            "phone": ["", "abc", "+48+600000000", "12345", "1" * 16, "+48 (600) 000-000-000-00", "600 000 000 ext 5"],
            "city": ["", "K", "x" * 81],
            "street": ["", "ab", "x" * 121],
            "apartment": ["x" * 21],
            "date": ["", yesterday, "2026-02-30", "12.11.2026", None],
            "time": ["", "night", None, "Morning"],
            "comment": ["x" * 501, 42],
            "requestToken": ["", "not-a-uuid", None],
        }
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=str(value)[:30]):
                    self.assert_rejected(order_payload(**{field: value}), field)
        self.assertEqual(count_rows(), 0)

    def test_valid_phone_formats_and_optional_fields(self):
        for phone in ("+48 600 000 000", "(22) 123-45-67", "+1 (555) 010-9999", "600000000", "+380 44 123 4567"):
            with self.subTest(phone=phone):
                status, body = self.post(order_payload(phone=phone))
                self.assertEqual(status, 201, body)
        status, _ = self.post(order_payload(apartment="12B", comment=""))
        self.assertEqual(status, 201)
        self.assertEqual(query("SELECT phone FROM requests ORDER BY id LIMIT 1")[0][0], "+48 600 000 000")
        # Inner whitespace (incl. line breaks pasted by mistake) is collapsed to single spaces.
        status, _ = self.post(order_payload(phone="600\n000   000"))
        self.assertEqual(status, 201)
        self.assertEqual(query("SELECT phone FROM requests ORDER BY id DESC LIMIT 1")[0][0], "600 000 000")

    def test_today_in_warsaw_is_allowed(self):
        self.assertEqual(self.post(order_payload(date=business_today().isoformat()))[0], 201)

    def test_missing_body_fields(self):
        status, body = self.post({})
        self.assertEqual(status, 422)
        for key in ("requestToken", "items", "name", "email", "phone", "city", "street", "date", "time"):
            self.assertIn(key, body["fields"])
        self.assertNotIn("apartment", body["fields"])
        self.assertNotIn("comment", body["fields"])

    def test_non_json_and_wrong_shape(self):
        with self.client.post("/api/requests", data="name=x", headers=HOST,
                              content_type="application/x-www-form-urlencoded") as r:
            self.assertEqual(r.status_code, 415)
        with self.client.post("/api/requests", json=[1, 2], headers=HOST) as r:
            self.assertEqual(r.status_code, 400)
        self.assertEqual(count_rows(), 0)


class IdempotencyTests(ApiTestCase):
    def test_same_token_same_data_returns_same_order(self):
        payload = order_payload(items=[{"service": "deep", "quantity": 50}, {"service": "sofa", "quantity": 3}])
        first = self.post(payload)
        second = self.post(payload)
        third = self.post(dict(payload, totalGrosze=1))  # ignored fields do not matter
        self.assertEqual((first[0], second[0], third[0]), (201, 200, 200))
        self.assertEqual(first[1]["id"], second[1]["id"])
        self.assertTrue(second[1]["duplicate"])
        self.assertEqual(count_rows(), 1)
        self.assertEqual(query("SELECT COUNT(*) FROM request_items")[0][0], 2)

    def test_lost_response_then_retry_returns_the_same_request(self):
        """Regression: the server saved the order but the browser never got the answer."""
        payload = order_payload()
        status, body = self.post(payload)   # saved...
        saved_id = body["id"]               # ...but imagine this answer was lost
        status, retry = self.post(payload)  # browser retries with the SAME token and data
        self.assertEqual((status, retry["id"], retry["duplicate"]), (200, saved_id, True))
        self.assertEqual(count_rows(), 1)

    def test_same_token_different_data_conflicts(self):
        payload = order_payload()
        self.post(payload)
        for change in ({"items": [{"service": "deep", "quantity": 51}]}, {"phone": "+48 700 000 000"},
                       {"time": "evening"}, {"street": "Inna 2"}):
            with self.subTest(change=change):
                status, body = self.post(dict(payload, **change))
                self.assertEqual((status, body["error"]), (409, "token_conflict"))
        self.assertEqual(count_rows(), 1)

    def test_replay_after_midnight_is_still_recognised(self):
        payload = order_payload(date=business_today().isoformat())
        status, body = self.post(payload)
        self.assertEqual(status, 201)
        tomorrow = business_today() + timedelta(days=1)
        with mock.patch("app.business_today", return_value=tomorrow), \
             mock.patch("validation.business_today", return_value=tomorrow):
            status, again = self.post(payload)                   # same token: recognised
            self.assertEqual((status, again["id"]), (200, body["id"]))
            status, fresh = self.post(dict(payload, requestToken=str(uuid.uuid4())))
            self.assertEqual((status, fresh["fieldCodes"]["date"]["code"]), (422, "date_past"))


class LegacyReplayTests(ApiTestCase):
    """A stage-2 page retrying its ORIGINAL request after the upgrade must not duplicate it."""

    def legacy_row(self, token):
        clean = {"request_token": token, "cleaning_type": "deep", "area_sqm": 50, "name": "Old Person",
                 "email": "old@example.com", "preferred_date": "2026-01-10", "comment": ""}
        query("INSERT INTO requests (request_token, payload_hash, hash_version, created_at, cleaning_type, "
              "area_sqm, rate_per_sqm_grosze, subtotal_grosze, minimum_topup_grosze, total_grosze, currency, "
              "name, email, preferred_date, comment) VALUES (?, ?, 1, '2026-01-01T10:00:00+00:00', 'deep', 50, "
              "800, 40000, 0, 40000, 'PLN', 'Old Person', 'old@example.com', '2026-01-10', '')",
              (token, db.payload_hash_v1(clean)))
        rid = query("SELECT id FROM requests WHERE request_token = ?", (token,))[0][0]
        query("INSERT INTO request_items (request_id, position, category, service, unit, quantity_scaled, "
              "quantity_decimals, unit_rate_grosze, line_total_grosze, label_snapshot) "
              "VALUES (?, 0, 'rooms', 'deep', 'm2', 50, 0, 800, 40000, 'Deep cleaning')", (rid,))
        return rid

    def legacy_payload(self, token, **overrides):
        data = {"requestToken": token, "cleaningType": "deep", "area": 50, "name": "Old Person",
                "email": "old@example.com", "date": "2026-01-10", "comment": ""}
        data.update(overrides)
        return data

    def test_old_token_old_format_returns_same_request(self):
        token = str(uuid.uuid4())
        rid = self.legacy_row(token)
        status, body = self.post(self.legacy_payload(token))   # past date is fine for a replay
        self.assertEqual((status, body["id"], body["duplicate"], body["totalGrosze"]), (200, rid, True, 40000))
        self.assertEqual((body["cleaningType"], body["area"]), ("deep", 50))
        self.assertEqual(count_rows(), 1)

    def test_old_token_with_other_data_conflicts(self):
        token = str(uuid.uuid4())
        self.legacy_row(token)
        self.assertEqual(self.post(self.legacy_payload(token, area=60))[0], 409)
        new_format = order_payload(requestToken=token)
        self.assertEqual(self.post(new_format)[0], 409)          # new format can never take over an old token
        self.assertEqual(count_rows(), 1)

    def test_new_request_in_old_format_is_refused(self):
        status, body = self.post(self.legacy_payload(str(uuid.uuid4()), date=business_today().isoformat()))
        self.assertEqual((status, body["error"]), (422, "outdated_form"))
        self.assertEqual(count_rows(), 0)


class SafetyTests(ApiTestCase):
    def test_no_listing_api(self):
        self.post(order_payload())
        with self.client.get("/api/requests", headers=HOST) as r:
            self.assertIn(r.status_code, (404, 405))
            self.assertNotIn(b"example.com", r.data)

    def test_database_not_served(self):
        self.assertFalse(str(config.DATABASE_PATH).startswith(str(config.STATIC_DIR)))
        for path in ("/instance/cleanspace.db", "/app.py", "/../config.py"):
            self.assertEqual(self.status_of(path), 404)

    def test_foreign_host_header_rejected(self):
        self.assertEqual(self.status_of("/api/pricing", {"Host": "evil.example:5000"}), 400)
        self.assertEqual(self.status_of("/api/pricing", {"Host": "localhost:5000"}), 200)

    def test_personal_data_not_logged(self):
        payload = order_payload(name="Secretname Unique", email="secret.unique@example.com", phone="+48 611 222 333",
                                city="Secretcity", street="Secretstreet 9", apartment="Apt77",
                                comment="secret-comment-xyz")
        with self.assertLogs("cleanspace", level="INFO") as logs:
            self.post(payload)
            self.post(dict(payload, requestToken=str(uuid.uuid4()), email="broken"))
        text = "\n".join(logs.output)
        for secret in ("Secretname", "secret.unique", "611 222", "Secretcity", "Secretstreet", "Apt77",
                       "secret-comment-xyz", "broken", payload["requestToken"]):
            self.assertNotIn(secret, text)
        self.assertIn("saved", text)

    def test_sql_injection_is_stored_as_text(self):
        evil = "Robert'); DROP TABLE requests;--"
        self.assertEqual(self.post(order_payload(name=evil, street=evil[:30]))[0], 201)
        self.assertEqual(query("SELECT name FROM requests")[0][0], evil)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
