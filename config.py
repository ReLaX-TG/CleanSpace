"""CleanSpace — single source of truth for server settings and demo pricing.

All money values are whole numbers in grosze (1 PLN = 100 grosze),
so no floating-point arithmetic is ever used for prices.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Static website files (HTML/CSS/JS). The database must NOT live here.
STATIC_DIR = BASE_DIR / "static"

# Private runtime files live in ./instance: outside static/ and listed in .gitignore.
# CLEANSPACE_INSTANCE / CLEANSPACE_DB can override the locations (used by tests).
INSTANCE_DIR = Path(os.environ.get("CLEANSPACE_INSTANCE", BASE_DIR / "instance"))

# SQLite database.
DATABASE_PATH = Path(os.environ.get("CLEANSPACE_DB", INSTANCE_DIR / "cleanspace.db"))

# Automatic copies of the database made right before a schema migration.
DB_BACKUP_DIR = INSTANCE_DIR / "backups"

# Admin password HASH (never the password). Created by: flask --app app set-admin-password
ADMIN_CREDENTIALS_FILE = INSTANCE_DIR / "admin_credentials.json"

# Random session-signing secret, generated once and kept between restarts.
SECRET_KEY_FILE = INSTANCE_DIR / "secret_key"

# Local learning server: listen on the loopback interface only.
HOST = "127.0.0.1"
PORT = int(os.environ.get("CLEANSPACE_PORT", "5000"))

# Host names accepted in the HTTP Host header (protects against DNS rebinding).
ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost"}

# Business dates (the preferred cleaning date) are evaluated in Polish time.
BUSINESS_TIMEZONE = "Europe/Warsaw"

# Max size of a request body in bytes.
MAX_CONTENT_LENGTH = 16 * 1024

# ---------------------------------------------------------------------------
# Demo service catalog — the ONLY place where services, rates and limits are defined.
# DEMO values for a portfolio project, NOT verified market prices.
# The frontend loads them from GET /api/pricing; the server re-checks every line.
#
# Money: whole grosze (1 PLN = 100 grosze). Quantities: decimal strings; a unit's
# `decimals` says how many decimal places are allowed (m² -> 1, i.e. 0.1 m² steps).
# Rounding: each line total = rate × quantity, rounded HALF UP to 1 grosz; the order
# subtotal is the sum of the lines; the minimum order value is applied ONCE per order.
# ---------------------------------------------------------------------------
CURRENCY = "PLN"
MINIMUM_ORDER_GROSZE = 150_00   # 150 PLN, once per order
MAX_ORDER_ITEMS = 20

UNITS = {
    # unit code: decimals allowed in the quantity
    "m2": {"decimals": 1},
    "seat": {"decimals": 0},
    "item": {"decimals": 0},
}

SERVICE_CATEGORIES = ("rooms", "carpets", "upholstery", "mattresses")

# English labels are used by the server (Telegram, CRM fallback); the website and CRM
# translate by the stable codes below.
SERVICES = {
    # Room cleaning — existing rates and range kept (whole m², 20–300).
    "standard": {"category": "rooms", "label": "Standard cleaning", "unit": "m2",
                 "min": "20", "max": "300", "decimals": 0, "rate_grosze": 5_00},
    "deep": {"category": "rooms", "label": "Deep cleaning", "unit": "m2",
             "min": "20", "max": "300", "decimals": 0, "rate_grosze": 8_00},
    "moveout": {"category": "rooms", "label": "Move-out cleaning", "unit": "m2",
                "min": "20", "max": "300", "decimals": 0, "rate_grosze": 10_00},
    # Carpets — 0.1 m² precision.
    "carpet": {"category": "carpets", "label": "Carpet cleaning", "unit": "m2",
               "min": "1", "max": "60", "decimals": 1, "rate_grosze": 15_00},
    "fitted_carpet": {"category": "carpets", "label": "Fitted carpet cleaning", "unit": "m2",
                      "min": "5", "max": "200", "decimals": 1, "rate_grosze": 9_50},
    # Upholstery.
    "sofa": {"category": "upholstery", "label": "Sofa cleaning", "unit": "seat",
             "min": "1", "max": "10", "decimals": 0, "rate_grosze": 55_00},
    "armchair": {"category": "upholstery", "label": "Armchair cleaning", "unit": "item",
                 "min": "1", "max": "10", "decimals": 0, "rate_grosze": 70_00},
    "upholstered_chair": {"category": "upholstery", "label": "Upholstered chair cleaning", "unit": "item",
                          "min": "1", "max": "24", "decimals": 0, "rate_grosze": 25_00},
    # Mattresses — the size is the service; one or both sides is an option with its own rate.
    "mattress_single": {"category": "mattresses", "label": "Single mattress", "unit": "item",
                        "min": "1", "max": "6", "decimals": 0,
                        "options": {"sides": {"one": 90_00, "both": 150_00}}},
    "mattress_double": {"category": "mattresses", "label": "Double mattress", "unit": "item",
                        "min": "1", "max": "6", "decimals": 0,
                        "options": {"sides": {"one": 130_00, "both": 220_00}}},
}
OPTION_LABELS = {"sides": {"one": "one side", "both": "both sides"}}

# Preferred time window chosen by the customer (a wish, not a confirmed booking).
PREFERRED_TIMES = ("morning", "afternoon", "evening", "flexible")
PREFERRED_TIME_LABELS = {"morning": "Morning", "afternoon": "Afternoon",
                         "evening": "Evening", "flexible": "Flexible"}

# Legacy single-service pricing (stage 2 format). Still used to validate a REPLAY of an
# old request with its original token; new requests use SERVICES above.
PRICING = {
    "currency": CURRENCY,
    "minimum_price_grosze": MINIMUM_ORDER_GROSZE,
    "area": {"min": 20, "max": 300},
    "rates": {
        key: {"label": SERVICES[key]["label"], "short_label": SERVICES[key]["label"].split()[0],
              "per_sqm_grosze": SERVICES[key]["rate_grosze"]}
        for key in ("standard", "deep", "moveout")
    },
}

# Limits for request fields (characters, after trimming spaces at the edges).
FIELD_LIMITS = {
    "name_min": 2,
    "name_max": 80,
    "email_max": 120,
    "comment_max": 500,
    "phone_max": 25,          # characters incl. +, spaces, brackets, dashes
    "phone_digits_min": 7,
    "phone_digits_max": 15,   # E.164 maximum
    "city_min": 2,
    "city_max": 80,
    "street_min": 3,
    "street_max": 120,
    "apartment_max": 20,
}

# Admin price adjustment and agreed schedule.
AGREED_TOTAL_MAX_GROSZE = 100_000_00   # 100 000 PLN
AGREED_REASON_MIN = 3
AGREED_REASON_MAX = 300

# ---------------------------------------------------------------------------
# Admin CRM
# ---------------------------------------------------------------------------
# Request statuses: stored value -> label shown in the CRM.
REQUEST_STATUSES = {
    "new": "New",
    "in_progress": "In progress",
    "completed": "Completed",
    "cancelled": "Cancelled",
}
ADMIN_NOTE_MAX = 2000
ADMIN_PAGE_SIZE = 20
ADMIN_PASSWORD_MIN_LENGTH = 12

# Session cookie. The local server speaks plain HTTP, so the Secure flag is OFF by
# default (a Secure cookie would never be sent over http://127.0.0.1 and login would
# not work). Set CLEANSPACE_SECURE_COOKIES=1 only when the site is served over HTTPS.
SESSION_COOKIE_NAME = "cleanspace_admin"
SESSION_COOKIE_SECURE = os.environ.get("CLEANSPACE_SECURE_COOKIES") == "1"
SESSION_COOKIE_SAMESITE = "Strict"
SESSION_LIFETIME_HOURS = 8

# Login throttling: at most N failed attempts per client within the window.
# Older failures expire automatically, so there is no permanent lockout.
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60

# ---------------------------------------------------------------------------
# Telegram notifications (optional)
# ---------------------------------------------------------------------------
# Bot token + chat id, written by:  flask --app app setup-telegram
# Lives in ./instance (outside static/ and Git). Delete it to disable the integration.
TELEGRAM_CONFIG_FILE = INSTANCE_DIR / "telegram.json"
TELEGRAM_API_BASE = "https://api.telegram.org"

TELEGRAM_HTTP_TIMEOUT = 10          # seconds for one HTTP request (sendMessage, getMe, ...)
TELEGRAM_LONG_POLL_SECONDS = 20     # getUpdates long polling during setup
TELEGRAM_SETUP_WAIT_SECONDS = 300   # how long setup waits for the one-time code

NOTIFY_MAX_ATTEMPTS = 5             # per job (a manual retry gives a new budget)
NOTIFY_BACKOFF_SECONDS = (15, 60, 300, 900)  # pause before attempt 2, 3, 4, 5
# Telegram's 429 retry_after is honoured exactly (never shortened). If the wait would go
# past NOTIFY_MAX_AGE_SECONDS, the job is failed with an explanation instead of sent early.
NOTIFY_LEASE_SECONDS = 60           # a claimed job is locked to one worker this long
NOTIFY_MAX_AGE_SECONDS = 24 * 3600  # older pending jobs are not sent automatically
NOTIFY_POLL_SECONDS = 3             # worker idle wait between queue checks
