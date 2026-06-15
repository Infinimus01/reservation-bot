# Setup And Run Guide

This guide is for the current RabbitMQ-based architecture in this repository.

Use this guide for:

- local development
- local end-to-end testing
- cloud deployment
- running the warm worker container pool

Do not use the older worker registration / heartbeat deployment notes for new installs. The active model now is:

1. `master/availability_checker.py` finds live availability and calls the master API.
2. `master/main.py` syncs tasks and publishes matching jobs to RabbitMQ `booking_jobs`.
3. worker containers pull jobs directly from RabbitMQ.
4. workers publish terminal results to RabbitMQ `job_results`.
5. `master/reporting_worker.py` batches those results back into SQLite and Google Sheets.

## 1. Runtime Topology

### Local

```text
Windows or Linux host
  - Python virtualenv
  - master/main.py
  - master/reporting_worker.py
  - optional master/availability_checker.py
  - RabbitMQ in Docker
  - FlareSolverr containers
  - optional local worker process or the 4-container warm worker pool
```

### Cloud

```text
1 master VM
  - Python app
  - FastAPI master
  - reporting worker
  - SQLite

1 RabbitMQ broker
  - managed service or Docker/container VM

N worker VMs
  - Docker
  - warm pool of 4 worker containers
  - 4 local FlareSolverr containers
  - proxies.txt
```

The worker VM is intentionally dumb. It should not run a custom router script. It only keeps the containers running.

## 2. RabbitMQ Ports

If you start RabbitMQ with Docker's random published ports, the host-side AMQP and
management ports can change across restarts.

For host-side Python processes like `master.main`, `worker.worker_main`, and
`master.reporting_worker`, set:

- `RABBITMQ_URL=auto`

The app will resolve the current published host port for container port `5672` at startup.

If more than one running container publishes RabbitMQ, also set:

- `RABBITMQ_DOCKER_CONTAINER_NAME=<container-name>`

To inspect the current host port mapping manually, run:

```powershell
docker ps --format "table {{.Names}}\t{{.Ports}}"
```

## 3. Prerequisites

### Mandatory

- Python 3.13+
- Docker
- a working `proxies.txt`
- at least one reachable FlareSolverr instance
- `CAPSOLVER_API_KEY`
- one email provider:
  - `burner`
  - `simplelogin`
  - `addy`
  - `faker` for non-production testing

### Python dependencies

From the repo root:

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Linux:

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Create runtime folders if they do not exist:

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force logs, screenshots, availability_runs
```

Linux:

```bash
mkdir -p logs screenshots availability_runs
```

## 4. Local `.env` Setup

Start from `.env.example` and create `.env`.

Minimum local example for host-side Python processes against a Docker RabbitMQ broker:

```env
MASTER_HOST=127.0.0.1
MASTER_PORT=8000
MASTER_API_KEY=replace-with-strong-random-value
MASTER_STATE_DB=./master_state.db
MASTER_REQUIRE_AVAILABILITY_TRIGGER=true
REPORTING_FLUSH_INTERVAL_SECONDS=15

RABBITMQ_URL=auto
# Optional when more than one running container publishes RabbitMQ:
# RABBITMQ_DOCKER_CONTAINER_NAME=rabbitmqbroker
RABBITMQ_BOOKING_QUEUE=booking_jobs
RABBITMQ_BOOKING_ROUTING_KEY=booking_jobs
RABBITMQ_BOOKING_EXCHANGE=selenium_bot.booking
RABBITMQ_BOOKING_RETRY_QUEUE=booking_jobs.retry
RABBITMQ_BOOKING_RETRY_ROUTING_KEY=booking_jobs.retry
RABBITMQ_BOOKING_RETRY_EXCHANGE=selenium_bot.booking.retry
RABBITMQ_BOOKING_RETRY_DELAY_MS=0
RABBITMQ_BOOKING_MAX_RETRIES=3
RABBITMQ_RESULTS_QUEUE=job_results
RABBITMQ_RESULTS_ROUTING_KEY=job_results
RABBITMQ_RESULTS_EXCHANGE=selenium_bot.results
RABBITMQ_WORKER_PREFETCH_COUNT=3

