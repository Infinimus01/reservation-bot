# reservation-bot

> A distributed, anti-detection reservation automation framework — Google Sheets in, confirmed bookings out.

```
Google Sheets ──► Master API ──► RabbitMQ ──► Worker(s) ──► Confirmed Booking
                     │                           │
              Availability Checker          Playwright + Chrome
              (Playwright loop)             + Residential Proxy
                                           + DataDome bypass
                                           + CF WAF bypass
                                           + Turnstile solver
```

---

## What it does

Monitors a ticketing/reservation website for available slots, reads booking requests from a Google Sheet, and automatically completes end-to-end reservations through a real Chrome browser — including handling DataDome bot protection, Cloudflare WAF, waiting rooms, CAPTCHA challenges, and the final payment/confirmation step.

The system is built around a **master → queue → worker** architecture so it scales horizontally and handles transient failures gracefully.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         MASTER VM                                   │
│                                                                     │
│   ┌─────────────────┐     ┌──────────────────────────────────────┐  │
│   │ Availability     │     │ Master API  (FastAPI / uvicorn)      │  │
│   │ Checker          │────►│  POST /availability/trigger          │  │
│   │ (Playwright loop)│     │  GET  /tasks                         │  │
│   └─────────────────┘     │  GET  /health                        │  │
│                            └────────────────┬─────────────────────┘  │
│   ┌─────────────────┐                       │                        │
│   │ Google Sheets    │──── task sync ───────►│                        │
│   │ (service account)│     every 15s         │                        │
│   └─────────────────┘                       ▼                        │
│                            ┌────────────────────────────────────┐    │
│                            │  Queue Dispatcher                  │    │
│                            │  Matches available slots → tasks   │    │
│                            │  Publishes to RabbitMQ             │    │
│                            └────────────────┬───────────────────┘    │
│                                             │                        │
│                            ┌────────────────▼───────────────────┐    │
│                            │  SQLite (master_state.db)          │    │
│                            │  Task state machine                │    │
│                            │  pending → queued → completed/fail │    │
│                            └────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                              RabbitMQ broker
                         (booking_jobs exchange)
                                    │
┌─────────────────────────────────────────────────────────────────────┐
│                         WORKER VM                                   │
│                                                                     │
│   ┌────────────────────────────────────────────────────────────┐    │
│   │  worker_main.py  (RabbitMQ consumer, ThreadPoolExecutor)   │    │
│   │                                                            │    │
│   │   Job received → allocate fresh IPRoyal proxy → launch     │    │
│   │   BookingEngine (Playwright) → publish result              │    │
│   └──────────────────────┬─────────────────────────────────────┘    │
│                          │                                           │
│   ┌──────────────────────▼─────────────────────────────────────┐    │
│   │  BookingEngine  (booking_playwright_worker.py)              │    │
│   │                                                            │    │
│   │  Step 1 │ Tickets page    — pick count, handle DataDome    │    │
│   │  Step 2 │ Calendar page   — click date, inject time slot   │    │
│   │  Step 3 │ Personal details — fill name/email/phone/zip     │    │
│   │  Step 4 │ Donation page   — set €0, proceed               │    │
│   │  Step 5 │ Summary page    — confirm booking details        │    │
│   │  Step 6 │ Payment page    — solve Turnstile, click Complete│    │
│   │         └─► Thank-you page detected → result = confirmed   │    │
│   └────────────────────────────────────────────────────────────┘    │
│                                                                     │
│   ┌──────────────────────────────────────────┐                      │
│   │  Persistent browser profiles (per task)  │                      │
│   │  50-slot pool — warms CF/DataDome cookies│                      │
│   └──────────────────────────────────────────┘                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Key Technical Features

### Browser Automation — Playwright + Headless Chrome

- Uses **Playwright's persistent browser context** with a real Chromium binary, not a scripted HTTP client — the browser executes JavaScript, runs event handlers, fires XHR/fetch calls, and behaves identically to a human user
- `--disable-blink-features=AutomationControlled` masks `navigator.webdriver`
- 50-profile pool (one profile per task, stable across retries) — CF and DataDome cookies accumulate over sessions, reducing friction on repeat visits
- Each booking step uses a **real page interaction** (clicking, form injection) rather than raw HTTP, so the request signature (headers, cookies, TLS fingerprint) matches what a legitimate browser sends

### Anti-Bot Stack: DataDome

The site uses **DataDome** for real-time bot fingerprinting. The system handles two DataDome response modes:

| Mode | Detection | Resolution |
|------|-----------|------------|
| **Cookie-refresh** | JSON response `{"status":200,"cookie":"datadome=..."}` | Parse cookie value, inject via JS, reload page |
| **Slider challenge** | Redirect to `captcha-delivery.com` with embedded challenge URL | Submit `DataDomeSliderTask` to **2captcha**, inject solved cookie, reload |

After solving, the DataDome cookie is injected directly into the browser's cookie jar via `document.cookie`, then the page reloads to resume the original flow.

### Anti-Bot Stack: Cloudflare WAF + Rate Limiting

The reservation endpoint is protected by Cloudflare's WAF with:
- Per-IP rate limiting on POST requests (CF error 1015)
- Bot-score checks on session cookies (`__cf_bm`)
- Managed challenges served inline (CF challenge page injected as response body rather than a redirect)

**How the system handles it:**

1. **Fresh IP per attempt** — IPRoyal's rotating residential proxy gateway generates a unique sticky session (random 8-char token) for every booking attempt, so CF's per-IP rate limit never accumulates across retries
2. **Correct form flow** — the calendar date is clicked via JS (triggering the site's own event handlers), which updates the form action to the next step's endpoint; only then is the form submitted — this mirrors human navigation exactly
3. **Cookie warmup** — persistent browser profiles carry `__cf_bm` and `datadome` cookies from prior sessions, making each attempt look like a returning browser

### Anti-Bot Stack: Cloudflare Turnstile (final payment)

The final payment page embeds a **Cloudflare Turnstile** widget. The system:
1. Extracts the `data-sitekey` from the DOM (falls back to a hardcoded default)
2. Submits an `AntiTurnstileTaskProxyLess` task to **CapSolver**
3. Polls until the token is ready (up to 3 minutes)
4. Injects the token into every `input[name="cf-turnstile-response"]` on the page
5. Checks the T&C checkbox, then submits the form

### Proxy Management

- **Primary mode** — `IPROYAL_PROXY` env var enables automatic fresh-session allocation: each call to `acquire_warmed_iproyal_proxy()` generates a new random session token (e.g. `_session-X7kAiPQm_lifetime-1h`), fetching a fresh residential IP from IPRoyal's US pool
- **Warmup validation** — before handing a proxy to the booking engine, the allocator verifies connectivity with a timeout; retries up to `IPROYAL_PROXY_WARMUP_ATTEMPTS` times
- **Fallback mode** — if `IPROYAL_PROXY` is unset, the worker reads static proxy lines from `proxies.txt` (one `HOST:PORT:USER:PASS` per line) and validates them lazily

### RabbitMQ Message Queue

Two exchanges with dead-letter / retry routing:

```
booking_jobs (exchange)
    └── booking_jobs (queue)  ──── worker consumes ────► result published
              │
              ├── on transient failure: nack → booking_jobs.retry queue
              │                         (delay configured by RABBITMQ_BOOKING_RETRY_DELAY_MS)
              └── on retry exhaustion: result published as "failed"
```

- Worker prefetch = 1 (`RABBITMQ_WORKER_PREFETCH_COUNT=1`) — one booking at a time to avoid triggering site-side rate limits
- `booking_max_retries` controls total retry budget (default: 3 retries = 4 total attempts)
- Each retry is handled by a fresh worker loop iteration → fresh proxy IP → fresh email → full browser restart

### Google Sheets Integration

Booking requests live in a Google Sheet with columns:

| date | time | ticket_count | first_name | last_name | phone | zip | country | … |

The master syncs the sheet every 15 seconds using a service-account credential (`service.json`). New rows are ingested as `pending` tasks in SQLite. The reporting worker writes back status (`queued`, `completed`, `failed`) and confirmation details to the same sheet in real time.

Supported auth modes:
- **Service account** (recommended) — private sheet, full read/write
- **API key** — public sheet, read-only
- **CSV URL** — published-to-web sheet, no credentials

### Email Generation

Each booking attempt always generates a **fresh email address** — the site rejects reused emails. Supported providers:

| Provider | Mechanism |
|----------|-----------|
| `burner` | BurnerMail REST API — real deliverable alias |
| `faker` | Fully offline — random `name1234k@gmail.com` string |
| `addy` | addy.io anonymous forwarding alias |
| `simplelogin` | SimpleLogin alias API |

Falls back automatically to `faker` if the configured provider fails.

### Waiting Room Handler

Many high-demand ticketing sites gate access through a virtual waiting room. The engine detects waiting-room keywords in the page HTML and polls every `WAITING_ROOM_POLL_INTERVAL_SECONDS` seconds (default: 15s) until the real page appears or `WAITING_ROOM_MAX_WAIT_SECONDS` (default: 600s) expires.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Browser automation | Playwright 1.57 + Chromium headless |
| Task queue | RabbitMQ (pika 1.3) |
| API server | FastAPI + uvicorn |
| Task state | SQLite (via Python stdlib) |
| Sheet sync | Google Sheets API v4 (google-auth) |
| Proxy | IPRoyal rotating residential |
| DataDome solver | 2captcha `DataDomeSliderTask` |
| Turnstile solver | CapSolver `AntiTurnstileTaskProxyLess` |
| Email | BurnerMail / addy.io / SimpleLogin / Faker |
| Language | Python 3.12 |
| Deployment | DigitalOcean VMs (master + worker) |
| Process management | systemd |

---

## Project Structure

```
reservation-bot/
├── booking_playwright_worker.py   # Core booking engine (6-step flow)
├── alias_manager.py               # Email alias generation (all providers)
├── flare_bot.py                   # Standalone local runner / proxy validation
├── util.py                        # Shared data models (UserDetails)
│
├── master/
│   ├── main.py                    # FastAPI app entry point
│   ├── availability_checker.py    # Playwright loop — detects open slots
│   ├── availability.py            # Availability data models
│   ├── google_sheets.py           # Sheet sync + result write-back
│   ├── queue_dispatcher.py        # Matches slots → tasks → publishes to MQ
│   ├── task_store.py              # SQLite task state machine
│   └── reporting_worker.py        # Writes confirmed bookings back to sheet
│
├── worker/
│   ├── worker_main.py             # RabbitMQ consumer + proxy allocator
│   ├── email_resolver.py          # Per-task email resolution
│   └── flaresolverr_pool.py       # (Legacy) FlareSolverr pool manager
│
├── shared/
│   ├── config.py                  # Pydantic settings for master + worker
│   ├── iproyal_proxy.py           # IPRoyal sticky-session proxy builder
│   ├── models.py                  # Pydantic message models (BookingJobMessage)
│   └── rabbitmq.py                # Exchange/queue topology + publisher
│
├── tools/
│   └── generate_iproyal_proxies.py # CLI to pre-generate proxy session list
│
├── tests/                         # pytest suite (unit + integration)
├── Dockerfile.worker              # Worker container image
├── Dockerfile.availability        # Availability checker container image
├── docker-compose.worker-pool.yml # Multi-worker Docker Compose setup
├── .env.example                   # All env vars documented with defaults
└── requirements.txt
```

---

## Booking Flow (Step by Step)

```
 ┌─────────────────────────────────────────────────────────────┐
 │  STEP 1 — Tickets Page                                      │
 │  Navigate to reservation URL → handle DataDome if triggered  │
 │  → handle waiting room if active → extract product ID       │
 │  → submit ticket count via form POST → land on /date        │
 └──────────────────────────────┬──────────────────────────────┘
                                │
 ┌──────────────────────────────▼──────────────────────────────┐
 │  STEP 2 — Calendar / Date Selection                         │
 │  jQuery UI datepicker → click target date cell              │
 │  (triggers site XHR → timeslot selector appears,            │
 │   form action updates to /personal-details)                 │
 │  Inject ticketDate + ticketTime → submit form               │
 │  If date click didn't update form action: _post_form()      │
 │  directly to /personal-details (lane-file fallback)         │
 └──────────────────────────────┬──────────────────────────────┘
                                │
 ┌──────────────────────────────▼──────────────────────────────┐
 │  STEP 3 — Personal Details                                  │
 │  Wait for CF challenge to resolve (up to 23s total)         │
 │  Fill: firstName, surname, emailAddress (×2), phoneNumber,  │
 │        phone-number (+44 prefix), zipcode, country          │
 │  Submit via form.submit()                                   │
 └──────────────────────────────┬──────────────────────────────┘
                                │
 ┌──────────────────────────────▼──────────────────────────────┐
 │  STEP 4 — Donation Page                                     │
 │  Set donation to €0, uncheck donation checkbox              │
 │  Submit → land on /summary                                  │
 └──────────────────────────────┬──────────────────────────────┘
                                │
 ┌──────────────────────────────▼──────────────────────────────┐
 │  STEP 5 — Summary Page                                      │
 │  Verify CSRF present → submit summary form                  │
 │  → land on final /payment page                              │
 └──────────────────────────────┬──────────────────────────────┘
                                │
 ┌──────────────────────────────▼──────────────────────────────┐
 │  STEP 6 — Final Payment + Turnstile                         │
 │  Extract Turnstile sitekey from DOM                         │
 │  → CapSolver AntiTurnstileTaskProxyLess → token             │
 │  → inject token into cf-turnstile-response inputs           │
 │  → check terms-and-conditions checkbox                      │
 │  → click Complete / submit form                             │
 │  → detect thank-you URL or confirmation text                │
 │  → BOOKING CONFIRMED ✅                                     │
 └─────────────────────────────────────────────────────────────┘
```

---

## Setup

### Prerequisites

- Python 3.12+
- RabbitMQ (local Docker or managed)
- Two DigitalOcean (or any Linux) VMs: one master, one worker
- IPRoyal residential proxy account
- CapSolver account (Turnstile)
- 2captcha account (DataDome)
- Google Cloud service account with Sheets API enabled

### Install

```bash
git clone https://github.com/Infinimus01/reservation-bot.git
cd reservation-bot

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

playwright install chromium
```

### Configure

```bash
cp .env.example .env
# Edit .env — fill in all the values for your environment
```

Key values you must set:

| Variable | Description |
|----------|-------------|
| `MASTER_API_KEY` | Secret for the master REST API |
| `RABBITMQ_URL` | `amqp://user:pass@host:5672/%2F` |
| `GOOGLE_SHEETS_SPREADSHEET_ID` | Your Google Sheet ID |
| `GOOGLE_SHEETS_CREDENTIALS_FILE` | Path to `service.json` |
| `CAPSOLVER_API_KEY` | CapSolver API key (Turnstile) |
| `TWO_CAPTCHA_API_KEY` | 2captcha API key (DataDome) |
| `IPROYAL_PROXY` | `host:port:user:pass` (no session suffix) |
| `WORKER_EMAIL_PROVIDER` | `burner`, `faker`, `addy`, or `simplelogin` |

Put your proxy lines in `proxies.txt` (gitignored):

```
geo.iproyal.com:12321:username:password
```

### Run — Master

```bash
cd reservation-bot
uvicorn master.main:app --host 0.0.0.0 --port 8000
```

### Run — Worker

```bash
cd reservation-bot
python -m worker.worker_main
```

### Trigger a dispatch (manual)

```bash
curl -X POST http://<master-ip>:8000/availability/trigger \
  -H "X-API-Key: <MASTER_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "availabilities": [
      {"date": "YYYY/MM/DD", "time": "HH:MM", "quantity": 20}
    ]
  }'
```

---

## Docker

```bash
# Worker pool (4 parallel workers)
docker compose -f docker-compose.worker-pool.yml up -d

# Availability checker
docker compose -f docker-compose.availability.yml up -d
```

---

## Google Sheet Format

The sheet must have these column headers (case-sensitive):

| Column | Description |
|--------|-------------|
| `date` | `YYYY-MM-DD` |
| `time` | `HH:MM` |
| `ticket_count` | integer |
| `first_name` | visitor first name |
| `last_name` | visitor last name |
| `phone` | digits only, no country code |
| `zip` | postcode |
| `country` | full country name |
| `status` | written back by system |
| `confirmation` | written back on success |

The system generates a unique email per booking attempt automatically — no email column needed.

---

## Master API Reference

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | none | Liveness check |
| `GET` | `/tasks` | API key | List all tasks and their states |
| `POST` | `/tasks/sync` | API key | Force sheet re-sync |
| `POST` | `/availability/trigger` | API key | Dispatch tasks matching given slots |

---

## Retry & Error Handling

| Error type | Behaviour |
|------------|-----------|
| DataDome block | Detected → 2captcha solve attempted → reload; if unsolved → `DataDomeBlockError` → retry with new proxy |
| CF rate limit (1015) | Fresh IPRoyal session per attempt gives a new IP — no accumulation |
| CF Turnstile | CapSolver called; up to 3 min to solve; failure marks attempt failed |
| Waiting room | Polls every 15s up to 10 min; timeout raises `WaitingRoomTimeoutError` |
| Slot full | `SlotFullError` — non-retryable, marks task failed immediately |
| Order limit | `OrderLimitError` — non-retryable, marks task failed immediately |
| CSRF missing | Stale session detected — retried with fresh browser profile |
| Transient / timeout | Nacked to retry queue; up to `RABBITMQ_BOOKING_MAX_RETRIES` retries |

---

## Environment Variables Reference

See [`.env.example`](.env.example) for the full annotated list.

---

## License

MIT
