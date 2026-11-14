"""Server-side validation of demo orders.

The browser validates too, but the server never trusts it: every field and every order
line is checked again here, and prices are computed on the server (pricing.py).

Errors are returned as {field: FieldError(code, params)}. `code` is a stable key the
website/CRM translates; `message()` gives an English fallback text.
Field keys: "items", "items.<i>.service", "items.<i>.quantity", "items.<i>.options",
"name", "email", "phone", "city", "street", "apartment", "date", "time", "comment",
"requestToken" (and the legacy ones "cleaningType", "area").
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

import config
from pricing import QuantityError, parse_quantity

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PHONE_CHARS_RE = re.compile(r"^\+?[0-9 ()\-]+$")
# Control characters (except tab/newline/carriage return, allowed in comments).
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
LINE_BREAKS_RE = re.compile(r"[\t\r\n]")

MESSAGES = {
    "required": "This field is required.",
    "token_invalid": "Missing or invalid request token. Please preview the request again.",
    "items_required": "Add at least one service to the order.",
    "items_too_many": "An order can contain at most {max} services.",
    "items_invalid": "The order lines are not valid.",
    "item_invalid": "This order line is not valid.",
    "service_unknown": "Unknown service.",
    "item_duplicate": "This service with the same options is already in the order. Change the quantity instead.",
    "option_invalid": "Please choose a valid option.",
    "quantity_number": "Please enter the quantity as a number.",
    "quantity_whole": "Please enter a whole number.",
    "quantity_decimals": "Use at most {decimals} decimal place(s).",
    "quantity_range": "The quantity must be from {min} to {max}.",
    "name_length": "Name should be {min}–{max} characters.",
    "text_invalid_chars": "This field contains characters that are not allowed.",
    "email_invalid": "Please enter a valid email address, e.g. name@example.com.",
    "email_too_long": "Email should be at most {max} characters.",
    "phone_invalid": "Use digits, an optional + prefix, spaces, brackets or dashes.",
    "phone_digits": "The phone number should have {min}–{max} digits.",
    "phone_too_long": "The phone number should be at most {max} characters.",
    "length_range": "This field should be {min}–{max} characters.",
    "too_long": "This field should be at most {max} characters.",
    "date_invalid": "Please enter a valid date.",
    "date_not_real": "This date does not exist in the calendar.",
    "date_past": "The date cannot be in the past (Polish time, Europe/Warsaw).",
    "time_invalid": "Please choose a preferred time.",
    # legacy
    "type_invalid": "Please choose one of the available cleaning types.",
    "area_decimal": "Please enter a whole number of square metres, without decimals.",
    "area_number": "The floor area must be a number.",
    "area_range": "The area must be from {min} to {max} m².",
}


@dataclass
class FieldError:
    code: str
    params: dict = field(default_factory=dict)

    def message(self) -> str:
        return MESSAGES.get(self.code, self.code).format(**self.params)


def errors_to_json(errors: dict) -> tuple[dict, dict]:
    """-> ({field: English message}, {field: {code, params}})"""
    return ({k: e.message() for k, e in errors.items()},
            {k: {"code": e.code, "params": e.params} for k, e in errors.items()})


def business_today() -> date:
    """Today's date in the business time zone (Europe/Warsaw)."""
    return datetime.now(ZoneInfo(config.BUSINESS_TIMEZONE)).date()


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _token(data, errors, clean):
    token = data.get("requestToken")
    try:
        if not isinstance(token, str):
            raise ValueError
        clean["request_token"] = str(uuid.UUID(token))
    except ValueError:
        errors["requestToken"] = FieldError("token_invalid")


def _text(data, key, out_key, errors, clean, *, required, min_len=0, max_len, single_line=True):
    value = data.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        errors[key] = FieldError("required" if required else "too_long", {"max": max_len})
        return
    value = value.strip()
    if not value:
        if required:
            errors[key] = FieldError("required")
        else:
            clean[out_key] = ""
        return
    if CONTROL_CHARS_RE.search(value) or (single_line and LINE_BREAKS_RE.search(value)):
        errors[key] = FieldError("text_invalid_chars")
    elif len(value) < min_len or len(value) > max_len:
        if min_len:
            errors[key] = FieldError("length_range", {"min": min_len, "max": max_len})
        else:
            errors[key] = FieldError("too_long", {"max": max_len})
    else:
        clean[out_key] = value


