# ReCore — Architecture & Build Plan

A multi-workspace chat platform that talks to any LLM, using API keys the workspace brings itself.

Rendered version: see the published artifact link shared in the build conversation. This file is the durable, in-repo copy.

## 1. Decisions already made

**Stack — FastAPI backend, Next.js frontend.** Two apps in one repo. Python keeps us close to
the LLM ecosystem; Next.js owns rendering and nothing else. The API is the only thing that
touches the database, so a second client (mobile, CLI) costs nothing later.

**Auth — owned, not Keycloak.** Keycloak's groups and roles don't map onto per-workspace
membership, so workspace RBAC would live in our database regardless — split across two systems
instead of one. We own it, and put an `IdentityProvider` seam behind login so OIDC slots in
later without touching the session layer.

**LLM scope — multi-provider, streaming, metered.** Anthropic, OpenAI, Google, plus any
OpenAI-compatible base URL — which covers Ollama, Groq, OpenRouter and vLLM for free. Plus
plain file attachments: extract text, prepend to context. No embeddings, no vector store, no
retrieval.

**Sessions over JWTs.** ReCore is browser-first, so the access token is an opaque session id in
an `httpOnly; Secure; SameSite=Lax` cookie, resolved against Redis. Revocation is a `DEL` — no
refresh-token dance, no token stuck valid for fifteen minutes after someone is kicked out of a
workspace. Programmatic access gets a separate mechanism: workspace-scoped API keys, hashed in
Postgres, shown once.

## 2. System shape

Every request enters through the same four gates. Nothing reaches a repository without a
resolved workspace and role.

```
Client   → Next.js RSC shell, TanStack Query, SSE reader, FlagsProvider
Edge     → CORS + security headers, request id / structlog, rate limit (Redis)
Guards   → current_user → workspace_ctx → require_role → flag gate
Services → auth, workspaces, credentials, chat, flags, usage, audit
Adapters → Anthropic, OpenAI, Google, OpenAI-compatible
State    → Postgres (system of record), Redis (sessions/limits/streams/cache), object store
```

### Repository layout

```
recore/
├── apps/api/              # FastAPI
│   ├── app/core/            # settings, security, redis, db, logging
│   ├── app/models/          # SQLAlchemy declarative
│   ├── app/schemas/         # Pydantic v2 in/out
│   ├── app/deps/            # current_user, workspace_ctx, require_role
│   ├── app/services/        # business logic, no FastAPI imports
│   ├── app/providers/       # llm adapters behind one protocol
│   ├── app/routers/v1/      # thin — parse, delegate, serialize
│   ├── app/workers/         # ARQ tasks
│   └── migrations/          # alembic
├── apps/web/              # Next.js 15
│   ├── app/(auth)/          # login, signup, invite
│   ├── app/w/[slug]/        # chat + settings, workspace-scoped
│   ├── app/admin/           # flags, users — superuser only
│   ├── components/ui/       # shadcn primitives
│   └── lib/api/             # typed client generated from OpenAPI
├── packages/              # shared: openapi types, eslint, tsconfig
└── infra/                 # compose, Dockerfiles, seed script
```

The rule that keeps this clean: **routers never contain logic and services never import
FastAPI.** A service takes a context object and typed arguments, so every service is testable
without an HTTP client and reusable from an ARQ worker.

## 3. Data model

Fourteen tables. Every tenant-scoped one carries `workspace_id` — no exceptions, even where
it's derivable through a join, because derivable means forgettable.

| Table | Holds | Notable columns |
|---|---|---|
| users | Identity, global | email citext unique, password_hash, is_superuser, last_login_at |
| workspaces | The tenant boundary | slug unique, owner_id, settings jsonb, deleted_at |
| workspace_members | Who's in, at what role | pk(workspace_id, user_id), role enum, joined_at |
| invitations | Pending seats | email, role, token_hash, expires_at, accepted_at |
| api_keys | Programmatic access to ReCore | workspace_id, key_hash, prefix, scopes, last_used_at |
| provider_credentials | The keys users register | provider, label, base_url, ciphertext, nonce, last4 |
| models | Enabled models per workspace | credential_id, provider_model_id, context_window, cost_per_mtok in/out |
| conversations | Chat threads | workspace_id, user_id, title, model_id, system_prompt |
| messages | Turns | role, content, tokens_in/out, cost_usd, finish_reason, error |
| attachments | Uploaded files + extracted text | mime, size, storage_key, extracted_text, extract_status |
| feature_flags | Flag definitions | key unique, type enum, default_value jsonb, archived_at |
| flag_overrides | Targeting rules | unique(flag_id, scope, scope_id), value jsonb |
| usage_events | Append-only metering | model_id, tokens, cost_usd, latency_ms, created_at |
| audit_logs | Who did what | actor_id, action, target_type, target_id, ip, metadata jsonb |

