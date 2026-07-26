# AI Marketing Outreach POC

A proof of concept that ingests EarlyBid construction opportunities manually
or on a durable daily schedule, queues personalized Accoya nurturing emails for
background generation, supports human review on each opportunity, manages
strategy documents, durably delivers approved outreach through SMTP, and
provides a knowledge-base chatbot. The real-send endpoint requires the
browser's JWT; most lead, email-review, document, and chat routes remain
unauthenticated, so do not expose this POC publicly.

The backend separates requested work from provider execution. Every generation
request is first represented by a durable `email_generation_jobs` record, and
every claimed generation attempt is represented by an `agent_runs` record,
including expected and unexpected failures. Every real-send request is first
represented by an `email_delivery_jobs` record. The standalone agent remains
database-independent; FastAPI owns queueing, persistence, and the
production-safe API contracts.

## Architecture

- **Backend:** FastAPI 0.139.2, Pydantic 2.13.4, pydantic-settings 2.14.2,
  synchronous SQLAlchemy 2.0.51, Alembic 1.18.5, and psycopg2 2.9.12
- **Database:** PostgreSQL (the local Docker Compose setup uses PostgreSQL 16
  on port 5432)
- **Email agent:** LangGraph 1.2.9 with Google Gemini structured generation
- **Retrieval:** AWS Bedrock Knowledge Base `Retrieve` calls
- **Documents:** AWS S3
- **Frontend:** React 19, TypeScript 6, and Vite 8

```text
daily sync worker -> earlybid_sync_run -> EarlyBid CSV
                                           |
manual sync -------------------------------`
                                           |
                                           `-> current PostgreSQL lead projection
                                           |
                                           `-> queued email_generation_job
                                                  |
                                                  `-> separate worker
                                                         |
                                                         `-> agent_run + provider work
                                                                |
                                                                `-> terminal job/run
                                                                       |
                                                                       `-> review email
                                                                            + status events
                                                                                 |
                                                                                 `-> delivery job
                                                                                        |
                                                                                        `-> separate SMTP worker
```

The worker's successful agent path performs these stages in order:

1. Gemini selects a catalog product family and application.
2. Bedrock retrieves product strategy context.
3. Gemini selects nurturing email 1-7.
4. Bedrock retrieves the corresponding nurturing template.
5. Gemini composes the subject and body.

That is at most three Gemini calls and two Bedrock retrievals per successful
job. Both retrievals use `BEDROCK_KB_TOP_K`. Low-confidence and terminal
analysis outcomes skip all later work. Missing or failed retrieval adds a safe
warning and composition continues with whatever context is available.

## Run the application locally

### Prerequisites

- Git
- Python 3.11 or newer
- Docker Desktop or another local PostgreSQL instance
- Node.js `^20.19.0`, `^22.13.0`, or `>=24`
- Provider credentials only for the live features you intend to exercise

Run backend commands from `backend/` and frontend commands from `frontend/`.
After cloning, open PowerShell at the repository root:

```powershell
git clone <repository-url>
cd Accoya-POC
```

If the repository is already present, omit `git clone` and change into its
existing root directory.

### 1. Configure and bootstrap PostgreSQL

The repo now includes a local Docker Compose setup for PostgreSQL. Start it
from the repository root:

```powershell
docker compose up -d
```

This starts PostgreSQL 16 on `localhost:5432` with local-development
credentials `postgres` / `postgres`. The backend bootstrap command creates the
`ai_marketing` database automatically when it does not exist.

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set the local connection in the ignored `backend/.env`; keep real credentials
out of `.env.example` and other tracked files:

```dotenv
DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/ai_marketing
```

Generate a local JWT signing secret:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Copy the printed value into `backend/.env` and keep the local frontend URL:

```dotenv
JWT_SECRET_KEY=replace-with-the-generated-value
FRONTEND_URL=http://localhost:5173
```

Email/password signup and login then work locally without Google or SMTP
credentials. Password-reset email requires the SMTP settings described below.
Google sign-in additionally requires Google OAuth credentials and the exact
local callback `http://localhost:8000/api/auth/callback/google`.

Then create the database, if needed, and migrate it to the current Alembic
head:

```powershell
python -m app.db.bootstrap
```

The bootstrap command is PostgreSQL-only and idempotent. It derives a
maintenance connection to the `postgres` database from `DATABASE_URL`, creates
the configured target database when absent, and runs `alembic upgrade head`.
Run it again after pulling any new migration.

Application startup never creates or upgrades tables. It checks connectivity
and requires the database revision to exactly match the repository's Alembic
head; a missing or stale schema fails startup with an instruction to run the
bootstrap command.

### 2. Start FastAPI

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --reload
```

- API: `http://localhost:8000`
- OpenAPI: `http://localhost:8000/docs`
- Health: `http://localhost:8000/health`

### 3. Start the email-generation worker

In a second backend terminal, activate the same virtual environment and start
one worker:

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
python -m app.workers.email_generation
```

The web process only queues generation. It never calls Gemini or Bedrock while
handling a sync, CSV upload, or manual generation request. Queued jobs survive
web and worker restarts. Run one worker by default for this POC; PostgreSQL row
locking also prevents multiple workers from claiming the same job.

The worker exits without claiming work when required provider configuration is
missing. It does not automatically replay an ambiguous provider call:
`insufficient_context`, provider failures, system failures, and abandoned
leases remain terminal until a user explicitly retries.

### 4. Start the email-delivery worker

Configure `JWT_SECRET_KEY`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_EMAIL`, and
`SMTP_PASSWORD` in `backend/.env`. Then, in another backend terminal, start
one delivery worker:

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
python -m app.workers.email_delivery
```

The authenticated Send Email action only queues durable work. This separately
supervised worker is the process that can contact SMTP and send real external
email. Starting it is therefore explicit authorization to deliver any queued,
approved outreach using the configured account. Do not start it as a health
check or automated test.

The worker records relay acceptance as `succeeded` and then marks the review
email `sent`. Relay acceptance means the SMTP server accepted responsibility
for the message; it does not guarantee inbox placement. Definite failures leave
the email approved for an explicit retry. Ambiguous timeouts, disconnects, and
expired leases become `delivery_unknown` and are never retried automatically
because the first message may already have been accepted.

### 5. Start the daily EarlyBid sync worker

In another backend terminal, start one scheduler worker:

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
python -m app.workers.earlybid_sync
```

The worker schedules the configured reseller/client feed for local midnight in
`LEAD_AUTO_SYNC_TIMEZONE` (default `America/Los_Angeles`). If it starts after
midnight and today's run does not exist, it queues that current-day run as a
catch-up; it never replays missed prior dates. PostgreSQL uniqueness and row
locking make multiple worker processes safe, although one is the recommended
POC deployment. Once today's slot exists, prior-date queued, retrying, or
running rows are terminalized with the safe `superseded_schedule` code without
calling EarlyBid. A late prior-date result rechecks the schedule before lead
changes are staged, so it cannot overwrite the current projection.

Scheduled sync failures use bounded retries: four total attempts, delayed 5,
15, and 30 minutes. Network errors, timeouts/HTTP 408, HTTP 429/5xx,
persistence errors, and expired worker leases are retryable. The worker refuses
to start with missing configuration. Authentication errors, other HTTP 4xx
responses, invalid feeds, and a configuration failure discovered after startup
terminate the current run without retrying it.

### 6. Start the React frontend

In another terminal:

```powershell
cd frontend
npm ci
npm run dev
```

Open `http://localhost:5173`. Browser requests default to
`http://localhost:8000/api`; set `VITE_API_BASE_URL` to override the complete
API base, including the prefix. FastAPI allows the local `localhost` and
`127.0.0.1` Vite origins on port 5173.

Create an account with the email/password signup form on `/login`, then use
that account to enter the application. The JWT is stored by the browser for UI
session state and sent as a Bearer token for the real-send request. That
endpoint is the only core business API route protected by the JWT, so login is
not a production authorization boundary for the rest of this POC.

Google sign-in is currently local-development-specific: the frontend client ID
and the frontend/backend callback URLs are hardcoded in the authentication
implementation. Environment variables alone are not sufficient to move Google
OAuth to another host. Use email/password locally unless those values match
your Google OAuth application.

