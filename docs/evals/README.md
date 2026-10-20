# Agent evals

How to record, calibrate, gate and read ReCore's agent evals. The design is in
[`docs/superpowers/specs/2026-10-19-agent-evals-design.md`](../superpowers/specs/2026-10-19-agent-evals-design.md).

## Layout

| Path | What it is |
|---|---|
| `apps/api/app/evals/` | Harness: task loading, fixture tools, cassettes, scoring, judge, runner, report, CLI |
| `apps/api/evals/models.yaml` | Models under test and the judge; `key_env` names the env var holding each key |
| `apps/api/evals/tasks/*.yaml` | 60 golden tasks, one file per category |
| `apps/api/evals/fixtures/` | Tool definitions and the canned responses the fixture tools serve |
| `apps/api/evals/cassettes/` | Recorded provider responses that PR CI replays |
| `apps/api/evals/labels.jsonl` | Hand labels the judge is calibrated against |
| `apps/api/evals/baseline.json` | Pass rates the gate compares against |

All commands run from `apps/api` with Postgres and Redis up (`docker compose -f infra/docker-compose.yml up -d postgres redis`).

## First-time setup

1. Check `evals/models.yaml`: model ids, base URLs and prices. Confirm the judge id exists on your provider.
2. Record every task live (keys stay in your shell, never in files):

   ```bash
   EVAL_KEY_NEBIUS=... EVAL_KEY_GROQ=... uv run python -m app.evals run --mode record
   ```

   Investigate any `error` in `evals/out/report.md` before going on.
3. Hand-label about 40 answers from `evals/out/results.json` into `evals/labels.jsonl`, a mix of passes and fails, one JSON object per line:

   ```json
   {"task_id": "ms-warmer-manila-vienna", "model_id": "gpt-oss-120b", "prompt": "...", "rubric": "...", "answer": "...", "label": true}
   ```

4. Calibrate the judge: `uv run python -m app.evals calibrate --mode record`. If kappa is below 0.6, tighten the rubrics or `JUDGE_PROMPT` in `app/evals/judge.py` and re-run.
5. Write the baseline: `uv run python -m app.evals baseline --from evals/out/results.json --kappa <kappa>`.
6. Check the gate: `uv run python -m app.evals gate --mode replay` exits 0. Copy `evals/out/report.md` to `docs/evals/report.md` and commit cassettes, labels, baseline and report together.

## Day to day

- A change that alters anything sent to the model (prompts, tool definitions, loop behaviour) causes `cassette_miss`. Re-record the affected tasks (`--category`, `--models`) and commit the new cassettes in the same PR.
- Raising or lowering `baseline.json` is a deliberate, reviewed change.
- The nightly workflow (`.github/workflows/evals-nightly.yml`) runs live with `--k 3` and uploads the report as an artifact; add `EVAL_KEY_NEBIUS` and `EVAL_KEY_GROQ` as repository secrets first.

## Reading the report

- **pass@1**: share of non-error runs that passed.
- **pass^k**: share of tasks where every one of the k repeats passed; a consistency measure.
- **error**: provider or harness failure after two retries; excluded from pass rates, reported separately.
- **cassette_miss**: replay had no recording for a request; always fails the gate.

## Gate rules

The PR gate fails when any model's category drops more than 5 points below the baseline, when an injection task that passed in the baseline fails, on any `cassette_miss`, or on any `error` in replay. Judge verdicts count only when the baseline's kappa is at least 0.6.
