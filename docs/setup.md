# CleanSpace — setup and technical reference

Detailed notes behind the short [project README](../README.md): every command, the CRM
API, how Telegram delivery and retries work, security choices, the database schema and
its migrations, the public API and the known limitations.

> **Demo project — local learning server only.**
> CleanSpace is not a real company. Prices are illustrative. Requests are stored in a
> SQLite file on your own computer. **Enter only made-up data.**
> The server is **not** intended for public/production use: run it only on your own
> machine. By default it listens on `127.0.0.1` only (`HOST` in `config.py`).

## Stack

- Frontend: HTML, CSS, vanilla JavaScript (no frameworks, no build step).
- Backend: Python 3.10+, Flask, Flask-WTF (CSRF protection), SQLite (built into Python).
- No external images, fonts or scripts.

## Project structure

| Path                      | Purpose                                                                 |
|---------------------------|-------------------------------------------------------------------------|
| `app.py`                  | Flask app: website, public API, CRM, CLI command; listens on 127.0.0.1. |
| `admin.py`                | CRM pages and JSON API (login/logout, list, detail, update).            |
| `auth.py`                 | Admin password hash, session, login throttling, secret key.             |
| `config.py`               | **Single source of truth**: prices, limits, statuses, cookie and throttle settings, paths. |
| `pricing.py`              | Server-side price calculation (integers in grosze).                     |
| `validation.py`           | Server-side validation of public request fields.                        |
| `db.py`                   | SQLite schema + versioned migrations, parameterized queries, transactions, notification queue. |
| `telegram_client.py`      | Minimal Telegram Bot API client (stdlib `urllib`, bounded timeouts, token redaction). |
| `telegram_config.py`      | Reads/writes `instance/telegram.json`.                                  |
| `telegram_cli.py`         | CLI: `setup-telegram`, `test-telegram`, `telegram-worker`, `disable-telegram`. |
| `notifications.py`        | Worker logic: message text, retries, backoff, 429, permanent errors.    |
| `static/`                 | Public site (`index.html`, `styles.css`, `script.js`) and CRM assets (`admin.css`, `admin.js`). |
| `static/i18n/`            | `i18n.js` (language layer) and the dictionaries `en.js`, `uk.js`.       |
| `static/img/hero-interior.webp` | AI-generated decorative interior for the hero (see “Images”).     |
| `tests/manual/save_flow_check.js` | Browser console check for the save / lost-answer flow.         |
| `templates/admin/`        | CRM page templates (Jinja, auto-escaped).                               |
| `tests/`                  | Automated tests (`unittest`, temporary database and instance folder).   |
| `instance/`               | **Private runtime files — never commit** (see below).                   |
| `_backup/`                | Manual backups made before big changes (code + database).               |

`instance/` contains:

| File                          | What it is                                                         |
|-------------------------------|--------------------------------------------------------------------|
| `cleanspace.db`               | The SQLite database with all requests.                             |
| `admin_credentials.json`      | Salted **hash** of the admin password (never the password itself). |
| `secret_key`                  | Random secret that signs the admin session cookie (kept between restarts). |
| `telegram.json`               | Telegram **bot token** and chat id (optional). Plain text — treat it like a password. |
| `backups/`                    | Automatic database copies made right before a schema migration.    |

`instance/`, `_backup/`, `.venv/`, `*.db` and secret files are listed in `.gitignore`.

## Design — “Graphite”

Dark graphite surfaces, warm white text, restrained light-green accents, large display
typography, clear buttons and borders; no neon glow or decorative animation. Mobile-first
(checked at 320 / 375 / 768 / 1440 px). Contrast is calculated in `styles.css` (main text
≈ 15:1, muted text ≈ 8.9:1, accent buttons ≈ 11:1). `prefers-reduced-motion` turns off
smooth scrolling and transitions. The CRM uses the same tokens; request statuses differ by
colour **and** by text and badge style.

### Images

`static/img/hero-interior.webp` (1914 × 822, WebP, ≈ 240 KB) is an **AI-generated image**
supplied by the project owner to decorate this demo. It is **not a photograph** and does
not show work done by CleanSpace or any real company. The headline and all text are
regular HTML on top of / next to the image — no text is baked into the picture.