GOOGLE_SHEETS_ENABLED=true
GOOGLE_SHEETS_SPREADSHEET_ID=
GOOGLE_SHEETS_CSV_URL=
GOOGLE_SHEETS_CREDENTIALS_FILE=./service.json

WORKER_EMAIL_PROVIDER=burner
WORKER_FLARESOLVERR_STARTUP_TIMEOUT_SECONDS=90
WORKER_FLARESOLVERR_STARTUP_POLL_SECONDS=3
FLARESOLVERR_DISCOVERY_MODE=env
FLARESOLVERR_URLS=http://127.0.0.1:8191/v1,http://127.0.0.1:8192/v1,http://127.0.0.1:8193/v1,http://127.0.0.1:8194/v1

CAPSOLVER_API_KEY=replace-me
```

If you run a standalone worker container locally against the host RabbitMQ broker,
auto-discovery does not apply inside the container. Use the current published host port:

```env
RABBITMQ_URL=amqp://guest:guest@host.docker.internal:<published-port>/%2F
```

## 4.1 Environment Variable Reference

### Master Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `MASTER_HOST` | `0.0.0.0` | IP address the FastAPI master binds to. Use `0.0.0.0` in cloud, `127.0.0.1` for local-only. |
| `MASTER_PORT` | `8000` | HTTP port for the master API. |
| `MASTER_API_KEY` | (required) | Secret key required by all API clients via `X-API-Key` header. Use a strong random value. |
| `MASTER_STATE_DB` | `./master_state.db` | Path to SQLite database for task state and persistence. |
| `MASTER_REQUIRE_AVAILABILITY_TRIGGER` | `true` | When `true`, tasks will only run after availability is confirmed via `/availability/trigger`. When `false`, tasks queue immediately. |
| `MASTER_URL` | (optional) | Explicit base URL for external scripts to call the master. If omitted, derived from `MASTER_HOST` and `MASTER_PORT`. |

### Reporting Worker Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `REPORTING_FLUSH_INTERVAL_SECONDS` | `15` | How often the reporting worker polls RabbitMQ `job_results` and writes to SQLite/Google Sheets. |

### RabbitMQ Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `RABBITMQ_URL` | (required) | AMQP connection string. Format: `amqp://user:pass@host:port/%2F`. The `%2F` is URL-encoded `/` for the default vhost. |
| `RABBITMQ_BOOKING_QUEUE` | `booking_jobs` | Queue name for tasks waiting to be processed by workers. |
| `RABBITMQ_BOOKING_ROUTING_KEY` | `booking_jobs` | Routing key for publishing booking jobs. |
| `RABBITMQ_BOOKING_EXCHANGE` | `selenium_bot.booking` | Exchange name for booking job messages. |
| `RABBITMQ_BOOKING_RETRY_QUEUE` | `booking_jobs.retry` | Queue for failed jobs awaiting retry. |
| `RABBITMQ_BOOKING_RETRY_ROUTING_KEY` | `booking_jobs.retry` | Routing key for retry messages. |
| `RABBITMQ_BOOKING_RETRY_EXCHANGE` | `selenium_bot.booking.retry` | Exchange for retry routing. |
| `RABBITMQ_BOOKING_RETRY_DELAY_MS` | `0` | Delay before retrying a failed job. Set to `0` for immediate retry. |
| `RABBITMQ_BOOKING_MAX_RETRIES` | `3` | Maximum retry attempts after the first failure. `3` means 1 try + 3 retries = 4 total. |
| `RABBITMQ_RESULTS_QUEUE` | `job_results` | Queue for workers to publish final task results. |
| `RABBITMQ_RESULTS_ROUTING_KEY` | `job_results` | Routing key for result messages. |
| `RABBITMQ_RESULTS_EXCHANGE` | `selenium_bot.results` | Exchange name for result messages. |
| `RABBITMQ_WORKER_PREFETCH_COUNT` | `3` | Number of jobs a worker can hold unacknowledged. Controls per-worker concurrency. |

