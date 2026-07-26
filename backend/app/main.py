"""FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.dependencies.auth import get_current_user, require_csrf
from app.api.routes import agent_runs, auth, chat, documents, emails, leads
from app.config import get_settings
from app.db.database import check_database_schema
from app.services.auth_security import validate_web_auth_settings

settings = get_settings()
is_development = settings.app_env.strip().lower() == "development"


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Validate security configuration, connectivity, and migration state."""

    validate_web_auth_settings(settings)
    check_database_schema()
    yield


app = FastAPI(
    title="AI Marketing Outreach POC",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if is_development else None,
    redoc_url="/redoc" if is_development else None,
    openapi_url="/openapi.json" if is_development else None,
)

# Credentialed CORS accepts only explicitly configured frontend origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key"],
)


@app.middleware("http")
async def prevent_api_response_caching(request: Request, call_next):
    """Keep authentication and shared-workspace data out of HTTP caches."""

    response = await call_next(request)
    api_root = settings.api_prefix.rstrip("/") or "/"
    if request.url.path == api_root or request.url.path.startswith(
        f"{api_root}/"
    ):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(auth.router, prefix=settings.api_prefix)

protected_api = APIRouter(
    dependencies=[
        Depends(get_current_user),
        Depends(require_csrf),
    ],
)
protected_api.include_router(leads.router)
protected_api.include_router(documents.router)
protected_api.include_router(emails.router)
protected_api.include_router(agent_runs.router)
protected_api.include_router(chat.router)

if is_development:
    from app.api.routes import agent

    protected_api.include_router(agent.router)

app.include_router(protected_api, prefix=settings.api_prefix)
