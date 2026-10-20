"""The CLI end to end — record with a fake provider, write a baseline, then gate in replay."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from app.evals import execute, models_config
from app.evals.__main__ import amain
from app.providers.base import ToolCall
from app.providers.fake import VALID_KEY, FakeProvider

SOURCE = Path(__file__).parents[2] / "evals"

TASKS = """
- id: weather-01
  category: tool_selection
  prompt: "Weather in Manila?"
  tools: [get_weather]
  expect:
    calls: [{ tool: get_weather, args: { city: { contains: manila } } }]
    max_calls: 2
"""

MODELS = """
models:
  - { id: fake, provider: openai_compatible, provider_model_id: fake-small, key_env: EVAL_KEY_TEST,
      cost_per_mtok_in: 1, cost_per_mtok_out: 1 }
judge: { id: judge, provider: openai_compatible, provider_model_id: fake-small, key_env: EVAL_KEY_TEST,
         cost_per_mtok_in: 1, cost_per_mtok_out: 1 }
"""


class _Weather(FakeProvider):
    SCRIPTED_TOOL_CALL = ToolCall(id="c1", name="get_weather", arguments={"city": "Manila"})


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
def evals_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "evals"
    (root / "tasks").mkdir(parents=True)
    (root / "fixtures").mkdir()
    shutil.copy(SOURCE / "fixtures" / "tools.yaml", root / "fixtures" / "tools.yaml")
    (root / "fixtures" / "responses.json").write_text(
        json.dumps({"default": {"get_weather": [{"body": "31C"}]}})
    )
    (root / "tasks" / "a.yaml").write_text(TASKS)
    (root / "models.yaml").write_text(MODELS)
    monkeypatch.setenv("EVAL_KEY_TEST", VALID_KEY)
    monkeypatch.setattr(execute, "_sleep", _no_sleep)
    fake: Any = lambda provider, api_key, base_url: _Weather(api_key)  # noqa: E731
    monkeypatch.setattr(models_config, "build_provider", fake)
    return root


async def test_record_baseline_then_gate_passes(evals_dir: Path) -> None:
    args = ["--evals-dir", str(evals_dir)]
    assert await amain(["run", "--mode", "record", *args]) == 0
    results = evals_dir / "out" / "results.json"
    assert json.loads(results.read_text())["results"][0]["status"] == "pass"
    assert await amain(["baseline", "--from", str(results), "--kappa", "0.7", *args]) == 0
    assert (evals_dir / "baseline.json").exists()
    assert await amain(["gate", *args]) == 0


async def test_cli_gate_exits_nonzero_on_violation(evals_dir: Path) -> None:
    args = ["--evals-dir", str(evals_dir)]
    assert await amain(["run", "--mode", "record", *args]) == 0
    baseline = {
        "judge_kappa": 0.7,
        "models": {"fake": {"categories": {"delegation": 1.0}, "injection_passed": []}},
    }
    (evals_dir / "baseline.json").write_text(json.dumps(baseline))
    assert await amain(["gate", *args]) == 1


async def test_gate_without_baseline_fails(evals_dir: Path) -> None:
    assert await amain(["gate", "--evals-dir", str(evals_dir)]) == 1


async def test_gate_reports_misses_without_cassettes(evals_dir: Path) -> None:
    baseline = {"judge_kappa": 0.7, "models": {"fake": {"categories": {}, "injection_passed": []}}}
    (evals_dir / "baseline.json").write_text(json.dumps(baseline))
    assert await amain(["gate", "--evals-dir", str(evals_dir)]) == 1
    report = (evals_dir / "out" / "report.md").read_text()
    assert "cassette_miss" in report


async def test_live_run_checks_every_key_before_any_call(
    evals_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    judge_line = MODELS.index("judge:")
    models = MODELS[:judge_line] + MODELS[judge_line:].replace("EVAL_KEY_TEST", "EVAL_KEY_JUDGE", 1)
    (evals_dir / "models.yaml").write_text(models)
    monkeypatch.delenv("EVAL_KEY_JUDGE", raising=False)
    called: list[str] = []
    monkeypatch.setattr(
        models_config, "build_provider", lambda *a, **k: called.append("x") or _Weather(VALID_KEY)
    )
    assert await amain(["run", "--mode", "live", "--evals-dir", str(evals_dir)]) == 2
    assert not called
    assert not (evals_dir / "out" / "results.json").exists()
