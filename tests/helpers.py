"""Shared test data for the multi-service order format (made-up data only)."""

import uuid
from datetime import timedelta

import db
import testenv
from pricing import price_order
from validation import business_today, validate_order


def order_payload(**overrides) -> dict:
    """A valid POST /api/requests body. Override any key; items default to one deep cleaning."""
    data = {
        "requestToken": str(uuid.uuid4()),
        "items": [{"service": "deep", "quantity": 50}],
        "name": "Test Person",
        "email": "test@example.com",
        "phone": "+48 600 000 000",
        "city": "Kraków",
        "street": "Testowa 1",
        "apartment": "",
        "date": (business_today() + timedelta(days=3)).isoformat(),
        "time": "morning",
        "comment": "Made-up comment",
    }
    data.update(overrides)
    return data


def insert_order(notify: bool = False, **overrides) -> int:
    """Insert an order directly through the same validation + pricing code; returns its id."""
    clean, errors = validate_order(order_payload(**overrides))
    assert not errors, errors
    row, _ = db.save_order(testenv.TEST_DB, clean, price_order(clean["items"]), notify=notify)
    return row["id"]
