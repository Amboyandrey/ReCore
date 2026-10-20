"""CLI: `python -m app.evals run|gate|calibrate|baseline` — see docs/superpowers/specs for the design."""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from app.core.redis import new_redis_client
from app.core.tracing import configure_tracing
from app.evals.execute import TaskRun
from app.evals.fixtures import FixtureTools, load_fixtures
from app.evals.judge import Judge, calibration_stats, load_labels
from app.evals.models_config import Mode, build_adapter, load_models
from app.evals.report import baseline_from, gate, load_baseline, load_runs, write_results
from app.evals.runner import run_matrix
from app.evals.tasks import load_tasks, load_tool_catalog

EVALS_DIR = Path(__file__).resolve().parents[2] / "evals"

# The judge's verdicts only count toward pass/fail once calibration shows it agrees with humans this well.
MIN_JUDGE_KAPPA = 0.6


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.evals")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "gate"):
        p = sub.add_parser(name)
        p.add_argument(
            "--mode", choices=["live", "record", "replay"], default="replay" if name == "gate" else "live"
        )
        p.add_argument("--models", help="comma-separated model ids from models.yaml (default: all)")
        p.add_argument("--category", help="only run tasks in this category")
        p.add_argument("--k", type=int, default=1)
        p.add_argument("--concurrency", type=int, default=4)
        p.add_argument("--out", type=Path)
    p = sub.add_parser("calibrate")
    p.add_argument("--mode", choices=["live", "record", "replay"], default="live")
    p = sub.add_parser("baseline")
    p.add_argument("--from", dest="source", type=Path, required=True)
    p.add_argument("--kappa", type=float, required=True)
    for p in sub.choices.values():
        p.add_argument("--evals-dir", type=Path, default=EVALS_DIR)
    return parser


async def _run(args: argparse.Namespace) -> list[TaskRun]:
    root: Path = args.evals_dir
    catalog = load_tool_catalog(root / "fixtures" / "tools.yaml")
    tasks = load_tasks(root / "tasks", catalog)
    if args.category:
        tasks = [t for t in tasks if t.category == args.category]
    config = load_models(root / "models.yaml")
    models = config.models
    if args.models:
        wanted = set(args.models.split(","))
        models = [m for m in models if m.id in wanted]
    baseline_path = root / "baseline.json"
    judge_counts = baseline_path.exists() and load_baseline(baseline_path).judge_kappa >= MIN_JUDGE_KAPPA
    redis = new_redis_client()
    try:
        with FixtureTools(load_fixtures(root / "fixtures" / "responses.json")):
            runs = await run_matrix(
                tasks,
                models,
                mode=args.mode,
                k=args.k,
                concurrency=args.concurrency,
                catalog=catalog,
                redis=redis,
                cassette_dir=root / "cassettes",
                judge_spec=config.judge,
                judge_counts=judge_counts,
            )
    finally:
        await redis.aclose()
    meta = {
        "mode": args.mode,
        "k": 1 if args.mode == "record" else args.k,
        "judge_counted": judge_counts,
        "models": ",".join(m.id for m in models),
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    write_results(runs, meta, args.out or root / "out")
    return runs


async def _calibrate(args: argparse.Namespace) -> int:
    root: Path = args.evals_dir
    judge_spec = load_models(root / "models.yaml").judge
    pairs: list[tuple[bool, bool]] = []
    unparsed = 0
    for label in load_labels(root / "labels.jsonl"):
        path = root / "cassettes" / judge_spec.id / "calibration" / f"{label.model_id}__{label.task_id}.jsonl"
        if args.mode == "record":
            path.unlink(missing_ok=True)
        judge = Judge(build_adapter(judge_spec, args.mode, path), judge_spec)
        verdict = await judge.grade(prompt=label.prompt, rubric=label.rubric, answer=label.answer)
        if verdict is None:
            unparsed += 1
            continue
        pairs.append((verdict[0], label.label))
    report = calibration_stats(pairs)
    print(json.dumps({**asdict(report), "unparsed": unparsed, "counts_at": MIN_JUDGE_KAPPA}, indent=2))
    return 0


async def amain(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        configure_tracing("recore-evals")
    if args.command == "baseline":
        baseline = baseline_from(load_runs(args.source), kappa=args.kappa)
        (args.evals_dir / "baseline.json").write_text(baseline.model_dump_json(indent=2) + "\n")
        return 0
    if args.command == "calibrate":
        return await _calibrate(args)
    if args.command == "gate" and not (args.evals_dir / "baseline.json").exists():
        print("gate: no baseline.json; record a run and write one with the baseline command first")
        return 1
    runs = await _run(args)
    if args.command == "run":
        return 0
    mode: Mode = args.mode
    violations = gate(runs, load_baseline(args.evals_dir / "baseline.json"), mode=mode)
    for v in violations:
        print(f"gate: {v}")
    print(f"gate: {'FAILED' if violations else 'passed'} ({len(runs)} runs)")
    return 1 if violations else 0


def main() -> None:
    sys.exit(asyncio.run(amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
