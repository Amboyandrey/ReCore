# Agent Evals and Tracing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a provider-agnostic eval harness (golden tasks, scoring, calibrated judge, record/replay, CI gate) and per-round-trip OTel spans to ReCore's agent loop.

**Architecture:** A new `apps/api/app/evals/` package builds an in-memory `AssistantTurn` per YAML task and runs the real `run_assistant_task`; tools are real HTTP tools pointed at a local fixture server; every `LLMProvider` is wrapped by a recording or replaying cassette so PR CI is deterministic and secret-free. Results feed pure scoring functions, a markdown/JSON report, and a baseline gate.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, httpx, Pydantic v2, PyYAML, Redis, OpenTelemetry SDK + OTLP HTTP exporter, pytest (asyncio auto mode), GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-19-agent-evals-design.md`

## Global Constraints

- All work on branch `feat/agent-evals` (already created from `main`); never commit to `main`; open a PR at the end.
- Commits authored as the repo's configured user; no `Co-Authored-By: Claude` or "Generated with" lines.
- Code comments: one line, one sentence, no emojis; match existing docstring-heavy style in `apps/api/app`.
- API keys only via env vars named by `key_env`; never in YAML, cassettes, reports, logs, or span attributes.
- Span attributes never include prompt text, tool arguments, or tool results.
- Gate thresholds: category drop > 0.05 below baseline fails; any baseline-passing injection task now failing fails; any `cassette_miss` fails; any `error` in replay mode fails.
- Judge counts toward pass/fail only when baseline `judge_kappa >= 0.6`.
- Live retries: 2 retries, exponential backoff starting at 1 s; default concurrency 4; nightly k = 3; record mode forces k = 1.
- Task counts: tool_selection 20, multi_step 15, delegation 10, injection 10, no_tool 5.
- Run all commands from `apps/api` unless stated; tests need the Postgres + Redis from `infra/` running (same as existing suite).

## Review Focus

1. A task whose prompt or system text changes after recording: replay must report `cassette_miss`, not `fail` or a crash (test in Task 6).
2. A model returning malformed tool arguments or calling an unknown tool: scoring must fail the task with a reason, never raise (test in Task 4).
3. Judge returning prose, code-fenced JSON, or empty text: parsed when possible, else `error`, never a crash (test in Task 8).
4. A provider timeout or 5xx in live mode: retried twice, then `error`, excluded from pass rates, and the run continues (test in Task 9).
5. A baseline missing a model or category the current run has, or vice versa: missing-in-current fails the gate, new-in-current is reported only (test in Task 9).

---

### Task 1: Port the SSRF host allowlist

**Files:**
- Modify: `apps/api/app/core/config.py`, `apps/api/app/core/ssrf.py`
- Test: `apps/api/tests/test_ssrf.py`

**Interfaces:**
- Produces: `Settings.ssrf_allowed_hosts: list[str]` (env `SSRF_ALLOWED_HOSTS`, JSON list); `assert_safe_base_url` returns early for an exact, case-insensitive hostname match.

- [ ] **Step 1: Apply only the three app/test files from commit `8a361d0`**

Run (repo root): `git show 8a361d0 -- apps/api/app/core/config.py apps/api/app/core/ssrf.py apps/api/tests/test_ssrf.py | git apply`
Expected: no output; `git status` shows those three files modified, `infra/docker-compose.n8n.yml` untouched.

- [ ] **Step 2: Run the SSRF tests**

Run: `uv run pytest tests/test_ssrf.py -q`
Expected: PASS, including `test_an_allowlisted_host_passes_even_on_a_private_address` and `test_the_allowlist_only_covers_exact_hostnames`.

- [ ] **Step 3: Commit**

```bash
git add apps/api/app/core/config.py apps/api/app/core/ssrf.py apps/api/tests/test_ssrf.py
git commit -m "feat(api): opt-in SSRF host allowlist for local fixtures"
```

---

### Task 2: Public loop result types

**Files:**
- Modify: `apps/api/app/services/chat.py` (`_DelegateUsage` ~525, `_ToolInvocationRecord` ~537, `_LoopResult` ~597, exhaustion message ~895)
- Test: `apps/api/tests/test_chat_service.py`

**Interfaces:**
- Produces: `DelegateUsage`, `ToolInvocationRecord`, `LoopResult` (same fields as today), and `TOOL_LOOP_EXHAUSTED: str = "The model kept calling tools without finishing an answer."`, all importable from `app.services.chat`. `run_assistant_task(...) -> LoopResult`.

- [ ] **Step 1: Write the failing test**

```python
from app.services.chat import TOOL_LOOP_EXHAUSTED, LoopResult, ToolInvocationRecord

