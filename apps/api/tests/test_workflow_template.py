"""Substituting {{input}} / {{steps.<key>.output}} into a workflow step's prompt template."""

import pytest

from app.workflows.template import TemplateError, referenced_step_keys, references_input, render


def test_input_placeholder_is_substituted() -> None:
    assert render("Summarize: {{input}}", run_input="hello world", outputs={}) == "Summarize: hello world"


def test_step_output_placeholder_is_substituted() -> None:
    result = render(
        "Write from: {{steps.research.output}}",
        run_input="ignored",
        outputs={"research": "some facts"},
    )
    assert result == "Write from: some facts"


def test_whitespace_inside_braces_is_tolerated() -> None:
    assert render("{{ input }}", run_input="x", outputs={}) == "x"
    assert render("{{ steps.a.output }}", run_input="x", outputs={"a": "y"}) == "y"


def test_multiple_placeholders_in_one_template() -> None:
    result = render(
        "Input was {{input}}. Step a said {{steps.a.output}}, step b said {{steps.b.output}}.",
        run_input="Q",
        outputs={"a": "A", "b": "B"},
    )
    assert result == "Input was Q. Step a said A, step b said B."


def test_unknown_step_reference_raises() -> None:
    with pytest.raises(TemplateError):
        render("{{steps.missing.output}}", run_input="x", outputs={})


def test_referenced_step_keys_ignores_input() -> None:
    template = "{{input}} then {{steps.a.output}} then {{steps.b.output}}"
    assert referenced_step_keys(template) == {"a", "b"}


def test_referenced_step_keys_empty_for_plain_text() -> None:
    assert referenced_step_keys("no placeholders here") == set()


def test_a_template_with_no_placeholders_passes_through_unchanged() -> None:
    assert render("fixed instructions, no placeholders", run_input="x", outputs={}) == (
        "fixed instructions, no placeholders"
    )


def test_references_input_spots_the_placeholder_and_nothing_else() -> None:
    assert references_input("Summarize: {{ input }}")
    assert not references_input("From: {{steps.a.output}}")
    assert not references_input("plain text mentioning input")
