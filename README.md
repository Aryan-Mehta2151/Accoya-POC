# AI Marketing Outreach POC

An unauthenticated proof of concept that ingests EarlyBid construction
opportunities, queues personalized Accoya nurturing emails for background
generation, supports human review on each opportunity, manages strategy
documents, and provides a knowledge-base chatbot.

The backend separates requested work from provider execution. Every request is
first represented by a durable `email_generation_jobs` record, and every
claimed attempt is represented by an `agent_runs` record, including expected
and unexpected failures. The standalone agent remains database-independent;
FastAPI owns queueing, persistence, and the production-safe API contracts.

## Architecture

- **Backend:** FastAPI 0.139.2, Pydantic 2.13.4, pydantic-settings 2.14.2,
  synchronous SQLAlchemy 2.0.51, Alembic 1.18.5, and psycopg2 2.9.12
- **Database:** PostgreSQL 18 (the local POC uses PostgreSQL 18.4 on port 5433)
- **Email agent:** LangGraph 1.2.9 with Google Gemini structured generation
- **Retrieval:** AWS Bedrock Knowledge Base `Retrieve` calls
- **Documents:** AWS S3
- **Frontend:** React 19, TypeScript 6, and Vite 8

```text
EarlyBid CSV -> agent normalization -> current PostgreSQL lead projection
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

- Python 3.11 or newer
- Docker Desktop or another local PostgreSQL instance
- Node.js `^20.19.0`, `^22.13.0`, or `>=24`
- Provider credentials only for the live features you intend to exercise

Run backend commands from `backend/` and frontend commands from `frontend/`.

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
# From backend/ with the virtual environment active
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

### 4. Start the React frontend

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
| Email worker | `EMAIL_GENERATION_WORKER_POLL_SECONDS`, `EMAIL_GENERATION_HEARTBEAT_SECONDS`, `EMAIL_GENERATION_STALE_SECONDS` | Defaults to 2, 15, and 300 seconds. The stale threshold must remain comfortably above the heartbeat interval and expected scheduling jitter. |
| EarlyBid | `LEAD_API_BASE_URL`, `LEAD_API_KEY`, `LEAD_FEED_RESELLER`, `LEAD_FEED_CLIENT` | Configures Bearer-authenticated feed sync. The reseller/client scope is also part of the derived identity for rows without a source ID. |
| Frontend | `VITE_API_BASE_URL` | Optional full browser API base; include `/api` unless `API_PREFIX` changes. |

Settings and the configured email agent are process-cached. Restart FastAPI
and the worker after changing environment values.

## Agent-centric PostgreSQL database

Alembic contains a greenfield baseline rather than a conversion of the legacy
schema. It imports no old records and intentionally has no backfill path:

- `0001_agent_centric_baseline` creates the complete application schema.
- `0002_agent_run_pagination_index` adds the `(started_at, id)` index used by
  unfiltered descending agent-run cursor pagination.
- `0004_email_generation_queue` adds durable email-generation jobs and links a
  claimed job to at most one agent run. Applying it does not enqueue or
  generate drafts for existing leads.

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
                                                             |
                                                             `---- * email_status_events