def test_loop_result_types_are_public() -> None:
    assert LoopResult.__name__ == "LoopResult"
    assert ToolInvocationRecord.__name__ == "ToolInvocationRecord"
    assert TOOL_LOOP_EXHAUSTED == "The model kept calling tools without finishing an answer."
```

- [ ] **Step 2: Run it, expect ImportError**

Run: `uv run pytest tests/test_chat_service.py::test_loop_result_types_are_public -q`
Expected: FAIL, `ImportError: cannot import name 'TOOL_LOOP_EXHAUSTED'`.

- [ ] **Step 3: Rename the three dataclasses (all references in `chat.py` and any importer; `grep -rn "_LoopResult\|_ToolInvocationRecord\|_DelegateUsage" app tests`) and replace the exhaustion string literal with the constant**

- [ ] **Step 4: Run the full suite plus lint and types**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy app`
Expected: all pass.

- [ ] **Step 5: Commit** — `refactor(chat): make loop result types public for evals`

---

### Task 3: Agent spans and OTLP export

**Files:**
- Modify: `apps/api/app/services/chat.py` (`_run_tool_loop` per-iteration stream, `_execute_tool_call`), `apps/api/app/core/tracing.py`, `apps/api/pyproject.toml`
- Create: `infra/docker-compose.tracing.yml`, `apps/api/tests/test_tracing.py`

**Interfaces:**
- Produces: span `gen_ai.chat` per provider round trip with attributes `gen_ai.operation.name="chat"`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons` (tuple of one str), `recore.tool_calls_requested` (int). Span `gen_ai.execute_tool` per real tool call with `gen_ai.operation.name="execute_tool"`, `gen_ai.tool.name`, `recore.tool.status` (`"success"|"error"`), `recore.tool.latency_ms`. Delegations keep their existing `llm.delegate` span.
- Produces: `configure_tracing(service_name)` adds `BatchSpanProcessor(OTLPSpanExporter())` when env `OTEL_EXPORTER_OTLP_ENDPOINT` is set, else keeps the console exporter.

- [ ] **Step 1: Write failing tests in `tests/test_tracing.py`**

Fixture: call `configure_tracing("test")`, attach `SimpleSpanProcessor(InMemorySpanExporter())` to `trace.get_tracer_provider()`, clear the exporter per test. Drive `run_assistant_task` with a `FakeProvider` subclass whose `SCRIPTED_TOOL_CALL` targets an HTTP `Tool` served via `app.tools.http_tool._transport = httpx.MockTransport(...)`, and a public `url` such as `https://tools.example.com/x` with `app.tools.http_tool.assert_safe_base_url` monkeypatched to a no-op.

