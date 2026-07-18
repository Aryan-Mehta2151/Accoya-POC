# Isolated Accoya Email Agent

`backend/agent` is a standalone, synchronous LangGraph package that turns one
complete lead mapping into at most one validated Accoya outreach draft. It is
not registered with FastAPI and does not read or write the application database.

The package imports only `app.config` from the existing backend. Its Gemini,
Bedrock Knowledge Base retrieval, catalog, prompts, validation, telemetry, and
graph orchestration are folder-local; it does not import the legacy email, RAG,
Gemini, Bedrock, or AWS service modules.

## Install

Python 3.11 or newer is required. Run from `backend/`:

```powershell
python -m pip install -r agent/requirements.txt
```

The folder-local requirements file extends `backend/requirements.txt` and pins
LangGraph to `1.2.9` so graph behavior remains reproducible.

## Configuration

`AccoyaEmailAgent.from_settings()` uses the existing `app.config.Settings`
fields:

- `GEMINI_API_KEY` and `GEMINI_MODEL` configure structured lead analysis and
  email composition. The configured model name is used unchanged.
- `BEDROCK_KB_ID`, `BEDROCK_KB_TOP_K`, `AWS_REGION`, and the optional AWS access
  keys configure direct Bedrock Knowledge Base `Retrieve` calls.
- When `BEDROCK_KB_ID` is blank, generation continues in claim-light,
  catalog-only mode and returns a warning.
- Bedrock results must have `status=approved` metadata. This restriction is
  applied to the request and checked again on returned chunks.
- Dependency-injected construction may add product-family, application,
  audience, region, or other explicit metadata filters; every configured value
  is enforced both in the Bedrock request and again after retrieval.

Changing environment values still requires a process restart because the
backend settings object is cached.

## Public API

Run Python with `backend/` as the working directory:

```python
from agent import AccoyaEmailAgent

agent = AccoyaEmailAgent.from_settings()
result = agent.generate(
    {
        "external_id": "feed-1042",
        "Project": "Riverside Walkway",
        "Location": "Sacramento, CA",
        "Signal": "Thermory, or similar decking is being considered.",
        "Timing": "Early planning",
        "Contacts": "Taylor Smith - Project Architect, taylor@example.com",
    }
)

payload = result.model_dump(mode="json")
```

`generate()` accepts a mapping with either snake_case field names or the current
CSV display headers. The first nonblank value from `lead_id`, `id`, or
`external_id` is required. Dashboard ranks such as `Lead #4` are rejected
because they are not stable identifiers. All other current lead fields are
optional, and an untouched deep copy is returned as `original_lead`.

Expected input fields include Section, Project, Location, State, Signal,
Intelligence, Score, Timing, Next Step, Awarded To, Priority Reasons, Summary,
Contacts, Meeting Date, Tags, and URL. Normalization parses common dates,
contacts, tags, city/state, audience, project stage, and exact material or
competitor mentions when present.

The returned `GenerationResult.status` is one of:

| Status | Meaning |
| --- | --- |
| `generated` | The subject and body passed deterministic validation. |
| `insufficient_context` | Lead evidence did not support a catalog selection with at least `0.60` confidence. |
| `validation_failed` | The initial draft and the single repair both failed; subject and body are withheld. |
| `provider_error` | Structured analysis/composition was unavailable; subject and body are withheld. |

A generated result also includes the canonical product/application, lead
evidence, cited approved strategy chunks, warnings, prompt version, UTC
timestamp, validation state, and telemetry. Telemetry records model calls,
available token counts, retrieval query/document IDs, repair use, and node/total
latency. Logging uses the standard `accoya_email_agent` logger and does not
configure global handlers or LangSmith.

## Fixed graph

The compiled graph has no conditional loop or persistence hook:

```text
START -> analyze_lead -> retrieve_strategy -> compose_and_validate -> END
```

- `analyze_lead` normalizes the record, creates catalog-backed routing hints,
  obtains a structured `ProductSelection`, and rejects unknown, unsupported, or
  low-confidence selections. At most three benefit topics and one stage-routed
  CTA category are retained.
- `retrieve_strategy` calls Bedrock `Retrieve` for four to six results (five by
  default). Missing configuration, empty approved results, or retrieval failure
  produces a warning and safe catalog fallback; it never retries without the
  approved filter.
- `compose_and_validate` requests a structured email, checks formatting,
  catalog membership, CTA, lead/strategy evidence, competitor and warranty
  grounding, planning-stage wording, and the shared claim policy. An invalid
  draft receives exactly one repair request containing its violation codes.

The versioned catalog contains only Accoya Wood, Accoya Color Grey, and Tricoya
Panels. Color Grey requires explicit grey/pre-greyed application relevance.
Tricoya is always treated as a panel product, never solid lumber.

## Safety boundaries

This package only returns a Pydantic result. It does not:

- register an HTTP route or change existing backend/frontend behavior;
- query or mutate PostgreSQL, save a draft, update a lead, or send an email;
- upload/delete S3 objects, start Knowledge Base ingestion, or scrape a website;
- expose an invalid draft or run an unbounded model/repair loop.

`from_settings()` can make live, billable Gemini and AWS calls. Use injected
`StructuredModel` and `StrategyRetriever` fakes for automated checks.

## Tests

All tests use `unittest` with fake model, Bedrock, and retrieval clients. They do
not require a database, credentials, or network access. From `backend/` run:

```powershell
.\.venv\Scripts\python.exe -m compileall -q agent
.\.venv\Scripts\python.exe -m unittest discover -s agent/tests -t . -p "test_*.py"
```

Coverage includes stable IDs and normalization, every required product route,
the complete prohibited-phrase policy, catalog/CTA/evidence/warranty validation,
approved-only Bedrock mapping and failure fallback, graph order and metadata,
low-confidence/provider outcomes, and the bounded single repair. The Beckstrom
Cabin golden fixture is intentionally deferred until its complete real lead
record is available; no synthetic record is substituted.