An empty database is a valid starting point, so opportunities and agent-run API
lists remain empty until data is ingested. Newly inserted opportunities queue
their first draft; existing or updated opportunities are never regenerated by
sync. The opportunity page provides explicit Generate, Regenerate, or Retry
actions when applicable. Provider work, document operations, sync, and chat may
process contact data, mutate external resources, or incur charges.

## Configuration

Start from `backend/.env.example`. Settings are loaded from the backend-root
`.env` using Pydantic Settings, even if the process has a different current
directory.

| Area | Variables | Behavior |
| --- | --- | --- |
| Application | `APP_ENV`, `API_PREFIX` | The default prefix is `/api`. Raw `/api/agent/*` diagnostics are registered only when `APP_ENV=development`. |
| PostgreSQL | `DATABASE_URL` | Must be a PostgreSQL SQLAlchemy URL with a database name. The local Docker setup targets port 5432 and database `ai_marketing`. |
| AWS | `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | The region must contain the configured KB and S3 resources. Leave explicit keys blank to use boto3's normal credential chain or an IAM role. |
| Strategy documents | `S3_BUCKET_STRATEGY_DOCS` | Required for document upload, list, and delete operations. |
| Bedrock KB | `BEDROCK_KB_ID`, `BEDROCK_KB_TOP_K` | Used for both email-agent retrievals. |
| Bedrock chat | `BEDROCK_KB_MODEL_ARN` | Model ID or ARN used by the separate `RetrieveAndGenerate` chatbot flow. |
| Gemini | `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_REQUEST_TIMEOUT_SECONDS` | Used by the worker's structured email agent; the 180-second default applies to each Gemini request. |
| Generation worker | `EMAIL_GENERATION_WORKER_POLL_SECONDS`, `EMAIL_GENERATION_HEARTBEAT_SECONDS`, `EMAIL_GENERATION_STALE_SECONDS` | Defaults to 2, 15, and 300 seconds. The stale threshold must remain comfortably above the heartbeat interval and expected scheduling jitter. |
| Delivery worker | `EMAIL_DELIVERY_WORKER_POLL_SECONDS`, `EMAIL_DELIVERY_HEARTBEAT_SECONDS`, `EMAIL_DELIVERY_STALE_SECONDS` | Defaults to 2, 15, and 300 seconds. Keep the stale threshold comfortably above the heartbeat interval. |
| EarlyBid | `LEAD_API_BASE_URL`, `LEAD_API_KEY`, `LEAD_FEED_RESELLER`, `LEAD_FEED_CLIENT` | Configures Bearer-authenticated manual and scheduled feed sync. The reseller/client scope is also part of the derived identity for rows without a source ID. |
| Daily sync worker | `LEAD_AUTO_SYNC_TIMEZONE`, `LEAD_AUTO_SYNC_POLL_SECONDS`, `LEAD_AUTO_SYNC_HEARTBEAT_SECONDS`, `LEAD_AUTO_SYNC_STALE_SECONDS` | Defaults to `America/Los_Angeles`, 30, 15, and 300 seconds. The timezone must be an IANA name and the stale threshold must exceed the heartbeat interval. |
| Authentication | `JWT_SECRET_KEY`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `FRONTEND_URL` | Set a strong local JWT secret before signup. The access-token default is 1,440 minutes and the frontend defaults to `http://localhost:5173`. The real-send endpoint requires this JWT; other core business routes remain unprotected. |
| Google OAuth | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Optional. The current frontend client ID and local callback URLs are also hardcoded in code, so these settings alone do not make Google OAuth portable. |
| SMTP | `SMTP_HOST`, `SMTP_PORT`, `SMTP_EMAIL`, `SMTP_PASSWORD`, `SMTP_TIMEOUT_SECONDS`, `PASSWORD_RESET_TOKEN_EXPIRE_MINUTES` | Required to start the delivery worker and optional for password reset. SMTP defaults to Gmail on port 587, the request timeout defaults to 30 seconds, and reset tokens default to 15 minutes. |
| Frontend | `VITE_API_BASE_URL` | Optional full browser API base; include `/api` unless `API_PREFIX` changes. |

Settings and the configured email agent are process-cached. Restart FastAPI
and all three workers after changing environment values.

