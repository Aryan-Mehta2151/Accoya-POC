# Simplified Accoya Email Agent

`backend/agent` is a standalone synchronous LangGraph package that turns one
lead mapping into at most one Accoya outreach email. It is not registered with
FastAPI and does not read or write the application database.

The package imports only `app.config` from the existing backend. Its Gemini,
Bedrock retrieval, catalog, prompts, telemetry, and graph orchestration remain
folder-local; no legacy email, RAG, Gemini, Bedrock, or AWS service is imported.

## Install

Run from `backend/`:

```powershell
python -m pip install -r agent/requirements.txt
```

The folder requirements extend `backend/requirements.txt` and pin LangGraph to
`1.2.9`.

## Public API

```python
from agent import AccoyaEmailAgent

agent = AccoyaEmailAgent.from_settings()
result = agent.generate(
    {
        "external_id": "feed-1042",
        "Project": "Riverside Walkway",
        "Location": "Sacramento, CA",
        "Signal": "Thermory decking is being considered.",
        "Timing": "Early planning",
    }
)
```

`generate()` accepts current snake_case fields or CSV display headers. The
first nonblank value from `lead_id`, `id`, or `external_id` is required;
dashboard ranks such as `Lead #4` are rejected. The result retains an untouched
deep copy of the supplied mapping.

`GenerationResult.status` is one of:

| Status | Meaning |
| --- | --- |
| `generated` | Gemini returned a nonblank email whose product metadata matches the analyzed catalog pair. |
| `insufficient_context` | Confidence was below `0.60` or the selected pair was outside the catalog. |
| `provider_error` | Analysis/composition failed or composition returned a mismatched pair. |

A result also includes the canonical family/application, every retrieved KB
chunk, warnings, prompt version, UTC timestamp, and telemetry. There is no
evidence ledger, validation status, violation list, or repair state.

## Fixed graph

```text
START -> analyze_lead -> retrieve_strategy -> compose_email -> END
```

- `analyze_lead` preserves the robust normalization and routing hints, then asks
  Gemini for one catalog family/application, confidence, reason, and retrieval
  query. The pair is accepted using only catalog membership and confidence.
- `retrieve_strategy` makes one direct Bedrock `Retrieve` call using the
  configured result count. It sends no metadata filter and retains every result
  without checking approval status or other metadata.
- `compose_email` asks Gemini for a subject, body, and the selected canonical
  IDs. It requires only nonblank content and an exact pair match. There is no
  deterministic email validator, retry, or repair call.

Selected leads therefore make at most two Gemini calls. Low-confidence leads
make one Gemini call and skip retrieval/composition provider work.

## Knowledge Base behavior

Bedrock retrieval sends only the Gemini-generated query and
`BEDROCK_KB_TOP_K`. Every provider result is mapped with its document ID, text,
title, metadata, score, and location. A stable hash ID is derived only when
Bedrock omits `documentId`.

Missing configuration, an empty response, or retrieval failure adds a warning
and composition continues using the lead and catalog without KB context. The
retriever does not retry or add a broader/narrower filter.

## Minimal composition guardrails

The concise prompt requests a short subject, two or three paragraphs, one
selected product/application, grounded use of lead and KB context, and one
low-friction CTA. Retrieved text is explicitly treated as untrusted context.

Code only checks that structured subject/body fields are nonblank and that the
returned canonical pair exactly matches analysis. Length, citations, claims,
warranties, competitor language, CTA wording, and evidence are not validated.

## Safety boundary

This package does not register routes, query PostgreSQL, save drafts, send
email, mutate leads, upload documents, start KB ingestion, or scrape websites.
`from_settings()` can make live, billable Gemini and AWS calls; automated tests
use injected fakes.

## Tests

From `backend/`:

```powershell
.\.venv\Scripts\python.exe -m compileall -q agent
.\.venv\Scripts\python.exe -m unittest discover -s agent/tests -t . -p "test_*.py"
```

The suite covers normalization, stable IDs, catalog/routing behavior, canonical
selection and confidence, unfiltered Bedrock mapping, retrieval fallback, graph
order, two-call bounds, raw chunk propagation, minimal draft matching, and safe
provider errors. No test uses a database, network, or live provider.
