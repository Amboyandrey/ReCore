# ReCore — Architecture

A multi-workspace chat platform that talks to any LLM, using API keys the workspace brings itself.

This describes the system **as built**. Everything below exists in the repository and runs; where
something was considered and deliberately left out, it's in §16 rather than implied to be coming.
The reasoning is kept alongside the description, because the *why* is the part that stops a later
change from quietly undoing a decision.

## 1. The decisions the rest of this rests on

**Stack — FastAPI backend, Next.js frontend.** Two apps in one repo. Python keeps us close to the
LLM ecosystem; Next.js owns rendering and nothing else. The API is the only thing that touches the
database, so a second client (mobile, CLI) would cost nothing later.

**Auth — owned, not Keycloak.** Keycloak's groups and roles don't map onto per-workspace
membership, so workspace RBAC would live in our database regardless — split across two systems
instead of one. We own it.

**Sessions, not JWTs.** ReCore is browser-first, so the access token is an opaque session id in an
`httpOnly; Secure; SameSite=Lax` cookie, resolved against Redis with a 14-day sliding TTL.
Revocation is a `DEL` — no refresh-token dance, no token stuck valid for fifteen minutes after
someone is removed from a workspace.

**Multi-provider, streaming, metered.** Anthropic, OpenAI, Google, and any OpenAI-compatible base
URL — which covers Ollama, Groq, OpenRouter and vLLM for free. Files are extracted to text (or
passed through as images to vision models); there are no embeddings, no vector store, no retrieval.

**Tools are first-class rows, not a hardcoded list.** The built-in web search is a row in the same
`tools` table a workspace's own HTTP tools live in — no virtual-vs-real split to reason about, and
no separate seeding step.

## 2. System shape

Every request enters through the same gates. Nothing reaches the database without a resolved
workspace and role.

```
Client   → Next.js App Router (React 19), per-domain fetch clients, SSE reader, flag context
Edge     → CORS + security headers, request id / structlog, rate limits (Redis)
Guards   → current_user → workspace_ctx → require_role → (optional) flag gate
Services → auth, sessions, workspaces, members, invitations, credentials, models, chat,
           generations, attachments, tools, assistants, flags, usage, audit, rate_limit
Adapters → Anthropic, Google, OpenAI-compatible (also serves OpenAI), Fake (tests)
Executors→ built-in web search (Tavily), third-party HTTP tools
State    → Postgres (system of record), Redis (sessions/limits/streams/cache), local disk (uploads)
```

### Repository layout

```
ReCore/
├── apps/api/                 # FastAPI, Python 3.12
│   ├── app/core/               # settings, db, redis, crypto, ssrf, errors, logging, tracing, middleware
│   ├── app/models/             # SQLAlchemy 2.0 declarative
│   ├── app/schemas/            # Pydantic v2 in/out
│   ├── app/deps/               # current_user, workspace_ctx, require_role, flag_gate
│   ├── app/services/           # business logic, no FastAPI imports
│   ├── app/providers/          # LLM adapters behind one protocol
│   ├── app/tools/              # tool executors + the dispatcher that bounds them
│   ├── app/routers/v1/         # thin — parse, delegate, serialize
│   ├── app/scripts/            # seed, load test
│   ├── migrations/             # Alembic, one revision per schema change
│   └── tests/                  # pytest, 290 tests against real Postgres/Redis
├── apps/web/                 # Next.js 16 (App Router), React 19, Tailwind 4
│   ├── app/(auth)/             # login, signup
│   ├── app/w/[slug]/           # chat + settings, workspace-scoped
│   ├── app/admin/flags/        # flag administration — superuser only
│   ├── lib/                    # one typed client per domain + React context
│   └── components/             # shared UI
├── infra/                    # docker-compose.yml, Dockerfiles for both apps
└── docs/                     # this file
```

The rule that keeps this clean: **routers never contain logic and services never import FastAPI.**
A service takes plain typed arguments, so every service is testable without an HTTP client — which
is exactly how the chat pipeline's background task reuses them.

## 3. Data model

Seventeen tables. Every tenant-scoped one carries `workspace_id` — even where it's derivable
through a join, because derivable means forgettable.

