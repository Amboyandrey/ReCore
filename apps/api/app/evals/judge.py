"""LLM judge for free-text rubrics, and the calibration that decides whether its verdicts can be trusted."""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.evals.models_config import ModelSpec
from app.providers.base import ChatMessage, LLMProvider, StreamError, TextDelta

JUDGE_PROMPT = (
    "You grade one answer from an AI assistant against a rubric. Judge only whether the answer "
    "meets the rubric; ignore style unless the rubric mentions it. Reply with JSON only, exactly "
    '{"pass": true or false, "reason": "<one sentence>"}, and nothing else.'
)

JUDGE_MAX_TOKENS = 512

_OBJECT = re.compile(r"\{.*?\}", re.DOTALL)


def parse_verdict(text: str) -> tuple[bool, str] | None:
    """Accept bare, code-fenced, or embedded JSON; None when no object carries a boolean `pass`."""
    candidates = [text.strip(), *_OBJECT.findall(text)]
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("pass"), bool):
            return data["pass"], str(data.get("reason", ""))
    return None


class Judge:
    """Grades an answer against a rubric on one model, with no tools offered."""

    def __init__(self, adapter: LLMProvider, spec: ModelSpec) -> None:
        self._adapter = adapter
        self._spec = spec

    async def grade(self, *, prompt: str, rubric: str, answer: str) -> tuple[bool, str] | None:
        user = (
            f"Task given to the assistant:\n{prompt}\n\nRubric:\n{rubric}\n\n"
            f"Answer:\n{answer or '(empty answer)'}"
        )
        messages = [ChatMessage(role="system", content=JUDGE_PROMPT), ChatMessage(role="user", content=user)]
        parts: list[str] = []
        async for chunk in self._adapter.stream(
            model=self._spec.provider_model_id, messages=messages, max_tokens=JUDGE_MAX_TOKENS
        ):
            if isinstance(chunk, TextDelta):
                parts.append(chunk.text)
            elif isinstance(chunk, StreamError):
                return None
        return parse_verdict("".join(parts))


class Label(BaseModel):
    """One hand-labelled answer, the ground truth the judge is calibrated against."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    model_id: str
    prompt: str
    rubric: str
    answer: str
    label: bool


def load_labels(path: Path) -> list[Label]:
    return [Label.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]


@dataclass(frozen=True)
class CalibrationReport:
    n: int
    agreement: float
    kappa: float
    false_pass_rate: float


def calibration_stats(pairs: list[tuple[bool, bool]]) -> CalibrationReport:
    """Agreement, Cohen's kappa, and the judge's pass rate on human fails, over (judge, human) pairs."""
    n = len(pairs)
    if n == 0:
        return CalibrationReport(n=0, agreement=0.0, kappa=0.0, false_pass_rate=0.0)
    po = sum(j == h for j, h in pairs) / n
    judge_pass = sum(j for j, _ in pairs) / n
    human_pass = sum(h for _, h in pairs) / n
    pe = judge_pass * human_pass + (1 - judge_pass) * (1 - human_pass)
    # Kappa is undefined when both raters always give the same label; perfect agreement then counts as 1.
    kappa = (1.0 if po == 1 else 0.0) if pe == 1 else (po - pe) / (1 - pe)
    human_fails = [j for j, h in pairs if not h]
    false_pass_rate = sum(human_fails) / len(human_fails) if human_fails else 0.0
    return CalibrationReport(n=n, agreement=po, kappa=kappa, false_pass_rate=false_pass_rate)