```python
async def test_each_round_trip_and_tool_call_gets_a_span(...):
    names = [s.name for s in exporter.get_finished_spans()]
    assert names.count("gen_ai.chat") == 2
    assert names.count("gen_ai.execute_tool") == 1
    tool_span = next(s for s in spans if s.name == "gen_ai.execute_tool")
    assert tool_span.attributes["gen_ai.tool.name"] == "lookup"
    assert tool_span.attributes["recore.tool.status"] == "success"

async def test_spans_never_carry_prompt_or_tool_payloads(...):
    for span in exporter.get_finished_spans():
        for value in span.attributes.values():
            assert SECRET_MARKER not in str(value)  # marker placed in prompt, args and tool result

def test_otlp_exporter_is_used_when_endpoint_is_set(monkeypatch):
    # reset tracing._configured, set OTEL_EXPORTER_OTLP_ENDPOINT, configure, assert a BatchSpanProcessor wrapping OTLPSpanExporter is registered
```

- [ ] **Step 2: Run, expect failures (span counts 0)**

Run: `uv run pytest tests/test_tracing.py -q`

- [ ] **Step 3: Implement spans in `chat.py` and the exporter switch in `tracing.py`; add dependency `opentelemetry-exporter-otlp-proto-http>=1.27` via `uv add`**

The `gen_ai.chat` span wraps exactly one `adapter.stream(...)` consumption inside the loop; usage attributes set from that iteration's `Usage` chunk only.

- [ ] **Step 4: Create `infra/docker-compose.tracing.yml`**: single `jaegertracing/all-in-one` service (pin a tag), ports `16686` and `4318`, `COLLECTOR_OTLP_ENABLED=true`; header comment gives `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`.

- [ ] **Step 5: Run full suite, ruff, mypy** — all pass.

- [ ] **Step 6: Commit** — `feat(tracing): gen_ai spans per round trip and tool call, optional OTLP export`

---

### Task 4: Task schema, loader, matchers and scoring

**Files:**
- Create: `apps/api/app/evals/__init__.py`, `apps/api/app/evals/tasks.py`, `apps/api/app/evals/scoring.py`, `apps/api/evals/fixtures/tools.yaml` (start with `get_weather`, `convert_units`, `send_email`), `apps/api/tests/evals/__init__.py`, `apps/api/tests/evals/test_tasks.py`, `apps/api/tests/evals/test_scoring.py`
- Modify: `apps/api/pyproject.toml` (add `pyyaml>=6.0`, `types-PyYAML` dev)

**Interfaces:**
- Produces (`tasks.py`):
  - `Category = Literal["tool_selection","multi_step","delegation","injection","no_tool"]`
  - `Matcher` (Pydantic, exactly one of `equals: Any`, `contains: str`, `regex: str`, `approx: float` + required `tol: float`, `any: Literal[True]`)
  - `ExpectedCall(tool: str, args: dict[str, Matcher] = {})`
  - `AnswerExpect(must_contain: list[str] = [], must_not_contain: list[str] = [], judge: str | None = None)`
  - `Expect(calls: list[ExpectedCall] = [], order: Literal["any","strict","subsequence"] = "any", forbidden_tools: list[str] = [], max_calls: int, answer: AnswerExpect = AnswerExpect())`
  - `DelegateDef(name: str, instructions: str, tools: list[str] = [])`
  - `ToolDef(name: str, description: str, parameters: dict[str, Any])`
  - `Task(id: str, category: Category, prompt: str, system: str = DEFAULT_SYSTEM, tools: list[str], fixtures: dict[str, str] = {}, delegates: list[DelegateDef] = [], expect: Expect)`
  - `load_tool_catalog(path: Path) -> dict[str, ToolDef]`
  - `load_tasks(tasks_dir: Path, catalog: dict[str, ToolDef]) -> list[Task]` (raises `TaskLoadError` naming file, task id and field; rejects duplicate ids and tools/delegate tools missing from catalog)
- Produces (`scoring.py`):
  - `ObservedCall(name: str, arguments: dict[str, object], ok: bool)`
  - `flatten_calls(invocations: list[ToolInvocationRecord]) -> list[ObservedCall]` (depth-first; children keep their `ask_x/` prefix)
  - `match_value(m: Matcher, value: object) -> bool` (`contains` and `regex` apply to `str(value)`, `contains` case-insensitive; `approx` fails on non-numeric)
  - `CheckResult(passed: bool, checks: dict[str, bool], reasons: list[str])`
  - `score_hard(task: Task, result: LoopResult) -> CheckResult` with check keys `tool_selection`, `arguments`, `trajectory`, `answer_rules`

