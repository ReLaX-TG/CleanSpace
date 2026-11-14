"""CleanSpace — local learning server (Flask + SQLite).

Serves the static website, the public JSON API and the admin CRM from one process.
Listens on 127.0.0.1 only. This is NOT a production server.

Run:              .venv\\Scripts\\python app.py           ->  http://127.0.0.1:5000
Admin password:   .venv\\Scripts\\flask --app app set-admin-password
"""

import logging
from datetime import timedelta

import click
from flask import Flask, abort, jsonify, render_template, request
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.exceptions import HTTPException
from werkzeug.serving import WSGIRequestHandler

import auth
import config
import db
import telegram_cli
import telegram_config
from admin import bp as admin_bp
from admin import item_to_json
from pricing import price_order, public_pricing
from validation import (FieldError, business_today, errors_to_json, is_legacy_payload,
                        validate_legacy_request, validate_order)

log = logging.getLogger("cleanspace")

csrf = CSRFProtect()


class QuietRequestHandler(WSGIRequestHandler):
    """Access log WITHOUT the query string: CRM search text may contain names or emails."""

    def log_request(self, code="-", size="-"):
        path = self.path.split("?", 1)[0]
        self.log("info", '"%s %s %s" %s', self.command, path, self.request_version, getattr(code, "value", code))


