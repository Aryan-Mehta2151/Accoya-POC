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
strategy context through an AWS Bedrock Knowledge Base, generates outreach
emails with Gemini, supports a human-review status workflow, and exposes a
knowledge-base chatbot. A React SPA provides Leads, Strategy Docs, Email
Approval, and Chatbot tabs.

The backend stack is FastAPI, Pydantic 2, synchronous SQLAlchemy 2, PostgreSQL,
boto3, LangChain/Gemini, and httpx. The frontend is React 19, TypeScript 6,
Vite 8, native `fetch`, and global CSS.

## Repository map

- `backend/app/main.py`: FastAPI app, local-development CORS, startup table
  creation, health endpoint, and router registration.
- `backend/app/config.py`: cached Pydantic settings loaded from environment
  variables and a cwd-relative `.env` file.
- `backend/app/api/routes/`: HTTP handlers for leads, documents, emails, and
  chat.
- `backend/app/schemas/`: Pydantic request/response contracts for leads,
  emails, and chat. Document and chat-history responses are currently untyped.
- `backend/app/db/`: synchronous SQLAlchemy engine/session setup and ORM models.
- `backend/app/services/`: integrations and business logic for EarlyBid, S3,
  Bedrock, Gemini, email generation, and RAG.
- `frontend/src/App.tsx`: in-memory tab shell; there is no client-side router.
- `frontend/src/pages/`: hook-based page components, one per main feature.
- `frontend/src/api.ts`: all browser API calls and `VITE_API_BASE_URL` handling.
- `frontend/src/types.ts`: shared TypeScript representations of server data.
- `frontend/src/App.css` and `frontend/src/index.css`: global styles. The latter
  still contains Vite-template styling, so account for the global cascade.
- `README.md`: backend-centric overview and setup notes.
- `frontend/README.md`: unchanged Vite template documentation; it is not a
  project guide.

There is no root task runner, Docker setup, test suite, CI workflow, Python
lint/type-check configuration, or configured Alembic environment.

## Local setup and commands

### Backend

Python 3.11 or newer is required by the pinned pandas 3.x dependency. Run
backend commands from `backend/`; the `app.*` imports and `.env` lookup depend
on that working directory.

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Fill in .env, and make sure its PostgreSQL database already exists.
python -m uvicorn app.main:app --reload
```

The default server is `http://localhost:8000`, OpenAPI is at `/docs`, health is
at `/health`, and application routes use `API_PREFIX` (default `/api`). Startup
calls `Base.metadata.create_all()`, so it requires a reachable database and can
create tables. The repository does not provision PostgreSQL or create the
database itself.

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
| App | `APP_ENV`, `API_PREFIX` | `APP_ENV` is currently unused. `/health` is not under the prefix. |
| Database | `DATABASE_URL` | Defaults to local PostgreSQL database `ai_marketing`; no SQLite fallback. |
| AWS | `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Blank keys let boto3 use its normal credential chain/IAM role. Code defaults to `us-east-1`, while `.env.example` says `us-east-2`. |
| S3 | `S3_BUCKET_STRATEGY_DOCS` | Required for document list/upload/delete. |
| Bedrock KB | `BEDROCK_KB_ID`, `BEDROCK_KB_MODEL_ARN`, `BEDROCK_KB_TOP_K` | The model setting accepts a model ID or full ARN. |
| Gemini | `GEMINI_API_KEY`, `GEMINI_MODEL` | Used by email generation; the active chat route does not use Gemini. |
| EarlyBid | `LEAD_API_BASE_URL`, `LEAD_API_KEY`, `LEAD_FEED_RESELLER`, `LEAD_FEED_CLIENT` | Sync uses Bearer auth and the configured reseller/client feed. |
| Frontend | `VITE_API_BASE_URL` | Optional; include `/api` unless `API_PREFIX` was changed. |

`get_settings()` is cached, and multiple modules retain the returned object at
module scope. Restart the backend after changing environment values.

## Implemented request and data flows

- Leads: `POST /api/leads/sync` fetches
  `/v1/feeds/{reseller}/{client}/latest.csv`; `POST /api/leads/upload-csv`
  accepts the same schema. Both upsert on the feed's case-sensitive `id` column,
  which maps to unique `Lead.external_id`. `GET /api/leads` sorts by descending
  score. CSV headers are exact/case-sensitive, missing IDs are skipped, invalid
  scores become `None`, and only the first email-looking contact is extracted.
- Documents: upload reads the entire file, stores it under a UUID-prefixed S3
  key, then records metadata in PostgreSQL. Listing uses S3, not the metadata
  table, as its source of truth. Deletion removes the S3 object and performs
  best-effort metadata cleanup. There is no size/type validation.
- Emails: generation retrieves Bedrock KB chunks, prompts Gemini, parses a
  `SUBJECT`/`BODY` response, and creates an email in `pending_review`. The enum is
  `draft`, `pending_review`, `approved`, `sent`, or `rejected`. The frontend
  suggests allowed transitions, but the backend accepts any enum transition.
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
- `Base.metadata.create_all()` does not modify existing columns. Any ORM schema
  change needs deliberate migration handling; installing Alembic alone is not a
  migration strategy.
- If lead ingestion changes, keep the EarlyBid column map, sync path, upload
  path, ORM model, Pydantic schema, and frontend type aligned. Note that
  `routes/leads.py` currently calls the private `_row_to_fields()` helper.

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
# backend/ - syntax-only check; no backend test/lint suite exists yet
python -m compileall -q app

# frontend/
npm run lint
npm run build
```

For API smoke testing, start a deliberately configured local backend and check
`/health` and `/docs`. Starting it touches the configured database. New backend
tests should use an isolated database and mock EarlyBid, S3, Bedrock, and Gemini;
routine verification must not call live services. If tests or new tooling are
added, document the exact command here and in the relevant README.

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
- The configured Gemini 1.5 Pro default was shut down in September 2025.
- Scheduled feed sync, Alembic migrations, and AWS deployment are not
  implemented.
- The root README's "frontend coming next" statement is stale. Its chat diagram
  also describes Bedrock retrieval followed by Gemini, while the active chat
  endpoint uses Bedrock `RetrieveAndGenerate`.