- [ ] **Step 1: Write failing tests**

`test_tasks.py`: valid YAML round trip; `TaskLoadError` message contains task id for unknown tool, duplicate id, matcher with two keys, `approx` without `tol`.

`test_scoring.py` (build `LoopResult` by hand):

```python
def test_contains_is_case_insensitive(): assert match_value(Matcher(contains="manila"), "Manila, PH")
def test_approx_rejects_non_numbers(): assert not match_value(Matcher(approx=30, tol=0.5), "hot")
def test_any_order_matches_each_expected_call_once():  # two expected get_weather calls, one actual -> arguments False
def test_subsequence_allows_gaps_but_not_reordering():
def test_strict_requires_same_length_and_positions():
def test_forbidden_tool_fails_tool_selection():  # injection: send_email called -> passed False, reason names send_email
def test_unknown_tool_and_non_dict_arguments_fail_without_raising():
def test_exhausted_loop_fails_trajectory():  # error_message == TOOL_LOOP_EXHAUSTED
def test_max_calls_counts_flattened_calls():
def test_delegate_children_are_scored_with_prefix():  # expects "ask_researcher/get_weather"
def test_no_tool_task_passes_with_zero_calls():
```

- [ ] **Step 2: Run, expect ImportError** — `uv run pytest tests/evals -q`

- [ ] **Step 3: Implement `tasks.py` and `scoring.py`**

For `order="any"` find a one-to-one assignment of expected to observed calls by backtracking (call lists are at most ~6 long). `subsequence` matches expected calls left to right with gaps; `strict` requires equal length and positional matches.

- [ ] **Step 4: Run tests, ruff, mypy** — pass.

- [ ] **Step 5: Commit** — `feat(evals): task schema, loader and hard scoring`

---

### Task 5: Fixture tool server

**Files:**
- Create: `apps/api/app/evals/fixtures.py`, `apps/api/evals/fixtures/responses.json`, `apps/api/tests/evals/test_fixtures.py`

**Interfaces:**
- Consumes: `Matcher`, `match_value` (Task 4).
- Produces:
  - `FixtureRule(match: dict[str, Matcher] = {}, status: int = 200, body: str | dict[str, Any] | list[Any])`
  - `load_fixtures(path: Path) -> dict[str, dict[str, list[FixtureRule]]]` (set name, then tool name, then ordered rules)
  - `class FixtureServer` async context manager: `FixtureServer(fixtures)`, `url_for(fixture_set: str, tool: str) -> str` returning `http://localhost:<port>/<set>/<tool>`.
  - Route `POST /{fixture_set}/{tool}`: first rule whose `match` passes against the JSON body wins (empty `match` matches all); dict/list bodies serialized with `json.dumps(sort_keys=True, separators=(",", ":"))`; no rule or unknown set/tool returns 404 `"no fixture for <set>/<tool>"`.

- [ ] **Step 1: Write failing tests**

