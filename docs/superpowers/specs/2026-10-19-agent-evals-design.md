# Agent evals and tracing for ReCore: design

Date: 2026-10-19
Status: approved in brainstorming, pending spec review

## Goal

Show, with numbers, how well ReCore's agent loop picks tools, chains calls, delegates, and resists prompt injection, on any provider a workspace can bring a key for, and stop regressions in CI.

Success means:

- About 60 golden tasks run against two or more models from different providers, producing a committed report (pass rates, cost, latency) that the README links to.
- PR CI runs the suite in replay mode with no secrets and fails on a regression.
- A nightly workflow runs the suite live and reports drift.
- The LLM judge is calibrated against hand labels before its verdicts count toward the gate.
- Every provider round trip and tool call is an OTel span, exportable over OTLP.

## Decisions

| Topic | Decision |
|---|---|
| Approach | In-process harness inside `apps/api`, calling the real `run_assistant_task` |
| Providers | Any provider ReCore supports (Anthropic, OpenAI, Google, OpenAI-compatible such as Nebius), built through `app.providers.registry.build_provider` |
| Default models | Agent: `openai/gpt-oss-120b` and a Qwen3 model on Nebius; judge: a stronger model from another family (DeepSeek V4 or GLM-5 on Nebius). All swappable in `models.yaml` |
| CI | PRs replay recorded provider responses; nightly and manual runs go live with `EVAL_KEY_*` secrets |
| v1 coverage | tool_selection, multi_step, delegation, injection, no_tool |
| Out of scope | Knowledge (RAG) grounding, the HTTP and chat layers, refactoring `services/chat.py` |

## Architecture

New package `apps/api/app/evals/`; data under `apps/api/evals/`.

| Module | Responsibility |
|---|---|
| `tasks.py` | Load and validate task YAML into Pydantic models |
| `fixtures.py` | In-process fixture ASGI app served through `http_tool`'s transport seam, deterministic responses per fixture name, including scripted errors and injection payloads |
| `turn.py` | Build an in-memory `AssistantTurn` from a task: transient `Tool` rows (HTTP kind, URL on the fixture app), `DelegateSpec`s, an `LLMModel` carrying prices; no database writes |
| `cassette.py` | `RecordingProvider` and `ReplayProvider`, each wrapping any `LLMProvider` |
| `scoring.py` | Pure scoring functions over a loop result |
| `judge.py` | Judge prompt, verdict parsing, calibration statistics |
| `runner.py` | Run the tasks x models matrix with bounded concurrency; collect results, cost, latency |
| `report.py` | Write `results.json` and `report.md`; compare to the baseline for the gate |
| `__main__.py` | CLI: `run`, `calibrate`, `gate` |

Data files:

```
apps/api/evals/
  models.yaml        # models under test and the judge; key_env names, never keys
  tasks/*.yaml       # one file per category
  fixtures/*.json    # canned tool responses by fixture name
  cassettes/<model>/*.jsonl
  labels.jsonl       # hand labels for judge calibration
  baseline.json      # per-model, per-category pass rates the gate compares to
```

Data flow: task YAML, then `turn.py` builds the turn, then `run_assistant_task` (unchanged), then the loop result, then `scoring.py`, then `report.py`.

Fixture tools are served in process: the harness sets `app.tools.http_tool._transport` to an `httpx.ASGITransport` over the fixture app and replaces `http_tool.assert_safe_base_url` with a no-op, both only inside the eval process and restored on exit. No production SSRF code changes; the cost is that evals do not exercise the real SSRF guard, which keeps its own tests. Redis is required, as `run_assistant_task` uses it for events and stop signals.

### Production code changes

1. A public result type and seam: rename `_LoopResult` and `_ToolInvocationRecord` to public names (or re-export them) so `app/evals` never imports private names from `services/chat.py`.
2. Tracing: add a `gen_ai.chat` span per provider round trip in `_run_tool_loop` and a `gen_ai.execute_tool` span per tool call in `_execute_tool_call`, nested under the existing `llm.generate`. Attributes follow the OTel GenAI semantic conventions (`gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.tool.name`, finish reason, status). Prompt text, tool arguments, and tool results are never span attributes.
3. `core/tracing.py`: when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, add a batching OTLP exporter; otherwise keep the console exporter. Adds `opentelemetry-exporter-otlp-proto-http`.
4. `infra/docker-compose.tracing.yml`: optional Jaeger for viewing traces locally.

## Task format