def _date(raw, errors, clean, key, allow_past):
    if not isinstance(raw, str) or not raw:
        errors[key] = FieldError("required")
    elif not ISO_DATE_RE.match(raw):
        errors[key] = FieldError("date_invalid")
    else:
        try:
            preferred = date.fromisoformat(raw)  # rejects e.g. 2026-02-30
        except ValueError:
            errors[key] = FieldError("date_not_real")
            return
        if preferred < business_today() and not allow_past:
            errors[key] = FieldError("date_past")
        else:
            clean["preferred_date"] = preferred.isoformat()


def _name_email(data, errors, clean):
    limits = config.FIELD_LIMITS
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        errors["name"] = FieldError("required")
    else:
        name = name.strip()
        if CONTROL_CHARS_RE.search(name) or LINE_BREAKS_RE.search(name):
            errors["name"] = FieldError("text_invalid_chars")
        elif not limits["name_min"] <= len(name) <= limits["name_max"]:
            errors["name"] = FieldError("name_length", {"min": limits["name_min"], "max": limits["name_max"]})
        else:
            clean["name"] = name

    email = data.get("email")
    if not isinstance(email, str) or not email.strip():
        errors["email"] = FieldError("required")
    else:
        email = email.strip()
        if len(email) > limits["email_max"]:
            errors["email"] = FieldError("email_too_long", {"max": limits["email_max"]})
        elif CONTROL_CHARS_RE.search(email) or not EMAIL_RE.match(email):
            errors["email"] = FieldError("email_invalid")
        else:
            clean["email"] = email


def _comment(data, errors, clean):
    comment = data.get("comment", "")
    if comment is None:
        comment = ""
    if not isinstance(comment, str):
        errors["comment"] = FieldError("too_long", {"max": config.FIELD_LIMITS["comment_max"]})
        return
    comment = comment.strip()
    if CONTROL_CHARS_RE.search(comment):
        errors["comment"] = FieldError("text_invalid_chars")
    elif len(comment) > config.FIELD_LIMITS["comment_max"]:
        errors["comment"] = FieldError("too_long", {"max": config.FIELD_LIMITS["comment_max"]})
    else:
        clean["comment"] = comment


def _phone(data, errors, clean):
    limits = config.FIELD_LIMITS
    phone = data.get("phone")
    if not isinstance(phone, str) or not phone.strip():
        errors["phone"] = FieldError("required")
        return
    phone = " ".join(phone.split())  # collapse inner whitespace
    digits = sum(ch.isdigit() for ch in phone)
    if len(phone) > limits["phone_max"]:
        errors["phone"] = FieldError("phone_too_long", {"max": limits["phone_max"]})
    elif not PHONE_CHARS_RE.match(phone) or phone.count("+") > 1:
        errors["phone"] = FieldError("phone_invalid")
    elif not limits["phone_digits_min"] <= digits <= limits["phone_digits_max"]:
        errors["phone"] = FieldError("phone_digits", {"min": limits["phone_digits_min"],
                                                      "max": limits["phone_digits_max"]})
    else:
        clean["phone"] = phone