- Desktop (≥ 1024 px): the image fills the hero; the text sits on the left, over the dark
  wall, with only light shading so the interior on the right stays visible.
- Phones and tablets: the text comes first on the graphite background and the image is
  shown right below it, cropped to the table, lamp and window.

The file was already WebP, so it is used as is (no re-encoding, to avoid extra quality
loss). To replace it, add an image you are allowed to use (note its source and licence
here) and change the `src` of `.hero-media` in `index.html`.

`static/img/hero-interior.svg` (the earlier placeholder illustration) is no longer used
by the page and can be deleted.

## Languages (English / Українська)

- The site and the CRM start in **English**. The browser language and location are never
  used for an automatic switch.
- The visitor chooses **EN / UA** in the header. An explicit choice is saved in
  `localStorage` (`cleanspace.lang`); a missing or invalid value means English. If storage
  is blocked, the switch still works for the current page.
- Site and CRM share the setting (same origin). `<html lang>` is updated; dates and numbers
  follow the language (`en-GB` / `uk-UA`); the currency stays **PLN**; business time is
  **Europe/Warsaw**.
- Dictionaries: `static/i18n/en.js`, `static/i18n/uk.js` (same keys; missing keys fall back
  to English). To add a language: copy `en.js`, translate it, add it to `LANGS` in
  `static/i18n/i18n.js` and load the file in `index.html` and `templates/admin/base.html`.
- The database stores **codes** only (services, units, statuses, time windows). Names,
  addresses, comments and notes are never translated. Server errors come with stable codes
  (`fieldCodes`) that the page translates; English text is the fallback.
- Switching the language only re-renders text: the form, the order, the prices and the
  `requestToken` stay the same. Telegram messages stay in English.
- Messages that stay on screen (line added / merged / updated / removed, builder and field
  errors, “saved”, CRM conflicts and Telegram retries) keep **codes and raw values** —
  service and option codes, status codes, amounts in grosze, quantities, ISO times — never
  already translated text. `CSI18n.ref.*` marks such values and `CSI18n.t` translates and
  formats them on every render, so a switch updates every part of the message.

## Services and demo rates

**Demo values for a portfolio project — not verified market prices.** All of them are in
`config.py` (`SERVICES`, `MINIMUM_ORDER_GROSZE`, `MAX_ORDER_ITEMS`); the page loads them from
`GET /api/pricing` and the server re-checks every line.

| Category | Service (code) | Unit | Allowed quantity | Demo rate |
|---|---|---|---|---|
| Home cleaning (`rooms`) | Standard cleaning (`standard`) | m² | 20–300, whole | 5 PLN / m² |
| Home cleaning (`rooms`) | Deep cleaning (`deep`) | m² | 20–300, whole | 8 PLN / m² |
| Home cleaning (`rooms`) | Move-out cleaning (`moveout`) | m² | 20–300, whole | 10 PLN / m² |
| Carpets | Carpet cleaning (`carpet`) | m² | 1–60, step 0.1 | 15 PLN / m² |
| Carpets | Fitted carpet cleaning (`fitted_carpet`) | m² | 5–200, step 0.1 | 9.50 PLN / m² |
| Upholstery | Sofa cleaning (`sofa`) | seat | 1–10 seats | 55 PLN / seat |
| Upholstery | Armchair cleaning (`armchair`) | piece | 1–10 | 70 PLN / piece |
| Upholstery | Upholstered chair cleaning (`upholstered_chair`) | piece | 1–24 | 25 PLN / piece |
| Mattresses | Single mattress (`mattress_single`) | piece | 1–6 | one side 90 / both sides 150 PLN |
| Mattresses | Double mattress (`mattress_double`) | piece | 1–6 | one side 130 / both sides 220 PLN |

- Minimum order value **150 PLN**, applied **once per order** (shown as a separate top-up line).
- At most **20 lines** per order (browser and server). An empty order cannot be saved.
- Adding the same service with the same options again **adds to that line** (with a message)
  instead of creating a duplicate; the server rejects duplicate lines.

### Money and rounding

Money is stored in whole **grosze**. Quantities are parsed with `Decimal` and stored as
scaled integers (`6.5 m²` → `65` with 1 decimal). Rule: *line total = rate × quantity,
rounded half up to 1 grosz*; subtotal = sum of lines; top-up = max(0, 150 PLN − subtotal);
total = subtotal + top-up. The browser uses the same integer formula for the estimate; the
server's result is what is saved. Each saved line keeps its **rate and parameters as a
snapshot**, so later rate changes never change old estimates.