### Google Sheets Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `GOOGLE_SHEETS_ENABLED` | `true` | Master syncs tasks from Google Sheets when `true`. |
| `GOOGLE_SHEETS_CSV_URL` | (optional) | Public CSV export URL for reading tasks (simplest option). |
| `GOOGLE_SHEETS_SPREADSHEET_ID` | (optional) | Sheet ID for API-based access (requires credentials). |
| `GOOGLE_SHEETS_API_KEY` | (optional) | Public API key for read-only Sheets access. |
| `GOOGLE_SHEETS_CREDENTIALS_FILE` | `./service.json` | Path to service account JSON key file for private Sheets access. |
| `GOOGLE_SHEETS_CREDENTIALS_JSON` | (optional) | Raw JSON content of service account credentials (alternative to file path). |
| `GOOGLE_SHEETS_SYNC_INTERVAL_SECONDS` | `15` | How often the master polls Google Sheets for new/updated tasks. |
| `GOOGLE_SHEETS_TIMEOUT_SECONDS` | `15` | HTTP timeout for Google Sheets API calls. |

### Worker Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `WORKER_ID` | `worker-01` | Unique identifier for this worker instance. Must be unique across the fleet. |
| `WORKER_NAME` | `worker-01` | Human-readable worker name, used in logs and reporting. |
| `WORKER_MAX_TASKS` | `10` | Maximum number of tasks this worker will process before shutting down. Use `0` for unlimited. |
| `WORKER_IDLE_SHUTDOWN_SECONDS` | `0` | Seconds of inactivity before auto-shutdown. `0` means never auto-shutdown. |
| `WORKER_EMAIL_PROVIDER` | `burner` | Which email service workers use: `burner`, `simplelogin`, `addy`, or `faker` (for testing). |
| `WORKER_STATUS_POLL_INTERVAL_SECONDS` | `1` | How often workers check for task cancellation signals. |
| `WORKER_FLARESOLVERR_STARTUP_TIMEOUT_SECONDS` | `90` | Max time to wait for FlareSolverr containers to become ready. |
| `WORKER_FLARESOLVERR_STARTUP_POLL_SECONDS` | `3` | Interval between health checks during FlareSolverr startup. |
| `WORKER_FLARESOLVERR_COUNT` | `4` | Number of FlareSolverr instances to maintain (when using docker discovery mode). |

### FlareSolverr Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `FLARESOLVERR_DISCOVERY_MODE` | `docker` | How workers find FlareSolverr: `docker`, `env`, or `ports`. |
| `FLARESOLVERR_HOST` | `127.0.0.1` | Base IP for generated FlareSolverr URLs (ports mode only). |
| `FLARESOLVERR_BASE_PORT` | `8191` | Starting port number for FlareSolverr instances (ports mode only). |
| `FLARESOLVERR_INSTANCE_COUNT` | `4` | Number of FlareSolverr instances expected (ports mode only). |
| `FLARESOLVERR_URLS` | (optional) | Comma-separated list of FlareSolverr URLs when `DISCOVERY_MODE=env`. Example: `http://127.0.0.1:8191/v1,http://127.0.0.1:8192/v1` |
| `WORKER_AUTOSTART_FLARESOLVERR` | `true` | Whether workers should automatically start/manage FlareSolverr containers (docker mode only). |
| `FLARESOLVERR_DOCKER_LABEL` | `app=flaresolverr` | Docker label selector for finding existing FlareSolverr containers. |
| `FLARESOLVERR_DOCKER_IMAGE` | `ghcr.io/flaresolverr/flaresolverr:latest` | Docker image to use when creating FlareSolverr containers. |
| `FLARESOLVERR_CONTAINER_PREFIX` | `flaresolverr-worker` | Name prefix for created FlareSolverr containers. |

### Availability Checker Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `AVAILABILITY_CHECKER_SOURCE` | `master-availability-checker` | Identifier used when posting availability to the master API. |
| `AVAILABILITY_CHECKER_POLL_INTERVAL_SECONDS` | `60` | Seconds between availability scans. |
| `AVAILABILITY_CHECKER_REQUEST_TIMEOUT_SECONDS` | `30` | HTTP timeout for individual requests during scanning. |
| `AVAILABILITY_CHECKER_PROXY_VALIDATION_CONCURRENCY` | `10` | Number of concurrent proxy validation requests. |
| `AVAILABILITY_CHECKER_OUTPUT_DIR` | `./availability_runs` | Directory to write availability scan JSON files. |