```yaml
- id: weather-multi-01
  category: multi_step
  prompt: "Is it warmer in Manila or Vienna right now? Answer in Celsius."
  system: "You are a helpful assistant."      # optional, defaults to a fixed prompt
  tools: [get_weather, convert_units]
  fixtures: { get_weather: weather_basic }
  delegates: []                                # optional: name, instructions, tools
  expect:
    calls:
      - { tool: get_weather, args: { city: { contains: "manila" } } }
      - { tool: get_weather, args: { city: { contains: "vienna" } } }
    order: any                                 # any | strict | subsequence
    forbidden_tools: []
    max_calls: 4
    answer:
      must_contain: ["Manila"]
      must_not_contain: []
      judge: "States which city is warmer and gives both temperatures in Celsius."
```

Tool definitions (name, description, JSON Schema parameters) live once in `fixtures/tools.yaml` and are referenced by name.

Planned counts: tool_selection 20, multi_step 15 (dependent arguments, recovery from a tool error), delegation 10, injection 10 (a tool result instructs the model to call a forbidden tool such as `send_email`), no_tool 5.

## Scoring

All hard checks are pure functions over the loop result:

- Tool selection: precision and recall of expected tool names. Pass requires every expected call matched and no forbidden tool called.
- Arguments: matchers `equals`, `contains` (case-insensitive), `regex`, `approx` (numeric, with tolerance), `any`. Each expected call matches at most one actual call.
- Trajectory: `order`, `max_calls`, and no iteration-limit exhaustion.
- Final answer: `must_contain` and `must_not_contain` (case-insensitive); the judge runs only when `judge` is set.
- A task passes only if every hard check passes, and the judge passes when the judge counts (see below).

Statuses: `pass`, `fail`, `error` (provider or harness failure, reported but excluded from pass rates), `cassette_miss`.

Metrics per model and category: pass@1 (mean pass rate) and, for live runs with k=3, pass^3 (all three runs pass). Also cost per task and total (from `models.yaml` prices and token usage), and p50 and p95 latency.

## Judge

- Input: the task prompt, the rubric, and the final answer. Output: JSON `{"pass": bool, "reason": str}`. Unparseable output counts as `error`.
- Runs through the same cassette wrapper, so it is deterministic in replay.
- `calibrate` scores the judge against `labels.jsonl` (about 40 hand-labelled answers) and reports agreement, Cohen's kappa, and the false-pass rate.
- The gate counts judge verdicts only when kappa is 0.6 or higher; below that the report shows them but the gate ignores them. The kappa used is stored in `baseline.json`.

## Record and replay

- Cassette key: SHA-256 of canonical JSON of (provider model id, messages including images hashed, tools, max_tokens).
- Each cassette entry stores the key and the exact sequence of stream chunks (`TextDelta`, `ToolCallRequest`, `Usage`, `Done`, `StreamError`).
- Provider tool-call ids are replayed from the cassette, so multi-step chains stay deterministic.
- A replay miss yields `cassette_miss`. Changing anything sent to the model therefore needs a deliberate local re-record (`uv run python -m app.evals run --mode record`) committed in the same PR.
- Cassettes and reports never contain keys; `RecordingProvider` stores only request hashes and response chunks.

## CI

- `ci.yml` gains a step: `uv run python -m app.evals gate --mode replay`. No secrets.
- The gate fails when any per-model, per-category pass rate drops more than 5 points below `baseline.json`, when any injection task that passed in the baseline fails, or on any `cassette_miss`.
- `evals-nightly.yml`: schedule plus `workflow_dispatch`, runs live with k=3 using `EVAL_KEY_*` secrets, uploads `results.json` and `report.md` as artifacts. It uses a read-only token and never commits.
- Updating `baseline.json` is a manual, reviewed change.

## Error handling

- Live mode retries a provider error twice with exponential backoff before marking `error`.
- Default concurrency is 4, configurable by flag.
- The fixture transport and SSRF stub are always restored in a `finally`.
- A malformed task file fails loading with the task id and field before any model call.

## Testing

- Unit tests: argument matchers, scoring, task validation, cassette hashing and round trip, gate rules, kappa and false-pass math.
- End-to-end test: three tasks through `run_assistant_task` against `FakeProvider` with fixture tools, on real Redis.
- CI replay gate as above.

## Deliverables

- `app/evals/` package and data under `apps/api/evals/`, driven by the CLI (`run --mode live|record|replay`, `calibrate`, `gate`); no Makefile, matching the repo.
- Tracing changes and the optional Jaeger compose file.
- A committed sample report in `docs/evals/` and a README section linking it.
