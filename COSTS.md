# Accoya Demo Costs

Short version: this shows the approximate AI cost per request and per 100 requests.

## Chatbot

- One normal chatbot answer: **about $0.002 - $0.006**
- 100 chatbot answers: **about $0.20 - $0.60**
- Greeting like Hi / Hello: **$0**

## Email generation

- One generated email: **about $0.006 - $0.015**
- 100 generated emails: **about $0.60 - $1.50**
- Syncing or importing an existing opportunity: **$0 AI cost**
- Syncing or importing a new opportunity: queues one email-generation attempt
- Manual Generate, Regenerate, or Retry: queues another billable attempt

The API request itself is not billable AI work. Cost occurs when the separate
worker claims the job and calls Gemini and Bedrock. Replaying the same
idempotency key, repeatedly syncing an existing opportunity, and viewing or
editing an email do not queue another attempt. Failed provider calls can still
consume some tokens, but failed jobs are never retried automatically.

## Other actions

- Overview, opportunities list, search, email history, edit, approve:
  **$0 AI cost**
- Document upload: **S3 cost only**

## Tiny pilot example

- 100 chatbot answers + 100 email generations
- Estimated total AI cost: **about $0.80 - $2.10**

## Simple takeaway

- **Chatbot:** a few tenths of a cent per request
- **Email generation:** a little over half a cent per request
- **All UI-only actions:** no AI cost
