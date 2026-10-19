"""Fixture tools — deterministic canned responses served in process through http_tool's transport seam.

Eval tools are ordinary HTTP tools whose URLs point at `FIXTURE_BASE`; while `FixtureTools` is
active, http_tool sends them to an in-process ASGI app instead of the network, and the SSRF guard
is stubbed because no request ever leaves the process. Both are restored on exit.
"""

import json
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.evals.scoring import match_value
from app.evals.tasks import Matcher
from app.tools import http_tool

FIXTURE_BASE = "http://fixtures.evals"


class FixtureRule(BaseModel):
    """One canned response; the first rule whose argument matchers all pass is served."""

    model_config = ConfigDict(extra="forbid")

    match: dict[str, Matcher] = Field(default_factory=dict)
    status: int = 200
    body: str | dict[str, Any] | list[Any]


Fixtures = dict[str, dict[str, list[FixtureRule]]]

_FIXTURES_ADAPTER = TypeAdapter(Fixtures)


def load_fixtures(path: Path) -> Fixtures:
    """Read fixture rules keyed by fixture set, then tool name."""
    return _FIXTURES_ADAPTER.validate_json(path.read_text())


def render_body(body: str | dict[str, Any] | list[Any]) -> str:
    """Serialize a rule body the same way every time, so recorded requests replay byte for byte."""
    if isinstance(body, str):
        return body
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def url_for(fixture_set: str, tool: str) -> str:
    return f"{FIXTURE_BASE}/{fixture_set}/{tool}"


def fixture_app(fixtures: Fixtures) -> FastAPI:
    """An ASGI app answering `POST /{fixture_set}/{tool}` from the matching rule."""
    app = FastAPI()

    @app.api_route("/{fixture_set}/{tool}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def serve(fixture_set: str, tool: str, request: Request) -> PlainTextResponse:
        raw = await request.body()
        args: Any = json.loads(raw) if raw else dict(request.query_params)
        if not isinstance(args, dict):
            args = {}
        for rule in fixtures.get(fixture_set, {}).get(tool, []):
            if all(name in args and match_value(m, args[name]) for name, m in rule.match.items()):
                return PlainTextResponse(render_body(rule.body), status_code=rule.status)
        return PlainTextResponse(f"no fixture for {fixture_set}/{tool}", status_code=404)

    return app


def _no_ssrf_check(url: str) -> None:
    """Stands in for the SSRF guard while fixtures are active; fixture requests never leave the process."""


class FixtureTools:
    """Routes every HTTP tool call in this process to the fixture app until exit."""

    def __init__(self, fixtures: Fixtures) -> None:
        self._fixtures = fixtures
        self._saved: tuple[httpx.AsyncBaseTransport | None, Any] | None = None

    def __enter__(self) -> "FixtureTools":
        # The guard is re-imported into http_tool's namespace, so it is swapped there, by name.
        self._saved = (http_tool._transport, getattr(http_tool, "assert_safe_base_url"))  # noqa: B009
        http_tool._transport = httpx.ASGITransport(app=fixture_app(self._fixtures))
        setattr(http_tool, "assert_safe_base_url", _no_ssrf_check)  # noqa: B010
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._saved is not None
        http_tool._transport = self._saved[0]
        setattr(http_tool, "assert_safe_base_url", self._saved[1])  # noqa: B010
        self._saved = None
