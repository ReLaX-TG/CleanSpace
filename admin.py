"""CleanSpace admin CRM: login/logout pages, request list/detail pages and JSON API.

Every page and API route (except the login page itself) checks the session on
the server. Without a valid session no request data is returned.
CSRF protection for all POST/PATCH routes comes from Flask-WTF's CSRFProtect
(registered in app.py).

Texts: the API returns stable CODES (statuses, services, units, error codes) plus an
English fallback text; the browser translates them (static/i18n/*.js).
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

import auth
import config
import db
from pricing import PLAIN_DECIMAL_RE, format_quantity

log = logging.getLogger("cleanspace.admin")

bp = Blueprint("admin", __name__, url_prefix="/admin")

SEARCH_MAX = 120
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@bp.after_request
def no_store(response):
    # Admin pages and data must not be cached by the browser.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def item_to_json(line) -> dict:
    """One order line as stored (snapshot of rate and parameters at order time)."""
    try:
        options = json.loads(line["options_json"] or "{}")
    except ValueError:
        options = {}
    return {
        "service": line["service"],
        "category": line["category"],
        "label": line["label_snapshot"],          # English name at order time (fallback)
        "unit": line["unit"],
        "quantity": format_quantity(line["quantity_scaled"], line["quantity_decimals"]),
        "options": options,
        "unitRateGrosze": line["unit_rate_grosze"],
        "lineTotalGrosze": line["line_total_grosze"],
    }


def request_to_json(row, items: list | None = None, detail: bool = False) -> dict:
    data = {
        "id": row["id"],
        "createdAt": row["created_at"],
        "name": row["name"],
        "items": [item_to_json(i) for i in (items or [])],
        "totalGrosze": row["total_grosze"],            # original estimate (never changed)
        "agreedTotalGrosze": row["agreed_total_grosze"],
        "currency": row["currency"],
        "preferredDate": row["preferred_date"],
        "preferredTime": row["preferred_time"] or None,   # None = not provided (old requests)
        "agreedDate": row["agreed_date"],
        "agreedTime": row["agreed_time"],
        "status": row["status"],
        "statusLabel": config.REQUEST_STATUSES.get(row["status"], row["status"]),
        "version": row["version"],
        "updatedAt": row["updated_at"],
    }
    if detail:
        data.update(
            email=row["email"],
            phone=row["phone"] or None,
            city=row["city"] or None,
            street=row["street"] or None,
            apartment=row["apartment"] or None,
            comment=row["comment"],
            adminNote=row["admin_note"],
            subtotalGrosze=row["subtotal_grosze"],
            minimumTopupGrosze=row["minimum_topup_grosze"],
            agreedTotalReason=row["agreed_total_reason"],
        )
    return data


def history_to_json(row) -> dict:
    try:
        entry = json.loads(row["changes_json"])
    except ValueError:
        entry = {"changes": []}
    return {"id": row["id"], "createdAt": row["created_at"], "actor": row["actor"],
            "action": row["action"], "changes": entry.get("changes", []), "reason": entry.get("reason")}


def _iso(ts) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


NOTIFICATION_LABELS = {
    "not_configured": "Not configured",
    "pending": "Pending",
    "sending": "Sending",
    "sent": "Sent",
    "failed": "Failed",
}


def notification_to_json(row, now: float | None = None) -> dict:
    """Telegram DELIVERY status — separate from the cleaning request's own status.

    `blockedUntil`: end of Telegram's pause (429) while it is still running, else None.
    """
    if row is None:
        return {
            "state": "not_configured",
            "label": NOTIFICATION_LABELS["not_configured"],
            "canRetry": False,
            "deliveryUncertain": False,
        }
    return {
        "state": row["status"],
        "label": NOTIFICATION_LABELS.get(row["status"], row["status"]),
        "attempts": row["attempts"],
        "maxAttempts": config.NOTIFY_MAX_ATTEMPTS,
        "manualRetries": row["manual_retries"],
        "lastError": row["last_error"],      # already token-free (see telegram_client.redact)
        "nextAttemptAt": _iso(row["next_attempt_at"]) if row["status"] == "pending" else None,
        "sentAt": _iso(row["sent_at"]),
        "queuedAt": _iso(row["created_at"]),
        "deliveryUncertain": bool(row["delivery_uncertain"]),
        "canRetry": row["status"] == "failed",
        "blockedUntil": (_iso(row["blocked_until"])
                         if row["status"] in ("pending", "failed")
                         and (row["blocked_until"] or 0) > (time.time() if now is None else now) else None),
    }


def _statuses_json() -> list:
    return [{"id": key, "label": label} for key, label in config.REQUEST_STATUSES.items()]


def _detail_json(row, request_id: int) -> dict:
    items = db.get_items(config.DATABASE_PATH, [request_id])[request_id]
    return request_to_json(row, items, detail=True)


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------
def _render_login(message: str = "", key: str = "", kind: str = "error", status: int = 200, **params):
    """`key` is the translation key; `message` the English fallback shown before JS runs."""
    return render_template(
        "admin/login.html",
        configured=auth.admin_is_configured(),
        message=message,
        message_key=key,
        message_params=params,
        kind=kind,
    ), status


@bp.get("/login")
def login():
    if auth.is_logged_in():
        return redirect(url_for("admin.requests_page"))
    if request.args.get("logged_out") == "1":
        return _render_login("You have been logged out.", "login.loggedOut", kind="info")
    return _render_login()


@bp.post("/login")
def login_post():
    if not auth.admin_is_configured():
        return _render_login(
            "The admin password is not set yet. Run the set-admin-password command (see README).",
            "login.notSetShort", status=503,
        )

    wait = auth.seconds_until_login_allowed()
    if wait:
        minutes = max(1, -(-wait // 60))
        log.warning("Admin login throttled")
        return _render_login(
            f"Too many failed attempts. Please wait about {minutes} minute(s) and try again.",
            "login.throttled", status=429, minutes=minutes,
        )

    password = request.form.get("password", "")
    if auth.verify_password(password):
        auth.reset_failed_logins()
        auth.log_in()
        log.info("Admin login succeeded")
        return redirect(url_for("admin.requests_page"), code=303)

    auth.register_failed_login()
    log.warning("Admin login failed")
    if auth.seconds_until_login_allowed():
        return _render_login(
            "Incorrect password. Too many failed attempts — please wait before trying again.",
            "login.incorrectThrottled", status=429,
        )
    return _render_login("Incorrect password.", "login.incorrect", status=401)


@bp.post("/logout")
def logout():
    was_logged_in = auth.is_logged_in()
    auth.log_out()
    if was_logged_in:
        log.info("Admin logged out")
    return redirect(url_for("admin.login", logged_out=1), code=303)


# ---------------------------------------------------------------------------
# Pages (HTML shells; data is loaded by admin.js from the API below)
# ---------------------------------------------------------------------------
@bp.get("/")
@bp.get("")  # registered first -> url_for() builds the canonical "/admin"
@auth.admin_page_required
def requests_page():
    return render_template("admin/list.html", statuses=config.REQUEST_STATUSES)


@bp.get("/requests/<int:request_id>")
@auth.admin_page_required
def request_page(request_id: int):
    return render_template(
        "admin/detail.html",
        request_id=request_id,
        statuses=config.REQUEST_STATUSES,
        note_max=config.ADMIN_NOTE_MAX,
        reason_max=config.AGREED_REASON_MAX,
    )


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------
@bp.get("/api/requests")
@auth.admin_api_required
def api_list():
    status = request.args.get("status", "").strip()
    query = request.args.get("q", "").strip()
    page_raw = request.args.get("page", "1").strip()

    errors, codes = {}, {}
    if status and status not in config.REQUEST_STATUSES:
        errors["status"], codes["status"] = "Unknown status.", {"code": "status_invalid", "params": {}}
    if len(query) > SEARCH_MAX:
        errors["q"] = f"Search text should be at most {SEARCH_MAX} characters."
        codes["q"] = {"code": "too_long", "params": {"max": SEARCH_MAX}}
    if not page_raw.isdigit() or int(page_raw) < 1 or len(page_raw) > 6:
        errors["page"], codes["page"] = "Page must be a positive whole number.", {"code": "page_invalid", "params": {}}
    if errors:
        return jsonify(error="validation_error", message="Invalid filter.", fields=errors, fieldCodes=codes), 400

    page_size = config.ADMIN_PAGE_SIZE
    page = int(page_raw)
    rows, total = db.list_requests(config.DATABASE_PATH, status or None, query or None, page, page_size)
    total_pages = max(1, -(-total // page_size))
    if total and page > total_pages:
        # Out of range (e.g. after a filter change): return the last existing page.
        page = total_pages
        rows, total = db.list_requests(config.DATABASE_PATH, status or None, query or None, page, page_size)

    items = db.get_items(config.DATABASE_PATH, [r["id"] for r in rows])
    return jsonify(
        items=[request_to_json(r, items[r["id"]]) for r in rows],
        page=page,
        pageSize=page_size,
        total=total,
        totalPages=total_pages,
        filters={"status": status, "q": query},
        counts=db.status_counts(config.DATABASE_PATH),
        statuses=_statuses_json(),
    )


@bp.get("/api/requests/<int:request_id>")
@auth.admin_api_required
def api_detail(request_id: int):
    row = db.get_request(config.DATABASE_PATH, request_id)
    if row is None:
        return jsonify(error="not_found", message=f"Request #{request_id} does not exist."), 404
    return jsonify(request=_detail_json(row, request_id), statuses=_statuses_json(),
                   noteMax=config.ADMIN_NOTE_MAX, reasonMax=config.AGREED_REASON_MAX,
                   history=[history_to_json(h) for h in db.get_history(config.DATABASE_PATH, request_id)],
                   notification=notification_to_json(db.get_notification(config.DATABASE_PATH, request_id)))


@bp.post("/api/requests/<int:request_id>/notification/retry")
@auth.admin_api_required
def api_retry_notification(request_id: int):
    """Manual retry of a FAILED Telegram notification (re-uses the existing queue job).

    Admin session + CSRF token required (CSRFProtect covers every POST).
    """
    result, row = db.retry_notification(config.DATABASE_PATH, request_id, time.time())
    if result == "not_found":
        if db.get_request(config.DATABASE_PATH, request_id) is None:
            return jsonify(error="not_found", message=f"Request #{request_id} does not exist."), 404
        return jsonify(error="no_notification",
                       message="No Telegram notification exists for this request, so there is nothing to retry.",
                       notification=notification_to_json(None)), 409
    if result == "not_failed":
        return jsonify(error="not_failed",
                       message="Only a failed notification can be retried.",
                       notification=notification_to_json(row)), 409
    log.info("Notification for request #%s queued again by admin", request_id)
    # `waitsForPause`: queued, but the worker sends it only after Telegram's pause ends.
    body = notification_to_json(row)
    return jsonify(notification=body, waitsForPause=body["blockedUntil"] is not None)


def _parse_money(raw) -> int | None:
    """'420', '420.5', '420,50' -> grosze (int). None/'' -> None. Raises ValueError."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ValueError("agreed_total_invalid")
    text = str(raw).strip().replace(",", ".").replace(" ", "")
    # Format before any arithmetic: plain decimal notation, at most 12 characters (no
    # exponent / NaN / Infinity), so "1e999999" can no longer raise decimal.Overflow.
    if len(text) > 12 or not PLAIN_DECIMAL_RE.fullmatch(text):
        raise ValueError("agreed_total_invalid")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError("agreed_total_invalid") from None
    grosze = value * 100
    if grosze != grosze.to_integral_value():
        raise ValueError("agreed_total_invalid")
    grosze = int(grosze)
    if not 0 < grosze <= config.AGREED_TOTAL_MAX_GROSZE:
        raise ValueError("agreed_total_range")
    return grosze