### Booking Engine Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `CAPSOLVER_API_KEY` | (required) | API key for CapSolver captcha-solving service. |
| `CAPTURE_AVAILABLE_TICKETS` | `false` | When `true`, ignores task time and books any available slot on the requested date. |
| `WAITING_ROOM_POLL_INTERVAL_SECONDS` | `15` | How often to check for slot availability when in waiting room. |
| `WAITING_ROOM_MAX_WAIT_SECONDS` | `600` | Maximum time to wait in waiting room before giving up (10 minutes). |
| `WORKER_CALENDAR_RETRY_ATTEMPTS` | `3` | Number of retry attempts for calendar-related failures. |

### Email Provider Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| **Burner (preferred)** |
| `BURNER_EMAIL_API_KEY` | (required) | API key for Burner email service. |
| `BURNER_EMAIL_SITE` | `resa.notredamedeparis.fr` | Site domain for email generation. |
| `BURNER_EMAIL_TTL_DAYS` | (optional) | Email address lifetime in days. |
| `BURNER_EMAIL_JWT_TOKEN` | (optional) | Alternative JWT authentication for Burner. |
| `BURNER_EMAIL_USERNAME` | (optional) | Username for Burner login (alternative auth). |
| `BURNER_EMAIL_PASSWORD` | (optional) | Password for Burner login (alternative auth). |
| **SimpleLogin** |
| `SIMPLELOGIN_API_KEY` | (required) | API key for SimpleLogin service. |
| **Addy.io** |
| `ADDY_API_KEY` | (required) | API key for Addy.io service. |
| `ADDY_DOMAIN` | `anonaddy.me` | Domain for Addy email aliases. |

## 5. Local Infrastructure Startup

### RabbitMQ

If RabbitMQ is already running in Docker, verify it:

```powershell
docker ps --format "table {{.Names}}\t{{.Ports}}"
```

Open the management UI:

```text
http://localhost:<published-management-port>
```

### FlareSolverr

Your local FlareSolverr containers are already mapped like this:

- `8191`
- `8192`
- `8193`
- `8194`

Verify:

```powershell
docker ps --format "table {{.Names}}\t{{.Ports}}"
```

## 6. How To Run Locally

Open separate terminals from the repo root.

### Terminal 1: start the master API

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m master.main
```

Linux:

```bash
.venv/bin/python -m master.main
```

Master health check:

```text
http://127.0.0.1:8000/health
```

### Terminal 2: start the reporting worker

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m master.reporting_worker
```

Linux:

```bash
.venv/bin/python -m master.reporting_worker
```

### Terminal 3: choose one worker mode

#### Option A: run one local Python worker

Use this for quick debugging.

Windows PowerShell:

```powershell
$env:WORKER_ID="worker-local-01"
$env:WORKER_NAME="worker-local-01"
$env:WORKER_MAX_TASKS="2"
$env:RABBITMQ_WORKER_PREFETCH_COUNT="2"
.\.venv\Scripts\python.exe -m worker.worker_main
```

Linux:

```bash
WORKER_ID=worker-local-01 \
WORKER_NAME=worker-local-01 \
WORKER_MAX_TASKS=2 \
RABBITMQ_WORKER_PREFETCH_COUNT=2 \
.venv/bin/python -m worker.worker_main
```

#### Option B: run the warm 4-container worker pool

Use this to match the intended production shape.

```powershell
docker compose -f docker-compose.worker-pool.yml up -d --build
```

That compose file creates:

- `worker-a` with concurrency `3`
- `worker-b` with concurrency `3`
- `worker-c` with concurrency `2`
- `worker-d` with concurrency `2`

Total per VM: `10`

Check logs:

```powershell
docker compose -f docker-compose.worker-pool.yml logs -f
```

### Terminal 4: optional availability checker

Run one scan:

```powershell
.\.venv\Scripts\python.exe -m master.availability_checker --once
```

Run continuously:

```powershell
.\.venv\Scripts\python.exe -m master.availability_checker
```

## 7. Local Verification Checklist

### Verify the broker

In RabbitMQ UI:

- queue `booking_jobs` exists
- queue `booking_jobs.retry` exists
- queue `job_results` exists

### Verify the master

`GET /health` should return:

- `status: ok`
- task source summary
- broker queue names

### Verify worker consumption

When availability is triggered and tasks match:

