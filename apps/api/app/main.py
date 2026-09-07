"""ASGI entrypoint — builds the FastAPI app, wires middleware, and mounts the versioned routers."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.middleware import request_context_middleware, security_headers_middleware
from app.routers.v1 import health

settings = get_settings()
configure_logging(settings.debug)

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


@app.get("/")
async def root() -> dict[str, str]:
    """Confirm the API is reachable at its base path."""
    return {"service": settings.app_name, "status": "running"}