chat_messages                 strategy_documents
```

| Table | Purpose and important invariants |
| --- | --- |
| `leads` | Current normalized lead projection with native UUID primary key and unique `(source_system, external_id)` identity. It stores all current agent inputs, first-class `next_step`, optional `contact_email`, JSONB tags and detached raw feed payload, source metadata, and create/update/archive timestamps. Feed sync updates this projection rather than creating historical snapshots. |
| `email_generation_jobs` | Durable requested work for one lead. A job records its trigger, requested input hash, idempotency key, optional retry link, safe error code, attempt count, and queue/claim/heartbeat/completion timestamps. Status is `queued`, `running`, `generated`, `insufficient_context`, `provider_error`, or `system_error`. A unique idempotency key makes request replay safe, and a PostgreSQL partial unique index allows at most one queued/running job per lead. |
| `agent_runs` | One durable attempt for one lead, with optional `retry_of_run_id` and nullable unique `email_generation_job_id` for worker-created attempts. Status is `running`, `generated`, `insufficient_context`, `provider_error`, or `system_error`. A run stores the curated-input SHA-256 hash, safe selections/warnings/error code, immutable original draft, prompt/catalog versions, model name, aggregate model/retrieval/token counts, latency, and start/completion times. Database checks enforce the SHA-256 shape, nonnegative telemetry, nurturing numbers 1-7, and valid running/generated/failure terminal shapes. |
| `emails` | At most one mutable review email for a generated run through a unique, non-null `agent_run_id`. It stores unrestricted subject/body text, optional recipient snapshot, review status, and timestamps. `lead_id` in the existing API response is derived through the run instead of duplicated. |
| `email_status_events` | Append-only status history. The generated email starts with a `pending_review` event; later changes store previous/new status, optional actor, and timestamp. Status updates lock the email row so concurrent transitions retain a contiguous audit chain. Same-status requests are no-ops. |
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
and status events. Deleting a referenced generation job clears its job retry
link, while run retry links use `RESTRICT`. Indexes cover active job claiming,
lead score/archive queries, run lead/status/time and cursor queries, email
status/time queries, and event history.

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

The full detached source row is retained in `raw_data` JSONB for the current
projection, but email generation passes Gemini only an explicit allowlist of
stored lead fields plus the application lead UUID, source system, and external
ID. ORM internals and arbitrary raw feed fields are never added to the prompt.

## API surface

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Process health after successful database/schema startup |
| POST | `/api/leads/sync` | Fetch and upsert the configured EarlyBid feed |
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
| PATCH | `/api/emails/{email_id}` | Edit mutable subject/body content |
| POST | `/api/emails/{email_id}/status` | Update review status and append an audit event |
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
the latest generation job. A newer draft does not delete or mutate prior
emails; history remains available read-only in the UI. The stored
`recipient_email` snapshot is returned with each email so a later feed update
does not make an old draft appear addressed to a different contact.

All attempts, including failures, retain durable job/run records; only
successful generation creates an email. The production endpoints never return
raw lead data, KB chunks, prompts, or agent telemetry. Human edits update only
the mutable email, leaving the original generated subject/body on the run
unchanged. Malformed and nonexistent identifiers return stable 404 responses.

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
python -m unittest discover -s tests -v

# From frontend/
npm run lint
npm run build
```

The offline suites cover new-only automatic queueing, replay-safe manual
queueing, sync/upload response counts, workspace ordering and staleness,
provider-free worker outcomes, and the rule that ingestion never invokes a
provider. Existing ingestion coverage includes explicit-ID precedence,
deterministic `earlybid-natural-v1` IDs, repeat imports, mutable-field changes,
scope separation, invalid components, duplicate conflicts, and the upload/sync
422/502 atomic failure contracts.

The opt-in PostgreSQL integration suite additionally verifies migration/index
constraints, partial active-job uniqueness, `SKIP LOCKED` claiming, stale
lease handling, clean/idempotent bootstrap, native types/defaults, immutable
originals, and concurrent status-event ordering. Use only a dedicated database
whose name ends in `_test`:

```powershell
# From backend/; PostgreSQL must be listening on port 5433.
$env:ACCOYA_TEST_DATABASE_URL="postgresql+psycopg2://postgres:YOUR_PASSWORD@localhost:5433/accoya_agent_test"
python -m unittest tests.test_postgres_agent_database -v
Remove-Item Env:ACCOYA_TEST_DATABASE_URL
```

The suite may create and migrate `accoya_agent_test`, uses a fake agent, and
cleans only agent-subsystem rows in that isolated test database. Against a
configured local database that is already at head, migration drift can also be
checked with:

```powershell
python -m alembic check
```

## Known gaps and safety

- The API has no authentication or authorization and must not be exposed
  publicly.
- Lead sync and CSV upload persist contact data and automatically queue a draft
  for every newly inserted opportunity. The worker, document operations, and
  chat can call billable or mutating external services.
- Email status changes do not send mail, and `sent` emails are not indexed into
  the knowledge base.
- Document upload does not start a Bedrock KB ingestion job.
- There is no scheduled feed sync, Docker environment, CI workflow, or AWS
  deployment configuration.
- The worker must be deployed and supervised separately from FastAPI. Deploy
  the migration, API, and worker together; enable ingestion only after a worker
  is available.
- EarlyBid does not supply an immutable opportunity ID. A project rename or
  location correction therefore produces a new lead identity; reconciliation
  of renamed opportunities is not automated.
- The Alembic baseline is greenfield; legacy import/backfill is out of scope.
- Production database roles and credential management are not implemented.
