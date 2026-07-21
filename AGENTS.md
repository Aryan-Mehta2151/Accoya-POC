# Repository guide for agents

## Scope and source of truth

This file applies to the entire repository. There are currently no nested
`AGENTS.md` files. Treat the implementation and configuration files as the
source of truth when they disagree with a README; known documentation drift is
listed below.

This is an unauthenticated proof of concept, not a production-ready service. It
handles contact data and can call live, billable, or mutating external services.

## What this project does

The application ingests construction opportunities from an EarlyBid CSV feed,
stores them as leads, uploads marketing strategy documents to S3, retrieves
strategy and nurturing context through an AWS Bedrock Knowledge Base, generates
outreach emails through a LangGraph/Gemini agent, supports a human-review status
workflow, and exposes a knowledge-base chatbot. A React SPA provides Leads,
Strategy Docs, Email Approval, and Chatbot tabs.

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
  Bedrock, Gemini, email-agent integration, and RAG.
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
```

The default server is `http://localhost:8000`, OpenAPI is at `/docs`, health is
at `/health`, and application routes use `API_PREFIX` (default `/api`). Startup
checks connectivity and requires the configured database to be at the current
Alembic head; it never creates or upgrades tables. `python -m app.db.bootstrap`
creates the configured PostgreSQL database when absent and idempotently applies
all migrations. Run it after changing `DATABASE_URL` or pulling a migration.

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
| Gemini | `GEMINI_API_KEY`, `GEMINI_MODEL` | Used by the three email-agent model stages; the active chat route does not use Gemini. |
| EarlyBid | `LEAD_API_BASE_URL`, `LEAD_API_KEY`, `LEAD_FEED_RESELLER`, `LEAD_FEED_CLIENT` | Sync uses Bearer auth; reseller/client also scope derived natural identities. |
| Frontend | `VITE_API_BASE_URL` | Optional; include `/api` unless `API_PREFIX` was changed. |

`get_settings()` is cached, and multiple modules retain the returned object at
module scope. Restart the backend after changing environment values.

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
  writes. Normalized `next_step`, decimal score, best recipient, and tags are
  persisted. Tags and the detached complete source row use JSONB. `GET
  /api/leads` sorts by descending score. This uses the existing external-ID
  column and unique constraint, so it requires no database migration.
- Documents: upload reads the entire file, stores it under a UUID-prefixed S3
  key, then records metadata in PostgreSQL. Listing uses S3, not the metadata
  table, as its source of truth. Deletion removes the S3 object and performs
  best-effort metadata cleanup. There is no size/type validation.
- Agent runs: `POST /api/agent-runs` synchronously creates and commits a
  `running` record before provider work, then finalizes it as `generated`,
  `insufficient_context`, `provider_error`, or `system_error`. `GET
  /api/agent-runs` supports lead/status filters and descending cursor
  pagination; `GET /api/agent-runs/{run_id}` reads one safe outcome; `POST
  /api/agent-runs/{run_id}/retry` creates a linked run from the lead's current
  projection. Terminal outcomes retain only the input hash, safe selection and
  error fields, original draft, code versions, and aggregate telemetry.
- Emails: `POST /api/emails/generate/{lead_id}` is the compatibility facade over
  persisted agent runs. Generated outcomes create a mutable email in
  `pending_review`; insufficient context returns 422 and provider failure 502.
  Failures retain the run but create no email. A generated run's original draft
  remains immutable while subject/body edits affect the review email and each
  status transition appends an `email_status_events` row. The status enum is
  `draft`, `pending_review`, `approved`, `sent`, or `rejected`.
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
- Use FastAPI dependencies for database sessions and response models for typed
  endpoints. Existing database and external clients are synchronous.
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
python -m unittest discover -s tests -v

# frontend/
npm run lint
npm run build
```

The PostgreSQL suite is explicitly opt-in and requires a dedicated database
whose name ends in `_test`:

```powershell
# backend/; never point this at accoya_agent or another non-test database.
$env:ACCOYA_TEST_DATABASE_URL="postgresql+psycopg2://postgres:YOUR_PASSWORD@localhost:5433/accoya_agent_test"
python -m unittest tests.test_postgres_agent_database -v
Remove-Item Env:ACCOYA_TEST_DATABASE_URL
```

It provisions/migrates the test database and cleans only its agent-subsystem
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

## Safety and known gaps

- `.env`, virtual environments, `node_modules`, build output, logs, local
  SQLite files, and temp files are ignored. Do not commit secrets, real lead
  CSVs, contact data, or captured API payloads.
- The API has no authentication or authorization. Do not expose it publicly in
  its current form.
- Treat lead sync, document upload/delete/list, email generation, and chat as
  live integration operations: they can write PostgreSQL/S3, process PII, or
  incur API charges. Do not use them as automated checks without explicit
  authorization and non-production resources.
- Document upload does not trigger a Bedrock KB ingestion job.
- Email status changes do not send mail, and `sent` emails are not indexed into
  the KB. The UI's "Send to client" action only changes the status.
- Scheduled feed sync and AWS deployment remain out of scope. Alembic is
  configured with a clean baseline; legacy import/backfill is out of scope.
- EarlyBid supplies no immutable opportunity ID. Project renames or location
  corrections produce new natural identities; automatic reconciliation is out
  of scope.
- Production database roles and credential management are not implemented.