Indexes that matter from day one: `(workspace_id, updated_at desc)` on conversations,
`(conversation_id, created_at)` on messages, `(workspace_id, created_at)` on usage_events.
Pagination is cursor-based on those composite keys — `OFFSET` never appears in the codebase.

## 4. Tenancy, enforced in one place

Multi-tenant bugs are cross-tenant data leaks. The defense is that isolation is structural, not
remembered.

1. **Workspace lives in the path**, never the body: `/api/v1/workspaces/{workspace_id}/conversations`.
   A body field can't silently override what the URL authorized.
2. **One dependency chain resolves it.** `current_user` → `workspace_ctx` (loads membership or
   404s — not 403, which would confirm the workspace exists) → `require_role(Role.ADMIN)`.
3. **Repositories take the context, not an id.** A scoped-session helper injects
   `.where(Model.workspace_id == ctx.workspace_id)`, so the filter can't be left off.
4. **Roles are ordered** — `viewer < member < admin < owner` — so `require_role` is one
   comparison rather than a set of permissions to keep in sync.
5. **Postgres RLS as the backstop** (phase 08): the transaction sets `app.workspace_id` and
   policies enforce it at the database. If a query ever escapes the helper, it returns zero
   rows instead of someone else's.

```python
@router.get("/workspaces/{workspace_id}/conversations")
async def list_conversations(
    ctx: WorkspaceCtx = Depends(require_role(Role.VIEWER)),
    cursor: str | None = None,
) -> Page[ConversationOut]:
    """Return the caller's conversations, newest first, scoped to their workspace."""
    return await conversations.list_for_user(ctx, cursor=cursor)
```

## 5. Handling other people's API keys

Users hand us live billing credentials. This is the part of the platform most worth getting
visibly right.

- **Envelope encryption.** A random data key per credential encrypts the secret with
  AES-256-GCM; the data key is wrapped by a master key from the environment (KMS in
  production). Rotating the master key rewraps data keys without touching ciphertext.
- **Store the shape, not the secret.** Ciphertext, nonce, provider, label, and `last4`. The
  plaintext key is never a field on any response model — the read schema physically cannot
  carry it.
- **Validate on save.** A cheap list-models call proves the key works before it's stored, and
  the failure message tells the user which provider rejected it.
- **Decrypt late, hold briefly.** Only the provider service decrypts, at request time, into an
  in-process TTL cache. Decrypted material never touches Redis, logs, or an error trace.
- **SSRF guard on custom base URLs.** "Any OpenAI-compatible endpoint" is a request-forgery
  primitive if unguarded: resolve the hostname, reject loopback, link-local, and RFC1918
  ranges, then connect to the resolved address so DNS can't rebind between check and use.
- **Every touch is audited.** Create, disable, delete, and validation failures all write an
  `audit_logs` row with actor and IP.

## 6. The LLM layer

One protocol, four adapters, one normalized chunk type. Adding a fifth provider should touch
exactly one new file.

```python
class LLMProvider(Protocol):
    async def stream(self, req: ChatRequest) -> AsyncIterator[Chunk]: ...
    async def list_models(self) -> list[ModelInfo]: ...
    async def validate(self) -> CredentialCheck: ...

# Chunk is a closed union — adapters translate, callers never branch on provider
Chunk = TextDelta | ToolCall | Usage | ProviderError | Done
```

The chat pipeline for one message: persist the user turn → resolve model and credential → open
the provider stream → relay `TextDelta`s over SSE while appending to a Redis Stream → on
`Done`, write the assistant message and a `usage_event` in a single transaction, with cost
computed from the model's price columns.

What makes it survive contact with reality:

- **Resumable streams.** Because deltas go to a Redis Stream keyed by generation id, a
  reconnecting tab replays from its last offset instead of losing a half-written answer.
- **Stop generation** is a pub/sub message on that generation id, so it works even when the
  reader and the producer are different API pods.
