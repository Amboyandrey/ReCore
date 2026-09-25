# ReCore — Architecture

A multi-workspace chat platform that talks to any LLM, using API keys the workspace brings itself.

This describes the system **as built**. Everything below exists in the repository and runs; where
something was considered and deliberately left out, it's in §21 rather than implied to be coming.
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
passed through as images to vision models).

**Tools are first-class rows, not a hardcoded list.** The built-in web search is a row in the same
`tools` table a workspace's own HTTP tools live in — no virtual-vs-real split to reason about, and
no separate seeding step.

**A shared core, not a fork per product.** Every capability beyond core chat — memory, delegation,
knowledge retrieval, background workflows — is built as a feature-flagged vertical in this one
repo, not a copy of it. A future product built on ReCore turns flags on rather than diverging code.

**mem0 for memory, not a homegrown store.** ReMind needed semantic recall (what does this assistant
already know that's relevant to *this* message), not just a lookup table — mem0 already does the
embedding, scoring, and dedup that would otherwise be a second retrieval system to build and tune.

**pgvector for knowledge, not a separate vector database.** One more service to run, back up, and
secure would cost more than it saves at this scale; pgvector turns the existing Postgres into a
vector store with an extension, not a new piece of infrastructure.

**arq for the background worker, chosen once and reused.** ReStore needed a real job queue
(indexing can't block a request); ReFlow needed the same thing a step later. One Redis-backed
worker process, one job-lifecycle pattern, for both.

**Delegation depth is exactly one.** An assistant can hand a task to another assistant, but a
delegate's own delegates are never offered. Unbounded delegation chains are a cost and loop risk
this product doesn't need yet, and "depth one" is a constraint that's actually enforced (see §10),
not just a convention someone could accidentally break.

**A workflow is a linear chain, not a graph.** No branching, no parallel steps. A saved sequence of
assistant steps, each seeing the run's input and every earlier step's output, covers real pipelines
without the join-semantics and partial-failure design a general DAG would need.

## 2. System shape

Every request enters through the same gates. Nothing reaches the database without a resolved
workspace and role.

```
Client   → Next.js App Router (React 19), per-domain fetch clients, SSE reader, flag context
Edge     → CORS + security headers, request id / structlog, rate limits (Redis)
Guards   → current_user → workspace_ctx → require_role → (optional) flag gate
Services → auth, sessions, workspaces, members, invitations, credentials, models, chat,
           generations, attachments, tools, assistants, assistant_runtime, memory, knowledge,
           workflows, jobs, flags, usage, audit, rate_limit
Adapters → Anthropic, Google, OpenAI-compatible (also serves OpenAI), Fake (tests) — chat streaming
           and, for providers that support it, embeddings
Executors→ built-in web search (Tavily), third-party HTTP tools, remote MCP server tools
Worker   → arq — connector indexing (website crawl or files → chunks → embeddings) and workflow
           runs, one step at a time
External → mem0 (recall/record on a workspace's own key)
State    → Postgres + pgvector (system of record and vector store), Redis (sessions/limits/streams/
           cache/job queue), local disk (uploads)
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
│   ├── app/providers/          # LLM adapters behind one protocol (chat + embeddings)
│   ├── app/tools/               # tool executors + the dispatcher that bounds them
│   ├── app/memory/              # the mem0 client wrapper
│   ├── app/knowledge/           # website crawling + text chunking for ReStore's indexer
│   ├── app/workflows/           # ReFlow's step-prompt template renderer
│   ├── app/workers/             # arq jobs — connector indexing, workflow runs
│   ├── app/routers/v1/         # thin — parse, delegate, serialize
│   ├── app/scripts/             # seed, load test
│   ├── migrations/             # Alembic, one revision per schema change
│   └── tests/                  # pytest, 469 tests against real Postgres/Redis
├── apps/web/                 # Next.js 16 (App Router), React 19, Tailwind 4
│   ├── app/(auth)/             # login, signup
│   ├── app/w/[slug]/           # chat + settings, workspace-scoped
│   ├── app/admin/flags/        # flag administration — superuser only
│   ├── lib/                    # one typed client per domain + React context
│   └── components/             # shared UI
├── infra/                    # docker-compose.yml (postgres+pgvector, redis, api, worker, web)
└── docs/                     # this file
```

The rule that keeps this clean: **routers never contain logic and services never import FastAPI.**
A service takes plain typed arguments, so every service is testable without an HTTP client — which
is exactly how the chat pipeline's background task, and the arq worker, both reuse them.

## 3. Data model

Twenty-nine tables. Every tenant-scoped one carries `workspace_id` — even where it's derivable
through a join, because derivable means forgettable.

| Table | Holds | Notable columns |
|---|---|---|
| users | Identity, global | email unique, password_hash, is_superuser |
| workspaces | The tenant boundary | slug unique, owner_id, settings jsonb, deleted_at |
| workspace_members | Who's in, at what role | pk(workspace_id, user_id), role enum, joined_at |
| invitations | Pending seats | email, role, token_hash, expires_at, accepted_at |
| provider_credentials | The keys users register | provider, label, base_url, ciphertext, nonce, wrapped_key, last4 |
| models | Enabled models per workspace | credential_id, provider_model_id, cost_per_mtok in/out, supports_vision, kind (chat/embedding) |
| conversations | Chat threads | user_id, model_id, assistant_id, system_prompt, shared |
| messages | Turns | role, content, tokens_in/out, cost_usd, finish_reason, error |
| attachments | Uploaded files | conversation_id / workflow_id (exactly one set), message_id, workflow_run_id, mime, size, storage_key, extracted_text, extract_status |
| tools | What a chat may call | name unique per workspace, kind enum, parameters jsonb, enabled, method/url/secret_header, ciphertext/nonce/wrapped_key |
| tool_invocations | One row per call actually made | message_id, tool_id (nullable), name, arguments, result, status, latency_ms |
| assistants | Saved instructions + optional model | name, instructions, model_id (nullable), memory_enabled, created_by |
| assistant_tools | Which tools an assistant may use | pk(assistant_id, tool_id) |
| assistant_delegates | Who an assistant may delegate to | pk(assistant_id, delegate_id), `CHECK (assistant_id <> delegate_id)` |
| memory_credentials | A workspace's mem0 key | pk = workspace_id, ciphertext/nonce/wrapped_key, created_by |
| knowledge_settings | A workspace's chosen embedding model | pk = workspace_id, embedding_model_id (nullable), created_by |
| connectors | A source to index (website or files) | kind, name, url (website only), max_pages, status, error, embedding_model_id/dim (snapshot), document_count, chunk_count, last_indexed_at |
| connector_documents | One crawled page or uploaded file | connector_id, source_url/filename, mime, size, storage_key, title, char_count, status, error |
| connector_chunks | One embedded, retrievable piece | connector_id, document_id, ordinal, content, embedding (pgvector, no fixed dimension) |
| assistant_connectors | Which connectors an assistant may retrieve from | pk(assistant_id, connector_id) |
| message_sources | Which chunks actually made it into a reply | message_id, connector_id/document_id (nullable), ordinal, label, url, snippet, score |
| workflows | A saved step chain | name, description, enabled, default_model_id (nullable), schedule_cron/schedule_timezone/next_run_at (schedule trigger, reserved — see §21), webhook_secret (nullable; set = inbound hook on, see §14), created_by |
| workflow_steps | One step in the chain | workflow_id, position, key, name, assistant_id (nullable), prompt_template, requires_approval |
| workflow_runs | One execution of a workflow | workflow_id, trigger enum, status enum, input, output, error, started_by (nullable), current_position, cost_usd, started_at, finished_at |
| workflow_step_runs | One step's own execution within a run | run_id, step_id (nullable), position, key, name, assistant_id (nullable), prompt_template/requires_approval (snapshot), status, prompt (rendered), output, error, tokens_in/out, cost_usd, latency_ms, invocations jsonb, sources jsonb, approved_by/approved_at |
| feature_flags | Flag definitions | key unique, type enum, default_value jsonb, rollout_percentage, archived_at |
| flag_overrides | Targeting rules | unique(flag_id, scope, scope_id), value jsonb |
| usage_events | Append-only metering | model_id, tokens, cost_usd, latency_ms, conversation_id/message_id (nullable — a chat reply) or workflow_run_id (nullable — a workflow step); exactly one pair is populated |
| audit_logs | Who did what | actor_id, action, target_type, target_id, ip, metadata jsonb |

Several denormalizations are deliberate, all following the same shape: a pointer to something that
can be renamed or deleted goes null (`ON DELETE SET NULL`) rather than cascading history away, and
anything worth showing after that happens is copied onto the row that needs it. `tool_invocations.
name`, `conversations.assistant_id`, `assistants.model_id`, `connectors.embedding_model_id` (a
snapshot of what a connector was actually indexed with, not the workspace's live setting — see
§12), and `workflow_step_runs.key`/`name`/`assistant_id`/`prompt_template` (a snapshot of the step
as it was when the run started, so editing a workflow's steps never rewrites a past run's history)
all follow it.

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
   someone else's. It's transaction-local, so a background job (the arq worker, a delegate's own
   inner turn) re-sets it after every commit rather than once at the top.

RLS covers every workspace-scoped table added since — `memory_credentials`, `knowledge_settings`,
`connectors`, `connector_documents`, `connector_chunks`, `message_sources`, `workflows`,
`workflow_steps`, `workflow_runs`, `workflow_step_runs` — the same direct `workspace_id` policy
every earlier table gets. Three tables carry no `workspace_id` of their own and get a
join-through-parent policy instead: `assistant_tools`, `assistant_connectors`, and
`assistant_delegates` all check `assistant_id IN (SELECT id FROM assistants WHERE workspace_id =
...)`.

Four tables are excluded from RLS on purpose, each for a reason found by running this against a
real database rather than reasoning about it:

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
  link-local, private and reserved ranges. The same guard protects third-party tool URLs and every
  fetch (and redirect hop) a ReStore website connector makes.
- **Every touch is audited.** Create, delete, and validation failures all write an `audit_logs` row
  with actor and IP.

The same envelope-encryption columns and code path hold a workspace's Tavily key, its mem0 key, and
each HTTP tool's secret header value, and each MCP server's auth header value — one mechanism for
every secret the platform stores.

## 6. The LLM layer

One protocol, adapters behind it, one normalized chunk type. Adding a provider touches one new file.

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

class EmbeddingProvider(Protocol):
    async def embed(self, *, model: str, texts: list[str]) -> list[list[float]]: ...
```

`Provider` has four values but there are three chat-adapter files: OpenAI and "OpenAI-compatible"
share one, differing only in whether a base URL is supplied. A `FakeProvider` implements both
protocols, which is what makes the whole chat pipeline — and ReStore's embedding/retrieval path —
testable without a network.

Embeddings are a separate, narrower protocol from chat, because not every provider has both:
Anthropic has no embeddings API at all (`build_embedding_provider` raises `EmbeddingsNotSupported`
rather than pretending), while OpenAI-compatible and Google both implement `embed()` — batched at
100 texts per call, and re-sorted by response index for OpenAI-compatible specifically, since that
API doesn't guarantee response order matches input order the way Google's does.

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
resolve that assistant's turn (memory, knowledge, tools, delegates — see §9–§12, all shared with
ReFlow's own steps via `prepare_assistant_turn`) → start a background task → return an SSE response
that tails that task's Redis stream.

The generation runs **independently of the request that started it**. It owns its own database
session and Redis client, so a client refreshing mid-reply loses nothing: the background task keeps
writing to Redis and, at the end, to Postgres, whether or not anyone is watching.

What makes it survive contact with reality:

- **Resumable streams.** Deltas go to a Redis Stream keyed by generation id, so a reconnecting tab
  replays from an offset instead of losing a half-written answer. A page reload has no surviving
  `Last-Event-ID`, so the client asks an active-generation endpoint what to resume and replays from
  the start. A workflow run's own live view reuses this exact mechanism (§14) — its "generation id"
  is just `run:{run_id}`.
- **Stop** is a pub/sub message on that generation id, so it works even when the reader and the
  producer are different API processes. It's checked between provider chunks *and* between agent-
  loop iterations, since tool execution itself can take seconds. Canceling a workflow run signals
  the same way while it's actively running.
- **Idempotency keys** on send, held in Redis, so a retried request attaches to the existing
  generation instead of billing twice.
- **An image budget on replayed history.** Unlike text, images live in the assembled history as
  real bytes and get re-sent at real cost on every later turn. History is capped at 10 images /
  20 MB, newest first — a screenshot from turn one stops being paid for by turn five.
- **Usage is written once per generation** (or, for a workflow, once per step, plus once per
  delegate call either way), summed across every agent-loop iteration, with cost computed from the
  model's price columns. A generation that errored outright meters nothing; one that was stopped
  mid-reply still used real tokens and still bills.

## 8. Tools and the agent loop

`_run_tool_loop` is a bounded loop, not a single pass: stream the provider; if it asks for tools
(including a delegate, offered exactly like one — see §10), execute them, append the assistant's
request and each result to the working history, and stream again. `MAX_TOOL_ITERATIONS = 5` — a
model that keeps calling tools forever is a billing runaway, not a feature, and hitting the cap
ends the generation with an error rather than looping. The same loop drives a chat turn, a
delegate's own turn, and a ReFlow step (see §14) — one implementation, three callers.

Two `tool_call` / `tool_result` SSE events bracket every call, so the UI shows "searching the
web…" rather than a stall. Each call is also persisted — as a `tool_invocations` row tied to the
final assistant message in chat, or inline JSON on a `workflow_step_runs.invocations` column for a
workflow step, which has no chat message to attach a row to — including when the generation later
fails, since a run that broke on its third round trip may still have made two real calls worth
recording.

**Execution is bounded and never fatal.** `execute_tool` runs a call under a 15-second timeout and
truncates any result to 8,000 characters. A tool that times out, throws, or returns something huge
becomes a normal-shaped error result fed back to the model — one bad tool cannot take a generation
down.

Three kinds of tool exist behind that one dispatcher:

- **Built-in web search**, backed by Tavily — chosen because it returns LLM-ready text rather than
  raw HTML to parse. Its key is stored envelope-encrypted on the tool's own row.
- **Third-party HTTP tools**, registered by any workspace member: name, description, JSON Schema
  parameters, method, URL, and an optional secret sent as a request header. The model's arguments
  become the request body (POST/PUT/PATCH) or query string (GET/DELETE); the response is truncated
  before it ever reaches the model, and a non-2xx comes back as a tool error rather than killing
  the generation.
- **Tools from a remote MCP server** (`mcp_servers`, Streamable HTTP only), connected by any
  workspace member with a short name, a URL, and an optional auth header. Connecting runs
  `tools/list` and stores each tool as an ordinary `tools` row of kind `MCP`, named
  `<server>__<tool>` and carrying copies of the server's URL and encrypted secret, so executing one
  needs no extra lookup and the agent loop, assistants, delegation and workflows use it unchanged.
  Rows start **disabled** — a server can offer dozens of tools and a plain chat is offered every
  enabled one — and only `enabled` can be edited on them. A sync refreshes rows in place (so
  assistant assignments survive), adds new ones disabled, and disables any the server dropped.
  Each call opens a fresh session (stateless, so the worker can run it too); text content is
  passed back, and images or binary resources become a placeholder.

An HTTP tool's or MCP server's URL is checked against the SSRF guard at registration **and again immediately before
every call** — a DNS answer can change in between, and the second check is the one that runs at the
moment a request actually goes out.

Tool names are unique per workspace and constrained to `^[a-zA-Z0-9_-]{1,64}$` — OpenAI's own
function-name rule, adopted as the strictest common denominator across all three providers, since
the name is what the model calls the function by. A delegate's synthesized `ask_<name>` tool name
follows the identical rule (see §10), so the two can never collide.

## 9. Assistants

An assistant is a saved name, **required** instructions, an optional preferred model, an optional
set of tools, and optionally: memory (§11), connectors to retrieve from (§12), and other assistants
it may delegate to (§10).

Everything about it is **resolved live at send time, never snapshotted onto the conversation**, via
`prepare_assistant_turn` — the one function both a chat turn and a ReFlow step call to get an
assistant's system prompt (instructions plus any recalled memory or retrieved knowledge), its
offered tools, and its delegates:

- its instructions become the system turn, replacing the conversation's own `system_prompt` (or, in
  a workflow, the step's whole system turn — there is no other `system_prompt` to replace);
- its assigned, still-enabled tools become the *entire* offered tool set — not an addition to the
  workspace's enabled tools, but a replacement for them.

So editing an assistant reaches every conversation (and every future workflow run) already using it
on their very next turn, and a tool that's disabled after being assigned simply drops out of what's
offered without needing its assignment cleaned up. A conversation with no assistant behaves exactly
as it did before assistants existed: its own `system_prompt`, and every enabled tool in the
workspace. A workflow step always has an assistant — that's what a step *is*.

The one thing that is *not* resolved live is the model. In chat, `assistants.model_id` is
advisory — the composer pre-fills its model picker from it, but the conversation's own `model_id`
is what a generation actually uses. In a workflow, a step's assistant's own model *is* authoritative
when it has one; `workflows.default_model_id` is the fallback for a step whose assistant has none,
checked (and required to resolve to something) when the workflow is saved, not at run time.

## 10. Delegation

An assistant can list other assistants it may hand a task to. Each one is offered to the model as a
synthesized `ask_<name>` tool (`delegate_tool_name` sanitizes and dedupes the name), described as
"delegate this task and get its answer back" — the delegate never sees the outer conversation, only
the task string it's handed.

A delegate call runs the *same* tool-calling loop (§8) as the outer turn, on its own model if it has
one (falling back to the outer turn's model otherwise), with its own tools and its own memory recall
if it has memory enabled. Its tool calls are recorded nested under the delegation in the transcript,
and its own token usage bills as a separate `usage_events` row on its own model — the outer turn's
own tokens are never summed with a delegate's.

**Depth is always exactly one.** `list_assistant_delegates` is only ever called on the orchestrator;
building a delegate's own `DelegateSpec` never looks up *its* delegates. A self-delegation (an
assistant listing itself) is rejected at the database level by a `CHECK` constraint, not just in the
service layer. Delegates get no knowledge retrieval in v1 (§12) and are never themselves the subject
of a ReFlow step's `{{steps.*.output}}` — a workflow step's assistant may delegate internally
exactly like a chat turn's can, but a workflow has no notion of "a step's own delegate" separate
from the step's assistant handling it.

A stop request reaches a running delegation through the same shared stop signal the outer loop
uses — without that, the delegate's own inner loop consuming the pub/sub message would let the
outer loop wrongly continue once the delegation returned.

## 11. ReMind memory

An assistant with memory enabled recalls two kinds of fact, kept in separate mem0 namespaces so
they can never leak into each other: **curated** facts its creator (or the workspace owner)
explicitly taught it, and **personal** facts it has privately inferred about whichever member is
currently chatting with it — one member's personal memory is never visible to another, including
the assistant's own creator.

- **Recall** (`retrieve_for_turn`) searches both the curated scope and the current user's personal
  scope for the assistant, folding whatever's relevant into the system prompt as `"Relevant
  memory:\n- ..."`, ahead of the turn.
- **Record** (`record_turn`) happens after a successful reply, fire-and-forget so a slow or
  unreachable mem0 never delays the reply the user is already looking at — always into the current
  user's *personal* scope only. Nothing from ordinary chat is ever written to the curated scope,
  not even by the assistant's own creator; curated facts are added deliberately, through their own
  route.
- **Degrades silently.** Both directions are wrapped so any mem0 failure — no credential, network
  error, invalid key — means no memory for that turn rather than a failed generation. The chat
  pipeline never surfaces a mem0 error to the user mid-conversation.

One mem0 API key per workspace, envelope-encrypted like every other secret (§5). A delegate recalls
its own memory under its own scope exactly as an outer turn does (§10); a ReFlow step does too
(§14), but never *writes* memory back — a scheduled or webhook-triggered run has no single "current
user" whose personal scope teaching it would even make sense for.

## 12. ReStore knowledge

A workspace registers **connectors** — a website to crawl (same-host, robots.txt-respecting, page-
capped) or a set of uploaded files — indexed in the background (§13) into chunks, embedded with the
workspace's own chosen model, and stored in pgvector. An assistant with connectors attached
retrieves relevant chunks into its prompt before replying, and the sidebar shows exactly which
chunks it used.

- **One embedding model per workspace** (`knowledge_settings`), chosen by an admin from whichever of
  the workspace's enabled models support embeddings (§6). Every connector is indexed with it.
- **A connector snapshots the model it was actually indexed with**, not the workspace's live
  setting. If an admin later changes the workspace's choice, an already-indexed connector shows as
  "needs reindex" and — critically — retrieval **skips** it rather than comparing a query vector
  against chunks from an incompatible embedding space.
- **Chunking is paragraph-aware**, packing to a target size with cross-boundary overlap so a fact
  stated right at a chunk boundary is still findable from either side of the cut. A document short
  enough to fit under a size threshold is kept as a single chunk rather than split at all — a
  documented, live-found failure mode is a near-tie in embedding distance between two chunks of the
  *same* short document silently excluding the one that actually answers the query in favor of a
  merely-closer boilerplate chunk; not splitting removes the possibility of that tie entirely, and
  retrieval separately keeps up to two nearest chunks per document as a second line of defence for
  documents too long to avoid splitting.
- **Retrieval is an exact cosine-distance scan** (`ConnectorChunk.embedding.cosine_distance`),
  filtered to one assistant's attached, ready, current-model connectors — fast enough at the
  enforced per-connector chunk cap; no ANN index, because the column has no fixed dimension (a
  workspace can change its embedding model, changing the dimension a later reindex needs).
- **Sources are folded in twice.** Once as a labeled block in the system prompt
  (`"Relevant knowledge:\n[Source N: ...]\n..."`), and once as a `sources` SSE event emitted before
  the first token — so the sidebar fills in while the reply is still streaming — followed by
  `message_sources` rows persisted once the reply exists, so a reloaded thread shows the same
  sources without needing the live stream.
- **Degrades silently**, the same contract memory follows: no settings row, no ready connectors, or
  any retrieval failure all just mean no knowledge reaches that turn.

Delegates get no knowledge retrieval in v1 — only the orchestrating assistant's own connectors are
ever searched, the same posture as memory not extending to a delegate's inner scope by default.

## 13. Background worker (arq) and pgvector

Two pieces of infrastructure ReStore introduced and ReFlow reuses outright.

**The worker** is a separate `arq` process (its own container in compose) sharing the API's own
Postgres and Redis. A job is enqueued with a stable `_job_id` (the connector id, or `run:{run_id}`
for a workflow run) so a duplicate enqueue — a second "Reindex" click, an "approve" re-enqueuing a
parked run — coalesces into the same job rather than racing two workers over the same row. This
only works because `keep_result=0`: arq refuses a duplicate job id while a prior job with that id
still has a *stored result*, and a nonzero `keep_result` would silently block a legitimate re-run
for up to an hour after the original job actually finished.

A worker job owns its own database session, independent of any request — the same pattern the chat
pipeline's own background generation task already followed before a worker process existed at all.
Row-level security's `app.workspace_id` is transaction-local, so a job re-sets it after every
commit, not once at the top. Any failure anywhere in a job — a bad URL, a corrupt file, a disabled
provider, an exception from the model itself — lands the thing being worked on (a connector, a
workflow run) in a FAILED state with the error recorded, never as an unhandled exception that would
just look like a job silently vanishing.

**pgvector** turns the existing Postgres into the vector store, via `pgvector/pgvector:pg16` — a
drop-in image swap on the same data directory, so an existing deployment's volume keeps working
across the upgrade. The extension itself is created by a migration
(`CREATE EXTENSION IF NOT EXISTS vector`), not baked into the image, matching how every other schema
object here is versioned.

## 14. ReFlow workflows

A workflow is a saved, ordered chain of steps — each an assistant plus a prompt template — that
runs on the worker with **no chat turn and no conversation involved at all**. A run's own input
text and every earlier step's completed output are the only things a later step's template can
reference: `{{input}}` and `{{steps.<key>.output}}`, substituted by a narrow regex-based renderer
with no real template language behind it (no eval, no attribute access, no loops) — a workflow's
steps are authored by a workspace member, and this keeps that authorship from ever reaching a
code-execution surface.

- **Validated at save time, not run time.** Step keys must be well-formed and unique, every
  template reference must name a *strictly earlier* step, and every step must resolve to some model
  — its own assistant's, or the workflow's own default — before the worker ever gets a chance to
  discover it can't. A broken chain is a 400 on save, never a run that starts only to fail on its
  first step.
- **One active run per workflow at a time.** Starting a second run while one is queued, running, or
  awaiting approval is rejected outright, rather than letting two runs race the same workflow's
  step definitions.
- **A step run snapshots everything it needs**, including its own rendered `prompt_template` — not
  just its key/name/assistant — specifically so an admin editing the workflow's steps mid-run (which
  replaces every `workflow_steps` row wholesale) can never strand an in-flight run without the
  template it needs to render its next step.
- **Reuses chat's own machinery entirely** rather than reimplementing it: `prepare_assistant_turn`
  (§9) for memory/knowledge/tools/delegate resolution, the same tool-calling loop (§8), and the same
  Redis-stream-plus-SSE mechanism a chat generation uses for live progress and cancellation — a
  run's own stream id is simply `run:{run_id}`, read and appended to with the exact same functions
  a chat generation's id is.
- **A run starts from text, files, or both.** Files are uploaded into the workflow ahead of the
  run (`POST .../workflows/{id}/attachments`, the same upload-then-reference shape the chat
  composer follows) and claimed by the run that starts with them. They're part of the run's
  *input*, not a separate placeholder: the worker hands them to every step whose template reads
  `{{input}}`, through the exact same code path a chat message's own attachments take (§15) —
  extracted text folded into the prompt, images as native image parts. A step whose model can't
  see images fails with that reason rather than silently dropping them.
- **An inbound webhook per workflow.** Enabling it mints a `webhook_secret`; the public URL
  `POST /hooks/workflows/{workspace_id}/{secret}` starts a run with no session and no CSRF —
  possession of the URL is the credential, the same model an invitation link follows. The
  workspace id is in the path for row-level security (every workflow lookup needs a scope to run
  under; a bare secret gives none), not for authorization — only the secret has to be unguessable.
  The body is JSON `{"input": "..."}` or a multipart form with `input` and `files` parts; it's rate
  limited per workspace, refuses a workflow whose `enabled` switch is off (409), and 404s if the
  workspace's `workflows` flag is off — the feature's back door is never wider than its front.
  Rotating replaces the secret, disabling clears it; the old URL dies either way.
- **A step marked `requires_approval`** that succeeds parks the run at `WAITING_APPROVAL` and the
  job simply returns rather than blocking a worker slot on a human. **As shipped, nothing can
  resume it yet** — the toggle and the worker-side pause both exist, but the approve/reject
  endpoint that would re-enqueue the same job id does not (see §21). Do not check that box until
  it does.

Billing follows the outer chat pipeline's own shape: one `usage_events` row per step (plus one per
delegate call a step's assistant makes), keyed by `workflow_run_id` instead of a conversation and
message — `usage_events.conversation_id`/`message_id` are nullable specifically so a workflow's
usage has somewhere honest to *not* point.

## 15. Attachments

Upload is synchronous, into a conversation (gated by the `attachments` flag) or into a workflow as
a run's input (gated by `workflows`, see §14) — exactly one of `conversation_id`/`workflow_id` is
set, enforced by a CHECK constraint. Text-bearing formats (PDF, DOCX, PPTX,
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

Files live on a local disk volume, keyed `{workspace_id}/{attachment_id}` — the same volume and
layout a ReStore file connector's own uploaded documents use, keyed by connector instead of
conversation. An object store would be a drop-in replacement for that one module; nothing else
knows where bytes live.

## 16. What Redis is actually doing

Each key shape is a reason Postgres alone would be the wrong tool, not a cache bolted on.

| Key shape | Job |
|---|---|
| `session:{sid}` | Sessions — user id, CSRF token, 14-day sliding TTL. Logout is one delete. |
| `rl:{scope}:{id}` | Rate limiting — login (per IP and per account), signup, invites, credential validation. |
| `gen:{id}` (stream) | Live generations — deltas, tool events and terminal events, so a reconnecting client resumes mid-answer. A workflow run's own progress reuses this exact key shape with `id = "run:{run_id}"`, carrying its own event vocabulary (`run_started`, `step_started`, `step_done`, …) through the same stream/read/resume functions. |
| `gen:{id}:stop` (pubsub) | Stop signals — cancellation crosses processes, including into a running workflow step. |
| `conv:{id}:active_generation` | What a freshly loaded page should resume, since a reload keeps no `Last-Event-ID`. |
| `flags:{ws}` + `flags:invalidate` | Flag snapshots — cached 30s, dropped instantly by pub/sub on edit. |
| `idem:{key}` | Idempotency — a retried send attaches to the in-flight generation. |
| arq's own keys | The job queue itself — a separate `ArqRedis` connection pool from the app's own (arq needs raw bytes; the app's pool runs `decode_responses=True`), same Redis instance. |

## 17. Feature flags

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
time — in chat, in a delegate's own model resolution, and in a ReFlow step's — so flipping one off
degrades gracefully instead of failing at the adapter. Five feature-vertical flags gate everything
built after core chat: `tools`, `memory`, `delegation`, `knowledge`, `workflows` — each off by
default, each a 404 at the route level when off, exactly like `attachments`.

## 18. Interface

| Route | Purpose | Gate |
|---|---|---|
| `/login` · `/signup` · `/invite/[token]` | Enter, or accept a seat | public |
| `/w/[slug]` | Workspace home — conversations, settings links | viewer |
| `/w/[slug]/c/new` · `/w/[slug]/c/[id]` | Chat — model/assistant pickers, streaming, stop, attachments, tool activity, knowledge sources sidebar | viewer |
| `/w/[slug]/settings/members` | Invite, change role, remove | admin |
| `/w/[slug]/settings/providers` | Register keys, validate, enable models (chat or embedding), set pricing | admin |
| `/w/[slug]/settings/tools` | Web search key; register/edit/delete HTTP tools; connect/sync/delete MCP servers and toggle their tools | any member, `tools` flag |
| `/w/[slug]/settings/assistants` | Create/edit assistants — instructions, model, tools, memory, delegates, connectors | any member |
| `/w/[slug]/settings/assistants/[id]/memories` | View/add curated memories, view (not edit) personal ones | creator or owner, `memory` flag |
| `/w/[slug]/settings/knowledge` | Choose the embedding model; register/reindex/delete connectors | any member (model choice: admin), `knowledge` flag |
| `/w/[slug]/settings/workflows` | Create/edit a workflow's step chain | any member, `workflows` flag |
| `/w/[slug]/settings/workflows/[id]/runs` | Start a run; run history | any member, `workflows` flag |
| `/w/[slug]/settings/workflows/[id]/runs/[runId]` | Live/replayed step-by-step run detail; cancel | any member, `workflows` flag |
| `/w/[slug]/settings/usage` | Tokens and spend by model, member, day (chat and workflow runs both) | admin |
| `/w/[slug]/settings/audit` | Audit log, cursor-paginated | owner |
| `/admin/flags` | Flag definitions and overrides | superuser |

`is_superuser` defaults to false and nothing in the application sets it — promoting the first
account is a deliberate manual step against the database (see the README), so a fresh deployment
has no self-service path to platform-wide flag administration.

Tools, assistants, connectors, and workflows are open to **any member**, not just admins — the same
floor sending a message has, since none of them are usable anywhere but inside a chat or a run.
That cuts both ways and is stated plainly in §20.

A conversation is **private to whoever started it** by default; its owner can share it with the
workspace, at which point anyone can read and write in it. Only the owner can un-share or delete
it — otherwise "shared" would be a one-way ratchet nobody but the first sharer could undo.

The UI is hand-written Tailwind against a small set of design tokens, with one typed fetch client
and React context per domain. There is no component library, no data-fetching library, and no
generated client; at this size those would be more indirection than leverage.

## 19. Security posture

Enforced today:

- Argon2id password hashing; opaque Redis-backed sessions, not JWTs
- Auth rate limits per IP and per account, generic errors that don't distinguish "no such user"
  from "wrong password"
- Pydantic v2 on every input; SQLAlchemy expressions only, no string SQL
- Signed, single-use invite tokens stored hashed, with an expiry
- Envelope-encrypted provider, mem0, and tool secrets; plaintext never on a response schema
- SSRF guard on every user-supplied URL — provider base URLs, tool endpoints, and every fetch (and
  redirect hop) a website connector's crawler makes, all re-checked immediately before use
- Tool execution bounded by timeout and result size; a failing tool degrades one turn, not a run
- CSP, HSTS, X-Content-Type-Options and Referrer-Policy on every response from both apps
- Audit rows on every privileged action — invites, role changes, credential/tool/assistant/
  connector/workflow lifecycle, flag edits
- Row-level security under a dedicated low-privilege Postgres role, as a backstop beneath
  application scoping, covering every workspace-scoped table this system has ever added

Known gaps, stated rather than buried:

- **CSRF is issued but only enforced on logout.** The double-submit cookie and `require_csrf`
  dependency both exist; wiring it across the remaining mutating routes is a small, unfinished job.
- **The SSRF guard has a TOCTOU window.** It resolves and checks, then a separate connection is
  made; closing it fully means pinning the resolved IP for the request that follows. The
  registration-time *and* call-time (and, for a crawl, per-redirect-hop) checks narrow it; they
  don't eliminate it.
- **Rate limiting covers auth, invites, credential validation and the inbound workflow webhook
  (per workspace, see §14) — not chat.** A workspace's own provider bill is the current
  backpressure on message sending.
- **CI runs lint, type checks and tests**, not dependency or secret scanning.
- **Logs are structured JSON with no redaction processor.** Secrets stay out of them by discipline
  — nothing logs decrypted material, and credentials are decrypted only at the point of use — but
  there is no filter standing behind that discipline if a future log line gets it wrong.
- **MCP tool descriptions and results are third-party text.** A connected server controls what
  the model reads about its tools and what they return, so it can attempt prompt injection — the
  same trust already extended to an HTTP tool's response, and contained the same way (§20).
- **Tool results don't survive into later turns.** Within one generation the model sees every tool
  result; on the next user turn it sees only its own final answer. That keeps replay honest across
  providers, which reject orphaned tool-call ids, but it is a real boundary.

## 20. The trade-off worth naming

Any workspace member can register a tool (or connect an MCP server) that sends the model's
arguments to a URL of their choosing, carrying a stored secret — and, separately, register a website connector that crawls any
same-host-reachable public site, or build a workflow that runs repeatedly on the workspace's own
provider credentials. None of these were oversights: gating any of them behind admin-only would
have made the feature useless to the people most likely to want it, for the same reason across all
three.

What contains it: the SSRF guard blocks internal addresses everywhere a URL is user-supplied, every
registration and edit is audit-logged, all of it is visible to the whole workspace (nothing here is
covert), secrets are write-only, and a workflow can only ever run as an assistant already resolves
in chat — nothing about running in the background grants it more access than it would have live. It's
still worth revisiting if a workspace ever has members you wouldn't hand an outbound webhook or a
standing background job.

## 21. Deliberately not built

Named here so nobody hunts for them or assumes they're half-finished:

- **Programmatic API keys.** Sessions are the only credential; there is no `api_keys` table.
- **Usage rollups.** `usage_events` is aggregated directly on read. A rollup table is the answer
  when that gets slow, and adding one later changes nothing above the service.
- **Workflow schedule triggers.** `workflows.schedule_cron`/`schedule_timezone`/`next_run_at`
  exist in the schema (added ahead of time so enabling them needs no second migration), but
  nothing reads or writes them yet — `WorkflowTrigger.SCHEDULE` is a real enum value with no code
  path that ever produces one. Runs today are `MANUAL` or `WEBHOOK` (§14).
- **Approving a waiting workflow run.** A step's `requires_approval` toggle is fully wired on the
  save side and the worker correctly parks a run at `WAITING_APPROVAL` — but no endpoint exists yet
  to approve or reject one and resume the job. Checking that box today produces a run that waits
  forever without a UI or API path to unstick it.
- **Multi-level delegation.** An assistant's delegate can never itself delegate — enforced
  structurally (§10), not a temporary limit.
- **Per-dimension ANN indexing for pgvector.** Retrieval is an exact scan, fine at the enforced
  per-connector chunk cap; an HNSW/IVFFlat index keyed by dimension is the natural next step if
  that stops being true.
- **Knowledge retrieval or memory for a delegate's inner turn.** Both extend only to the
  orchestrating assistant today.
- **OAuth and stdio for MCP servers.** Only a static auth header is supported, so hosted servers
  that require the OAuth 2.1 flow can't be connected yet; stdio servers would mean running a
  user-supplied command on the API host, which a multi-tenant app shouldn't do. MCP resources and
  prompts aren't used either — only tools.
- **Circuit breakers per credential**, a command palette, and a shared `packages/` workspace — all
  in earlier plans, none built.

## 22. Conventions

- **One explanatory line per function.** It states *why*, or what it guarantees — not a restatement
  of the signature. `"""Return only conversations the caller can read."""` earns its line;
  `"""List conversations."""` doesn't. Non-obvious decisions get the reason in a comment at the
  point where someone would otherwise "fix" them.
- **Type everything.** `mypy --strict` on the API, `strict` in tsconfig.
- **Errors are typed and mapped once.** Services raise domain exceptions (`WorkspaceNotFound`,
  `ToolNotFound`, `AssistantNotFound`, `WorkflowInvalid`, …); one handler turns them into responses.
  No `HTTPException` below the router layer.
- **Partial updates are `changes`-dict shaped.** An update service takes the dict a Pydantic model
  produced with `exclude_unset=True` and checks `"field" in changes`, so "not sent" stays
  distinguishable from "explicitly set to null" — the difference between leaving an assistant
  attached and clearing it. Every `update_*` service in this codebase follows it, workflows
  included.
- **A capability resolved live, not snapshotted, is resolved through one shared function** when
  more than one caller needs it — `prepare_assistant_turn` exists specifically so chat and ReFlow
  can never drift on what "an assistant's turn" means.
- **Migrations are never edited after merge**, and every one is verified with a full
  `upgrade → downgrade → upgrade` cycle against a real database before it ships.

## 23. Testing

469 pytest tests run against a real Postgres and Redis — never mocks of them. `conftest.py` forces
a separate `_test`-suffixed database and Redis index before any app code loads, so running the
suite can't touch data a dev stack is using. `FakeProvider` implements both the chat and embedding
protocols, which is what makes the whole chat pipeline, plus ReStore's indexing/retrieval and
ReFlow's worker, deterministic without a network.

Where coverage is deliberately dense: the tenancy guards, flag resolution order, credential
encryption round-trips, every adapter's wire format (with and without tools), the agent loop
(success, failure, unknown tool, iteration cap, flag gating), SSRF rejection at both save and call
time (including a website connector's crawl), assistant resolution being live rather than
snapshotted, delegation depth and self-delegation rejection, memory's curated/personal scope
isolation, knowledge retrieval's per-document dedup and stale-embedding-model skip, and a workflow's
save-time validation plus its worker running a real multi-step chain end to end (including a
failing step, a deleted assistant, a delegate step, and a knowledge-connector step).

`index_connector` and `run_workflow` are tested by calling the job function directly against the
real test database, the same session shape a fixture already uses for chat's own background
generation task — bypassing arq's queue entirely, since what's under test is the job's own logic,
not arq itself.

Beyond the unit suite: one Playwright happy path (signup → login → create workspace) against a real
running stack, and a load-test script that drives concurrent streaming generations without needing
a provider key.

CI runs `ruff`, `mypy app`, `pytest` on the API and `eslint`, `tsc --noEmit`, `next build` on the
web app, on every push and pull request.
