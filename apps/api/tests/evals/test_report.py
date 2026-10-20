"""Summaries, the markdown report, and the baseline gate's rules."""

import json
from pathlib import Path

from app.evals.execute import TaskRun
from app.evals.report import Baseline, BaselineModel, baseline_from, gate, summarize, write_results


def _run(
    task: str,
    status: str = "pass",
    category: str = "tool_selection",
    rep: int = 0,
    latency: int = 100,
    model: str = "m",
) -> TaskRun:
    return TaskRun(
        task_id=task,
        category=category,
        model_id=model,
        rep=rep,
        status=status,  # type: ignore[arg-type]
        latency_ms=latency,
        cost_usd=0.01,
        reasons=[] if status == "pass" else ["why"],
    )


def _baseline(rate: float = 0.8, injection: list[str] | None = None) -> Baseline:
    categories = {"tool_selection": rate}
    if injection is not None:
        categories["injection"] = 1.0
    return Baseline(
        judge_kappa=0.7, models={"m": BaselineModel(categories=categories, injection_passed=injection or [])}
    )


def test_pass_rates_exclude_errors() -> None:
    s = summarize([_run("a"), _run("b", "fail"), _run("c", "error")])["m"]["tool_selection"]
    assert s.n == 3 and s.errors == 1
    assert s.pass_at_1 == 0.5


def test_pass_pow_k_requires_all_reps() -> None:
    runs = [_run("a", rep=0), _run("a", rep=1), _run("b", rep=0), _run("b", "fail", rep=1)]
    s = summarize(runs)["m"]["tool_selection"]
    assert s.pass_at_1 == 0.75
    assert s.pass_pow_k == 0.5


def test_p95_latency() -> None:
    runs = [_run(f"t{i}", latency=i) for i in range(1, 101)]
    s = summarize(runs)["m"]["tool_selection"]
    assert s.latency_p50_ms == 50
    assert s.latency_p95_ms == 95


def test_gate_flags_drop_over_five_points() -> None:
    def runs(passes: int) -> list[TaskRun]:
        return [_run(f"t{i}", "pass" if i < passes else "fail") for i in range(50)]

    assert gate(runs(37), _baseline(0.80), mode="replay")  # 0.74
    assert not gate(runs(38), _baseline(0.80), mode="replay")  # 0.76


def test_gate_flags_regressed_injection_task() -> None:
    runs = [_run("a"), _run("inj-1", "fail", category="injection")]
    violations = gate(runs, _baseline(1.0, injection=["inj-1"]), mode="replay")
    assert any("inj-1" in v for v in violations)


def test_gate_flags_cassette_miss_and_replay_errors() -> None:
    assert any(
        "cassette" in v for v in gate([_run("a"), _run("b", "cassette_miss")], _baseline(0.0), mode="replay")
    )
    assert any("error" in v for v in gate([_run("a"), _run("b", "error")], _baseline(0.0), mode="replay"))
    assert not gate([_run("a"), _run("b", "error")], _baseline(0.0), mode="live")


def test_gate_flags_missing_category_but_not_new_one() -> None:
    missing = gate([_run("a", category="no_tool")], _baseline(0.5), mode="replay")
    assert any("tool_selection" in v for v in missing)
    extra = gate([_run("a"), _run("b", category="no_tool", status="fail")], _baseline(0.5), mode="replay")
    assert not extra


def test_baseline_from_runs() -> None:
    runs = [
        _run("a"),
        _run("b", "fail"),
        _run("inj-1", category="injection"),
        _run("inj-2", "fail", category="injection"),
    ]
    baseline = baseline_from(runs, kappa=0.65)
    assert baseline.judge_kappa == 0.65
    assert baseline.models["m"].categories["tool_selection"] == 0.5
    assert baseline.models["m"].injection_passed == ["inj-1"]


def test_report_md_lists_failures_with_reasons(tmp_path: Path) -> None:
    write_results([_run("a"), _run("b", "fail")], {"mode": "replay", "k": 1}, tmp_path)
    report = (tmp_path / "report.md").read_text()
    assert "tool_selection" in report
    assert "b" in report and "why" in report
    data = json.loads((tmp_path / "results.json").read_text())
    assert data["run"]["mode"] == "replay"
    assert len(data["results"]) == 2


def test_baseline_counts_judge_failures_once_kappa_is_trusted() -> None:
    judged = _run("a")
    judged.checks = {"judge": False}
    runs = [judged, _run("b")]
    assert baseline_from(runs, kappa=0.7).models["m"].categories["tool_selection"] == 0.5
    assert baseline_from(runs, kappa=0.5).models["m"].categories["tool_selection"] == 1.0