- **Idempotency keys** on send, held in Redis, so a retried request resumes the existing
  generation instead of billing the user twice.
- **Circuit breaker per credential.** Consecutive failures open a breaker in Redis; requests
  fail fast with a clear message rather than hanging for the provider's timeout.
- **Attachments** extract to text in an ARQ worker (pdf, docx, csv, md, txt), capped by size
  and token budget, prepended to the turn with a visible "from *filename*" marker so the
  model's context is never a mystery to the user.

## 7. What Redis is actually doing

Eight distinct jobs. Each one is a reason Postgres alone would be the wrong tool, not a cache
bolted on for the sake of it.

| Key shape | Job |
|---|---|
| `session:{sid}` | Sessions — user id, workspace, CSRF token, sliding TTL. Logout is one delete. |
| `rl:{scope}:{window}` | Rate limiting — sliding window per user/workspace/credential, atomic Lua script. |
| `gen:{id}` (stream) | Live generations — deltas land here so a reconnecting client resumes mid-answer. |
| `gen:{id}:ctl` (pubsub) | Stop signals — cancellation crosses pods. |
| `flags:{ws}` + invalidate | Flag snapshots — cached 30s, dropped instantly by pub/sub on edit. |
| `idem:{key}` | Idempotency — a retried send attaches to the in-flight generation. |
| `arq:queue` | Background work — titles, extraction, invite email, nightly rollups. |
| `cb:{cred}` / `models:{cred}` | Breakers & catalogs — provider health and model lists, cached. |

## 8. Feature flags

Resolution is strictly ordered, first match wins. Server-side only — the client receives
resolved values, never rules.

1. **User override** — an explicit value pinned to one user.
2. **Workspace override** — the tier that makes this a platform feature rather than a config file.
3. **Percentage rollout** — deterministic `hash(flag_key + ":" + workspace_id) % 100`, so a
   workspace never flickers between variants across requests.
4. **Default value** — from the flag definition.

A single `/flags/evaluate` call resolves the whole map for the current user and workspace; a
server component reads it once per navigation and hands it to `<FlagsProvider>`, so
`useFlag("attachments")` is a synchronous read with no waterfall. On the API side,
`Depends(flag_gate("attachments"))` guards the routes themselves — a flag that only hides UI
isn't a flag, it's a CSS rule.

Give every provider a killswitch flag. Flipping `provider.openai` off in the admin UI and
watching in-flight chat degrade gracefully — model picker greys out, existing conversations
stay readable — is the single most convincing thirty seconds in the demo.

## 9. Interface

Three columns, no chrome, keyboard-first. The UI's job is to disappear so the platform shows.

| Route | Purpose | Gate |
|---|---|---|
| `/login` · `/signup` · `/invite/[token]` | Enter, or accept a seat | public |
| `/w/[slug]/c/[id]` | Chat — model picker, streaming, stop, attachments | viewer |
| `/w/[slug]/settings/members` | Invite, change role, remove | admin |
| `/w/[slug]/settings/providers` | Register keys, validate, enable models | admin |
| `/w/[slug]/settings/usage` | Tokens and spend by model, member, day | admin |
| `/w/[slug]/settings/audit` | Audit log, filterable | owner |
| `/admin/flags` · `/admin/users` | Flag definitions, global user list | superuser |

- **Layout.** Narrow workspace rail, conversation list, message pane. The rail collapses under
  900px; the conversation list becomes a sheet.
- **Components.** shadcn/ui on Tailwind — accessible primitives we own the source of.
- **Loading.** Skeletons shaped like the content, never spinners. Streaming text is its own
  loading state.
- **Keyboard.** `⌘K` command palette, `⌘↵` send, `Esc` stop.
- **Errors are actionable.** "OpenAI rejected this key — check it in Settings → Providers", not
  "Request failed".

## 10. Security checklist

Each item lands in a specific phase; none are deferred to "later".

- Argon2id password hashing, tuned params, rehash on login when params change
- httpOnly / Secure / SameSite=Lax session cookie, double-submit CSRF token on mutations
- Auth rate limits per IP and per account, uniform timing and generic errors
- Pydantic strict mode on every input; SQLAlchemy expressions only, no string SQL
- Signed, single-use invite tokens stored hashed, expiring in 72 hours
- SSRF allowlist on custom provider base URLs, resolve-then-connect
- Upload guard — extension and magic-byte allowlist, size cap, no inline serving
- CSP, HSTS, X-Content-Type-Options, Referrer-Policy from one middleware
- Redaction filter in structlog so a key can't reach a log line or Sentry trace
- Audit log on every privileged action, immutable and workspace-visible
- Row-level security in Postgres as the backstop under application scoping
- CI gates — pip-audit, bandit, npm audit, secret scanning on every PR