## Agent-centric PostgreSQL database

Alembic contains a greenfield baseline rather than a conversion of the legacy
schema. It imports no old records and intentionally has no backfill path:

- `0001_agent_centric_baseline` creates the complete application schema.
- `0002_agent_run_pagination_index` adds the `(started_at, id)` index used by
  unfiltered descending agent-run cursor pagination.
- `0003_chat_session_sequencing` adds per-session message ordering and explicit
  human/assistant roles.
- `5662aa7157b7` adds users and password-reset tokens for the browser
  authentication flows.
- `0004_email_generation_queue` adds durable email-generation jobs and links a
  claimed job to at most one agent run. Applying it does not enqueue or
  generate drafts for existing leads.
- `0005_earlybid_daily_sync` adds durable daily feed runs. Applying it creates
  no run and contacts no provider; the separately started sync worker creates
  only the configured feed's current-day slot.
- `0006_email_delivery_queue` adds durable outbound delivery jobs. Applying it
  sends no mail; only the separately started delivery worker contacts SMTP.

PostgreSQL stores identifiers as native `UUID`, flexible source data as
`JSONB`, timestamps as timezone-aware `TIMESTAMPTZ`, and agent/email lifecycle
values as PostgreSQL enums. Agent input fields and email subjects/bodies use
`TEXT`; subjects must be nonblank but have no 512-character or other
application/database length cap.

### Tables and relationships

```text
leads 1 ---- * email_generation_jobs
                 |  ^              |
                 |  `---- optional retry_of_job_id
                 |                 |
                 |                 `---- 0..1 agent_runs
                 |                                |
                 `--------------------------------`
                                                  |
                                                  `---- 0..1 emails
                                                             |---- * email_status_events
                                                             |
                                                             `---- * email_delivery_jobs
                                                                        ^
                                                                        `---- optional retry_of_job_id