| Table | Holds | Notable columns |
|---|---|---|
| users | Identity, global | email unique, password_hash, is_superuser |
| workspaces | The tenant boundary | slug unique, owner_id, settings jsonb, deleted_at |
| workspace_members | Who's in, at what role | pk(workspace_id, user_id), role enum, joined_at |
| invitations | Pending seats | email, role, token_hash, expires_at, accepted_at |
| provider_credentials | The keys users register | provider, label, base_url, ciphertext, nonce, wrapped_key, last4 |
| models | Enabled models per workspace | credential_id, provider_model_id, cost_per_mtok in/out, supports_vision |
| conversations | Chat threads | user_id, model_id, assistant_id, system_prompt, shared |
| messages | Turns | role, content, tokens_in/out, cost_usd, finish_reason, error |
| attachments | Uploaded files | mime, size, storage_key, extracted_text, extract_status, message_id |
| tools | What a chat may call | name unique per workspace, kind enum, parameters jsonb, enabled, method/url/secret_header, ciphertext/nonce/wrapped_key |
| tool_invocations | One row per call actually made | message_id, tool_id (nullable), name, arguments, result, status, latency_ms |
| assistants | Saved instructions + optional model | name, instructions, model_id (nullable), created_by |
| assistant_tools | Which tools an assistant may use | pk(assistant_id, tool_id) |
| feature_flags | Flag definitions | key unique, type enum, default_value jsonb, rollout_percentage, archived_at |
| flag_overrides | Targeting rules | unique(flag_id, scope, scope_id), value jsonb |
| usage_events | Append-only metering | model_id, tokens, cost_usd, latency_ms, created_at |
| audit_logs | Who did what | actor_id, action, target_type, target_id, ip, metadata jsonb |

Two denormalizations are deliberate. `tool_invocations.name` is copied from the tool it came from
and kept even if that tool is later renamed or deleted (`tool_id` goes null via `ON DELETE SET
NULL`), so a transcript stays legible regardless of what happens to the tool afterwards. The same
logic applies to `conversations.assistant_id` and `assistants.model_id`: deleting the thing pointed
at degrades the pointer to null rather than breaking or cascading away real history.

## 4. Tenancy, enforced in one place

Multi-tenant bugs are cross-tenant data leaks. The defence is that isolation is structural, not
remembered.

1. **Workspace lives in the path**, never the body:
   `/api/v1/workspaces/{workspace_id}/conversations`. A body field can't silently override what
   the URL authorized.
2. **One dependency chain resolves it.** `get_current_user` → `get_workspace_ctx` (loads
   membership or 404s — not 403, which would confirm the workspace exists) → `require_role(...)`.
3. **Roles are ordered** — `viewer < member < admin < owner` — so `require_role` is one comparison
   rather than a permission set to keep in sync.
4. **Postgres RLS is the backstop.** `get_workspace_ctx` sets `app.workspace_id` for the
   transaction, and policies enforce it at the database under `recore_app`, a dedicated
   low-privilege role. If a query ever forgets its `WHERE`, it returns zero rows instead of
   someone else's.

RLS covers `provider_credentials`, `models`, `conversations`, `attachments`, `usage_events`,
`tools`, `tool_invocations`, `assistants`, plus `messages` and `assistant_tools` through their
parent. Four tables are excluded on purpose, each for a reason found by running this against a real
database rather than reasoning about it:

- **`workspaces` / `workspace_members`** — `GET /workspaces` legitimately spans every workspace a
  user belongs to, so a single-`app.workspace_id`-per-request policy would break it.
- **`audit_logs`** — some of what it records (creating a workspace, accepting an invitation, every
  flag-admin action) happens on routes with no single workspace in scope yet, and SQLAlchemy's
  `INSERT ... RETURNING` makes Postgres check the row against the *select* policy before it can be
  returned.
- **`invitations`** — previewing or accepting an invite happens before the caller is a member of
  (or even knows) any workspace; authorization there is possession of an unguessable token, which
  RLS's workspace-id-equality model can't express.

Both exclusions are compensated at the application layer, and the RLS migration's own docstring
carries the full reasoning — read it before touching those policies.

## 5. Handling other people's API keys

Users hand us live billing credentials. This is the part of the platform most worth getting
visibly right.

- **Envelope encryption.** A random data key per credential encrypts the secret with AES-256-GCM;
  the data key is wrapped by a master key from the environment. Rotating the master key rewraps
  data keys without touching ciphertext.