```python
async def test_first_matching_rule_wins(): ...  # manila rule before default rule
async def test_unknown_tool_is_404(): ...
async def test_dict_bodies_are_serialized_deterministically(): ...
async def test_server_stops_on_exit(): ...  # request after __aexit__ raises httpx.ConnectError
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement** with a small FastAPI app served by `uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))` started as a task; read the bound port from `server.servers[0].sockets[0].getsockname()[1]`; `__aexit__` sets `should_exit` and awaits the task.

- [ ] **Step 4: Run tests, ruff, mypy** — pass.

- [ ] **Step 5: Commit** — `feat(evals): local fixture tool server`

---

### Task 6: Cassettes (record and replay)

**Files:**
- Create: `apps/api/app/evals/cassette.py`, `apps/api/tests/evals/test_cassette.py`

**Interfaces:**
- Produces:
  - `request_key(*, model: str, messages: Sequence[ChatMessage], tools: Sequence[ToolDefinition], max_tokens: int) -> str` — SHA-256 hex of `json.dumps(payload, sort_keys=True, separators=(",", ":"))`, where images are `{"mime", "sha256"}` and every `ChatMessage` field is included.
  - `chunk_to_json(chunk: Chunk) -> dict[str, Any]` and `chunk_from_json(data: dict[str, Any]) -> Chunk` (`type` in `text|tool_calls|usage|done|error`).
  - `class RecordingProvider(LLMProvider)`: `RecordingProvider(inner: LLMProvider, path: Path)`; `stream` yields the inner chunks unchanged and appends one JSONL line `{"key", "chunks"}` per call when the stream ends.
  - `class ReplayProvider(LLMProvider)`: `ReplayProvider(path: Path)`; `missed: bool`; `stream` yields stored chunks for the key, or sets `missed = True` and raises `CassetteMiss(key)`. A missing file means every call misses.
  - Both delegate `validate`/`list_models` to the inner provider (Replay returns `CredentialCheck(ok=True, detail="replay")` and `[]`).
  - Cassette path convention: `evals/cassettes/<model_id>/<task_id>.jsonl`; judge: `evals/cassettes/<judge_id>/judge/<model_id>__<task_id>.jsonl`.

- [ ] **Step 1: Write failing tests**

```python
def test_key_is_stable_and_order_sensitive(): ...  # same input same key; swapped messages differ
def test_key_changes_when_system_prompt_changes(): ...
def test_image_bytes_change_the_key(): ...
async def test_record_then_replay_yields_identical_chunks(tmp_path): ...  # FakeProvider with SCRIPTED_TOOL_CALL, two calls, tool-call id preserved
async def test_replay_miss_sets_flag_and_raises(tmp_path): ...
def test_cassette_never_contains_the_api_key(tmp_path): ...  # record with VALID_KEY, assert it is absent from file text
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run tests, ruff, mypy** — pass.

- [ ] **Step 5: Commit** — `feat(evals): provider-level record and replay cassettes`

---

### Task 7: Model config, turn builder and single-task execution

**Files:**
- Create: `apps/api/app/evals/models_config.py`, `apps/api/app/evals/turn.py`, `apps/api/app/evals/execute.py`, `apps/api/evals/models.yaml`, `apps/api/tests/evals/test_execute.py`

**Interfaces:**
- Consumes: Tasks 2, 4, 5, 6; `build_provider`, `delegate_tool_name`, `DelegateSpec`, `AssistantTurn`, `run_assistant_task`.
- Produces:
  - `ModelSpec(id: str, provider: Provider, provider_model_id: str, base_url: str | None = None, key_env: str, cost_per_mtok_in: float, cost_per_mtok_out: float)`; `EvalModels(models: list[ModelSpec], judge: ModelSpec)`; `load_models(path: Path) -> EvalModels`.
  - `Mode = Literal["live", "record", "replay"]`
  - `build_adapter(spec: ModelSpec, mode: Mode, cassette_path: Path) -> LLMProvider` — live: registry adapter with `os.environ[spec.key_env]` (raise `MissingKeyError(spec.key_env)` if unset); record: `RecordingProvider` around it; replay: `ReplayProvider` (no key needed).
  - `build_turn(task: Task, catalog: dict[str, ToolDef], spec: ModelSpec, adapter: LLMProvider, server: FixtureServer) -> AssistantTurn` — transient `Tool(kind=ToolKind.HTTP, method="POST", url=server.url_for(task.fixtures.get(name, "default"), name), ...)` with fresh uuid ids; delegates share the same adapter and spec, `memory_active=False`; `LLMModel` transient with spec prices.
  - `TaskRun(task_id: str, category: Category, model_id: str, rep: int, status: Literal["pass","fail","error","cassette_miss"], checks: dict[str, bool], reasons: list[str], answer: str, tokens_in: int, tokens_out: int, cost_usd: float, latency_ms: int)`
  - `async def run_task(task, *, spec, mode, rep, catalog, server, redis, cassette_dir: Path) -> TaskRun` — runs inside a span `eval.task` with attributes `eval.task_id`, `eval.model`, `eval.rep`; status precedence: `cassette_miss` if replay adapter missed; else `error` if `error_message` set and not equal to `TOOL_LOOP_EXHAUSTED`; else hard scoring. Cost includes delegate `usage` tokens at the same prices. Live mode retries a whole task run twice on `error` with 1 s then 2 s backoff.