earlybid_sync_runs            chat_messages            strategy_documents
```

| Table | Purpose and important invariants |
| --- | --- |
| `leads` | Current normalized lead projection with native UUID primary key and unique `(source_system, external_id)` identity. It stores all current agent inputs, first-class `next_step`, optional `contact_email`, JSONB tags and detached raw feed payload, source metadata, and create/update/archive timestamps. Feed sync updates this projection rather than creating historical snapshots. |
| `earlybid_sync_runs` | One durable scheduled run per reseller/client/local schedule date. Status is `queued`, `running`, `retry_wait`, `succeeded`, or `failed`; the row records its UTC schedule, attempt and lease state, safe error code, next retry time, completion time, and final ingestion/generation counts. It stores no feed payload or contact data. |
| `email_generation_jobs` | Durable requested work for one lead. A job records its trigger, requested input hash, idempotency key, optional retry link, safe error code, attempt count, and queue/claim/heartbeat/completion timestamps. Status is `queued`, `running`, `generated`, `insufficient_context`, `provider_error`, or `system_error`. A unique idempotency key makes request replay safe, and a PostgreSQL partial unique index allows at most one queued/running job per lead. |
| `agent_runs` | One durable attempt for one lead, with optional `retry_of_run_id` and nullable unique `email_generation_job_id` for worker-created attempts. Status is `running`, `generated`, `insufficient_context`, `provider_error`, or `system_error`. A run stores the curated-input SHA-256 hash, safe selections/warnings/error code, immutable original draft, prompt/catalog versions, model name, aggregate model/retrieval/token counts, latency, and start/completion times. Database checks enforce the SHA-256 shape, nonnegative telemetry, nurturing numbers 1-7, and valid running/generated/failure terminal shapes. |
| `emails` | At most one mutable review email for a generated run through a unique, non-null `agent_run_id`. It stores unrestricted subject/body text, the editable optional recipient snapshot, review status, and timestamps. `lead_id` in the existing API response is derived through the run instead of duplicated. |
| `email_status_events` | Append-only status history. The generated email starts with a `pending_review` event; later changes store previous/new status, optional actor, and timestamp. Status updates lock the email row so concurrent transitions retain a contiguous audit chain. Same-status requests are no-ops. |
| `email_delivery_jobs` | Durable outbound work for one approved email. Each job retains the exact confirmed sender, recipient, subject, and body plus idempotency/retry links, stable Message-ID, requester, safe error code, and queue/lease/completion timestamps. Status is `queued`, `running`, `succeeded`, `failed`, or `delivery_unknown`; a partial unique index permits only one queued/running job per email. |
| `chat_messages` | Existing user/assistant chat history grouped by `session_id`. |
| `strategy_documents` | Existing S3 document metadata; S3 remains the source of truth for document listing. |

Database defaults initialize a lead's source system to `earlybid`, JSONB raw
data to `{}`, run status to `running`, warnings to `[]`, model/retrieval counts
to zero, and email status to `pending_review` when those values are omitted.
Alembic compares both column types and server defaults against ORM metadata.

EarlyBid natural identity is stored in the existing unrestricted
`leads.external_id` column and uses the existing unique `(source_system,
external_id)` constraint. This is an ingestion-only change and requires no
database migration.

Deleting a lead cascades through its generation jobs, runs, generated emails,
status events, and delivery jobs. Deleting a referenced generation or delivery
job clears its job retry link, while run retry links use `RESTRICT`. Indexes
cover active generation/delivery claiming, lead score/archive queries, run
lead/status/time and cursor queries, email status/time queries, and event
history.

An `agent_runs` record deliberately does **not** persist its curated/normalized
input snapshot, routing hints, prompts, retrieval queries or chunks, document
IDs, provider responses, secrets, or raw exceptions. This keeps run records
safe and minimal, but means exact replay after a lead changes is not
guaranteed.

## Lead ingestion

`POST /api/leads/sync` fetches the EarlyBid
`earlystack_client_feed_v1` CSV from
`GET /v1/feeds/{reseller}/{client}/latest.csv` with Bearer authentication.
`POST /api/leads/upload-csv` accepts the same feed shape.

Both paths use the standalone agent normalizer as their single interpretation
layer. Current EarlyBid feeds do not provide a stable row ID, so the backend
owns identity. It derives a versioned external ID in the form
`earlybid-natural-v1:<sha256>` from the configured reseller/client feed scope
plus normalized `Project`, `Location`, and `State`. Explicit `external_id`,
`lead_id`, or `id` values take precedence when present so a future feed can
provide its own immutable identity. URL, score, timing, summary, contacts,
tags, next step, and all other mutable fields are deliberately excluded from
the hash.

Re-importing the same opportunity updates its existing current projection on
`(source_system, external_id)`, even when excluded mutable fields change. The
normalized projection includes decimal score, `next_step`, contact data and
best recipient email, normalized tags, location, timing, signal, and the other
agent inputs. Because the source has no immutable ID, changing a project's
name or location creates a new identity; the backend cannot reliably infer
that the renamed or relocated row is the same opportunity.

Ingestion is strict and atomic. A CSV upload with an invalid row, missing
natural-identity component, or conflicting duplicate identity returns HTTP
422; a remote sync with the same feed validation problem returns HTTP 502. In
either case, no rows from that batch are written, rather than silently skipping
or partially importing them.

Each lead first inserted by either ingestion path queues exactly one initial
generation in the same database transaction. The deterministic
`initial-v1:<lead-id>` idempotency key makes repeated ingestion safe. Updating
an existing lead, including changing agent-relevant fields, never queues
another draft automatically. The migration and deployment do not backfill
existing leads; those leads show a manual Generate action on their opportunity
page. Provider work is performed only by the separate worker, after ingestion
has committed and returned.

The sync response includes `generation_queued`. CSV upload now returns
`{items, created, updated, total, generation_queued}` rather than a bare lead
array, so clients can report both ingestion and queue results without polling.

### Daily scheduled synchronization

The separate EarlyBid worker creates at most one run for each configured
reseller/client and local calendar date. Local midnight is converted with the
configured IANA timezone, so daylight-saving transitions naturally produce
23- or 25-hour UTC intervals without shifting the local schedule. Starting the
worker later that day creates only today's catch-up run; dates before today are
not replayed.

Workers claim due `queued` or `retry_wait` rows with PostgreSQL row locking,
heartbeat while the EarlyBid request and ingestion run, and recover expired
leases through the bounded retry policy. A successful attempt atomically
commits the normalized lead upserts, first-draft jobs for newly inserted leads,
and the sync run's success counts. Invalid feeds and failed attempts never
leave partial lead or email-generation-job writes.

Manual `POST /api/leads/sync` remains available and synchronous. It is not a
scheduler override and does not create or consume a daily run. The read-only
`GET /api/leads/sync-status` endpoint reports the configured timezone, next
local-midnight schedule in UTC, whether today's run is overdue, and the latest
safe run summary; reading status never contacts EarlyBid.

The full detached source row is retained in `raw_data` JSONB for the current
projection, but email generation passes Gemini only an explicit allowlist of
stored lead fields plus the application lead UUID, source system, and external
ID. ORM internals and arbitrary raw feed fields are never added to the prompt.

## API surface

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Process health after successful database/schema startup |
| POST | `/api/auth/signup` | Create an email/password user and return a JWT |
| POST | `/api/auth/login` | Authenticate an email/password user and return a JWT |
| GET | `/api/auth/callback/google` | Complete the local Google OAuth redirect flow |
| POST | `/api/auth/forgot-password` | Create a reset token and attempt an SMTP reset email |
| POST | `/api/auth/reset-password` | Consume a reset token and replace the password |
| GET | `/api/auth/me` | Read the JWT-authenticated user |
| POST | `/api/leads/sync` | Fetch and upsert the configured EarlyBid feed |
| GET | `/api/leads/sync-status` | Read daily schedule and latest durable run status without syncing |
| POST | `/api/leads/upload-csv` | Normalize and upsert an uploaded feed CSV |
| GET | `/api/leads` | List leads with current-email and latest-generation summaries |
| GET | `/api/leads/{lead_id}/workspace` | Read an opportunity, email history, active email, staleness, and latest generation |
| POST | `/api/leads/{lead_id}/email-generations` | Idempotently queue Generate, Regenerate, or Retry work |
| POST | `/api/agent-runs` | Deprecated compatibility adapter that queues work |
| GET | `/api/agent-runs` | List safe run records with filters and cursor pagination |
| GET | `/api/agent-runs/{run_id}` | Read one safe persisted outcome |
| POST | `/api/agent-runs/{run_id}/retry` | Deprecated compatibility adapter that queues a retry |
| POST | `/api/emails/generate/{lead_id}` | Deprecated compatibility adapter that queues generation |
| GET | `/api/emails` | List generated review emails |
| GET | `/api/emails/{email_id}` | Read one email for deep-link compatibility |
| PATCH | `/api/emails/{email_id}` | Edit the mutable recipient, subject, or body |
| POST | `/api/emails/{email_id}/status` | Update review status and append an audit event; clients cannot set `sent` |
| POST | `/api/emails/{email_id}/send` | JWT-authenticated, idempotent queueing of real SMTP delivery |
| POST | `/api/documents/upload` | Upload a strategy document to S3 and save metadata |
| GET | `/api/documents` | List strategy documents from S3 |
| DELETE | `/api/documents/{doc_id}` | Delete an S3 document and best-effort metadata record |
| POST | `/api/chat` | Ask the Bedrock knowledge-base chatbot |
| GET | `/api/chat/{session_id}` | Read stored chat history |

### Durable generation queue and agent runs

Generation requests accept a caller-generated UUID idempotency key and return
HTTP 202 with the queued job, or the original job when the same key is replayed.
If a lead already has queued/running work, the endpoint returns that active job
instead of creating competing provider work. Automatic initial jobs use the
deterministic `initial-v1:<lead-id>` key.

The worker claims the oldest queued job with `SELECT ... FOR UPDATE SKIP
LOCKED`, creates and commits the linked `running` agent run, captures the
curated lead input, and releases database locks before invoking the provider.
It heartbeats running jobs and finalizes stale leases as `system_error`; stale
work is not automatically requeued because the provider may already have
accepted the call.

Finalization updates the job and run atomically. A generated outcome also
creates one mutable email and its initial `pending_review` status event.
Insufficient context and provider failures retain safe terminal records without
an email. Unexpected exceptions are recorded as `system_error` without raw
provider details.

`GET /api/agent-runs` accepts optional `lead_id`, `status`, and opaque `cursor`
parameters. Results are newest first; `limit` defaults to 50 and allows 1-100.
Invalid cursors return 400. Retry returns 404 when the prior run or lead is
missing, accepts only terminal runs, and returns 409 for a still-running
attempt. It queues linked work from the lead's current projection; the worker
creates the new run when it claims that job. Prior attempts are never
overwritten.

### Opportunity email workspace and review

The opportunity workspace returns the lead, emails newest first, the current
email ID, whether that current draft was generated from older lead input, and
the latest generation job and delivery state. A newer draft does not delete or
mutate prior emails; history remains available read-only in the UI. Generation
initially snapshots the opportunity's current contact address into
`recipient_email`. Reviewers can fill or replace that one To address without
changing the lead, and a later feed update never silently retargets an existing
draft.

All attempts, including failures, retain durable job/run records; only
successful generation creates an email. The production endpoints never return
raw lead data, KB chunks, prompts, or agent telemetry. Human edits update only
the mutable email, leaving the original generated subject/body on the run
unchanged. A valid saved recipient and nonblank subject/body are required for
approval. Editing any of them after approval records a transition back to
`pending_review`. Historical, rejected, sent, and actively delivering emails
remain read-only. Malformed and nonexistent identifiers return stable 404
responses.

### Durable outbound email delivery

`POST /api/emails/{email_id}/send` requires a valid Bearer JWT and a configured
nonblank JWT secret. It accepts a caller-generated UUID idempotency key, the
current delivery-content SHA-256 hash, and an optional duplicate-risk
acknowledgement. It returns HTTP 202 with the existing or newly queued delivery
summary. The current approved email, its saved recipient/subject/body, and the
expected hash must still match; FastAPI never contacts SMTP in this request.

Each `email_delivery_jobs` row snapshots the sender, recipient, subject, and
body that the user confirmed. PostgreSQL locking and a partial unique index
allow at most one queued/running delivery for an email. Same-key replay for the
same email returns the original job; reusing that key for another email is a
conflict. The worker claims with `SKIP LOCKED`, commits and releases locks, and
then calls SMTP with a stable RFC Message-ID while heartbeating its lease.

SMTP relay acceptance atomically completes the job, marks the review email
`sent`, and appends the status event. A definite failure records a safe error
and leaves the email approved for a manual retry. A timeout, disconnect during
submission, expired lease, or another ambiguous outcome becomes
`delivery_unknown`; the queue never automatically replays it. Any explicit
resend while an unknown attempt exists must acknowledge that it could deliver
a duplicate. Regeneration and editing are blocked while delivery is active,
and unresolved unknown delivery blocks regeneration.

### Development-only diagnostics

With `APP_ENV=development`, OpenAPI additionally exposes:

- `POST /api/agent/generate`
- `POST /api/agent/normalize`
- `POST /api/agent/routing-hints`
- `POST /api/agent/trace`

These endpoints are non-persistent and may expose supplied lead data,
generated content, retrieval references, traces, and telemetry. They are not
registered for any other environment and must not be treated as production
APIs.

## Provider behavior

- The email agent uses Gemini plus two independent Bedrock `Retrieve` calls.
  Retrieval failures are nonterminal; required model-stage failures return a
  safe provider outcome.
- Feed sync and CSV upload only persist leads and jobs; they never instantiate
  or invoke the email agent. Only the worker performs billable generation.
- The delivery API only persists a job. Only the separately started delivery
  worker contacts SMTP, and it logs safe identifiers/outcomes rather than the
  recipient, subject, body, credentials, or raw relay response.
- The daily sync worker is the only automatic EarlyBid caller. It logs run/feed
  identifiers, attempts, safe error codes, timings, and aggregate counts, never
  CSV rows, contact data, credentials, or upstream response bodies.
- Structured operational logs contain job/run/lead identifiers, status,
  warning counts, call counts, token totals, and latency - not lead contents,
  generated email text, prompts, or retrieved chunk text.
- The chatbot uses Bedrock `RetrieveAndGenerate` directly and retries once
  without a stale Bedrock session ID.
- Strategy document upload stores a UUID-prefixed object in S3 and records
  metadata in PostgreSQL, but it does not trigger a Bedrock KB ingestion job.

## Verification

Routine tests inject fake providers and use isolated state. Leave
`ACCOYA_TEST_DATABASE_URL` unset so they do not connect to live PostgreSQL, and
never use live Gemini, Bedrock, EarlyBid, S3, or email services for automated
verification.

```powershell
# From backend/
python -m compileall -q app agent alembic
python -m unittest discover -s agent/tests -t . -p "test_*.py"
python -m unittest tests.test_email_generation_queue -v
python -m unittest tests.test_email_delivery_queue -v
python -m unittest tests.test_earlybid_sync_scheduler -v
python -m unittest discover -s tests -v