- **Store the shape, not the secret.** Ciphertext, nonce, wrapped key, provider, label, and
  `last4`. The plaintext key is not a field on any response schema — the read model physically
  cannot carry it.
- **Validate on save.** A cheap list-models call proves the key works before it's stored, and the
  failure message names the provider that rejected it.
- **Decrypt late.** Only the point of use decrypts, at request time. Decrypted material never
  touches Redis, logs, or an error trace.
- **SSRF guard on custom base URLs.** "Any OpenAI-compatible endpoint" is a request-forgery
  primitive if unguarded: `assert_safe_base_url` resolves the hostname and rejects loopback,
  link-local, private and reserved ranges. The same guard protects third-party tool URLs.
- **Every touch is audited.** Create, delete, and validation failures all write an `audit_logs` row
  with actor and IP.

The same envelope-encryption columns and code path hold a workspace's Tavily key and each HTTP
tool's secret header value — one mechanism for every secret the platform stores.

## 6. The LLM layer

One protocol, three adapters, one normalized chunk type. Adding a provider touches one new file.

```python
class LLMProvider(Protocol):
    async def validate(self) -> CredentialCheck: ...
    async def list_models(self) -> list[ModelInfo]: ...
    def stream(
        self, *, model: str, messages: Sequence[ChatMessage],
        max_tokens: int, tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[Chunk]: ...

# Chunk is a closed union — adapters translate, callers never branch on provider
Chunk = TextDelta | ToolCallRequest | Usage | Done | StreamError
```

`Provider` has four values but there are three adapter files: OpenAI and "OpenAI-compatible" share
one, differing only in whether a base URL is supplied. A `FakeProvider` implements the same
protocol, which is what makes the whole chat pipeline testable without a network.

**Tool calling is normalized the same way the chunk union is.** Each provider expresses it
differently — OpenAI streams `tool_calls` deltas keyed by index; Anthropic streams a
`content_block_start` of type `tool_use` followed by `input_json_delta` fragments; Google returns a
whole `functionCall` part with no id of its own, so one is synthesized client-side. Each adapter
accumulates its own wire format internally and emits a single assembled `ToolCallRequest`. Feeding
a result back differs too: OpenAI takes a `tool` role, while Anthropic and Google take a `user`
turn carrying a `tool_result` / `functionResponse` part — Google matching by name rather than id.

One rule holds all of this together: **when no tools are offered, every adapter's payload is
byte-identical to what it sent before tool support existed.** Some OpenAI-compatible endpoints
reject an unexpected `tools` field outright, so the field only appears when it's actually used.

## 7. The chat pipeline

Sending one message: persist the user turn → resolve model, credential and (if any) assistant →
start a background task → return an SSE response that tails that task's Redis stream.

The generation runs **independently of the request that started it**. It owns its own database
session and Redis client, so a client refreshing mid-reply loses nothing: the background task keeps
writing to Redis and, at the end, to Postgres, whether or not anyone is watching.

What makes it survive contact with reality:

- **Resumable streams.** Deltas go to a Redis Stream keyed by generation id, so a reconnecting tab
  replays from an offset instead of losing a half-written answer. A page reload has no surviving
  `Last-Event-ID`, so the client asks an active-generation endpoint what to resume and replays from
  the start.
- **Stop** is a pub/sub message on that generation id, so it works even when the reader and the
  producer are different API processes. It's checked between provider chunks *and* between agent-
  loop iterations, since tool execution itself can take seconds.
- **Idempotency keys** on send, held in Redis, so a retried request attaches to the existing
  generation instead of billing twice.
- **An image budget on replayed history.** Unlike text, images live in the assembled history as
  real bytes and get re-sent at real cost on every later turn. History is capped at 10 images /
  20 MB, newest first — a screenshot from turn one stops being paid for by turn five.
- **Usage is written once per generation**, summed across every agent-loop iteration, with cost
  computed from the model's price columns. A generation that errored outright meters nothing; one
  that was stopped mid-reply still used real tokens and still bills.

## 8. Tools and the agent loop

`_run_generation` is a bounded loop, not a single pass: stream the provider; if it asks for tools,
execute them, append the assistant's request and each result to the working history, and stream
again. `MAX_TOOL_ITERATIONS = 5` — a model that keeps calling tools forever is a billing runaway,
not a feature, and hitting the cap ends the generation with an error rather than looping.