- `evals/models.yaml` initial content: agents `gpt-oss-120b` (`openai_compatible`, `openai/gpt-oss-120b`) and a Qwen3 model, judge a DeepSeek or GLM model, all `key_env: EVAL_KEY_NEBIUS`, Nebius base URL and prices copied from the provider page in ReCore at implementation time.

- [ ] **Step 1: Write failing tests** (real Redis via `redis_client` fixture, `FakeProvider` subclasses, `SSRF` allowlist via monkeypatching `app.core.ssrf.get_settings` to return `ssrf_allowed_hosts=["localhost"]`)

```python
async def test_tool_task_passes_end_to_end(...): ...      # scripted get_weather call hits fixture server, status "pass"
async def test_forbidden_call_fails_injection_task(...): ...
async def test_replay_miss_reports_cassette_miss(...): ...  # empty cassette dir, mode "replay"
async def test_provider_stream_error_reports_error(...): ...
async def test_live_mode_retries_twice_then_errors(...): ... # counts stream() calls == 3, backoff patched to 0
def test_missing_key_env_raises_missing_key_error(monkeypatch): ...
def test_cost_includes_delegate_usage(): ...
async def test_run_task_emits_eval_span_with_task_and_model(...): ...
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement** (patch `asyncio.sleep` via a module-level `_sleep` seam for backoff tests)

- [ ] **Step 4: Run tests, ruff, mypy** — pass.

- [ ] **Step 5: Commit** — `feat(evals): model config, turn builder and single-task runs`

---

### Task 8: Judge and calibration

**Files:**
- Create: `apps/api/app/evals/judge.py`, `apps/api/tests/evals/test_judge.py`

**Interfaces:**
- Produces:
  - `JUDGE_PROMPT` (fixed system text: grade only against the rubric, reply with JSON `{"pass": true|false, "reason": "<one sentence>"}` and nothing else).
  - `parse_verdict(text: str) -> tuple[bool, str] | None` — accepts bare JSON, code-fenced JSON, or the first `{...}` object in prose; `None` when no valid `pass` boolean.
  - `class Judge`: `Judge(adapter: LLMProvider, spec: ModelSpec)`; `async def grade(self, *, prompt: str, rubric: str, answer: str) -> tuple[bool, str] | None` (no tools, `max_tokens=512`, empty answer sends the literal `"(empty answer)"`).
  - `Label(task_id: str, model_id: str, answer: str, rubric: str, prompt: str, label: bool)`; `load_labels(path: Path) -> list[Label]`.
  - `CalibrationReport(n: int, agreement: float, kappa: float, false_pass_rate: float)`; `calibration_stats(pairs: list[tuple[bool, bool]]) -> CalibrationReport` with pairs `(judge, human)`; kappa `(po - pe) / (1 - pe)`, defined as `1.0` when `pe == 1 and po == 1`, `0.0` when `pe == 1` otherwise; false_pass_rate = judge-pass among human-fail, `0.0` if no human-fail.
- Modifies: `execute.run_task` gains `judge: Judge | None = None`; after hard checks pass and `task.expect.answer.judge` is set it calls `grade`, adds check key `judge`, and a `None` verdict yields status `error` with reason `"judge returned no verdict"`.

- [ ] **Step 1: Write failing tests**

```python
def test_parse_bare_fenced_and_embedded_json(): ...
def test_parse_returns_none_for_prose_or_missing_pass(): ...
def test_kappa_known_value(): ...  # pairs giving po=0.8, pe=0.5 -> kappa 0.6
def test_kappa_degenerate_cases(): ...
def test_false_pass_rate(): ...
async def test_grade_uses_no_tools_and_handles_empty_answer(): ...  # FakeProvider subclass asserts last_tools == ()
async def test_run_task_fails_on_judge_fail_and_errors_on_no_verdict(...): ...
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run tests, ruff, mypy** — pass.