**Number input is checked before any arithmetic.** A quantity (and the CRM's agreed price)
must be a plain decimal of at most 12 characters — digits with an optional sign and
fraction (the agreed price also accepts a comma and spaces: `1 047,50`). Exponents
(`1e999999`, `1E5` as text), `NaN`, `Infinity`, booleans and other types are refused with
`422` and the field's code (`quantity_number` / `agreed_total_invalid`); values outside the
limits get `quantity_range` / `agreed_total_range`. So oversized numbers can no longer
cause `decimal.Overflow` (formerly HTTP 500). A JSON number such as `1e999999` arrives as
infinity and is refused the same way.

## Order form (contact and preferred time)

Name, email, **phone** (international format: optional `+`, spaces, brackets, dashes;
7–15 digits, max 25 characters), **city**, **street and house number**, **apartment/unit**
(optional), **preferred date** (today or later, Warsaw time), **preferred time window**
(Morning / Afternoon / Evening / Flexible — *“Preferred time — subject to confirmation.”*;
it is a wish, not a booking or an availability check) and an optional comment. Everything is
validated again on the server with length limits. No maps or geocoding. Contact data,
address and comments are never sent to Telegram or written to logs.

## How to run on Windows

You need **Python 3.10 or newer** from <https://www.python.org/downloads/>
(tick **“Add python.exe to PATH”** during installation).

Open **cmd** in the project folder and run (the same commands also work in PowerShell):

**1. Create a virtual environment** (once):

```bat
python -m venv .venv
```

**2. Install dependencies** (once, and again whenever `requirements.txt` changes):

```bat
.venv\Scripts\python -m pip install -r requirements.txt
```

**3. Start the server:**

```bat
.venv\Scripts\python app.py
```

**4. Open:**

- Website: <http://127.0.0.1:5000>
- Admin CRM: <http://127.0.0.1:5000/admin>

Stop the server with **Ctrl+C**.

Port 5000 busy? In cmd: `set CLEANSPACE_PORT=5050` and then `.venv\Scripts\python app.py`
(PowerShell: `$env:CLEANSPACE_PORT = "5050"`).

### Set the admin password

There is **no default password**. Set one with the CLI command (input is hidden and asked twice):

```bat
.venv\Scripts\flask --app app set-admin-password
```

- Minimum 12 characters, no leading/trailing spaces.
- Only a salted hash (Werkzeug `scrypt`) is written to `instance/admin_credentials.json`.
- The server does **not** need to be stopped; just reload the login page.
- Running the command again changes the password and **logs out all existing admin sessions**.
- Forgot the password? Run the command again.

### Run the tests

```bat
.venv\Scripts\python -m unittest discover -s tests -v
```

Tests use a temporary folder and never touch `instance/`.

## Admin CRM

- `/admin/login` — password login. `/admin` — request list. `/admin/requests/<id>` — details.
- **List:** newest first, 20 per page, card layout on phones and compact rows on desktop.
  Shows number, creation time (Warsaw time), name, first service + “+N more”, total (agreed
  total if set), preferred date and time window, status.
- **All-time totals** per status at the top — always for the whole database,
  **not affected** by the filter or search.
- **Filter** by status; **search** by number (`12` or `#12`), name, email or phone
  (case-insensitive, also for letters like “Ł”).
- Clear states for loading, server error (with *Try again*), empty database and “no results”.
- **Details:** all order lines with quantity, rate and amount (snapshot), subtotal, minimum
  top-up and original estimate; contact data and address; preferred date and time; customer
  comment; cleaning **status**; **internal note** (max 2000 characters); separate **Telegram
  delivery** status. Missing data of old requests is shown as *Not provided*.
- **Editable:** status, internal note, **agreed date**, **agreed time** (HH:MM) and an
  **agreed total** (PLN) with a **mandatory reason** whenever it changes. The customer's
  wishes, the order lines and the original estimate stay unchanged and visible. Saving never
  notifies the customer. Deleting requests, payments and multiple admin roles do not exist.
- **History of changes:** every save writes date and time, action, changed fields with old
  and new values and the author (*Admin*) — in the **same transaction** as the change. The
  history is read-only in the UI and append-only in the database (triggers refuse UPDATE and
  DELETE). Only a logged-in admin can see it.
- Statuses: **New, In progress, Completed, Cancelled**.
- “Saved” is shown only after the server confirms the database write. On an error the form
  keeps what you typed and you can retry.
- **Two tabs:** every request has a `version`. If it was changed elsewhere after you opened it,
  saving is refused with a clear message (no silent overwrite); your edits stay in the form
  until you choose “Discard my changes and load the latest version”.

### CRM API (all require an admin session; changes also require a CSRF token)

| Method & path                    | Purpose                                         |
|----------------------------------|-------------------------------------------------|
| `GET /admin/api/requests?status=&q=&page=` | Page of requests + global status counts. |
| `GET /admin/api/requests/<id>`   | One request with lines, contact data, history and Telegram status. |
| `PATCH /admin/api/requests/<id>` | Body `{"version", "status"?, "adminNote"?, "agreedDate"?, "agreedTime"?, "agreedTotal"?, "priceReason"?}` — only the fields sent are changed. `409` on version conflict, `422` with `fieldCodes` on invalid values. |

Without a valid session pages redirect to the login page and the API answers `401`
without any request data.

## Telegram notifications (optional)

When a **new** request is saved, the admin gets a short plain-text Telegram message:

```
CleanSpace — New demo request
Request #12
Services:
• Deep cleaning — 50 m²
• Sofa cleaning — 3 seats
• Carpet cleaning — 6.5 m²
Total: 662.50 PLN
Preferred: 2026-12-01, Morning (subject to confirmation)
```

Orders with more than 8 lines are summarised (“• … and N more service(s)”) so the message
always stays far below Telegram's 4096-character limit. The text is English.
No name, email, phone, address, comment or internal note is sent, and no link (a `127.0.0.1` link would
not open the CRM on a phone). Messages are sent without `parse_mode`, so nothing is
interpreted as markup.

### Commands (Windows **cmd**, opened in the main CleanSpace folder)

Install the dependencies first if you have not done it after the last update:

```bat
.venv\Scripts\python -m pip install -r requirements.txt
```

**1. Connect the bot** (once):

```bat
.venv\Scripts\flask --app app setup-telegram
```

1. Paste the bot token from @BotFather when asked. Input is **hidden** (nothing appears
   while you paste — that is expected; in cmd paste with right-click or Ctrl+V, then Enter).
   Never paste the token into a chat or a screenshot.
2. The command checks the token with Telegram (`getMe`) and shows the bot name.
3. If the bot has a **webhook**, setup stops and explains why: getUpdates does not work while a
   webhook is active. CleanSpace **never removes a webhook** automatically.
4. A one-time code like `CS-7F3A9B21` is shown. Send exactly this code to the bot in a
   **private** chat (the same chat where you pressed Start). Other messages and groups are
   ignored — the chat is never picked automatically.
5. The found chat is shown (name, @username, chat id). Confirm with `y`.
6. You are asked separately whether to send a test message (default: no).

Settings are saved to `instance\telegram.json` (outside `static/` and Git).

**2. Send a test message** (any time):

```bat
.venv\Scripts\flask --app app test-telegram
```

**3. Run the site and the worker in two separate cmd windows:**

Window 1 — website + CRM:

```bat
.venv\Scripts\python app.py
```

Window 2 — notification worker (keep it open; Ctrl+C stops it):

```bat
.venv\Scripts\flask --app app telegram-worker
```

**Turn the integration off** (site and CRM keep working; queued jobs are kept):

```bat
.venv\Scripts\flask --app app disable-telegram
```

### How delivery works

1. Saving a request and adding its notification job happen in **one SQLite transaction**
   (table `notifications`). The website never talks to Telegram, so a request is saved even
   when Telegram or the internet is down.
2. A repeated save with the same idempotency token returns the existing request and adds
   **no second job** (one job per request, `UNIQUE`).
3. `telegram-worker` claims one due job at a time inside a short `BEGIN IMMEDIATE`
   transaction (status `sending`, lock owner, 60 s lease), **commits**, and only then calls
   Telegram — no transaction is open during the network request. Two workers can never
   claim the same job; a late result from a worker whose lease expired is ignored.
4. Results:
   - sent → `sent` (Telegram message id stored);
   - **429** → the next attempt is scheduled **exactly** after Telegram's `retry_after` — never
     earlier and never shortened. The end of that pause is **stored in the job**
     (`blocked_until`), and the queue does not hand out a job before that time — so neither a
     manual retry, a worker restart nor a server restart can skip it. A later, shorter value
     never moves it backwards. A 429 means Telegram did not accept the message, so it does
     not use up an attempt. If the wait would go past the 24-hour limit for automatic sending,
     the job is marked `failed` with an explanation instead; a manual retry may queue it at
     once, but it is sent only after the pause ends;
   - **delivery unknown** — timeout, connection dropped after the request was sent, or an
     unreadable answer → retried after 15 s, 1 min, 5 min, 15 min (max **5 attempts**) and
     flagged *delivery uncertain*;
   - could not connect at all (refused, DNS, certificate) or a readable 5xx error → retried the
     same way, **not** flagged (Telegram certainly did not accept it);
   - **401** (bad/revoked token) → `failed`, and the worker **stops** (no endless retries);
   - **403** (bot blocked) / **400** (chat not found) → `failed` immediately.
5. State, attempt count and a short error text **without secrets** are stored per job.
6. Jobs older than 24 h are not sent automatically (marked failed, can be retried manually).
   Requests saved **before** Telegram was configured never get a job, so old requests are
   never announced.
7. **Settings are re-read before every job** (and once more right before sending). If
   `instance\telegram.json` was removed (`disable-telegram`) or now has another token or chat
   (`setup-telegram` again), the worker **stops** with an explanation and sends nothing more
   with the old settings; a job claimed in that moment is put back without using an attempt.
   A request that is **already on its way** to Telegram cannot be recalled. After changing the
   settings, start the worker again; jobs still waiting in the queue then go to the new chat.

**No exactly-once guarantee.** If Telegram accepted a message but the answer was lost
(timeout, broken connection, unreadable answer, worker killed while sending), the job is
retried and the message can arrive **twice**. Such jobs are flagged as “delivery uncertain”;
the flag stays **even after a later successful attempt**, and the CRM keeps warning about a
possible duplicate.

### In the CRM

Request details show a separate **Telegram notification** panel (delivery status — not the
cleaning status): **Not configured / Pending / Sending… / Sent / Failed**, with attempts,
next attempt time and the last error. For **Failed** there is **Retry notification**
(`POST /admin/api/requests/<id>/notification/retry`, admin session + CSRF required). It puts
the **same queue job** back to pending; the worker must be running to deliver it. While
Telegram's pause is running, the panel shows **Telegram pause until …** (Warsaw time) and
explains it in English and Ukrainian; a retry is accepted, but the job becomes due only when
the pause ends (`nextAttemptAt` = end of the pause; the answer has `waitsForPause: true`).
A second click is refused by the server (`409 not_failed`) — this is enforced on the server,
not only by hiding the button.

### Tests

All Telegram tests use **fake answers** and a fake token — they never contact Telegram:
success, bad token, bot blocked, chat not found, timeout, 429 + `retry_after` (not shortened,
lifetime limit; with a controlled clock: no send during the pause even after a manual retry,
a repeated click or a restart, and sending works after it), uncertain vs. certain failures, the uncertain flag kept after success,
settings removed/changed while the worker runs, network errors
with the token in the error text, repeated request creation, missing settings, queue after
restart, lease/crash recovery, two workers racing, manual retry protection, setup command
(code matching, webhook conflict, no sending without consent) and secrets in logs. The real
`urllib` transport is tested against a local fake HTTP server on `127.0.0.1`.

## Security (learning project)

- **Password:** Werkzeug `generate_password_hash` / `check_password_hash` (salted scrypt).
  No custom cryptography. No default password. Password never logged or stored in plain text.
- **Session:** Flask signed cookie, random 256-bit secret in `instance/secret_key`
  (persistent, outside Git). Cookie `cleanspace_admin` is `HttpOnly`, `SameSite=Strict`,
  expires after 8 hours. Login clears the old session.
- **Secure flag:** **off by default**, because the local server uses plain `http://127.0.0.1`
  and a `Secure` cookie would never be sent — login would be impossible. This means the cookie
  is not protected by TLS; acceptable only because traffic never leaves your computer
  (loopback). If you ever serve the app over HTTPS, set `CLEANSPACE_SECURE_COOKIES=1`.
- **CSRF:** Flask-WTF `CSRFProtect` on login, logout and every CRM change
  (form field or `X-CSRFToken` header). The public `POST /api/requests` is exempt: it uses no
  cookies and accepts JSON only.
- **Logout** is `POST` only.
- **Login throttling:** max 5 failed attempts per client within 15 minutes, then `429`.
  Old failures expire automatically — there is no permanent lockout. Stored in SQLite, so
  restarting the server does not reset it.
- **Authorization** is checked on the server for every CRM page and API call.
- Parameterized SQL only; writes in explicit transactions; schema changes versioned with
  `PRAGMA user_version` and safe to run repeatedly, with an automatic backup before migrating.
- User text is shown via `textContent` (CRM) or Jinja auto-escaping (login page) — HTML in
  names, comments or notes is displayed as text.
- **Logs** contain request ids, statuses and field *names* only — never passwords, emails,
  names, comments, notes or search text (the access log omits query strings).
- Only `127.0.0.1` / `localhost` Host headers are accepted; Flask debug mode is off;
  security headers (CSP, `nosniff`, `X-Frame-Options`); CRM responses are `no-store`.

## Database

`instance/cleanspace.db`, table `requests`:

| Column                | Meaning                                                      |
|-----------------------|--------------------------------------------------------------|
| `id`                  | Request number.                                              |
| `request_token`, `payload_hash`, `hash_version` | Idempotency (1 = stage-2 single-service format, 2 = orders). |
| `created_at`          | Creation time, UTC.                                          |
| `subtotal_grosze`, `minimum_topup_grosze`, `total_grosze`, `currency` | Original estimate by the server (never changed). |
| `cleaning_type`, `area_sqm`, `rate_per_sqm_grosze` | Legacy columns, only filled for requests created before v4. |
| `name`, `email`, `phone`, `city`, `street`, `apartment`, `preferred_date`, `preferred_time`, `comment` | Customer data (made-up!). Read-only. `''` = not provided (old requests). |
| `status`              | `new` / `in_progress` / `completed` / `cancelled`.           |
| `admin_note`          | Internal note, max 2000 characters.                          |
| `agreed_date`, `agreed_time` | Agreed with the customer (NULL until agreed).         |
| `agreed_total_grosze`, `agreed_total_reason` | Admin price adjustment and its reason.  |
| `version`             | Increases on every admin change (conflict detection).        |
| `updated_at`          | Time of the last admin change, UTC.                          |

Table `request_items`: one row per order line — `service`, `category`, `unit`,
`quantity_scaled` + `quantity_decimals`, `options_json`, `unit_rate_grosze`,
`line_total_grosze`, `label_snapshot` (English name at order time).

Table `request_history` (append-only): `request_id`, `created_at`, `actor` (`admin`),
`action`, `changes_json` (`[{field, from, to}]`, plus `reason` for price changes).

Table `login_failures` stores timestamps of failed logins (no passwords).

Table `notifications` (schema v3, extended in v5) is the Telegram queue: `request_id`
(unique), `status` (`pending` / `sending` / `sent` / `failed`), `attempts`, `manual_retries`,
`next_attempt_at`, `locked_by` / `locked_until` (worker lease), `last_error` (no secrets),
`delivery_uncertain`, `created_at`, `sent_at`, `telegram_message_id`, `blocked_until`
(end of Telegram's 429 pause as a Unix time; `0` = none).

**Migrations:** on start (server or any `flask --app app …` command) the database version
(`PRAGMA user_version`) is checked. Before any migration a copy is saved to
`instance/backups/`. Each step runs in one transaction and is verified; running it again
does nothing.

- **v2** — rebuilt `requests` for the CRM statuses (SQLite cannot change a `CHECK` in place).
- **v3** — added the `notifications` queue.
- **v4** — orders: `requests` is rebuilt (with foreign keys temporarily off, then
  `PRAGMA foreign_key_check`), new contact/time/agreement columns, new tables
  `request_items` and `request_history`. **Every old request becomes an order with one line**
  (its original service, area and rate). Unchanged: id, total, rate, status, note, version,
  token, payload hash, customer data and the link from the Telegram queue. The original
  minimum top-up is derived as `total − area × rate`; the migration aborts unless
  `subtotal + top-up = total` for every row and the row/total sums match. Phone, address and
  time stay empty (*Not provided*); nothing is invented. Next ids continue after the highest
  id ever used.
- **v5** — `notifications.blocked_until` (a new column; no existing value changes). For jobs
  whose last error is a 429, the pause is restored from that error text and the time it was
  recorded, so an already queued job cannot skip it.
- **Old tokens:** a stage-2 page that retries its original request (old format, same token)
  gets the same request back (`200`), a different payload gets `409`, and a new request in
  the old format is refused (`outdated_form`) — so no duplicates are created after the upgrade.

To start from scratch: stop the server and delete `instance/cleanspace.db`.

## Public site and API

- `GET /api/pricing` — service catalog from `config.py`: categories, services, units,
  min/max/decimals, rates (or option rates), minimum order, max lines, time windows, limits.
- `POST /api/requests` — create an order. Body:

  ```json
  {
    "requestToken": "6f1c1c1e-6a53-4e43-9a5b-2b3f7f3e0a11",
    "items": [
      {"service": "deep", "quantity": "50"},
      {"service": "sofa", "quantity": "3"},
      {"service": "mattress_single", "quantity": "2", "options": {"sides": "both"}}
    ],
    "name": "Test Person", "email": "test@example.com", "phone": "+48 600 000 000",
    "city": "Kraków", "street": "Testowa 1", "apartment": "",
    "date": "2026-12-01", "time": "morning", "comment": ""
  }
  ```

  Any prices sent by the browser are ignored. `201` new · `200` same token and same data
  (`"duplicate": true`) · `409` same token, other data · `422` validation (`fields` with
  English text + `fieldCodes` with stable codes).

### Save flow and the request token

- Each save attempt has one random `requestToken`. Sending the same token with the same data
  again returns the **same** request (`200`, `"duplicate": true`) instead of creating a new one.
- **Preview request** again **without changing the data** keeps the same attempt and token.
  A new token is created only when the data changes or after **Start a new request**.
- If the answer is lost (timeout, connection dropped) or the server answers with a 5xx or an
  unreadable response, the page does **not** say “Not saved”. It shows
  *“Could not confirm whether the request was saved”* and offers **Retry with the same
  request**: if the first attempt was in fact stored, the retry returns that request number
  and no duplicate is created.
- After a confirmed save the success message stays (also after Preview again), the Save button
  is hidden so the same attempt cannot be saved twice by accident, and **Start a new request**
  is offered for an intentional second request.
- “Not saved” is shown only when the server clearly refused the request (validation error,
  token conflict, other 4xx).

Regression check for the browser part: `tests/manual/save_flow_check.js` — paste it into the
DevTools console on a **local test** server. It lets the first save really reach the server
but throws away the answer, then verifies the message, the retry with the same token, the
kept success state and new tokens after changes. The server side of the same scenario is an
automated test (`test_lost_response_then_retry_returns_the_same_request`).

## Limitations

- **Not for public use.** No HTTPS, single admin without username/roles, development server
  (Werkzeug), no rate limiting on the public API, no backups schedule. The CRM history
  records admin changes to requests only (not logins or other actions).
- Throttling is per client IP; on a local machine every client is `127.0.0.1`.
- Sessions are signed cookies: logout clears the cookie in this browser, but a copied cookie
  stays valid until it expires (8 h) or the password is changed.
- Email validation checks the format only.
- The CRM needs JavaScript; there is no export and requests cannot be deleted.
- No online payment, no booking calendar and no check of staff availability: the preferred
  date and time window are only the customer's wish.
- CRM search scans the whole table (fine for demo data, not for large volumes).
- Two languages only (English, Ukrainian); Telegram messages are English only.
- Telegram: at-least-once delivery at best (duplicates possible, see above). The worker is a
  separate process you start yourself; if it is not running, notifications wait in the queue.
  The bot token is stored in plain text in `instance\telegram.json` (Windows user profile
  permissions only). One recipient chat. Public deployment is not part of this project.