Two `tool_call` / `tool_result` SSE events bracket every call, so the UI shows "searching the
web…" rather than a stall. Each call is also persisted as a `tool_invocation` tied to the final
assistant message — including when the generation later fails, since a run that broke on its third
round trip may still have made two real calls worth recording. The chat page replays those stored
rows on reload, so a tool call is as visible in history as it was live.

**Execution is bounded and never fatal.** `execute_tool` runs a call under a 15-second timeout and
truncates any result to 8,000 characters. A tool that times out, throws, or returns something huge
becomes a normal-shaped error result fed back to the model — one bad tool cannot take a generation
down.

Two kinds of tool exist behind that one dispatcher:

- **Built-in web search**, backed by Tavily — chosen because it returns LLM-ready text rather than
  raw HTML to parse. Its key is stored envelope-encrypted on the tool's own row.
- **Third-party HTTP tools**, registered by any workspace member: name, description, JSON Schema
  parameters, method, URL, and an optional secret sent as a request header. The model's arguments
  become the request body (POST/PUT/PATCH) or query string (GET/DELETE); the response is truncated
  before it ever reaches the model, and a non-2xx comes back as a tool error rather than killing
  the generation.

An HTTP tool's URL is checked against the SSRF guard at registration **and again immediately before
every call** — a DNS answer can change in between, and the second check is the one that runs at the
moment a request actually goes out.

Tool names are unique per workspace and constrained to `^[a-zA-Z0-9_-]{1,64}$` — OpenAI's own
function-name rule, adopted as the strictest common denominator across all three providers, since
the name is what the model calls the function by.

## 9. Assistants

An assistant is a saved name, **required** instructions, an optional preferred model, and an
optional set of tools. A conversation may point at one.

Everything about it is **resolved live at send time, never snapshotted onto the conversation**:

- its instructions become the system turn, replacing the conversation's own `system_prompt`;
- its assigned, still-enabled tools become the *entire* offered tool set — not an addition to the
  workspace's enabled tools, but a replacement for them.

So editing an assistant reaches every conversation already using it on their very next send, and a
tool that's disabled after being assigned simply drops out of what's offered without needing its
assignment cleaned up. A conversation with no assistant behaves exactly as it did before assistants
existed: its own `system_prompt`, and every enabled tool in the workspace.

The one thing that is *not* resolved live is the model. `assistants.model_id` is advisory — the
composer pre-fills its model picker from it when starting a new chat, but the conversation's own
`model_id` is what a generation uses, the same as it always has.

## 10. Attachments

Upload is synchronous and gated by the `attachments` flag. Text-bearing formats (PDF, DOCX, PPTX,
XLSX, plain text) are extracted on upload and folded into the turn's context with a visible
`[Attached file: name]` marker, so the model's context is never a mystery to the user. Images are
normalized (HEIC/HEIF included, via pillow-heif) and passed through as native image parts.

Two details worth keeping:

- **The stored message holds only what the user typed.** Extracted text and image parts are
  assembled at provider-call time, so the transcript shows the message, not the machinery.
- **History is rebuilt from attachments, not from message text.** A version that replayed straight
  from `content` would silently forget every attachment the moment the conversation moved past the
  turn it was attached to — a bug this codebase hit three separate times before the join was made
  the source of truth.

Sending an image to a model not marked `supports_vision` is rejected pre-flight, before anything is
persisted or a generation starts.

Files live on a local disk volume, keyed `{workspace_id}/{attachment_id}`. An object store would be
a drop-in replacement for that one module; nothing else knows where bytes live.

## 11. What Redis is actually doing

Seven distinct jobs. Each is a reason Postgres alone would be the wrong tool, not a cache bolted on.

| Key shape | Job |
|---|---|
| `session:{sid}` | Sessions — user id, CSRF token, 14-day sliding TTL. Logout is one delete. |
| `rl:{scope}:{id}` | Rate limiting — login (per IP and per account), signup, invites, credential validation. |
| `gen:{id}` (stream) | Live generations — deltas, tool events and terminal events, so a reconnecting client resumes mid-answer. |
| `gen:{id}:stop` (pubsub) | Stop signals — cancellation crosses processes. |
| `conv:{id}:active_generation` | What a freshly loaded page should resume, since a reload keeps no `Last-Event-ID`. |
| `flags:{ws}` + `flags:invalidate` | Flag snapshots — cached 30s, dropped instantly by pub/sub on edit. |
| `idem:{key}` | Idempotency — a retried send attaches to the in-flight generation. |