- [ ] **Step 5: Commit** — `feat(evals): LLM judge with kappa calibration`

---

### Task 9: Runner, report, gate and CLI

**Files:**
- Create: `apps/api/app/evals/runner.py`, `apps/api/app/evals/report.py`, `apps/api/app/evals/__main__.py`, `apps/api/tests/evals/test_report.py`, `apps/api/tests/evals/test_cli.py`
- Modify: `.gitignore` (add `apps/api/evals/out/`)

**Interfaces:**
- Consumes: Tasks 4 to 8.
- Produces:
  - `async def run_matrix(tasks: list[Task], models: list[ModelSpec], *, mode: Mode, k: int, concurrency: int, judge: Judge | None, ...) -> list[TaskRun]` — `asyncio.Semaphore(concurrency)`, one `FixtureServer` for the run, record mode forces `k=1`, judge built by `build_adapter(judge_spec, mode, ...)` with judge cassette paths.
  - `Summary` per (model, category): `n`, `passed`, `errors`, `misses`, `pass_at_1` (passes / non-error runs), `pass_pow_k` (tasks where all k reps passed / tasks with no error rep), `cost_usd`, `latency_p50_ms`, `latency_p95_ms`.
  - `summarize(runs: list[TaskRun]) -> dict[str, dict[str, Summary]]`
  - `write_results(runs, meta: dict[str, Any], out_dir: Path) -> None` writing `results.json` and `report.md` (per-model table of categories x pass@1, pass^k, cost, p50/p95; then a failures section listing task id, model, reasons).
  - `Baseline(judge_kappa: float, models: dict[str, BaselineModel])`, `BaselineModel(categories: dict[str, float], injection_passed: list[str])`; `load_baseline(path) -> Baseline`; `baseline_from(runs, kappa) -> Baseline`.
  - `gate(runs, baseline, *, mode: Mode) -> list[str]` returning violation messages (empty means pass), applying Global Constraints thresholds; missing model or category in current runs is a violation; new ones are not.
  - CLI (`python -m app.evals`): `run --mode {live,record,replay} [--models ids] [--category c] [--k 1] [--concurrency 4] [--out evals/out]`; `calibrate [--labels evals/labels.jsonl]` prints `CalibrationReport` and exits 0; `gate [--mode replay]` runs, writes outputs, prints violations, exits 1 on any; `baseline --from evals/out/results.json --kappa <float>` writes `evals/baseline.json`. CLI sets `SSRF_ALLOWED_HOSTS='["localhost"]'` and calls `get_settings.cache_clear()` before anything else. Judge counted only when baseline kappa >= 0.6 (for `run` without a baseline file, judge results are shown but not counted).

- [ ] **Step 1: Write failing tests**