def create_app() -> Flask:
    app = Flask(
        __name__,
        static_folder=str(config.STATIC_DIR),
        static_url_path="",  # /styles.css, /script.js are served from ./static
    )
    app.config.update(
        MAX_CONTENT_LENGTH=config.MAX_CONTENT_LENGTH,
        SECRET_KEY=auth.load_or_create_secret_key(config.SECRET_KEY_FILE),
        SESSION_COOKIE_NAME=config.SESSION_COOKIE_NAME,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE=config.SESSION_COOKIE_SAMESITE,
        SESSION_COOKIE_SECURE=config.SESSION_COOKIE_SECURE,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=config.SESSION_LIFETIME_HOURS),
        # CSRF token stays valid for the whole (8 h) session instead of the 1 h default,
        # so a CRM tab left open does not fail on save.
        WTF_CSRF_TIME_LIMIT=None,
    )
    app.json.ensure_ascii = False
    app.json.sort_keys = False

    backup = db.init_db(config.DATABASE_PATH, config.DB_BACKUP_DIR)
    if backup:
        log.warning("Database schema upgraded. Backup of the previous file: %s", backup)

    csrf.init_app(app)
    app.register_blueprint(admin_bp)

    # ------------------------------------------------------------------ guards
    @app.before_request
    def check_host():
        # Only answer to 127.0.0.1 / localhost (blocks DNS-rebinding style access).
        hostname = (request.host or "").rsplit(":", 1)[0].strip("[]").lower()
        if hostname not in config.ALLOWED_HOSTNAMES:
            abort(400)

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # ------------------------------------------------------------------ pages
    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    # ------------------------------------------------------------------ public API
    @app.get("/api/pricing")
    def get_pricing():
        return jsonify(public_pricing())

    @app.post("/api/requests")
    @csrf.exempt  # no cookies/session involved; JSON-only body (needs a CORS preflight cross-site)
    def create_request():
        if not request.is_json:
            return jsonify(error="unsupported_media_type",
                           message="Send the request as JSON (Content-Type: application/json)."), 415

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="invalid_json", message="The request body must be a JSON object."), 400

        def invalid(errors):
            # Field names only — never log the submitted values.
            log.info("Demo request rejected: invalid fields %s", sorted(errors))
            messages, codes = errors_to_json(errors)
            return jsonify(error="validation_error", message="Some fields need attention.",
                           fields=messages, fieldCodes=codes), 422

        def conflict():
            log.info("Demo request rejected: token reused with different data")
            return jsonify(error="token_conflict",
                           message="This save attempt was already used for different data. "
                                   "Please click Preview request again."), 409

        # --- Replay of a stage-2 (single-service) request with its ORIGINAL token ---
        if is_legacy_payload(data):
            clean, errors = validate_legacy_request(data, allow_past=True)
            if errors:
                return invalid(errors)
            existing = db.find_by_token(config.DATABASE_PATH, clean["request_token"])
            if existing is None:
                # New requests must use the current form (contact and address are required now).
                return jsonify(error="outdated_form",
                               message="This page is outdated. Please reload it and fill in the order again."), 422
            if existing["hash_version"] != 1 or existing["payload_hash"] != db.payload_hash_v1(clean):
                return conflict()
            log.info("Demo request #%s returned again for a repeated token", existing["id"])
            return jsonify(_order_body(existing, created=False, legacy=True)), 200

        # --- Multi-service order ---
        clean, errors = validate_order(data, allow_past=True)
        if errors:
            return invalid(errors)
        existing = db.find_by_token(config.DATABASE_PATH, clean["request_token"])
        if existing is None and clean["preferred_date"] < business_today().isoformat():
            # Past dates are refused for NEW orders; a replay of a saved one is still recognised.
            return invalid({"date": FieldError("date_past")})

        # The server prices every line itself. Prices/totals sent by the browser are ignored.
        priced = price_order(clean["items"])
        try:
            # If Telegram is configured, a notification job is queued in the SAME transaction.
            # Nothing is sent here: the separate telegram-worker delivers it later.
            row, created = db.save_order(config.DATABASE_PATH, clean, priced,
                                         notify=telegram_config.is_configured())
        except db.TokenConflict:
            return conflict()

        if created:
            log.info("Demo request #%s saved (%s lines)", row["id"], len(priced["lines"]))
        else:
            log.info("Demo request #%s returned again for a repeated token", row["id"])
        return jsonify(_order_body(row, created=created)), (201 if created else 200)

    def _order_body(row, created: bool, legacy: bool = False) -> dict:
        lines = db.get_items(config.DATABASE_PATH, [row["id"]])[row["id"]]
        body = {
            "id": row["id"],
            "status": row["status"],
            "createdAt": row["created_at"],
            "items": [item_to_json(line) for line in lines],
            "subtotalGrosze": row["subtotal_grosze"],
            "minimumTopupGrosze": row["minimum_topup_grosze"],
            "totalGrosze": row["total_grosze"],
            "currency": row["currency"],
            "duplicate": not created,
        }
        if legacy:  # fields the stage-2 page expects
            body.update(cleaningType=row["cleaning_type"], area=row["area_sqm"],
                        ratePerSqmGrosze=row["rate_per_sqm_grosze"])
        return body

    # ------------------------------------------------------------------ errors
    def _wants_json() -> bool:
        return request.path.startswith("/api/") or request.path.startswith("/admin/api/")

    @app.errorhandler(CSRFError)
    def csrf_error(exc: CSRFError):
        log.warning("Request rejected: missing or invalid CSRF token (%s)", request.path)
        message = "Security token is missing or expired. Reload the page and try again."
        if _wants_json():
            return jsonify(error="csrf_failed", message=message), 400
        return render_template("admin/login.html", configured=auth.admin_is_configured(),
                               message=message, message_key="login.csrf", message_params={},
                               kind="error"), 400

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException):
        if _wants_json():
            return jsonify(error=exc.name.lower().replace(" ", "_"), message=exc.description), exc.code
        return exc

    @app.errorhandler(Exception)
    def unexpected_error(exc: Exception):
        # The traceback contains code locations, not request bodies or form fields.
        log.exception("Unexpected server error")
        if _wants_json():
            return jsonify(error="server_error", message="Unexpected server error. Please try again."), 500
        return "Unexpected server error.", 500

    # ------------------------------------------------------------------ CLI
    @app.cli.command("set-admin-password")
    def set_admin_password():
        """Set or change the admin password (hidden input, asked twice)."""
        password = click.prompt(
            "New admin password", hide_input=True, confirmation_prompt="Repeat the password",
        )
        if len(password) < config.ADMIN_PASSWORD_MIN_LENGTH:
            raise click.ClickException(
                f"Password is too short: use at least {config.ADMIN_PASSWORD_MIN_LENGTH} characters."
            )
        if password.strip() != password:
            raise click.ClickException("Password must not start or end with spaces.")
        auth.save_admin_password(password)
        click.echo(f"Admin password saved (only a salted hash is stored in {config.ADMIN_CREDENTIALS_FILE}).")
        click.echo("Existing admin sessions are now logged out.")

    telegram_cli.register(app)

    return app


app = create_app()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(f"CleanSpace local demo server: http://{config.HOST}:{config.PORT}  (Ctrl+C to stop)")
    print(f"Admin CRM: http://{config.HOST}:{config.PORT}/admin")
    print(f"Database: {config.DATABASE_PATH}")
    if not auth.admin_is_configured():
        print("Admin password is NOT set. Run:  .venv\\Scripts\\flask --app app set-admin-password")
    if telegram_config.is_configured():
        print("Telegram: configured. Run the worker in a second window: "
              ".venv\\Scripts\\flask --app app telegram-worker")
    else:
        print("Telegram: not configured (optional). Setup: .venv\\Scripts\\flask --app app setup-telegram")
    # debug=False: the Werkzeug debugger must never be exposed.
    app.run(host=config.HOST, port=config.PORT, debug=False, request_handler=QuietRequestHandler)