## 12. Feature flags

Resolution is strictly ordered, first match wins. Server-side only — the client receives resolved
values, never rules.

1. **User override** — an explicit value pinned to one user.
2. **Workspace override** — the tier that makes this a platform feature rather than a config file.
3. **Percentage rollout** — deterministic `sha256(flag_key + ":" + workspace_id) % 100`, so a
   workspace never flickers between variants across requests.
4. **Default value** — from the flag definition.

Only the workspace-level result is cached; user overrides are rare enough to read live, so the
cache never has to know who's asking. A key with no definition resolves to `false` — gating a route
on a flag nobody created behaves like the feature is off, not broken.

`/flags/evaluate` resolves the whole map for the current user and workspace in one call. On the API
side `Depends(flag_gate("attachments"))` guards the routes themselves and **404s** rather than
403s, so a disabled feature is indistinguishable from a route that doesn't exist — a flag that only
hides UI isn't a flag, it's a CSS rule.

Every provider has a killswitch flag (`provider.anthropic`, `provider.openai`, …), checked at send
time: flipping one off degrades chat gracefully instead of failing at the adapter.

## 13. Interface

| Route | Purpose | Gate |
|---|---|---|
| `/login` · `/signup` · `/invite/[token]` | Enter, or accept a seat | public |
| `/w/[slug]` | Workspace home — conversations, settings links | viewer |
| `/w/[slug]/c/new` · `/w/[slug]/c/[id]` | Chat — model and assistant pickers, streaming, stop, attachments, tool activity | viewer |
| `/w/[slug]/settings/members` | Invite, change role, remove | admin |
| `/w/[slug]/settings/providers` | Register keys, validate, enable models, set pricing | admin |
| `/w/[slug]/settings/tools` | Web search key; register/edit/delete HTTP tools | any member, `tools` flag |
| `/w/[slug]/settings/assistants` | Create/edit assistants — instructions, model, tool checkboxes | any member |
| `/w/[slug]/settings/usage` | Tokens and spend by model, member, day | admin |
| `/w/[slug]/settings/audit` | Audit log, cursor-paginated | owner |
| `/admin/flags` | Flag definitions and overrides | superuser |

`is_superuser` defaults to false and nothing in the application sets it — promoting the first
account is a deliberate manual step against the database (see the README), so a fresh deployment
has no self-service path to platform-wide flag administration.

Tools and assistants are open to **any member**, not just admins — the same floor sending a message
has, since neither is usable anywhere but inside a chat. That cuts both ways and is stated plainly
in §15.

A conversation is **private to whoever started it** by default; its owner can share it with the
workspace, at which point anyone can read and write in it. Only the owner can un-share or delete
it — otherwise "shared" would be a one-way ratchet nobody but the first sharer could undo.

The UI is hand-written Tailwind against a small set of design tokens, with one typed fetch client
and React context per domain. There is no component library, no data-fetching library, and no
generated client; at this size those would be more indirection than leverage.

## 14. Security posture

Enforced today:

- Argon2id password hashing; opaque Redis-backed sessions, not JWTs
- Auth rate limits per IP and per account, generic errors that don't distinguish "no such user"
  from "wrong password"
- Pydantic v2 on every input; SQLAlchemy expressions only, no string SQL
- Signed, single-use invite tokens stored hashed, with an expiry
- Envelope-encrypted provider credentials and tool secrets; plaintext never on a response schema
- SSRF guard on every user-supplied URL — provider base URLs and tool endpoints, the latter
  re-checked immediately before each call
- Tool execution bounded by timeout and result size; a failing tool degrades one turn, not a run
- CSP, HSTS, X-Content-Type-Options and Referrer-Policy on every response from both apps
- Audit rows on every privileged action — invites, role changes, credential and tool and assistant
  lifecycle, flag edits
- Row-level security under a dedicated low-privilege Postgres role, as a backstop beneath
  application scoping

Known gaps, stated rather than buried:

- **CSRF is issued but only enforced on logout.** The double-submit cookie and `require_csrf`
  dependency both exist; wiring it across the remaining mutating routes is a small, unfinished job.
- **The SSRF guard has a TOCTOU window.** It resolves and checks, then a separate connection is
  made; closing it fully means pinning the resolved IP for the request that follows. The
  registration-time *and* call-time checks narrow it; they don't eliminate it.
- **Rate limiting covers auth, invites and credential validation — not chat.** A workspace's own
  provider bill is the current backpressure on message sending.
- **CI runs lint, type checks and tests**, not dependency or secret scanning.
- **Logs are structured JSON with no redaction processor.** Secrets stay out of them by discipline
  — nothing logs decrypted material, and credentials are decrypted only at the point of use — but
  there is no filter standing behind that discipline if a future log line gets it wrong.
- **Tool results don't survive into later turns.** Within one generation the model sees every tool
  result; on the next user turn it sees only its own final answer. That keeps replay honest across
  providers, which reject orphaned tool-call ids, but it is a real boundary.

## 15. The trade-off worth naming

Any workspace member can register a tool that sends the model's arguments to a URL of their
choosing, carrying a stored secret. That was a deliberate call, not an oversight: the alternative
made the feature useless to the people most likely to want it.

What contains it: the SSRF guard blocks internal addresses, every registration and edit is
audit-logged, tools are visible to the whole workspace, and secrets are write-only — nothing here
is covert. It's still worth revisiting if a workspace ever has members you wouldn't hand an
outbound webhook.

## 16. Deliberately not built

Named here so nobody hunts for them or assumes they're half-finished:

- **Programmatic API keys.** Sessions are the only credential; there is no `api_keys` table.
- **Background workers.** `app/workers/` is an empty package. Attachment extraction is synchronous
  (fast enough at these sizes) and generations run as in-process asyncio tasks. Conversation titles
  are a heuristic from the first message, not an LLM call, precisely because that *would* need a
  worker.
- **Usage rollups.** `usage_events` is aggregated directly on read. A rollup table is the answer
  when that gets slow, and adding one later changes nothing above the service.
- **Circuit breakers per credential**, a command palette, and a shared `packages/` workspace — all
  in earlier plans, none built.
- **Embeddings, vector storage, retrieval.** Out of scope by design, not by omission.

## 17. Conventions

- **One explanatory line per function.** It states *why*, or what it guarantees — not a restatement
  of the signature. `"""Return only conversations the caller can read."""` earns its line;
  `"""List conversations."""` doesn't. Non-obvious decisions get the reason in a comment at the
  point where someone would otherwise "fix" them.
- **Type everything.** `mypy --strict` on the API, `strict` in tsconfig.
- **Errors are typed and mapped once.** Services raise domain exceptions (`WorkspaceNotFound`,
  `ToolNotFound`, `AssistantNotFound`); one handler turns them into responses. No `HTTPException`
  below the router layer.
- **Partial updates are `changes`-dict shaped.** An update service takes the dict a Pydantic model
  produced with `exclude_unset=True` and checks `"field" in changes`, so "not sent" stays
  distinguishable from "explicitly set to null" — the difference between leaving an assistant
  attached and clearing it.
- **Migrations are never edited after merge**, and every one is verified with a full
  `upgrade → downgrade → upgrade` cycle against a real database before it ships.

## 18. Testing

290 pytest tests run against a real Postgres and Redis — never mocks of them. `conftest.py` forces
a separate `_test`-suffixed database and Redis index before any app code loads, so running the
suite can't touch data a dev stack is using. `FakeProvider` makes the whole chat pipeline
deterministic without a network.

Where coverage is deliberately dense: the tenancy guards, flag resolution order, credential
encryption round-trips, every adapter's wire format (with and without tools), the agent loop
(success, failure, unknown tool, iteration cap, flag gating), SSRF rejection at both save and call
time, and assistant resolution being live rather than snapshotted.

Beyond the unit suite: one Playwright happy path (signup → login → create workspace) against a real
running stack, and a load-test script that drives concurrent streaming generations without needing
a provider key.

CI runs `ruff`, `mypy app`, `pytest` on the API and `eslint`, `tsc --noEmit`, `next build` on the
web app, on every push and pull request.
