"""Loading golden tasks — valid files round-trip, and every malformed task names itself."""

from pathlib import Path

import pytest

from app.evals.tasks import TaskLoadError, load_tasks, load_tool_catalog

CATALOG_PATH = Path(__file__).parents[2] / "evals" / "fixtures" / "tools.yaml"

VALID = """
- id: weather-01
  category: tool_selection
  prompt: "Weather in Manila?"
  tools: [get_weather]
  expect:
    calls:
      - { tool: get_weather, args: { city: { contains: "manila" } } }
    max_calls: 2
    answer: { must_contain: ["Manila"] }
"""


def _write(tmp_path: Path, body: str) -> Path:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "a.yaml").write_text(body)
    return tasks_dir


def test_valid_task_file_loads() -> None:
    catalog = load_tool_catalog(CATALOG_PATH)
    assert {"get_weather", "convert_units", "send_email"} <= catalog.keys()


def test_valid_task_round_trips(tmp_path: Path) -> None:
    tasks = load_tasks(_write(tmp_path, VALID), load_tool_catalog(CATALOG_PATH))
    assert len(tasks) == 1
    task = tasks[0]
    assert task.id == "weather-01"
    assert task.expect.calls[0].args["city"].contains == "manila"
    assert task.expect.order == "any"
    assert task.system


def test_unknown_tool_names_the_task(tmp_path: Path) -> None:
    body = VALID.replace("tools: [get_weather]", "tools: [get_weather, teleport]")
    with pytest.raises(TaskLoadError, match="weather-01.*teleport"):
        load_tasks(_write(tmp_path, body), load_tool_catalog(CATALOG_PATH))


def test_unknown_delegate_tool_names_the_task(tmp_path: Path) -> None:
    body = VALID + "  delegates: [{ name: helper, instructions: help, tools: [teleport] }]\n"
    with pytest.raises(TaskLoadError, match="weather-01.*teleport"):
        load_tasks(_write(tmp_path, body), load_tool_catalog(CATALOG_PATH))


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(TaskLoadError, match="duplicate.*weather-01"):
        load_tasks(_write(tmp_path, VALID + VALID), load_tool_catalog(CATALOG_PATH))


def test_matcher_with_two_keys_is_rejected(tmp_path: Path) -> None:
    body = VALID.replace('{ contains: "manila" }', '{ contains: "manila", equals: "Manila" }')
    with pytest.raises(TaskLoadError, match="weather-01"):
        load_tasks(_write(tmp_path, body), load_tool_catalog(CATALOG_PATH))


def test_approx_without_tol_is_rejected(tmp_path: Path) -> None:
    body = VALID.replace('{ contains: "manila" }', "{ approx: 30 }")
    with pytest.raises(TaskLoadError, match="weather-01"):
        load_tasks(_write(tmp_path, body), load_tool_catalog(CATALOG_PATH))
