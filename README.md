# AI Marketing Outreach POC

An unauthenticated proof of concept that ingests EarlyBid construction
opportunities, generates personalized Accoya nurturing emails, supports human
review, manages strategy documents, and provides a knowledge-base chatbot.

The current backend is agent-centric: every generation attempt is represented
by a durable `agent_runs` record, including expected and unexpected failures.
The standalone agent remains database-independent; FastAPI owns orchestration,
persistence, and the production-safe API contracts.

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
                                           `-> committed agent_run (running)
                                                  |
                                                  `-> AccoyaEmailAgent
                                                         |
                                                         `-> terminal run outcome
                                                                |
                                                                `-> review email
                                                                     + status events
```

The synchronous successful path performs these stages in order:

1. Gemini selects a catalog product family and application.
2. Bedrock retrieves product strategy context.
3. Gemini selects nurturing email 1-7.
4. Bedrock retrieves the corresponding nurturing template.
5. Gemini composes the subject and body.

That is at most three Gemini calls and two Bedrock retrievals per successful
request. Both retrievals use `BEDROCK_KB_TOP_K`. Low-confidence and terminal
analysis outcomes skip all later work. Missing or failed retrieval adds a safe
warning and composition continues with whatever context is available.

## Run the application locally

### Prerequisites

- Python 3.11 or newer
- PostgreSQL listening on `localhost:5433` (the implementation is verified
  with PostgreSQL 18.4)
- Node.js `^20.19.0`, `^22.13.0`, or `>=24`
- Provider credentials only for the live features you intend to exercise

The repository has no Docker setup and does not install or start PostgreSQL.
Run backend commands from `backend/` and frontend commands from `frontend/`.

### 1. Configure and bootstrap PostgreSQL

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
DATABASE_URL=postgresql+psycopg2://postgres:YOUR_PASSWORD@localhost:5433/accoya_agent
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

### 3. Start the React frontend

In a second terminal:

```powershell
cd frontend
npm ci
npm run dev
```

Open `http://localhost:5173`. Browser requests default to
`http://localhost:8000/api`; set `VITE_API_BASE_URL` to override the complete
API base, including the prefix. FastAPI allows the local `localhost` and
`127.0.0.1` Vite origins on port 5173.

An empty database is a valid starting point, so the Leads, Email Approval, and
agent-run API lists remain empty until data is ingested and generation runs are
created. Sync, document, agent, and chat actions may call live services and can
incur charges.

## Configuration

Start from `backend/.env.example`. Settings are loaded from the backend-root
`.env` using Pydantic Settings, even if the process has a different current
directory.

| Area | Variables | Behavior |
| --- | --- | --- |
| Application | `APP_ENV`, `API_PREFIX` | The default prefix is `/api`. Raw `/api/agent/*` diagnostics are registered only when `APP_ENV=development`. |
| PostgreSQL | `DATABASE_URL` | Must be a PostgreSQL SQLAlchemy URL with a database name. The documented psycopg2 example targets port 5433 and database `accoya_agent`. |
| AWS | `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | The region must contain the configured KB and S3 resources. Leave explicit keys blank to use boto3's normal credential chain or an IAM role. |
| Strategy documents | `S3_BUCKET_STRATEGY_DOCS` | Required for document upload, list, and delete operations. |
| Bedrock KB | `BEDROCK_KB_ID`, `BEDROCK_KB_TOP_K` | Used for both email-agent retrievals. |
| Bedrock chat | `BEDROCK_KB_MODEL_ARN` | Model ID or ARN used by the separate `RetrieveAndGenerate` chatbot flow. |
| Gemini | `GEMINI_API_KEY`, `GEMINI_MODEL` | Used by the three structured email-agent model stages. |
| EarlyBid | `LEAD_API_BASE_URL`, `LEAD_API_KEY`, `LEAD_FEED_RESELLER`, `LEAD_FEED_CLIENT` | Configures Bearer-authenticated feed sync. |
| Frontend | `VITE_API_BASE_URL` | Optional full browser API base; include `/api` unless `API_PREFIX` changes. |

Settings and the configured email agent are process-cached. Restart FastAPI
after changing environment values.

## Agent-centric PostgreSQL database

Alembic contains a greenfield baseline rather than a conversion of the legacy
schema. It imports no old records and intentionally has no backfill path:

- `0001_agent_centric_baseline` creates the complete application schema.
- `0002_agent_run_pagination_index` adds the `(started_at, id)` index used by
  unfiltered descending agent-run cursor pagination.

PostgreSQL stores identifiers as native `UUID`, flexible source data as
`JSONB`, timestamps as timezone-aware `TIMESTAMPTZ`, and agent/email lifecycle
values as PostgreSQL enums. Agent input fields and email subjects/bodies use
`TEXT`; subjects must be nonblank but have no 512-character or other
application/database length cap.

### Tables and relationships

```text
leads 1 ---- * agent_runs
                 |  ^
                 |  `---- optional retry_of_run_id (self-reference)
                 |
                 `---- 0..1 emails 1 ---- * email_status_events