def _items(data, errors, clean):
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        errors["items"] = FieldError("items_required")
        return
    if not raw_items:
        errors["items"] = FieldError("items_required")
        return
    if len(raw_items) > config.MAX_ORDER_ITEMS:
        errors["items"] = FieldError("items_too_many", {"max": config.MAX_ORDER_ITEMS})
        return

    items, seen = [], set()
    for i, raw in enumerate(raw_items):
        prefix = f"items.{i}"
        if not isinstance(raw, dict):
            errors[prefix] = FieldError("item_invalid")
            continue
        service_id = raw.get("service")
        if not isinstance(service_id, str) or service_id not in config.SERVICES:
            errors[f"{prefix}.service"] = FieldError("service_unknown")
            continue
        service = config.SERVICES[service_id]

        options = raw.get("options") or {}
        if not isinstance(options, dict):
            errors[f"{prefix}.options"] = FieldError("option_invalid")
            continue
        clean_options = {}
        allowed = service.get("options", {})
        if set(options) - set(allowed):
            errors[f"{prefix}.options"] = FieldError("option_invalid")
            continue
        bad_option = False
        for group, choices in allowed.items():
            choice = options.get(group)
            if not isinstance(choice, str) or choice not in choices:
                bad_option = True
                break
            clean_options[group] = choice
        if bad_option:
            errors[f"{prefix}.options"] = FieldError("option_invalid")
            continue

        try:
            quantity_scaled, decimals = parse_quantity(service_id, raw.get("quantity"))
        except QuantityError as exc:
            errors[f"{prefix}.quantity"] = FieldError(exc.code, exc.params)
            continue

        key = (service_id, tuple(sorted(clean_options.items())))
        if key in seen:
            errors[f"{prefix}.service"] = FieldError("item_duplicate")
            continue
        seen.add(key)
        items.append({"service": service_id, "quantity_scaled": quantity_scaled,
                      "decimals": decimals, "options": clean_options})

    if not any(k.startswith("items") for k in errors):
        clean["items"] = items


def validate_order(data: dict, allow_past: bool = False) -> tuple[dict, dict]:
    """Validate the JSON body of POST /api/requests (multi-service order).

    allow_past=True skips only the "date in the past" rule (used to look up a replayed
    token before rejecting). Unknown fields — e.g. prices or totals sent by the browser —
    are ignored. Returns (clean_data, errors).
    """
    limits = config.FIELD_LIMITS
    errors: dict[str, FieldError] = {}
    clean: dict = {}

    _token(data, errors, clean)
    _items(data, errors, clean)
    _name_email(data, errors, clean)
    _phone(data, errors, clean)
    _text(data, "city", "city", errors, clean, required=True,
          min_len=limits["city_min"], max_len=limits["city_max"])
    _text(data, "street", "street", errors, clean, required=True,
          min_len=limits["street_min"], max_len=limits["street_max"])
    _text(data, "apartment", "apartment", errors, clean, required=False,
          max_len=limits["apartment_max"])
    _date(data.get("date"), errors, clean, "date", allow_past)

    time_window = data.get("time")
    if not isinstance(time_window, str) or time_window not in config.PREFERRED_TIMES:
        errors["time"] = FieldError("time_invalid")
    else:
        clean["preferred_time"] = time_window

    _comment(data, errors, clean)
    return clean, errors


def is_legacy_payload(data: dict) -> bool:
    """Stage-2 single-service format: {cleaningType, area, ...} without "items"."""
    return "items" not in data and "cleaningType" in data


def validate_legacy_request(data: dict, allow_past: bool = True) -> tuple[dict, dict]:
    """Old single-service format. Used ONLY to recognise a replay of an existing request
    (same token) after the upgrade; new requests in this format are not accepted."""
    errors: dict[str, FieldError] = {}
    clean: dict = {}
    _token(data, errors, clean)

    cleaning_type = data.get("cleaningType")
    if not isinstance(cleaning_type, str) or cleaning_type not in config.PRICING["rates"]:
        errors["cleaningType"] = FieldError("type_invalid")
    else:
        clean["cleaning_type"] = cleaning_type

    area = data.get("area")
    area_min, area_max = config.PRICING["area"]["min"], config.PRICING["area"]["max"]
    if area is None:
        errors["area"] = FieldError("required")
    elif isinstance(area, float):
        errors["area"] = FieldError("area_decimal")
    elif not _is_int(area):
        errors["area"] = FieldError("area_number")
    elif not area_min <= area <= area_max:
        errors["area"] = FieldError("area_range", {"min": area_min, "max": area_max})
    else:
        clean["area_sqm"] = area

    _name_email(data, errors, clean)
    _date(data.get("date"), errors, clean, "date", allow_past)
    _comment(data, errors, clean)
    return clean, errors
