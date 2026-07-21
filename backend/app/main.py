"""FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import agent_runs, chat, documents, emails, leads
from app.config import get_settings
from app.db.database import check_database_schema

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Validate database connectivity and migration state at startup."""

    check_database_schema()
    yield


app = FastAPI(
    title="AI Marketing Outreach POC",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS: explicit local origins so the browser accepts the response headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(leads.router, prefix=settings.api_prefix)
app.include_router(documents.router, prefix=settings.api_prefix)
app.include_router(emails.router, prefix=settings.api_prefix)
app.include_router(agent_runs.router, prefix=settings.api_prefix)
app.include_router(chat.router, prefix=settings.api_prefix)

if settings.app_env.strip().lower() == "development":
    from app.api.routes import agent

    app.include_router(agent.router, prefix=settings.api_prefix)