- the master should mark them `queued`
- worker logs should show task receipt
- worker logs should show FlareSolverr assignment
- results should land in `job_results`
- the reporting worker should flush them back to SQLite and Google Sheets

### Verify retries

For a failed worker attempt:

- the worker should `NACK`
- the message should move through the retry queue
- RabbitMQ should redeliver it immediately when `RABBITMQ_BOOKING_RETRY_DELAY_MS=0`
- after `RABBITMQ_BOOKING_MAX_RETRIES`, the worker should publish a terminal `failed` result

## 8. Local Task Submission Options

### Option A: Google Sheets

If Google Sheets is configured, the master will sync tasks from the configured source.

Trigger a manual sync:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/tasks/sync" `
  -Headers @{ "X-API-Key" = "replace-with-strong-random-value" }
```

### Option B: direct API task submission

Example:

```powershell
$headers = @{
  "X-API-Key" = "replace-with-strong-random-value"
  "Content-Type" = "application/json"
}

$body = @'
[
  {
    "task_id": "local-task-001",
    "firstName": "Ada",
    "lastName": "Lovelace",
    "email": "",
    "phone": "1234567890",
    "zip": "97220",
    "country": "United States Of America",
    "date": "2026-10-10",
    "time": "09:00",
    "ticket_count": 1
  }
]
'@

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/tasks" `
  -Headers $headers `
  -Body $body
```

### Trigger availability manually

```powershell
$headers = @{
  "X-API-Key" = "replace-with-strong-random-value"
  "Content-Type" = "application/json"
}

$body = @'
{
  "source": "manual-local-test",
  "availabilities": [
    {
      "date": "2026/10/10",
      "time": "09:00",
      "quantity": 3
    }
  ]
}
'@

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/availability/trigger" `
  -Headers $headers `
  -Body $body
```

## 9. Cloud Deployment

## Recommended Shape

### Master VM

Run on one machine:

- `master.main`
- `master.reporting_worker`
- SQLite database on persistent disk

Optional on the same VM:

- `master.availability_checker`

### RabbitMQ

Use one of:

- managed RabbitMQ
- a dedicated broker VM
- a Docker container on a dedicated infra host

Do not bury RabbitMQ inside the worker VM if multiple worker VMs depend on it.

### Worker VMs

Each worker VM should run:

- 4 worker containers from `docker-compose.worker-pool.yml`
- 4 FlareSolverr containers
- no custom host-side job router

The VM host should only ensure the containers stay alive across reboots.

## 10. Cloud Environment Values

### Master VM example

```env
MASTER_HOST=0.0.0.0
MASTER_PORT=8000
MASTER_API_KEY=replace-with-strong-random-value
MASTER_STATE_DB=/opt/selenium_bot/master_state.db
MASTER_REQUIRE_AVAILABILITY_TRIGGER=true
REPORTING_FLUSH_INTERVAL_SECONDS=15

RABBITMQ_URL=amqp://user:pass@rabbitmq.internal:5672/%2F

GOOGLE_SHEETS_ENABLED=true
GOOGLE_SHEETS_SPREADSHEET_ID=your-sheet-id
GOOGLE_SHEETS_CREDENTIALS_FILE=/opt/selenium_bot/service.json

CAPSOLVER_API_KEY=replace-me
```

### Worker VM example

Set the worker containers to connect directly to the broker:

```env
RABBITMQ_URL=amqp://user:pass@rabbitmq.internal:5672/%2F
WORKER_EMAIL_PROVIDER=burner
CAPSOLVER_API_KEY=replace-me
FLARESOLVERR_DISCOVERY_MODE=env
FLARESOLVERR_URLS=http://host.docker.internal:8191/v1,http://host.docker.internal:8192/v1,http://host.docker.internal:8193/v1,http://host.docker.internal:8194/v1
```

If FlareSolverr runs as sibling containers in the same Docker network, use their service names instead of host ports.

## 11. Cloud Startup Order

1. Start RabbitMQ.
2. Start the master API.
3. Start the reporting worker.
4. Confirm `GET /health` is healthy.
5. Start the worker pool on each worker VM.
6. Start or enable the availability checker.

## 12. Running The Worker Pool On A Cloud VM

Copy the repo and `.env` to the worker VM, then:

```bash
docker compose -f docker-compose.worker-pool.yml up -d --build
```

