"""Rendering a workflow step's prompt: substitution only, never a real template language — no
eval, no attribute access, no loops. Two placeholders: `{{input}}` (the run's own starting text)
and `{{steps.<key>.output}}` (an earlier step's completed output). Deliberately not Jinja or any
other template engine: a workflow's steps are authored by a workspace member, and keeping the
placeholder grammar this narrow means there's no server-side-template-injection surface to worry
about, however untrusted that authorship turns out to be.
"""

import re

_PLACEHOLDER = re.compile(r"\{\{\s*(input|steps\.([a-z0-9_]+)\.output)\s*\}\}")


class TemplateError(Exception):
    """A template references a step key that has no resolvable output — either it doesn't exist
    at all, or (checked at save time in services/workflows.py, not here) it isn't an *earlier*
    step. Raised here only as a defensive backstop; save-time validation is the primary guard.
    """


def referenced_step_keys(template: str) -> set[str]:
    """Every step key a template references via `{{steps.<key>.output}}` — what
    services/workflows.py checks against the workflow's own earlier step keys when it's saved."""
    return {match.group(2) for match in _PLACEHOLDER.finditer(template) if match.group(2)}


def references_input(template: str) -> bool:
    """Whether a template reads `{{input}}` at all — the steps that do are the ones the run's
    attached files are handed to as well, since the files are part of the run's input."""
    return any(match.group(2) is None for match in _PLACEHOLDER.finditer(template))


def render(template: str, *, run_input: str, outputs: dict[str, str]) -> str:
    """Substitute every placeholder in `template` — `run_input` for `{{input}}`, `outputs[key]`
    for `{{steps.<key>.output}}`. Raises TemplateError if a referenced key isn't in `outputs`."""

    def _substitute(match: re.Match[str]) -> str:
        key = match.group(2)
        if key is None:
            return run_input
        if key not in outputs:
            raise TemplateError(f"Unknown step reference: steps.{key}.output")
        return outputs[key]

    return _PLACEHOLDER.sub(_substitute, template)
