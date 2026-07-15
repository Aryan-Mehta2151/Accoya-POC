# AI Marketing Outreach — POC

AI-powered outreach platform for a marketing agency. It ingests leads, generates
personalized outreach emails grounded in strategy documents (RAG), routes them
through a human approval step, sends approved emails to the client, and provides
a QnA chatbot over strategy docs and sent emails.

## Architecture

- **Backend:** FastAPI + LangChain
- **LLM:** Google Gemini (generation)
- **RAG retrieval:** AWS Bedrock Knowledge Base (Titan Text Embeddings V2 +
  OpenSearch Serverless) via the `Retrieve` API
- **Object storage:** AWS S3 (strategy documents)
- **Database:** PostgreSQL / RDS (leads, emails, chat history, doc metadata)
- **Frontend:** React (Vite + TypeScript) — _coming next_
- **Deployment target:** AWS

```
Custom Lead API ─┐
                 ├─► FastAPI ─► LangChain ─► Gemini ─► Email drafts ─► Approval ─► Client
Strategy docs ─► S3 ─► Bedrock KB ─┘ (Retrieve)          │
                                                         └─► indexed back into KB
Chatbot ─► FastAPI ─► Bedrock KB (Retrieve) ─► Gemini ─► grounded answers
```

## Data storage strategy

| Data                          | Store                         |
| ----------------------------- | ----------------------------- |
| Strategy docs (raw files)     | S3                            |
| Strategy docs (vectors)       | Bedrock KB (OpenSearch)       |
| Lead CSV data                 | PostgreSQL                    |
| Email drafts + sent records   | PostgreSQL                    |
| Sent email content (semantic) | Bedrock KB                    |
| Chat history                  | PostgreSQL                    |

## Backend setup

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env   # then fill in values
uvicorn app.main:app --reload
```

API docs: http://localhost:8000/docs

## Key endpoints

| Method | Path                          | Purpose                                  |
| ------ | ----------------------------- | ---------------------------------------- |
| POST   | `/api/leads/sync`             | Pull latest EarlyBid feed, upsert leads  |
| POST   | `/api/leads/upload-csv`       | Upload a feed CSV; upsert on `id`        |
| GET    | `/api/leads`                  | List leads (cards)                       |
| POST   | `/api/documents/upload`       | Upload a strategy doc to S3              |
| POST   | `/api/emails/generate/{lead}` | Generate outreach email for a lead       |
| PATCH  | `/api/emails/{id}`            | Edit an email draft                      |
| POST   | `/api/emails/{id}/status`     | Approve / reject / send                  |
| POST   | `/api/chat`                   | Ask the QnA chatbot                      |

## Lead source — EarlyBid feed

Leads come from the EarlyBid feed API (`earlystack_client_feed_v1` schema):
`GET /v1/feeds/{reseller}/{client}/latest.csv` (Bearer auth). Configure the feed
and key via `LEAD_API_KEY`, `LEAD_FEED_RESELLER`, `LEAD_FEED_CLIENT` in `.env`.
`POST /api/leads/sync` pulls the latest feed and upserts on the opportunity `id`.

## Open TODOs

- Bedrock KB ingestion job trigger after doc upload
- Sending approved emails to the client + indexing sent emails into the KB
- Scheduled daily feed sync (after 07:00 UTC)
- Alembic migrations (replacing `create_all`)
- AWS deployment (App Runner / ECS + RDS + S3 + Bedrock)