Set the compose service to start on reboot with either:

- Docker Engine restart policy, already configured as `restart: always`
- or a small `systemd` unit that runs the compose command on boot

The important point is that the host does not decide where jobs go. RabbitMQ does that.

## 13. Suggested `systemd` Units

### Master API

`/etc/systemd/system/selenium-bot-master.service`

```ini
[Unit]
Description=Selenium Bot Master API
After=network.target

[Service]
WorkingDirectory=/opt/selenium_bot
ExecStart=/opt/selenium_bot/.venv/bin/python -m master.main
Restart=always
RestartSec=5
User=seleniumbot

[Install]
WantedBy=multi-user.target
```

### Reporting worker

`/etc/systemd/system/selenium-bot-reporting.service`

```ini
[Unit]
Description=Selenium Bot Reporting Worker
After=network.target

[Service]
WorkingDirectory=/opt/selenium_bot
ExecStart=/opt/selenium_bot/.venv/bin/python -m master.reporting_worker
Restart=always
RestartSec=5
User=seleniumbot

[Install]
WantedBy=multi-user.target
```

### Availability checker

`/etc/systemd/system/selenium-bot-availability.service`

```ini
[Unit]
Description=Selenium Bot Availability Checker
After=network.target

[Service]
WorkingDirectory=/opt/selenium_bot
ExecStart=/opt/selenium_bot/.venv/bin/python -m master.availability_checker
Restart=always
RestartSec=5
User=seleniumbot

[Install]
WantedBy=multi-user.target
```

## 14. Common Problems

### Workers cannot connect to RabbitMQ

Check:

- `RABBITMQ_URL`
- firewall rules
- broker hostname reachability
- whether you accidentally used the management port `15672` instead of AMQP `5672`

If you use `RABBITMQ_URL=auto`, the app resolves the current published AMQP port
for the Docker broker at startup.

### `PRECONDITION_FAILED - inequivalent arg 'x-message-ttl'`

This happens when `booking_jobs.retry` already exists in RabbitMQ with an older TTL value and the app now tries to declare it with a different `RABBITMQ_BOOKING_RETRY_DELAY_MS`.

RabbitMQ does not let you mutate queue arguments in place.

For this project, the usual fix is:

1. Open the RabbitMQ management UI on the published host port shown by `docker ps`
2. Log in with your broker credentials
3. Delete queue `booking_jobs.retry`
4. Restart the master and worker processes so the queue is recreated with the new TTL

Alternative if you do not want to delete the old queue yet:

- temporarily set `RABBITMQ_BOOKING_RETRY_DELAY_MS` back to the previous value
- restart the app
- drain or ignore the old retry queue
- then delete and recreate it when you are ready to switch TTL values

Deleting the queue will discard any retry messages that were still waiting inside it.

### Worker containers cannot reach the host broker

If you run a standalone worker container against a host broker, auto-discovery does
not apply inside the container. Use:

```env
RABBITMQ_URL=amqp://guest:guest@host.docker.internal:<published-port>/%2F
```

and keep:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

### Tasks are synced but never run

Check:

- `MASTER_REQUIRE_AVAILABILITY_TRIGGER=true`
- whether `dispatch_ready` is false in Google Sheets
- whether `/availability/trigger` was called
- whether tasks are `pending` or `queued`

### Results are not reaching Google Sheets

Check:

- `master.reporting_worker` is running
- `job_results` is receiving messages
- Google Sheets credentials are valid
- the task metadata includes `source=google_sheets` and `sheet_row_number`

## 15. Recommended First End-To-End Test

1. Start RabbitMQ.
2. Start FlareSolverr containers.
3. Start `master.main`.
4. Start `master.reporting_worker`.
5. Start one local worker or the warm worker pool.
6. POST one fake task to `/tasks`.
7. POST one fake availability payload to `/availability/trigger`.
8. Confirm the task moves:

```text
pending -> queued -> running -> completed|failed
```

9. Confirm the reporting worker flushes the final status.

## 16. Useful Commands

List Docker containers:

```powershell
docker ps --format "table {{.Names}}\t{{.Ports}}"
```

Tail worker pool logs:

```powershell
docker compose -f docker-compose.worker-pool.yml logs -f
```

Run tests:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Check master health:

```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8000/health"
```