chat_messages                 strategy_documents
```

| Table | Purpose and important invariants |
| --- | --- |
| `leads` | Current normalized lead projection with native UUID primary key and unique `(source_system, external_id)` identity. It stores all current agent inputs, first-class `next_step`, optional `contact_email`, JSONB tags and detached raw feed payload, source metadata, and create/update/archive timestamps. Feed sync updates this projection rather than creating historical snapshots. |
| `agent_runs` | One durable attempt for one lead, with optional `retry_of_run_id`. Status is `running`, `generated`, `insufficient_context`, `provider_error`, or `system_error`. A run stores the curated-input SHA-256 hash, safe selections/warnings/error code, immutable original draft, prompt/catalog versions, model name, aggregate model/retrieval/token counts, latency, and start/completion times. Database checks enforce the SHA-256 shape, nonnegative telemetry, nurturing numbers 1-7, and valid running/generated/failure terminal shapes. |
| `emails` | At most one mutable review email for a generated run through a unique, non-null `agent_run_id`. It stores unrestricted subject/body text, optional recipient snapshot, review status, and timestamps. `lead_id` in the existing API response is derived through the run instead of duplicated. |
| `email_status_events` | Append-only status history. The generated email starts with a `pending_review` event; later changes store previous/new status, optional actor, and timestamp. Status updates lock the email row so concurrent transitions retain a contiguous audit chain. Same-status requests are no-ops. |
| `chat_messages` | Existing user/assistant chat history grouped by `session_id`. |
| `strategy_documents` | Existing S3 document metadata; S3 remains the source of truth for document listing. |

Database defaults initialize a lead's source system to `earlybid`, JSONB raw
data to `{}`, run status to `running`, warnings to `[]`, model/retrieval counts
to zero, and email status to `pending_review` when those values are omitted.
Alembic compares both column types and server defaults against ORM metadata.

Deleting a lead cascades through its runs, generated emails, and status events;
a retry link uses `RESTRICT` so an attempt referenced by a retry cannot be
removed independently. Indexes cover lead score/archive queries, run
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
layer. The feed's stable, case-sensitive `id` becomes `external_id`; invalid
rows without a stable ID are skipped. The normalized projection includes
decimal score, `next_step`, contact data and best recipient email, normalized
tags, location, timing, signal, and the other agent inputs. Duplicate rows in
one feed collapse to the last projection, and upserts are scoped by
`(source_system, external_id)`.

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
| GET | `/api/leads` | List current leads by descending score |
| POST | `/api/agent-runs` | Execute a run for `{ "lead_id": "<uuid>" }` |
| GET | `/api/agent-runs` | List safe run records with filters and cursor pagination |
| GET | `/api/agent-runs/{run_id}` | Read one safe persisted outcome |
| POST | `/api/agent-runs/{run_id}/retry` | Create a linked attempt using the lead's current projection |
| POST | `/api/emails/generate/{lead_id}` | Compatibility facade returning one review email |
| GET | `/api/emails` | List generated review emails |
| PATCH | `/api/emails/{email_id}` | Edit mutable subject/body content |
| POST | `/api/emails/{email_id}/status` | Update review status and append an audit event |
| POST | `/api/documents/upload` | Upload a strategy document to S3 and save metadata |
| GET | `/api/documents` | List strategy documents from S3 |
| DELETE | `/api/documents/{doc_id}` | Delete an S3 document and best-effort metadata record |
| POST | `/api/chat` | Ask the Bedrock knowledge-base chatbot |
| GET | `/api/chat/{session_id}` | Read stored chat history |

### Persisted agent runs

`POST /api/agent-runs` is synchronous. The service:

1. Loads the lead and hashes its curated input.
2. Inserts and commits a `running` run before any provider call.
3. Invokes the agent without an open database transaction.
4. Atomically finalizes the run and, only for `generated`, creates the email and
   initial status event.

Generated, insufficient-context, and provider-error outcomes return HTTP 201
with the completed safe run record. Unexpected exceptions persist
`system_error` and return HTTP 500 with only a safe code, message, and `run_id`.

`GET /api/agent-runs` accepts optional `lead_id`, `status`, and opaque `cursor`
parameters. Results are newest first; `limit` defaults to 50 and allows 1-100.
Invalid cursors return 400. Retry returns 404 when the prior run or lead is
missing, accepts only terminal runs, and returns 409 for a still-running
attempt. It creates a new linked run using the lead's current projection; prior
attempts are never overwritten.

### Email compatibility and review

The existing frontend contract is preserved by
`POST /api/emails/generate/{lead_id}`. A generated result returns the same
top-level email DTO in `pending_review`. Every successful request creates a
separate run and draft, and a missing recipient address does not prevent
drafting.

| HTTP status | Generation facade outcome |
| --- | --- |
| 404 | Lead not found |
| 422 | Insufficient lead context; response contains safe `code`, `message`, and `warnings` |
| 502 | Generation provider failure with the same safe error shape |
| 500 | Unexpected system or persistence failure without provider details |

All attempts, including failures, retain an `agent_runs` row; only successful
generation creates an email. The production email endpoint never returns raw
lead data, KB chunks, or agent telemetry. Human edits update only the mutable
email, leaving the original generated subject/body on the run unchanged.
Malformed and nonexistent email UUIDs return the stable 404 response.

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
- The chatbot uses Bedrock `RetrieveAndGenerate` directly and retries once
  without a stale Bedrock session ID.
- Strategy document upload stores a UUID-prefixed object in S3 and records
  metadata in PostgreSQL, but it does not trigger a Bedrock KB ingestion job.
- Structured operational logs contain identifiers, status, warning counts,
  call counts, and latency - not lead contents, generated email text, or retrieved
  chunk text.

## Verification

Routine tests inject fake providers and use isolated state. Leave
`ACCOYA_TEST_DATABASE_URL` unset so they do not connect to live PostgreSQL, and
never use live Gemini, Bedrock, EarlyBid, S3, or email services for automated
verification.

```powershell
# From backend/
python -m compileall -q app agent alembic
python -m unittest discover -s agent/tests -t . -p "test_*.py"
python -m unittest discover -s tests -v

# From frontend/
npm run lint
npm run build
```

The opt-in PostgreSQL integration suite verifies clean/idempotent bootstrap,
native types and defaults, normalized JSONB ingestion, all run outcomes,
running-before-provider transaction boundaries, immutable originals, long
subjects, retry and hash behavior, cursor pagination, email compatibility, and
concurrent contiguous status events. Use only a dedicated database whose name
ends in `_test`:

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
- Lead sync, document operations, email generation, and chat can process PII,
  mutate external resources, or incur provider charges.
- Email status changes do not send mail, and `sent` emails are not indexed into
  the knowledge base.
- Document upload does not start a Bedrock KB ingestion job.
- There is no scheduled feed sync, Docker environment, CI workflow, or AWS
  deployment configuration.
- The Alembic baseline is greenfield; legacy import/backfill is out of scope.
- Production database roles and credential management are not implemented.
