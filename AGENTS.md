# Repository guide for agents

## Scope and source of truth

This file applies to the entire repository. There are currently no nested
`AGENTS.md` files. Treat the implementation and configuration files as the
source of truth when they disagree with a README; known documentation drift is
listed below.

This is a largely unauthenticated proof of concept, not a production-ready
service. The real-send endpoint requires the existing JWT, but other business
routes remain unprotected. It handles contact data and can call live, billable,
or mutating external services.

## What this project does

The application ingests construction opportunities from an EarlyBid CSV feed
manually and through a durable daily-midnight scheduler, stores them as leads,
uploads marketing strategy documents to S3, retrieves
strategy and nurturing context through an AWS Bedrock Knowledge Base, generates
outreach emails through a LangGraph/Gemini agent, supports a human-review status
workflow and durable SMTP delivery, and exposes a knowledge-base chatbot. New
leads durably queue their first email for a separate generation worker, while
approved messages queue delivery for a separate SMTP worker. A React SPA provides Overview,
Opportunities with inline outreach review, Strategy Docs, and Chatbot tabs.

The backend stack is FastAPI, Pydantic 2, synchronous SQLAlchemy 2, PostgreSQL,
boto3, LangGraph, LangChain/Gemini, and httpx. The frontend is React 19,
TypeScript 6, Vite 8, native `fetch`, and global CSS.

## Repository map

- `backend/app/main.py`: FastAPI app, local-development CORS, startup database
  connectivity/Alembic-head validation, health endpoint, and router registration.
- `backend/app/config.py`: cached Pydantic settings loaded from environment
  variables and the backend-root `.env` file resolved by absolute path.
- `backend/app/api/routes/`: HTTP handlers for leads, persisted agent runs,
  emails, documents, chat, and development-only raw agent diagnostics.
- `backend/app/schemas/`: Pydantic request/response contracts for leads, agent
  runs, emails, and chat. Document and chat-history responses are currently
  untyped.
- `backend/app/db/`: synchronous SQLAlchemy engine/session setup, ORM models,
  Alembic-head checks, and the idempotent database bootstrap command.
- `backend/alembic/`: the greenfield baseline and Alembic runtime environment.
- `backend/app/services/`: integrations and business logic for EarlyBid, S3,
  Bedrock, Gemini, email-agent integration, durable generation jobs, and RAG.
- `backend/app/workers/email_generation.py`: separately started PostgreSQL
  queue worker; the FastAPI process never performs queued provider work.
- `backend/app/workers/email_delivery.py`: separately started PostgreSQL queue
  worker that is solely responsible for live outreach SMTP calls.
- `backend/app/workers/earlybid_sync.py`: separately started PostgreSQL
  scheduler/worker for current-day local-midnight EarlyBid synchronization.
- `backend/agent/`: standalone synchronous Accoya email agent, including
  normalization, catalog routing, Gemini stages, Bedrock retrieval, telemetry,
  and offline unit tests.
- `frontend/src/App.tsx`: in-memory tab shell; there is no client-side router.
- `frontend/src/pages/`: hook-based page components, one per main feature.
- `frontend/src/api.ts`: all browser API calls and `VITE_API_BASE_URL` handling.
- `frontend/src/types.ts`: shared TypeScript representations of server data.
- `frontend/src/App.css` and `frontend/src/index.css`: global styles. The latter
  still contains Vite-template styling, so account for the global cascade.
- `README.md`: backend-centric overview and setup notes.
- `frontend/README.md`: unchanged Vite template documentation; it is not a
  project guide.

There is no root task runner, Docker setup, CI workflow, or Python
lint/type-check configuration. Backend tests use unittest; routine tests are
offline, with a separate opt-in PostgreSQL integration suite.

## Local setup and commands

### Backend

Python 3.11 or newer is required by the pinned pandas 3.x dependency. Run
backend commands from `backend/`; the `app.*` imports and documented command
paths depend on that working directory. Configuration resolves `.env` from the
backend root independently of the current directory.

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Set credentials and provider configuration. Example local database URL:
# postgresql+psycopg2://postgres:YOUR_PASSWORD@localhost:5433/accoya_agent
python -m app.db.bootstrap
python -m uvicorn app.main:app --reload
# In a second backend terminal with the same environment:
python -m app.workers.email_generation
# In a third backend terminal with the same environment:
python -m app.workers.email_delivery
# In a fourth backend terminal with the same environment:
python -m app.workers.earlybid_sync
```

The default server is `http://localhost:8000`, OpenAPI is at `/docs`, health is
at `/health`, and application routes use `API_PREFIX` (default `/api`). Startup
checks connectivity and requires the configured database to be at the current
Alembic head; it never creates or upgrades tables. `python -m app.db.bootstrap`
creates the configured PostgreSQL database when absent and idempotently applies
all migrations. Run it after changing `DATABASE_URL` or pulling a migration.
Run one instance of each worker by default for this POC. PostgreSQL claims make
multiple instances safe, and queued work survives process restarts. The
generation worker exits when model configuration is incomplete, the delivery
worker exits when SMTP or lease configuration is invalid, and the sync worker exits
when EarlyBid/schedule configuration is invalid. Starting the delivery worker
authorizes real outbound email for already queued approved messages.

