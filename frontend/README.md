# Accoya Outreach frontend

Accoya Outreach is the internal sales workspace for turning prioritized EarlyBid opportunities into reviewed, context-aware outreach. This frontend is a React 19 and TypeScript application that consumes the existing FastAPI backend without requiring backend changes.

## Local setup

Use a Node.js version supported by the locked Vite toolchain: `^20.19.0`, `^22.13.0`, or `>=24`.

```powershell
cd frontend
npm ci
npm run dev
```

Vite serves the application at `http://localhost:5173`. Start the backend separately at `http://localhost:8000`.

The API prefix defaults to `http://localhost:8000/api`. To use another backend, create an untracked `.env.local`:

```dotenv
VITE_API_BASE_URL=http://localhost:8000/api
```

Include the backend API prefix in the value.

## Product areas

- **Overview** — live opportunity, review, approval, and strategy-document summaries.
- **Opportunities** — local search and filters, EarlyBid sync, CSV import, details, and outreach generation.
- **Outreach** — editing and the draft, review, approval, rejection, and marked-sent workflow.
- **Knowledge Base** — S3-backed strategy-document upload, listing, opening, and deletion.
- **Assistant** — Bedrock knowledge-base chat with session restoration and source display.

The frontend intentionally does not expose agent runs, model details, prompt versions, token usage, traces, or telemetry. The backend may persist those details internally.

Marking outreach as sent updates workflow state only; it does not deliver email. Uploading a strategy document stores it, but the backend does not expose knowledge-base ingestion status.

## Architecture

- `src/app/` contains the routed application shell and navigation.
- `src/features/` contains one folder for each product area.
- `src/components/` contains shared accessible UI primitives.
- `src/lib/api.ts` is the only browser API client and normalizes backend errors.
- `src/lib/queryKeys.ts` defines shared TanStack Query cache keys.
- `src/styles/global.css` contains the design tokens, reset, and global controls.
- Feature presentation is isolated with CSS Modules.

The app uses a React Router data router so unsaved outreach edits can block navigation. TanStack Query owns remote state: GET requests retry once, while mutations never retry automatically.

The assistant keeps only its current Bedrock session identifier in `sessionStorage`. It restores messages through the backend history endpoint when the page reloads.

## Verification

```powershell
npm run lint
npm run test:run
npm run build
```

Tests are offline and mock all network interactions. They must not call EarlyBid, S3, Bedrock, Gemini, email services, or a live database.

Use `npm test` during development for watch mode. Before handing off UI changes, also inspect the application at desktop, tablet, and 390px mobile widths.
