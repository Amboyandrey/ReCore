"""Fixture tools — canned responses served in process through http_tool's own transport seam."""

import json
import uuid
from pathlib import Path

import pytest

from app.evals.fixtures import FixtureRule, FixtureTools, load_fixtures, render_body, url_for
from app.evals.tasks import Matcher
from app.models import Tool, ToolKind
from app.tools import http_tool

FIXTURES = {
    "weather_basic": {
        "get_weather": [
            FixtureRule(match={"city": Matcher(contains="manila")}, body={"temp_c": 31, "city": "Manila"}),
            FixtureRule(body={"temp_c": 12, "city": "elsewhere"}),
        ]
    }
}


def _tool(fixture_set: str, name: str) -> Tool:
    return Tool(
        id=uuid.uuid4(),
        name=name,
        description="d",
        parameters={"type": "object"},
        kind=ToolKind.HTTP,
        method="POST",
        url=url_for(fixture_set, name),
    )


async def test_first_matching_rule_wins() -> None:
    with FixtureTools(FIXTURES):
        manila = await http_tool.execute(_tool("weather_basic", "get_weather"), {"city": "Manila"})
        other = await http_tool.execute(_tool("weather_basic", "get_weather"), {"city": "Oslo"})
    assert manila.ok and json.loads(manila.content)["temp_c"] == 31
    assert other.ok and json.loads(other.content)["temp_c"] == 12


async def test_unknown_tool_is_404() -> None:
    with FixtureTools(FIXTURES):
        result = await http_tool.execute(_tool("weather_basic", "teleport"), {})
    assert not result.ok
    assert "no fixture for weather_basic/teleport" in result.content


def test_dict_bodies_are_serialized_deterministically() -> None:
    assert render_body({"b": 1, "a": [2, 1]}) == '{"a":[2,1],"b":1}'
    assert render_body("plain text") == "plain text"


def test_transport_and_guard_are_restored_on_exit() -> None:
    original_transport = http_tool._transport
    original_guard = http_tool.assert_safe_base_url
    with pytest.raises(RuntimeError), FixtureTools(FIXTURES):
        assert http_tool._transport is not original_transport
        assert http_tool.assert_safe_base_url is not original_guard
        raise RuntimeError("boom")
    assert http_tool._transport is original_transport
    assert http_tool.assert_safe_base_url is original_guard


def test_load_fixtures_reads_rules(tmp_path: Path) -> None:
    path = tmp_path / "responses.json"
    path.write_text(json.dumps({"s": {"t": [{"status": 500, "body": "down"}]}}))
    loaded = load_fixtures(path)
    assert loaded["s"]["t"][0].status == 500