## 11. Built to scale out

Not premature optimization — just the handful of choices that are expensive to reverse later.

- **The API holds no state.** Sessions, locks, breakers and live generations all live in Redis,
  so pods are interchangeable and a rolling deploy doesn't drop a stream.
- **Async end to end** — asyncpg, httpx, redis.asyncio. Streaming is IO-bound, so one worker
  holds hundreds of concurrent generations; pool sizes are what you tune, not worker count.
- **Workers scale separately.** File extraction is CPU-heavy and bursty; it shouldn't compete
  with chat for the same process.
- **Append-only metering.** `usage_events` is never updated, rolled up nightly into daily
  aggregates, and partitions by month the day it gets slow.
- **Cursor pagination everywhere**, on the composite indexes above — deep pages cost the same
  as the first one.
- **Migrations are additive.** Add-then-backfill-then-drop across releases, so a deploy never
  requires downtime.

## 12. Build order

Eight phases, each ending in something runnable. If you stop after any of them, what exists
still works.

1. **Foundation** — compose stack (Postgres, Redis, api, web), FastAPI skeleton with settings
   and health checks, Alembic wired, Next.js shell with the design tokens, CI running lint and
   tests. *Demo: `docker compose up` and both apps answer.*
2. **Identity** — users, argon2 hashing, Redis sessions, CSRF, auth rate limits, login/signup
   screens, `/me`. *Demo: sign up, sign in, session survives restart, logout kills it everywhere.*
3. **Workspaces & roles** — workspace CRUD, membership, the dependency chain, invitations by
   email token, members settings UI, workspace switcher. *Demo: invite a second account, demote
   it, watch a route 403.*
4. **LLM registry** — envelope encryption, credential CRUD, the four adapters, live validation,
   model catalog with pricing, providers settings UI. *Demo: paste a key, see it validate,
   enable three models.*
5. **Chat** — conversations and messages, SSE streaming, Redis stream resumption, stop,
   idempotent send, markdown/code rendering, auto-titling. *Demo: chat against two providers in
   one thread, refresh mid-stream, it resumes.*
6. **Flags & attachments** — flag engine, Redis cache with pub/sub invalidation, admin UI,
   route gates — with file upload and text extraction shipped behind a flag. *Demo: toggle a
   provider killswitch and watch the UI degrade cleanly.*
7. **Metering** — usage events, cost computation, nightly rollups, usage dashboard, audit log
   viewer, OpenTelemetry traces through the provider call. *Demo: spend by model and member, a
   trace of one generation.*
8. **Hardening** — RLS policies, security headers, load test on concurrent streams, seed script
   with a demo workspace, README with the architecture diagram, one Playwright happy path.
   *Demo: a fresh clone to a populated, working platform in one command.*

## 13. Code conventions

Decided once here so they never need re-deciding in review.

- **One explanatory line per function.** A single-line docstring in Python, a single-line
  comment above TypeScript functions. It states *why* or *what it guarantees* — not a
  restatement of the signature. `"""Return only conversations the caller can read."""` earns
  its line; `"""List conversations."""` doesn't.
- **Type everything.** `mypy --strict` on the API, `strict` in tsconfig, and the web client's
  types generated from the OpenAPI schema so a backend change breaks the frontend build rather
  than production.
- **Errors are typed and mapped once.** Services raise domain exceptions
  (`WorkspaceNotFound`, `CredentialRejected`); a single exception handler turns them into
  responses. No `HTTPException` below the router layer.
- **Tests where they pay.** `pytest` + `httpx.AsyncClient` against a real Postgres and Redis in
  compose; a `FakeProvider` adapter makes chat deterministic. Non-negotiable coverage: the
  tenancy guards, the flag resolution order, and credential encryption round-trips.
- **Tooling.** `uv` and `ruff` on Python, `pnpm` and `biome` on the web, both enforced by
  pre-commit and repeated in CI.
- **Commits scoped to a phase**, migrations never edited after merge.
