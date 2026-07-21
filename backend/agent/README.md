# Accoya Email Agent

`backend/agent` is a standalone synchronous LangGraph package that turns one
lead mapping into at most one Accoya nurturing email. The package itself does
not register FastAPI routes or read or write the application database; the
backend application owns its HTTP and persistence integration.

The package imports only `app.config` from the existing backend. Its Gemini,
Bedrock retrieval, catalog, prompts, telemetry, and graph orchestration remain
folder-local; no legacy email, RAG, Gemini, Bedrock, or AWS service is imported.

## Install

Run from `backend/`:

```powershell
python -m pip install -r requirements.txt
```

The canonical backend requirements pin LangGraph to `1.2.9`.
`agent/requirements.txt` remains a convenience include of that canonical file.

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
| `provider_error` | Analysis failed without a deterministic routing fallback, composition failed, or composition returned a mismatched pair. |

A result also includes the canonical family/application, nurturing-route
metadata, product-strategy KB chunks, warnings, prompt version, UTC timestamp,
and telemetry. Nurturing chunks remain run-local composition context and are
available in the development trace. There is no evidence ledger, validation
status, violation list, or repair state.

## Fixed graph

```text
START -> analyze_lead -> retrieve_strategy -> compose_email -> END
```

- `analyze_lead` preserves the robust normalization and routing hints, then asks
  Gemini for one catalog family/application, confidence, reason, and retrieval
  query. The pair is accepted using only catalog membership and confidence.
- `retrieve_strategy` first retrieves product strategy, asks Gemini to select
  one nurturing step, canonicalizes that step to a template-only query, and
  retrieves the nurturing template. Both retrievals use the configured result
  count, send no metadata filter, and retain every result without checking
  approval status or other metadata.
- `compose_email` asks Gemini for a subject, body, and the selected canonical
  IDs using both context sets and the nurturing route. It requires only nonblank
  content and an exact pair match. There is no deterministic email validator,
  retry, or repair call.

The normal successful path makes three Gemini calls and, when Bedrock is
configured, two retrieval calls in this order: product selection, strategy
retrieval, nurturing selection, nurturing retrieval, and composition. A
nurturing-selection failure uses a project-stage fallback and still composes.
Low-confidence selections and terminal analysis failures make only the first
Gemini call and skip both retrievals, nurturing selection, and composition.
An analysis provider failure may continue only when deterministic routing hints
produce a valid catalog pair.

## Knowledge Base behavior

The product retrieval sends the selected strategy query. The nurturing
retrieval sends a template-only query derived from the selected email number.
Both use `BEDROCK_KB_TOP_K`. Every provider result is mapped with its document
ID, text, title, metadata, score, and location. A stable hash ID is derived only
when Bedrock omits `documentId`.

Missing configuration, an empty response, or either retrieval failure adds a
stage-specific warning and composition continues with the available context.
The retriever does not retry or add a broader/narrower filter. Telemetry counts
both result sets and records document IDs in strategy-then-nurturing order.

## Minimal composition guardrails

The prompt requests a nonblank subject, two or three paragraphs, one selected
product/application, grounded use of lead and KB context, and one low-friction
CTA. Retrieved text is explicitly treated as untrusted context.

Code only checks that structured subject/body fields are nonblank and that the
returned canonical pair exactly matches analysis. There is no subject-length
limit. Body length, citations, claims, warranties, competitor language, CTA
wording, and evidence are not validated.

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
selection and confidence, unfiltered Bedrock mapping, both retrieval fallbacks,
nurturing success and route fallback, graph order, three-call/two-retrieval
bounds, combined telemetry, long subjects, safe logging, short-circuiting, and
provider errors. No test uses a database, network, or live provider.