UPDATE_MESSAGES = {
    "status_invalid": "Please choose one of: " + ", ".join(config.REQUEST_STATUSES.values()) + ".",
    "note_type": "The note must be text.",
    "note_too_long": "The note should be at most {max} characters (now {length}).",
    "version_missing": "Missing record version. Reload the page and try again.",
    "agreed_date_invalid": "Please enter a real date (YYYY-MM-DD) or leave it empty.",
    "agreed_time_invalid": "Please enter a time as HH:MM (24-hour) or leave it empty.",
    "agreed_total_invalid": "Enter an amount in PLN with at most 2 decimals, e.g. 420 or 420.50, or leave it empty.",
    "agreed_total_range": "The agreed amount must be more than 0 and at most {max} PLN.",
    "price_reason_required": "Please give a reason for changing the agreed price.",
    "price_reason_length": "The reason should be {min}–{max} characters.",
}


@bp.patch("/api/requests/<int:request_id>")
@auth.admin_api_required
def api_update(request_id: int):
    """Change status, internal note, agreed date/time and/or agreed price (with reason).

    Fields left out of the body are not changed. Customer data, the order lines and the
    original estimate can never be changed. Every change is recorded in the history.
    """
    if not request.is_json:
        return jsonify(error="unsupported_media_type", message="Send JSON."), 415
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="invalid_json", message="The request body must be a JSON object."), 400

    errors: dict[str, tuple[str, dict]] = {}
    changes: dict = {}

    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors["version"] = ("version_missing", {})

    if "status" in data:
        status = data["status"]
        if not isinstance(status, str) or status not in config.REQUEST_STATUSES:
            errors["status"] = ("status_invalid", {})
        else:
            changes["status"] = status

    if "adminNote" in data:
        note = data["adminNote"]
        if note is None:
            note = ""
        if not isinstance(note, str):
            errors["adminNote"] = ("note_type", {})
        else:
            note = note.replace("\r\n", "\n").strip()
            if len(note) > config.ADMIN_NOTE_MAX:
                errors["adminNote"] = ("note_too_long", {"max": config.ADMIN_NOTE_MAX, "length": len(note)})
            else:
                changes["admin_note"] = note

    if "agreedDate" in data:
        raw = data["agreedDate"]
        if raw in (None, ""):
            changes["agreed_date"] = None
        elif not isinstance(raw, str) or not ISO_DATE_RE.match(raw):
            errors["agreedDate"] = ("agreed_date_invalid", {})
        else:
            try:
                changes["agreed_date"] = date.fromisoformat(raw).isoformat()
            except ValueError:
                errors["agreedDate"] = ("agreed_date_invalid", {})

    if "agreedTime" in data:
        raw = data["agreedTime"]
        if raw in (None, ""):
            changes["agreed_time"] = None
        elif not isinstance(raw, str) or not TIME_RE.match(raw):
            errors["agreedTime"] = ("agreed_time_invalid", {})
        else:
            changes["agreed_time"] = raw

    reason = ""
    if "agreedTotal" in data:
        try:
            changes["agreed_total_grosze"] = _parse_money(data["agreedTotal"])
        except ValueError as exc:
            errors["agreedTotal"] = (str(exc), {"max": config.AGREED_TOTAL_MAX_GROSZE // 100})

    current = db.get_request(config.DATABASE_PATH, request_id)
    if current is None:
        return jsonify(error="not_found", message=f"Request #{request_id} does not exist."), 404

    # A reason is mandatory whenever the agreed price actually changes (set, change or clear).
    if "agreed_total_grosze" in changes and changes["agreed_total_grosze"] != current["agreed_total_grosze"]:
        reason = data.get("priceReason")
        reason = reason.strip() if isinstance(reason, str) else ""
        if not reason:
            errors["priceReason"] = ("price_reason_required", {})
        elif not config.AGREED_REASON_MIN <= len(reason) <= config.AGREED_REASON_MAX or "\x00" in reason:
            errors["priceReason"] = ("price_reason_length", {"min": config.AGREED_REASON_MIN,
                                                             "max": config.AGREED_REASON_MAX})

    if errors:
        log.info("Admin update of request #%s rejected: invalid fields %s", request_id, sorted(errors))
        return jsonify(
            error="validation_error", message="Some fields need attention.",
            fields={k: UPDATE_MESSAGES[c].format(**p) for k, (c, p) in errors.items()},
            fieldCodes={k: {"code": c, "params": p} for k, (c, p) in errors.items()},
        ), 422

    result, row = db.update_request(config.DATABASE_PATH, request_id, changes, version, price_reason=reason)
    if result == "not_found":
        return jsonify(error="not_found", message=f"Request #{request_id} does not exist."), 404
    if result == "conflict":
        log.info("Admin update of request #%s rejected: version conflict", request_id)
        return jsonify(
            error="version_conflict",
            message="This request was changed in another tab or window after you opened it.",
            current=_detail_json(row, request_id),
        ), 409

    history = [history_to_json(h) for h in db.get_history(config.DATABASE_PATH, request_id)]
    if result == "unchanged":
        return jsonify(request=_detail_json(row, request_id), history=history, unchanged=True)
    # Field names and status only — note texts, reasons and amounts are not logged.
    log.info("Request #%s updated by admin (fields=%s, version=%s)", request_id,
             sorted(f for f in changes if f in db.EDITABLE_FIELDS), row["version"])
    return jsonify(request=_detail_json(row, request_id), history=history)