### Frontend

The locked toolchain supports Node.js `^20.19.0`, `^22.13.0`, or `>=24`.

```powershell
cd frontend
npm ci
npm run dev
```

Vite serves on `http://localhost:5173` by default. The browser API base defaults
to `http://localhost:8000/api`. Override it with `VITE_API_BASE_URL`, including
the API prefix. The backend CORS allowlist only contains localhost and
127.0.0.1 on port 5173.

## Configuration

Start from `backend/.env.example`. Never put real credentials in that file or
any other tracked file.

| Area | Variables | Important behavior |
| --- | --- | --- |
| App | `APP_ENV`, `API_PREFIX` | `/api/agent/*` is registered only when `APP_ENV=development`. `/health` is not under the prefix. |
| Database | `DATABASE_URL` | Example/default targets `accoya_agent` on local port 5433. Runtime has no SQLite fallback; bootstrap creates the database and migrates it. |
| AWS | `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Blank keys let boto3 use its normal credential chain/IAM role. Code defaults to `us-east-1`, while `.env.example` says `us-east-2`. |
| S3 | `S3_BUCKET_STRATEGY_DOCS` | Required for document list/upload/delete. |
| Bedrock KB | `BEDROCK_KB_ID`, `BEDROCK_KB_MODEL_ARN`, `BEDROCK_KB_TOP_K` | The model setting accepts a model ID or full ARN. |
| Gemini | `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_REQUEST_TIMEOUT_SECONDS` | Used by the worker's email agent; the timeout applies to each Gemini request. The active chat route does not use Gemini. |
| Generation worker | `EMAIL_GENERATION_WORKER_POLL_SECONDS`, `EMAIL_GENERATION_HEARTBEAT_SECONDS`, `EMAIL_GENERATION_STALE_SECONDS` | Defaults to 2, 15, and 300 seconds. Keep the stale threshold safely above the heartbeat interval. |
| Delivery worker | `EMAIL_DELIVERY_WORKER_POLL_SECONDS`, `EMAIL_DELIVERY_HEARTBEAT_SECONDS`, `EMAIL_DELIVERY_STALE_SECONDS` | Defaults to 2, 15, and 300 seconds. Keep stale safely above heartbeat. |
| SMTP delivery | `SMTP_HOST`, `SMTP_PORT`, `SMTP_EMAIL`, `SMTP_PASSWORD`, `SMTP_TIMEOUT_SECONDS` | Required by the delivery worker; timeout defaults to 30 seconds. The worker uses authenticated STARTTLS and stable Message-IDs; a timeout may produce an unknown outcome. |
| Authentication | `JWT_SECRET_KEY`, `ACCESS_TOKEN_EXPIRE_MINUTES` | The real-send endpoint requires a valid Bearer JWT and a nonblank signing secret; other business routes remain unprotected. |
| EarlyBid | `LEAD_API_BASE_URL`, `LEAD_API_KEY`, `LEAD_FEED_RESELLER`, `LEAD_FEED_CLIENT` | Sync uses Bearer auth; reseller/client also scope derived natural identities. |
| Daily sync | `LEAD_AUTO_SYNC_TIMEZONE`, `LEAD_AUTO_SYNC_POLL_SECONDS`, `LEAD_AUTO_SYNC_HEARTBEAT_SECONDS`, `LEAD_AUTO_SYNC_STALE_SECONDS` | Defaults to `America/Los_Angeles`, 30, 15, and 300 seconds. Use an IANA timezone and keep stale greater than heartbeat. |
| Frontend | `VITE_API_BASE_URL` | Optional; include `/api` unless `API_PREFIX` was changed. |

`get_settings()` is cached, and multiple modules retain the returned object at
module scope. Restart the backend and all three workers after changing
environment values.

## Implemented request and data flows

- Leads: `POST /api/leads/sync` fetches
  `/v1/feeds/{reseller}/{client}/latest.csv`; `POST /api/leads/upload-csv`
  accepts the same schema. Both pass rows through the standalone agent
  normalizer and upsert the current projection on `(source_system,
  external_id)`. Because EarlyBid supplies no immutable row ID, the backend
  derives `earlybid-natural-v1:<sha256>` from reseller/client scope and
  normalized Project/Location/State. A supplied `external_id`, `lead_id`, or
  `id` takes precedence for future compatibility. URL and all mutable fields
  are excluded. Repeat sync updates the same projection; changing project name
  or location creates a new identity. Invalid or conflicting batches are
  atomic: upload returns 422 and remote sync returns 502 without partial
  writes. Each newly inserted lead queues one initial email-generation job in
  the same transaction; updated/unchanged leads never auto-regenerate and
  existing rows are not backfilled. Normalized `next_step`, decimal score, best
  recipient, and tags are persisted. Tags and the detached complete source row
  use JSONB. `GET /api/leads` sorts by descending score and includes separate
  current-email/latest-generation summaries.
- Daily EarlyBid sync: `earlybid_sync_runs` stores one scheduled run per
  reseller/client/local date. `python -m app.workers.earlybid_sync` schedules
  local midnight in `LEAD_AUTO_SYNC_TIMEZONE`, creates only a current-day
  catch-up after restart, claims due work with `SKIP LOCKED`, and heartbeats its
  lease. Once the current slot exists, prior-date active rows are terminalized
  as `superseded_schedule` without a feed request, and late results cannot
  mutate leads. It makes at most four attempts with 5/15/30-minute retry delays
  for network/timeout, HTTP 429/5xx, persistence, and stale-lease failures.
  Startup exits before touching the database or feed when configuration is
  invalid; auth/other 4xx, invalid feeds, and late configuration failures are
  terminal. Success atomically commits run counts, lead upserts, and initial
  email jobs. `GET /api/leads/sync-status` is read-only and never calls
  EarlyBid; manual sync remains synchronous and independent of the daily run.
- Documents: upload reads the entire file, stores it under a UUID-prefixed S3
  key, then records metadata in PostgreSQL. Listing uses S3, not the metadata
  table, as its source of truth. Deletion removes the S3 object and performs
  best-effort metadata cleanup. There is no size/type validation.
- Email-generation jobs: `POST /api/leads/{lead_id}/email-generations`
  idempotently queues manual Generate, Regenerate, or Retry work and returns
  202. Jobs persist the trigger, requested input hash, safe outcome, attempt
  count, and queue/claim/heartbeat/completion timestamps, never prompts or
  lead/email content. At most one queued/running job exists per lead. The
  worker claims with PostgreSQL `SKIP LOCKED`, commits the linked running
  `AgentRun`, calls providers outside database transactions, heartbeats, and
  atomically finalizes the job/run/email/status event. Failures and stale jobs
  are terminal and are never automatically replayed.
- Email-delivery jobs: JWT-authenticated `POST /api/emails/{email_id}/send`
  idempotently queues a snapshotted sender, recipient, subject, and body and
  returns 202. It requires the current approved email, valid saved content, and
  a matching delivery-content hash. At most one queued/running job exists per
  email. The separately started worker claims with PostgreSQL `SKIP LOCKED`,
  releases database locks before SMTP, heartbeats its lease, and atomically
  records relay acceptance plus the `sent` status event. Definite failures
  leave the email approved. Timeouts, submission disconnects, and stale leases
  become `delivery_unknown` and are never automatically retried; a user must
  explicitly acknowledge duplicate risk before resending.
- Agent runs: a worker claim creates and commits a `running` record before
  provider work, then finalizes it as `generated`, `insufficient_context`,
  `provider_error`, or `system_error`. `GET
  /api/agent-runs` supports lead/status filters and descending cursor
  pagination; `GET /api/agent-runs/{run_id}` reads one safe outcome; `POST
  /api/agent-runs/{run_id}/retry` is a deprecated enqueueing adapter. Terminal
  outcomes retain only the input hash, safe selection/error fields, original
  draft, code versions, and aggregate telemetry.
- Opportunity workspace: `GET /api/leads/{lead_id}/workspace` returns the lead,
  newest-first email history, current email ID, input-staleness flag, and latest
  generation and delivery state. `GET /api/emails/{email_id}` supports legacy
  deep links. Email responses expose the editable stored recipient snapshot,
  initially copied from the opportunity contact so later feed updates cannot
  silently retarget a draft. A generated run's original draft remains immutable
  while recipient/subject/body edits affect the review email and each status
  transition appends an `email_status_events` row. Approval requires a valid
  saved recipient and nonblank content; editing approved content returns it to
  pending review. Clients cannot directly set `sent`. The old
  generation endpoints remain only as deprecated queue adapters; HTTP request
  handlers never call the provider.
- Agent diagnostics: `/api/agent/*` accepts raw lead mappings and can expose
  detailed results, retrieval references, and traces. It is registered only in
  development and is not part of the production API surface.
- Chat: `POST /api/chat` uses Bedrock `RetrieveAndGenerate` directly, persists
  user and assistant messages, returns Bedrock citations, and retries once
  without a stale Bedrock session ID. `GET /api/chat/{session_id}` returns stored
  history. `services/rag_service.py` implements a separate retrieve-then-Gemini
  path but is currently unused.

## Change conventions

### Backend

- Keep HTTP concerns in `api/routes`, Pydantic contracts in `schemas`, ORM state
  in `db`, and integration/business logic in `services`.
- Use absolute `app.*` imports, snake_case names, module docstrings, modern type
  hints, and SQLAlchemy 2 `Mapped` declarations, matching nearby code.
- Obtain configuration through `get_settings()`; do not read secrets directly
  from the environment in feature modules.
- Keep queue operations transactionally durable and idempotent. Do not add a
  browser mount effect, FastAPI background task, or in-memory queue as a second
  generation or delivery path.
- Do not hold a database transaction or row lock across Gemini/Bedrock calls.
  Preserve `SKIP LOCKED` job claiming, heartbeat/stale-job handling, one active
  job per lead, and no automatic queue-level retry.
- Never hold a database transaction or row lock across SMTP. Preserve
  `SKIP LOCKED` delivery claiming, independent heartbeats, one active delivery
  per email, stable Message-IDs, exact confirmed-content snapshots, and no
  automatic replay of `delivery_unknown`. Only the delivery worker may set an
  email to `sent` after relay acceptance.
- Keep daily scheduling in the separate worker, based on local calendar dates
  and timezone-aware midnight conversion. Preserve current-day-only catch-up,
  the unique feed/date identity, bounded retry classification, and atomic
  success finalization. Do not schedule from FastAPI startup or page polling.
- Never hold a database lock across the EarlyBid HTTP request. Worker leases
  must heartbeat from an independent session; stale recovery may retry only
  within the four-attempt limit.
- Use FastAPI dependencies for database sessions and response models for typed
  endpoints. Existing database and external clients are synchronous.
- Keep the real-send endpoint JWT-protected and validate its expected content
  hash under the email row lock. Do not infer authentication for the remaining
  business routes without a separate authorization change.
- Translate expected integration failures into useful HTTP errors without
  exposing credentials. The existing convention generally uses 502 for an
  upstream failure and 404 for a missing local record.
- Never restore `Base.metadata.create_all()` to application startup. Any ORM
  schema change requires an Alembic revision; keep the migration and ORM
  metadata aligned, then run `python -m app.db.bootstrap`.
- The existing baseline is intentionally greenfield. Do not infer authorization
  to import or mutate a legacy database.
- If lead ingestion changes, keep agent normalization, sync/upload paths, the
  composite source identity, ORM model, Pydantic schema, JSONB representation,
  and frontend wire type aligned.
- Keep `earlybid-natural-v1` deterministic and backward compatible. Do not add
  URL, score, timing, summary, contacts, tags, next step, or other mutable feed
  values to its hash. Identity-version changes require an explicit
  reconciliation strategy even though they do not require a schema migration.

### Frontend

- Use function components and hooks. Keep page-level UI in `src/pages`, shared
  HTTP calls in `src/api.ts`, and shared server shapes in `src/types.ts`.
- Use `import type` for type-only imports. TypeScript rejects unused locals,
  unused parameters, and switch fallthrough; ESLint is the configured style
  check.
- Tab switches unmount pages, so component-local chat sessions, previews, and
  other page state reset. React StrictMode can also repeat initial GET effects
  during development.
- Vite has no development proxy. The API helper expects every successful
  response, including deletes, to contain JSON and has no auth, retry, or
  cancellation layer.
- There is no formatter, and quote/semicolon style varies. Follow the adjacent
  file rather than performing unrelated formatting churn.
- Keep `types.ts` and `api.ts` synchronized with backend response models and
  paths. Preserve snake_case JSON field names unless both sides intentionally
  change.
- Send the stored Bearer JWT only for real delivery, retain one idempotency key
  across a network-error retry, and poll while delivery is queued/running.
  Never automatically acknowledge or retry an unknown delivery outcome.
- Styles are global; reuse existing classes/variables or account for both CSS
  files. No component library or state-management package is installed.
- Change dependencies through npm and commit `package.json` and
  `package-lock.json` together. Do not hand-edit the generated lockfile.

## Verification

Run the checks relevant to the changed area from the indicated directory.

```powershell
# backend/
python -m compileall -q app agent alembic
python -m unittest discover -s agent/tests -t . -p "test_*.py"
python -m unittest tests.test_email_generation_queue -v
python -m unittest tests.test_email_delivery_queue -v
python -m unittest tests.test_earlybid_sync_scheduler -v
python -m unittest discover -s tests -v

# frontend/
npm run test:run -- src/features/opportunities/Opportunities.test.tsx src/lib/api.test.ts
npm run lint
npm run build
```

The PostgreSQL suite is explicitly opt-in and requires a dedicated database
whose name ends in `_test`:

```powershell
# backend/; never point this at accoya_agent or another non-test database.
$env:ACCOYA_TEST_DATABASE_URL="postgresql+psycopg2://postgres:YOUR_PASSWORD@localhost:5433/accoya_agent_test"
python -m unittest tests.test_postgres_agent_database tests.test_postgres_email_delivery -v
Remove-Item Env:ACCOYA_TEST_DATABASE_URL
```

They provision/migrate the test database and clean only their application
rows. For API smoke testing, start a deliberately configured local backend and
check `/health` and `/docs`; startup connects to PostgreSQL and validates its
Alembic revision but does not change schema. Routine backend tests use isolated
state and fake the email agent or providers; they must not call EarlyBid, S3,
Bedrock, Gemini, email services, or live PostgreSQL. If tests or new tooling are
added, document the exact command here and in the relevant README.

Lead-ingestion coverage must include deterministic natural IDs for rows without
source IDs, explicit-ID precedence, repeat sync, mutable-field changes, feed
scope separation, missing identity components, duplicate conflicts, atomic
uploads/syncs, and their respective HTTP 422/502 responses. Sample CSV tests
must remain offline and must not persist or commit real contact data.

Queue coverage must remain provider-free and include new-only initial jobs,
unchanged/updated import skips, manual idempotency, one-active-job behavior,
workspace ordering/staleness, all worker terminal outcomes, sync never invoking
the provider, stale leases, and PostgreSQL concurrency/index behavior. Do not
start the live worker as an automated smoke test.

Delivery coverage must fake SMTP and include recipient default/edit/clear,
approval and reapproval rules, JWT enforcement, content-hash staleness,
idempotency and concurrent enqueueing, relay acceptance, definite failure,
unknown outcomes, stable Message-IDs, stale leases, and the absence of
automatic resend. Never start the live delivery worker or contact SMTP in an
automated check.

Scheduler coverage must use fixed clocks and fake EarlyBid responses. Cover
timezone/DST midnight conversion, current-day-only catch-up, unique schedules,
historical supersession and late-result rejection, due claims,
heartbeats/stale recovery, retry classification and delays, the
four-attempt terminal limit, atomic lead/email-job/run finalization, safe status
responses, and invalid configuration. PostgreSQL coverage must exercise the
unique feed/date constraint and `SKIP LOCKED`; never run the live sync worker in
automated verification.

## Safety and known gaps

- `.env`, virtual environments, `node_modules`, build output, logs, local
  SQLite files, and temp files are ignored. Do not commit secrets, real lead
  CSVs, contact data, or captured API payloads.
- Only the real-send endpoint enforces the existing JWT; the remaining business
  API has no authorization. Do not expose it publicly in its current form.
- Treat manual/scheduled lead sync and CSV upload as PostgreSQL/PII writes that
  automatically queue one draft for every new lead. Running the generation or
  sync worker, document upload/delete/list, manual regeneration, and chat can
  call live, billable, or mutating integrations. Do not use them as automated
  checks without explicit authorization and non-production resources.
- Document upload does not trigger a Bedrock KB ingestion job.
- Email status changes alone do not send mail. The authenticated Send Email
  action queues a real external message, and starting the delivery worker
  explicitly authorizes SMTP delivery. `sent` means the relay accepted the
  message, not guaranteed inbox delivery; sent emails are not indexed into the
  KB. Unknown delivery is never replayed automatically because that could send
  a duplicate.
- AWS deployment and process supervision remain out of scope. Alembic is
  configured with a clean baseline; legacy import/backfill is out of scope.
- Deploy the database migration, web API, and all three separately supervised
  workers together. Starting the delivery worker authorizes queued live SMTP
  sends. Starting the sync worker authorizes recurring live EarlyBid calls; do
  not infer authorization to replay prior dates or backfill leads.
- EarlyBid supplies no immutable opportunity ID. Project renames or location
  corrections produce new natural identities; automatic reconciliation is out
  of scope.
- Production database roles and credential management are not implemented.
