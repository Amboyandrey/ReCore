# ReCore

A multi-workspace chat platform that talks to any LLM, using API keys the workspace brings itself.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full architecture and phased build plan.

## Stack

- **API** — FastAPI (Python 3.12, async), SQLAlchemy 2, PostgreSQL, Redis, ARQ
- **Web** — Next.js 15 (App Router, TypeScript), Tailwind CSS
- **Infra** — Docker Compose for local dev

## Local development

```bash
docker compose -f infra/docker-compose.yml up --build
```

- API: http://localhost:8000 (docs at `/docs`)
- Web: http://localhost:3000

## Repository layout

```
apps/api/     FastAPI backend
apps/web/     Next.js frontend
infra/        Docker Compose, Dockerfiles
docs/         Architecture and planning docs
```

## Build status

Building in phases per `docs/ARCHITECTURE.md#12-build-order`. Currently: **Phase 1 — Foundation**.
