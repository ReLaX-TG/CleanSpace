"""SQLite storage for CleanSpace.

- Parameterized queries only (user data is never formatted into SQL text).
- Every write runs inside an explicit transaction (BEGIN IMMEDIATE ... COMMIT).
- Schema changes are versioned with PRAGMA user_version, safe to run again, and an
  automatic backup copy is written before any migration.
- Idempotent request creation: UNIQUE request_token + hash of the request data
  (hash_version 1 = stage-2 single-service format, 2 = multi-service orders).
- Optimistic locking for admin edits (`version`); every admin change is written to the
  append-only `request_history` in the SAME transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import config
from config import ADMIN_NOTE_MAX, REQUEST_STATUSES

SCHEMA_VERSION = 5

# Status / time lists come from config (trusted constants, not user input).
_STATUS_SQL = ", ".join(f"'{key}'" for key in REQUEST_STATUSES)
_TIME_SQL = ", ".join(f"'{key}'" for key in ("",) + config.PREFERRED_TIMES)

# --- v2/v3 requests table (kept to migrate very old databases step by step) ---
REQUESTS_V2_SQL = f"""
CREATE TABLE {{name}} (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    request_token        TEXT    NOT NULL UNIQUE,
    payload_hash         TEXT    NOT NULL,
    created_at           TEXT    NOT NULL,
    cleaning_type        TEXT    NOT NULL,
    area_sqm             INTEGER NOT NULL CHECK (typeof(area_sqm) = 'integer'),
    rate_per_sqm_grosze  INTEGER NOT NULL CHECK (typeof(rate_per_sqm_grosze) = 'integer'),
    total_grosze         INTEGER NOT NULL CHECK (typeof(total_grosze) = 'integer'),
    currency             TEXT    NOT NULL,
    name                 TEXT    NOT NULL,
    email                TEXT    NOT NULL,
    preferred_date       TEXT    NOT NULL,
    comment              TEXT    NOT NULL DEFAULT '',
    status               TEXT    NOT NULL DEFAULT 'new' CHECK (status IN ({_STATUS_SQL})),
    admin_note           TEXT    NOT NULL DEFAULT '' CHECK (length(admin_note) <= {ADMIN_NOTE_MAX}),
    version              INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    updated_at           TEXT
)
"""

# --- v4: an order with contact data; the lines live in request_items ---
REQUESTS_TABLE_SQL = f"""
CREATE TABLE {{name}} (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    request_token         TEXT    NOT NULL UNIQUE,
    payload_hash          TEXT    NOT NULL,
    hash_version          INTEGER NOT NULL DEFAULT 2 CHECK (hash_version IN (1, 2)),
    created_at            TEXT    NOT NULL,              -- UTC, ISO 8601
    -- legacy single-service columns (only filled for requests created before v4)
    cleaning_type         TEXT,
    area_sqm              INTEGER CHECK (area_sqm IS NULL OR typeof(area_sqm) = 'integer'),
    rate_per_sqm_grosze   INTEGER CHECK (rate_per_sqm_grosze IS NULL OR typeof(rate_per_sqm_grosze) = 'integer'),
    -- original estimate (never changed after creation)
    subtotal_grosze       INTEGER NOT NULL CHECK (typeof(subtotal_grosze) = 'integer' AND subtotal_grosze >= 0),
    minimum_topup_grosze  INTEGER NOT NULL DEFAULT 0 CHECK (typeof(minimum_topup_grosze) = 'integer' AND minimum_topup_grosze >= 0),
    total_grosze          INTEGER NOT NULL CHECK (typeof(total_grosze) = 'integer'),
    currency              TEXT    NOT NULL,
    -- customer data (never changed after creation; '' = not provided by old requests)
    name                  TEXT    NOT NULL,
    email                 TEXT    NOT NULL,
    phone                 TEXT    NOT NULL DEFAULT '',
    city                  TEXT    NOT NULL DEFAULT '',
    street                TEXT    NOT NULL DEFAULT '',
    apartment             TEXT    NOT NULL DEFAULT '',
    preferred_date        TEXT    NOT NULL,              -- YYYY-MM-DD
    preferred_time        TEXT    NOT NULL DEFAULT '' CHECK (preferred_time IN ({_TIME_SQL})),
    comment               TEXT    NOT NULL DEFAULT '',
    -- admin fields
    status                TEXT    NOT NULL DEFAULT 'new' CHECK (status IN ({_STATUS_SQL})),
    admin_note            TEXT    NOT NULL DEFAULT '' CHECK (length(admin_note) <= {ADMIN_NOTE_MAX}),
    agreed_date           TEXT,                          -- YYYY-MM-DD, NULL = not agreed yet
    agreed_time           TEXT,                          -- HH:MM, NULL = not agreed yet
    agreed_total_grosze   INTEGER CHECK (agreed_total_grosze IS NULL OR
                                         (typeof(agreed_total_grosze) = 'integer' AND agreed_total_grosze > 0)),
    agreed_total_reason   TEXT    NOT NULL DEFAULT '',
    version               INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    updated_at            TEXT                           -- UTC, last admin change
)
"""

ITEMS_SQL = (
    """
    CREATE TABLE IF NOT EXISTS request_items (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id         INTEGER NOT NULL REFERENCES requests (id),
        position           INTEGER NOT NULL,
        category           TEXT    NOT NULL,
        service            TEXT    NOT NULL,
        unit               TEXT    NOT NULL,
        quantity_scaled    INTEGER NOT NULL CHECK (typeof(quantity_scaled) = 'integer' AND quantity_scaled > 0),
        quantity_decimals  INTEGER NOT NULL CHECK (quantity_decimals BETWEEN 0 AND 3),
        options_json       TEXT    NOT NULL DEFAULT '{}',
        unit_rate_grosze   INTEGER NOT NULL CHECK (typeof(unit_rate_grosze) = 'integer'),
        line_total_grosze  INTEGER NOT NULL CHECK (typeof(line_total_grosze) = 'integer'),
        label_snapshot     TEXT    NOT NULL,             -- English name at order time
        UNIQUE (request_id, position)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_items_request ON request_items (request_id)",
)