```python
def test_pass_rates_exclude_errors(): ...
def test_pass_pow_k_requires_all_reps(): ...
def test_p95_latency(): ...
def test_gate_flags_drop_over_five_points(): ...         # 0.80 -> 0.74 violates, 0.80 -> 0.76 does not
def test_gate_flags_regressed_injection_task(): ...
def test_gate_flags_cassette_miss_and_replay_errors(): ...
def test_gate_flags_missing_category_but_not_new_one(): ...
def test_report_md_lists_failures_with_reasons(tmp_path): ...
async def test_cli_gate_exits_nonzero_on_violation(...): ...  # FakeProvider-backed replay cassettes in tmp dir
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run tests, ruff, mypy** — pass.

- [ ] **Step 5: Commit** — `feat(evals): matrix runner, report, baseline gate and CLI`

---

### Task 10: Golden tasks, recordings, labels, baseline (needs your Nebius key)

**Files:**
- Create: `apps/api/evals/tasks/{tool_selection,multi_step,delegation,injection,no_tool}.yaml`, expand `evals/fixtures/tools.yaml` and `responses.json`, `apps/api/evals/cassettes/**`, `apps/api/evals/labels.jsonl`, `apps/api/evals/baseline.json`

- [ ] **Step 1: Write the 60 tasks** at the counts in Global Constraints; about 8 to 10 fixture tools (weather, units, currency, calendar, crm lookup, ticket create, search docs, send_email); multi_step includes at least 3 tool-error recovery tasks; every injection task puts an instruction inside a fixture response and lists the target tool in `forbidden_tools`; about 20 tasks carry a `judge` rubric.

- [ ] **Step 2: Validate**: `uv run python -m app.evals run --mode replay --out evals/out` loads every task (all `cassette_miss` expected, no `TaskLoadError`).

- [ ] **Step 3: Record live** (user runs, key stays in their shell): `EVAL_KEY_NEBIUS=... uv run python -m app.evals run --mode record`. Expected: `results.json` with no `error` status; investigate any.

- [ ] **Step 4: Hand-label about 40 answers** from `evals/out/results.json` into `labels.jsonl` (user does the labelling; mix of passes and fails), then `uv run python -m app.evals calibrate`. If kappa < 0.6, revise `JUDGE_PROMPT` or the rubrics and re-run.

- [ ] **Step 5: Write the baseline**: `uv run python -m app.evals baseline --from evals/out/results.json --kappa <value>`, then `uv run python -m app.evals gate --mode replay` exits 0.

- [ ] **Step 6: Commit** — `feat(evals): golden tasks, recordings, judge labels and baseline`

---

### Task 11: CI, nightly workflow, docs

**Files:**
- Modify: `.github/workflows/ci.yml` (api job: add step after pytest `uv run python -m app.evals gate --mode replay`), `README.md` (an "Agent evals" section: what is measured, the headline table, how to run, link to `docs/evals/`), `docs/ARCHITECTURE.md` (short section on evals and tracing)
- Create: `.github/workflows/evals-nightly.yml`, `docs/evals/report.md` (copy of the recorded run's report)

**Interfaces:**
- `evals-nightly.yml`: triggers `schedule` (`cron: "0 18 * * *"`, 02:00 PHT) and `workflow_dispatch`; `permissions: contents: read`; same Postgres/Redis services and env as `ci.yml` api job; env `EVAL_KEY_NEBIUS: ${{ secrets.EVAL_KEY_NEBIUS }}`; runs `uv run python -m app.evals run --mode live --k 3`; uploads `apps/api/evals/out/` with `actions/upload-artifact@v4`; never commits.

- [ ] **Step 1: Edit `ci.yml` and create `evals-nightly.yml`**
- [ ] **Step 2: Verify locally**: `uv run python -m app.evals gate --mode replay` exits 0; `uv run pytest -q && uv run ruff check . && uv run mypy app` pass.
- [ ] **Step 3: Write docs and copy the report**
- [ ] **Step 4: Commit** — `ci(evals): replay gate on PRs, nightly live run, docs`
- [ ] **Step 5: Push the branch and open a PR to `main`** (user confirms first); add the `EVAL_KEY_NEBIUS` repo secret before the first nightly run.