# From frontend/
npm run test:run -- src/features/opportunities/Opportunities.test.tsx src/lib/api.test.ts
npm run lint
npm run build
```

The offline suites cover new-only automatic queueing, replay-safe generation
and delivery queueing, editable/default recipient behavior, approval and
content-hash rules, definite/unknown fake SMTP outcomes, workspace ordering
and staleness, provider-free worker outcomes, and the rule that API handlers
never perform provider delivery. Existing ingestion coverage includes
explicit-ID precedence, deterministic `earlybid-natural-v1` IDs, repeat
imports, mutable-field changes, scope separation, invalid components,
duplicate conflicts, and the upload/sync 422/502 atomic failure contracts.

Scheduler tests use fixed clocks and fake EarlyBid responses. They cover local
midnight and daylight-saving boundaries, current-day catch-up, uniqueness,
claim/heartbeat/stale recovery, retry classification and delays, terminal
attempt limits, atomic ingestion, safe status responses, and invalid worker
configuration without making a network request. PostgreSQL-only coverage
checks the schedule uniqueness constraint and `SKIP LOCKED` worker claims.

The opt-in PostgreSQL integration suite additionally verifies migration/index
constraints, partial active-job uniqueness, `SKIP LOCKED` claiming, stale
lease handling, clean/idempotent bootstrap, native types/defaults, immutable
originals, and concurrent status-event ordering. Use only a dedicated database
whose name ends in `_test`:

```powershell
# From backend/; the local Docker PostgreSQL instance listens on port 5432.
$env:ACCOYA_TEST_DATABASE_URL="postgresql+psycopg2://postgres:postgres@localhost:5432/ai_marketing_test"
python -m unittest tests.test_postgres_agent_database tests.test_postgres_email_delivery -v
Remove-Item Env:ACCOYA_TEST_DATABASE_URL
```

The suites create or migrate only the configured `_test` database, use fake
providers, and clean only their application rows there. Against a
configured local database that is already at head, migration drift can also be
checked with:

```powershell
python -m alembic check
```

## Known gaps and safety

- The real-send endpoint enforces the browser JWT, but the remaining lead,
  email-review, document, and chat routes do not. The API must not be exposed
  publicly.
- Manual/scheduled lead sync and CSV upload persist contact data and
  automatically queue a draft for every newly inserted opportunity. Both
  generation/sync workers, document operations, and chat can call billable or
  mutating external services.
- Review status changes alone never send mail; the authenticated Send Email
  action queues a real external message. Starting the delivery worker
  authorizes live SMTP sends. `sent` means relay acceptance, not inbox delivery,
  and sent emails are not indexed into the knowledge base.
- Document upload does not start a Bedrock KB ingestion job.
- There is no bundled process supervisor, CI workflow, or AWS deployment
  configuration. FastAPI and all three workers must be deployed and supervised
  as separate processes.
- Deploy the database migration, API, generation worker, delivery worker, and
  sync worker together. Starting the delivery worker authorizes queued live
  SMTP sends; starting the sync worker authorizes live daily EarlyBid calls
  and downstream draft queueing. Do not use either as a health check or
  automated test.
- EarlyBid does not supply an immutable opportunity ID. A project rename or
  location correction therefore produces a new lead identity; reconciliation
  of renamed opportunities is not automated.
- The Alembic baseline is greenfield; legacy import/backfill is out of scope.
- Production database roles and credential management are not implemented.