HISTORY_SQL = (
    """
    CREATE TABLE IF NOT EXISTS request_history (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id    INTEGER NOT NULL REFERENCES requests (id),
        created_at    TEXT    NOT NULL,                  -- UTC
        actor         TEXT    NOT NULL DEFAULT 'admin',
        action        TEXT    NOT NULL,                  -- status_changed / note_changed / schedule_changed / price_adjusted
        changes_json  TEXT    NOT NULL                   -- [{"field", "from", "to"}], optional "reason"
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_history_request ON request_history (request_id, id)",
    # Append-only: the database itself refuses edits and deletions of history rows.
    """
    CREATE TRIGGER IF NOT EXISTS request_history_no_update BEFORE UPDATE ON request_history
    BEGIN SELECT RAISE(ABORT, 'request_history is append-only'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS request_history_no_delete BEFORE DELETE ON request_history
    BEGIN SELECT RAISE(ABORT, 'request_history is append-only'); END
    """,
)

INDEXES_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_requests_status ON requests (status)",
    "CREATE INDEX IF NOT EXISTS idx_requests_created ON requests (created_at)",
)

LOGIN_FAILURES_SQL = (
    """
    CREATE TABLE IF NOT EXISTS login_failures (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        client_key TEXT    NOT NULL,
        failed_at  REAL    NOT NULL                      -- Unix time, seconds
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_login_failures_key ON login_failures (client_key, failed_at)",
)

# v3: outbox/queue for Telegram notifications. One job per request at most.
NOTIFICATIONS_SQL = (
    """
    CREATE TABLE IF NOT EXISTS notifications (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id          INTEGER NOT NULL UNIQUE REFERENCES requests (id),
        channel             TEXT    NOT NULL DEFAULT 'telegram' CHECK (channel IN ('telegram')),
        status              TEXT    NOT NULL DEFAULT 'pending'
                                    CHECK (status IN ('pending', 'sending', 'sent', 'failed')),
        attempts            INTEGER NOT NULL DEFAULT 0,
        manual_retries      INTEGER NOT NULL DEFAULT 0,
        next_attempt_at     REAL    NOT NULL,
        locked_by           TEXT,
        locked_until        REAL,
        last_error          TEXT    NOT NULL DEFAULT '',
        delivery_uncertain  INTEGER NOT NULL DEFAULT 0,  -- 1: an earlier attempt MAY have been delivered
        created_at          REAL    NOT NULL,
        updated_at          REAL    NOT NULL,
        sent_at             REAL,
        telegram_message_id INTEGER,
        blocked_until       REAL    NOT NULL DEFAULT 0
    )
    """,
    # blocked_until (v5): Telegram's pause from a 429 (retry_after), as a Unix time.
    # Nothing is sent before it, not even after a manual retry. 0 = no pause.
    "CREATE INDEX IF NOT EXISTS idx_notifications_due ON notifications (status, next_attempt_at)",
)

# Matches the error text written for a 429 before v5, to restore the pause on migration.
_OLD_429_RE = re.compile(r"Rate limited by Telegram \(429\): (?:asked to wait|next attempt after) (\d{1,9}) s")


def _migrate_to_v5(conn) -> None:
    """v4 -> v5: notifications.blocked_until (additive; no existing value changes).

    Jobs whose last error is a 429 get their pause restored from that error text and
    the time it was recorded (updated_at), so a pending manual retry cannot skip it.
    """
    if not _table_exists(conn, "notifications"):
        return  # created below with the new column
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(notifications)")}
    if "blocked_until" not in columns:
        conn.execute("ALTER TABLE notifications ADD COLUMN blocked_until REAL NOT NULL DEFAULT 0")
    for row in conn.execute("SELECT id, last_error, updated_at FROM notifications").fetchall():
        match = _OLD_429_RE.search(row["last_error"] or "")
        if match:
            conn.execute("UPDATE notifications SET blocked_until = MAX(blocked_until, ?) WHERE id = ?",
                         (row["updated_at"] + int(match.group(1)), row["id"]))

V1_COLUMNS = (
    "id, request_token, payload_hash, created_at, cleaning_type, area_sqm, "
    "rate_per_sqm_grosze, total_grosze, currency, name, email, preferred_date, comment, status"
)

# Fields that define "the same request" (hash_version 1 = stage-2 format).
HASHED_FIELDS_V1 = ("cleaning_type", "area_sqm", "name", "email", "preferred_date", "comment")
HASHED_FIELDS_V2 = ("name", "email", "phone", "city", "street", "apartment",
                    "preferred_date", "preferred_time", "comment")

ADMIN_LIST_COLUMNS = (
    "id, created_at, name, total_grosze, agreed_total_grosze, currency, "
    "preferred_date, preferred_time, agreed_date, agreed_time, status, version, updated_at"
)
ADMIN_DETAIL_COLUMNS = ADMIN_LIST_COLUMNS + (
    ", email, phone, city, street, apartment, comment, admin_note, "
    "subtotal_grosze, minimum_topup_grosze, agreed_total_reason"
)


class TokenConflict(Exception):
    """The request token was already used with different request data."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _casefold(value):
    return value.casefold() if isinstance(value, str) else value


def connect(db_path: Path) -> sqlite3.Connection:
    # isolation_level=None: transactions are controlled explicitly with BEGIN/COMMIT.
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Unicode-aware case-insensitive search (SQLite's lower()/LIKE only fold ASCII).
    conn.create_function("py_casefold", 1, _casefold, deterministic=True)
    return conn


# ---------------------------------------------------------------------------
# Schema creation and migration
# ---------------------------------------------------------------------------
def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _backup_before_migration(conn, backup_dir: Path, from_version: int) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"cleanspace-before-v{SCHEMA_VERSION}-from-v{from_version}-{stamp}.db"
    with closing(sqlite3.connect(target)) as dst:
        conn.backup(dst)  # consistent copy via the SQLite backup API
    return target


def _sequence(conn, name: str) -> int:
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (name,)).fetchone()
    return row[0] if row else 0


def _migrate_to_v2(conn) -> None:
    """v0/v1 -> v2: allow all CRM statuses, add admin_note, version, updated_at (table rebuild)."""
    old_count = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    old_seq = _sequence(conn, "requests")
    conn.execute(REQUESTS_V2_SQL.format(name="requests_v2"))
    conn.execute(f"INSERT INTO requests_v2 ({V1_COLUMNS}) SELECT {V1_COLUMNS} FROM requests")
    if conn.execute("SELECT COUNT(*) FROM requests_v2").fetchone()[0] != old_count:
        raise RuntimeError("Migration to v2 lost rows; aborting.")
    conn.execute("DROP TABLE requests")
    conn.execute("ALTER TABLE requests_v2 RENAME TO requests")
    conn.execute("UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'requests'", (old_seq,))


def _migrate_to_v4(conn) -> None:
    """v2/v3 -> v4: every old single-service request becomes an order with ONE line.

    Kept unchanged: id, request_token, payload_hash (as hash_version 1), created_at,
    total_grosze, rate, status, admin_note, version, updated_at, name, email, date,
    comment — and the link from notifications (same id). Missing new data (phone,
    address, time) stays empty = "Not provided"; nothing is invented.
    The line keeps the ORIGINAL rate; the original minimum top-up is derived as
    total − area × rate, so the stored total never changes.
    Caller runs this with foreign keys OFF inside one transaction.
    """
    old_count = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    old_sum = conn.execute("SELECT COALESCE(SUM(total_grosze), 0) FROM requests").fetchone()[0]
    old_seq = _sequence(conn, "requests")
    bad = conn.execute(
        "SELECT COUNT(*) FROM requests WHERE area_sqm * rate_per_sqm_grosze > total_grosze"
    ).fetchone()[0]
    if bad:
        raise RuntimeError("Migration to v4: a stored total is below area × rate; aborting.")

    conn.execute(REQUESTS_TABLE_SQL.format(name="requests_v4"))
    conn.execute(
        """
        INSERT INTO requests_v4 (
            id, request_token, payload_hash, hash_version, created_at,
            cleaning_type, area_sqm, rate_per_sqm_grosze,
            subtotal_grosze, minimum_topup_grosze, total_grosze, currency,
            name, email, preferred_date, comment, status, admin_note, version, updated_at
        )
        SELECT id, request_token, payload_hash, 1, created_at,
               cleaning_type, area_sqm, rate_per_sqm_grosze,
               area_sqm * rate_per_sqm_grosze, total_grosze - area_sqm * rate_per_sqm_grosze,
               total_grosze, currency,
               name, email, preferred_date, comment, status, admin_note, version, updated_at
          FROM requests
        """
    )
    for sql in ITEMS_SQL:
        conn.execute(sql)
    labels = {k: v["label"] for k, v in config.SERVICES.items()}
    for row in conn.execute("SELECT id, cleaning_type, area_sqm, rate_per_sqm_grosze FROM requests").fetchall():
        conn.execute(
            "INSERT INTO request_items (request_id, position, category, service, unit, quantity_scaled, "
            "quantity_decimals, options_json, unit_rate_grosze, line_total_grosze, label_snapshot) "
            "VALUES (?, 0, 'rooms', ?, 'm2', ?, 0, '{}', ?, ?, ?)",
            (row["id"], row["cleaning_type"], row["area_sqm"], row["rate_per_sqm_grosze"],
             row["area_sqm"] * row["rate_per_sqm_grosze"], labels.get(row["cleaning_type"], row["cleaning_type"])),
        )

    new_count, new_sum = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(total_grosze), 0) FROM requests_v4").fetchone()
    items = conn.execute("SELECT COUNT(*) FROM request_items").fetchone()[0]
    mismatch = conn.execute(
        "SELECT COUNT(*) FROM requests_v4 WHERE subtotal_grosze + minimum_topup_grosze != total_grosze"
    ).fetchone()[0]
    if (new_count, new_sum, items, mismatch) != (old_count, old_sum, old_count, 0):
        raise RuntimeError("Migration to v4 verification failed; nothing was changed.")

    conn.execute("DROP TABLE requests")
    conn.execute("ALTER TABLE requests_v4 RENAME TO requests")
    conn.execute("UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'requests'", (old_seq,))


def init_db(db_path: Path, backup_dir: Path | None = None) -> Path | None:
    """Create or upgrade the database. Safe to call on every start.

    Returns the path of the automatic backup if a migration was performed.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    with closing(connect(db_path)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        has_requests = _table_exists(conn, "requests")
        if has_requests and version < SCHEMA_VERSION and backup_dir is not None:
            backup_path = _backup_before_migration(conn, backup_dir, version)

        rebuild = has_requests and version < 4
        if rebuild:
            # Rebuilding `requests` while other tables reference it requires foreign keys
            # OFF (this PRAGMA cannot change inside a transaction); checked again below.
            conn.execute("PRAGMA foreign_keys = OFF")

        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-read inside the lock: another process may have migrated meanwhile.
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if not _table_exists(conn, "requests"):
                conn.execute(REQUESTS_TABLE_SQL.format(name="requests"))
            else:
                if version < 2:
                    _migrate_to_v2(conn)
                if version < 4:
                    # v3 only added notifications; it is created below if missing.
                    _migrate_to_v4(conn)
                if version < 5:
                    _migrate_to_v5(conn)
            for sql in INDEXES_SQL + LOGIN_FAILURES_SQL + NOTIFICATIONS_SQL + ITEMS_SQL + HISTORY_SQL:
                conn.execute(sql)
            if rebuild:
                # Only after a table rebuild (foreign keys were OFF): every reference must still resolve.
                problems = conn.execute("PRAGMA foreign_key_check").fetchall()
                if problems:
                    raise RuntimeError(f"Foreign key check failed after migration ({len(problems)} rows).")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            if rebuild:
                conn.execute("PRAGMA foreign_keys = ON")
    return backup_path


# ---------------------------------------------------------------------------
# Public site: create an order (idempotent)
# ---------------------------------------------------------------------------
def payload_hash_v1(clean: dict) -> str:
    """Stage-2 hash, unchanged, so old tokens keep matching after the upgrade."""
    canonical = json.dumps({k: clean[k] for k in HASHED_FIELDS_V1}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def payload_hash_v2(clean: dict) -> str:
    canonical = {k: clean[k] for k in HASHED_FIELDS_V2}
    canonical["items"] = [[i["service"], i["quantity_scaled"], i["decimals"], sorted(i["options"].items())]
                          for i in clean["items"]]
    text = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Kept for code that still imports the old name.
payload_hash = payload_hash_v1


def find_by_token(db_path: Path, token: str) -> sqlite3.Row | None:
    with closing(connect(db_path)) as conn:
        return conn.execute("SELECT * FROM requests WHERE request_token = ?", (token,)).fetchone()


def save_order(db_path: Path, clean: dict, priced: dict, notify: bool = False) -> tuple[sqlite3.Row, bool]:
    """Insert an order with its lines, or return the existing one for a repeated token.

    The request row, all its lines and (if `notify`) the notification job are written
    in ONE transaction. A repeated token with the same data returns the existing order
    and adds nothing. No network call happens here.

    Returns (row, created). Raises TokenConflict if the token was used with other data
    (including a stage-2 request's token sent with the new format).
    """
    digest = payload_hash_v2(clean)
    with closing(connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT * FROM requests WHERE request_token = ?", (clean["request_token"],)
            ).fetchone()
            if existing is not None:
                conn.execute("ROLLBACK")
                if existing["hash_version"] != 2 or existing["payload_hash"] != digest:
                    raise TokenConflict()
                return existing, False

            cur = conn.execute(
                """
                INSERT INTO requests (
                    request_token, payload_hash, hash_version, created_at,
                    subtotal_grosze, minimum_topup_grosze, total_grosze, currency,
                    name, email, phone, city, street, apartment,
                    preferred_date, preferred_time, comment, status
                ) VALUES (?, ?, 2, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')
                """,
                (clean["request_token"], digest, _utc_now(),
                 priced["subtotal_grosze"], priced["minimum_topup_grosze"], priced["total_grosze"],
                 config.CURRENCY, clean["name"], clean["email"], clean["phone"], clean["city"],
                 clean["street"], clean["apartment"], clean["preferred_date"], clean["preferred_time"],
                 clean["comment"]),
            )
            request_id = cur.lastrowid
            for position, line in enumerate(priced["lines"]):
                conn.execute(
                    "INSERT INTO request_items (request_id, position, category, service, unit, quantity_scaled, "
                    "quantity_decimals, options_json, unit_rate_grosze, line_total_grosze, label_snapshot) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (request_id, position, line["category"], line["service"], line["unit"],
                     line["quantity_scaled"], line["decimals"], json.dumps(line["options"], sort_keys=True),
                     line["unit_rate_grosze"], line["line_total_grosze"],
                     config.SERVICES[line["service"]]["label"]),
                )
            if notify:
                now = time.time()
                conn.execute(
                    "INSERT INTO notifications (request_id, next_attempt_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (request_id, now, now, now),
                )
            row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
            conn.execute("COMMIT")
            return row, True
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def get_items(db_path: Path, request_ids) -> dict[int, list[sqlite3.Row]]:
    """Lines (snapshots) per request id, in order."""
    ids = list(request_ids)
    result: dict[int, list] = {i: [] for i in ids}
    if not ids:
        return result
    placeholders = ",".join("?" for _ in ids)
    with closing(connect(db_path)) as conn:
        for row in conn.execute(
            f"SELECT * FROM request_items WHERE request_id IN ({placeholders}) ORDER BY request_id, position",
            ids,
        ):
            result[row["request_id"]].append(row)
    return result


# ---------------------------------------------------------------------------
# Admin CRM: read
# ---------------------------------------------------------------------------
def _search_clause(query: str) -> tuple[str, list]:
    """Search by number (exact, "12" or "#12"), name, email or phone (case-insensitive)."""
    needle = query.casefold()
    parts = ["instr(py_casefold(name), ?) > 0", "instr(py_casefold(email), ?) > 0",
             "instr(phone, ?) > 0"]
    params: list = [needle, needle, query]
    digits = query[1:] if query.startswith("#") else query
    if digits.isdigit() and len(digits) <= 12:
        parts.insert(0, "id = ?")
        params.insert(0, int(digits))
    return "(" + " OR ".join(parts) + ")", params


def list_requests(db_path: Path, status: str | None, query: str | None,
                  page: int, page_size: int) -> tuple[list[sqlite3.Row], int]:
    """Newest first. Returns (rows for the page, total matching rows)."""
    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(status)
    if query:
        clause, clause_params = _search_clause(query)
        where.append(clause)
        params.extend(clause_params)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with closing(connect(db_path)) as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM requests {where_sql}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT {ADMIN_LIST_COLUMNS} FROM requests {where_sql} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
    return rows, total


def status_counts(db_path: Path) -> dict:
    """Totals for the whole table — NOT affected by any filter or search."""
    counts = {key: 0 for key in REQUEST_STATUSES}
    with closing(connect(db_path)) as conn:
        for row in conn.execute("SELECT status, COUNT(*) AS n FROM requests GROUP BY status"):
            counts[row["status"]] = row["n"]
    counts["all"] = sum(counts[key] for key in REQUEST_STATUSES)
    return counts


def get_request(db_path: Path, request_id: int) -> sqlite3.Row | None:
    with closing(connect(db_path)) as conn:
        return conn.execute(
            f"SELECT {ADMIN_DETAIL_COLUMNS} FROM requests WHERE id = ?", (request_id,)
        ).fetchone()


def get_history(db_path: Path, request_id: int) -> list[sqlite3.Row]:
    with closing(connect(db_path)) as conn:
        return conn.execute(
            "SELECT * FROM request_history WHERE request_id = ? ORDER BY id DESC", (request_id,)
        ).fetchall()


# ---------------------------------------------------------------------------
# Admin CRM: update (status, note, agreed schedule, agreed price) + history
# ---------------------------------------------------------------------------
# field -> history action group
EDITABLE_FIELDS = {
    "status": "status_changed",
    "admin_note": "note_changed",
    "agreed_date": "schedule_changed",
    "agreed_time": "schedule_changed",
    "agreed_total_grosze": "price_adjusted",
}


def update_request(db_path: Path, request_id: int, changes: dict, expected_version: int,
                   price_reason: str = "") -> tuple[str, sqlite3.Row | None]:
    """Apply admin changes if the row still has `expected_version`.

    `changes` may contain only EDITABLE_FIELDS (validated by the caller). Customer data,
    the original lines and the original estimate are never touched. The update and its
    history rows are written in ONE transaction.

    Returns ("updated", row), ("unchanged", row), ("conflict", current_row) or ("not_found", None).
    """
    unknown = set(changes) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValueError(f"not editable: {sorted(unknown)}")
    with closing(connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = conn.execute(
                f"SELECT {ADMIN_DETAIL_COLUMNS} FROM requests WHERE id = ?", (request_id,)
            ).fetchone()
            if current is None:
                conn.execute("ROLLBACK")
                return "not_found", None
            if current["version"] != expected_version:
                conn.execute("ROLLBACK")
                return "conflict", current

            diff = {f: (current[f], v) for f, v in changes.items() if current[f] != v}
            if not diff:
                conn.execute("ROLLBACK")
                return "unchanged", current

            now = _utc_now()
            sets = ", ".join(f"{f} = ?" for f in diff)
            values = [new for _, new in diff.values()]
            if "agreed_total_grosze" in diff:
                sets += ", agreed_total_reason = ?"
                values.append(price_reason)
            cur = conn.execute(
                f"UPDATE requests SET {sets}, version = version + 1, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (*values, now, request_id, expected_version),
            )
            if cur.rowcount != 1:  # cannot happen inside the lock, but never write history blindly
                raise RuntimeError("version changed during update")

            groups: dict[str, list] = {}
            for f, (old, new) in diff.items():
                groups.setdefault(EDITABLE_FIELDS[f], []).append({"field": f, "from": old, "to": new})
            for action, field_changes in groups.items():
                entry = {"changes": field_changes}
                if action == "price_adjusted":
                    entry["reason"] = price_reason
                conn.execute(
                    "INSERT INTO request_history (request_id, created_at, actor, action, changes_json) "
                    "VALUES (?, ?, 'admin', ?, ?)",
                    (request_id, now, action, json.dumps(entry, ensure_ascii=False)),
                )
            row = conn.execute(
                f"SELECT {ADMIN_DETAIL_COLUMNS} FROM requests WHERE id = ?", (request_id,)
            ).fetchone()
            conn.execute("COMMIT")
            return "updated", row
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


# ---------------------------------------------------------------------------
# Notification queue (outbox)
# ---------------------------------------------------------------------------
# Life cycle:  pending --claim--> sending --> sent
#                                    |------> pending (retry later)
#                                    '------> failed  (permanent error / attempts used up)
#              failed --manual retry--> pending (same job, new attempt budget)
# Network calls happen OUTSIDE any transaction: claim commits first, the worker
# talks to Telegram, then a separate short transaction records the result.

NOTIFICATION_JOB_SQL = """
    SELECT n.*, r.total_grosze, r.agreed_total_grosze, r.currency, r.preferred_date, r.preferred_time
      FROM notifications n JOIN requests r ON r.id = n.request_id
     WHERE n.id = ?
"""


def claim_notification(db_path: Path, worker_id: str, now: float, lease_seconds: int) -> sqlite3.Row | None:
    """Atomically take one due job for `worker_id`, or return None.

    BEGIN IMMEDIATE holds SQLite's write lock while selecting and updating, so two
    worker processes can never claim the same job. A job stuck in 'sending' whose
    lease expired (worker crashed) is taken again and marked as possibly delivered.
    """
    with closing(connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """
                SELECT id FROM notifications
                 WHERE ((status = 'pending' AND next_attempt_at <= ?)
                        OR (status = 'sending' AND locked_until < ?))
                   AND blocked_until <= ?          -- Telegram's pause (429) is never skipped
                 ORDER BY next_attempt_at, id
                 LIMIT 1
                """,
                (now, now, now),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            conn.execute(
                """
                UPDATE notifications
                   SET delivery_uncertain = CASE WHEN status = 'sending' THEN 1 ELSE delivery_uncertain END,
                       last_error = CASE WHEN status = 'sending'
                                         THEN 'A previous worker stopped while sending; delivery unknown.'
                                         ELSE last_error END,
                       status = 'sending', locked_by = ?, locked_until = ?,
                       attempts = attempts + 1, updated_at = ?
                 WHERE id = ?
                """,
                (worker_id, now + lease_seconds, now, row["id"]),
            )
            job = conn.execute(NOTIFICATION_JOB_SQL, (row["id"],)).fetchone()
            conn.execute("COMMIT")
            return job
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def finish_notification(db_path: Path, job_id: int, worker_id: str, status: str, now: float, *,
                        error: str = "", next_attempt_at: float | None = None,
                        uncertain: bool = False, message_id: int | None = None,
                        attempts_delta: int = 0, blocked_until: float | None = None) -> bool:
    """Record the result of an attempt. Only the worker holding the lock may do this.

    `uncertain` can only SET delivery_uncertain; nothing here clears it.
    `attempts_delta=-1` gives the attempt back (used for 429: Telegram did not accept it).
    `blocked_until` (429) can only EXTEND the stored pause, never shorten it.
    Returns False if the lock was lost (lease expired and another worker took the job).
    """
    if status not in ("sent", "pending", "failed"):
        raise ValueError(status)
    with closing(connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                """
                UPDATE notifications
                   SET status = ?, last_error = ?, updated_at = ?,
                       next_attempt_at = COALESCE(?, next_attempt_at),
                       delivery_uncertain = CASE WHEN ? THEN 1 ELSE delivery_uncertain END,
                       sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END,
                       telegram_message_id = COALESCE(?, telegram_message_id),
                       attempts = MAX(attempts + ?, 0),
                       blocked_until = MAX(blocked_until, COALESCE(?, 0)),
                       locked_by = NULL, locked_until = NULL
                 WHERE id = ? AND locked_by = ? AND status = 'sending'
                """,
                (status, error, now, next_attempt_at, 1 if uncertain else 0,
                 status, now, message_id, attempts_delta, blocked_until, job_id, worker_id),
            )
            conn.execute("COMMIT")
            return cur.rowcount == 1
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def release_notification(db_path: Path, job_id: int, worker_id: str, now: float) -> bool:
    """Give a claimed job back WITHOUT an attempt (nothing was sent): pending, due now."""
    with closing(connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                """
                UPDATE notifications
                   SET status = 'pending', attempts = MAX(attempts - 1, 0), next_attempt_at = ?,
                       locked_by = NULL, locked_until = NULL, updated_at = ?
                 WHERE id = ? AND locked_by = ? AND status = 'sending'
                """,
                (now, now, job_id, worker_id),
            )
            conn.execute("COMMIT")
            return cur.rowcount == 1
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def get_notification(db_path: Path, request_id: int) -> sqlite3.Row | None:
    with closing(connect(db_path)) as conn:
        return conn.execute(
            "SELECT * FROM notifications WHERE request_id = ?", (request_id,)
        ).fetchone()


def retry_notification(db_path: Path, request_id: int, now: float) -> tuple[str, sqlite3.Row | None]:
    """Put a FAILED job back to pending (same row, fresh attempt budget).

    Telegram's pause is kept: the job may be queued now, but it becomes due only at
    MAX(now, blocked_until), and claim_notification also refuses it before that time.
    Returns ("queued", row), ("not_failed", row) or ("not_found", None).
    """
    with closing(connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                """
                UPDATE notifications
                   SET status = 'pending', attempts = 0, manual_retries = manual_retries + 1,
                       next_attempt_at = MAX(?, blocked_until), locked_by = NULL, locked_until = NULL,
                       updated_at = ?
                 WHERE request_id = ? AND status = 'failed'
                """,
                (now, now, request_id),
            )
            row = conn.execute("SELECT * FROM notifications WHERE request_id = ?", (request_id,)).fetchone()
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    if cur.rowcount == 1:
        return "queued", row
    return ("not_failed", row) if row is not None else ("not_found", None)


# ---------------------------------------------------------------------------
# Login throttling
# ---------------------------------------------------------------------------
def recent_login_failures(db_path: Path, client_key: str, now: float, window: int) -> tuple[int, float | None]:
    """Number of failures inside the window and the time of the oldest one."""
    with closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT COUNT(*), MIN(failed_at) FROM login_failures WHERE client_key = ? AND failed_at > ?",
            (client_key, now - window),
        ).fetchone()
    return row[0], row[1]


def record_login_failure(db_path: Path, client_key: str, now: float, window: int) -> None:
    with closing(connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM login_failures WHERE failed_at <= ?", (now - window,))
            conn.execute(
                "INSERT INTO login_failures (client_key, failed_at) VALUES (?, ?)", (client_key, now)
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def clear_login_failures(db_path: Path, client_key: str) -> None:
    with closing(connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM login_failures WHERE client_key = ?", (client_key,))
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
