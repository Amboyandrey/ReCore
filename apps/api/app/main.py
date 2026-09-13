"""ASGI entrypoint — builds the FastAPI app, wires middleware, and mounts the versioned routers."""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.core.middleware import request_context_middleware, security_headers_middleware
from app.core.tracing import configure_tracing
from app.routers.v1 import (
    assistants,
    attachments,
    audit,
    auth,
    chat,
    credentials,
    flags,
    health,
    invitations,
    knowledge,
    members,
    memory,
    models,
    sources,
    tool_invocations,
    tools,
    usage,
    workspaces,
)

settings = get_settings()
configure_logging(settings.debug)
configure_tracing(settings.app_name)

app = FastAPI(title=settings.app_name, debug=settings.debug)

# Order matters: security headers wrap everything, then request context, then CORS closest to the app
app.middleware("http")(security_headers_middleware)
app.middleware("http")(request_context_middleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api/v1")
app.include_router(auth.router, prefix="/api/v1")
app.include_router(workspaces.router, prefix="/api/v1")
app.include_router(members.router, prefix="/api/v1")
app.include_router(invitations.workspace_router, prefix="/api/v1")
app.include_router(invitations.token_router, prefix="/api/v1")
app.include_router(credentials.router, prefix="/api/v1")
app.include_router(models.credentials_router, prefix="/api/v1")
app.include_router(models.models_router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")
app.include_router(flags.evaluate_router, prefix="/api/v1")
app.include_router(flags.admin_router, prefix="/api/v1")
app.include_router(attachments.router, prefix="/api/v1")
app.include_router(tools.router, prefix="/api/v1")
app.include_router(tool_invocations.router, prefix="/api/v1")
app.include_router(assistants.router, prefix="/api/v1")
app.include_router(memory.credential_router, prefix="/api/v1")
app.include_router(memory.memories_router, prefix="/api/v1")
app.include_router(knowledge.settings_router, prefix="/api/v1")
app.include_router(knowledge.connectors_router, prefix="/api/v1")
app.include_router(sources.router, prefix="/api/v1")
app.include_router(usage.router, prefix="/api/v1")
app.include_router(audit.router, prefix="/api/v1")


@app.exception_handler(AppError)
async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    """Turn any domain exception into a JSON error response at its mapped status code."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/")
async def root() -> dict[str, str]:
    """Confirm the API is reachable at its base path."""
    return {"service": settings.app_name, "status": "running"}
