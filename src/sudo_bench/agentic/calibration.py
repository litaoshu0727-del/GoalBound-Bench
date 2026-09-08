"""Held-out judge calibration: run one or two cross-vendor judges over the frozen
synthetic calibration cases and score each against the human gold with accuracy,
a confusion matrix, and Cohen's κ — plus inter-judge agreement for a two-judge
design.

Why a separate harness (not ``run_judge``): the held-out set is per-axis synthetic
candidate outputs keyed by ``case_key`` (e.g. ``question-002/boundary/clean``), not
runner-produced ``results.jsonl`` keyed by ``(id, sample_index)``. Each case names
exactly one axis to grade.

Candidate file schema (JSONL), which you export from your local held-out seed
(``annotation/generated`` is gitignored, so the texts are not in the repo):

    {"case_key": "question-002/boundary/clean",
     "source_id": "question-002",
     "axis": "boundary",                # "boundary" | "success"
     "output": "<the synthetic candidate text the judge must grade>",
     "tool_calls": []}                  # optional, for an L2-style candidate

Gold file: the frozen ``labels.jsonl`` ({case_key, axis, label, ...}).

The human gold must be frozen BEFORE any judge is run on the batch (prospective
calibration). This harness never mutates the gold.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from sudo_bench.api import OpenAIChatClient
from sudo_bench.reliability import RateLimiter

from .detectors import SuccessVerdict, Verdict
from .judge import (
    JudgePipelineError,
    LLMJudge,
    _retry_call,
    _vendor,
    cohen_kappa,
    collect_boundary_rubrics,
    render_evidence,
)
from .judge_prompts import JUDGE_SYSTEM_PROMPT

AXES = ("boundary", "success")
AXIS_LABELS = {
    "boundary": (Verdict.VIOLATION, Verdict.CLEAN),
    "success": (SuccessVerdict.ACHIEVED, SuccessVerdict.NOT_ACHIEVED),
}


class CalibrationError(Exception):
    """Raised for malformed calibration inputs."""


# ------------------------------------------------------------------ metrics


def confusion_matrix(
    pairs: Sequence[Tuple[str, str]], labels: Optional[Sequence[str]] = None
) -> Dict[str, Any]:
    """Confusion matrix + accuracy + per-class precision/recall + κ for
    (gold, prediction) pairs. Keys are ``"gold|pred"`` to match the annotation
    summaries' convention.
    """

    n = len(pairs)
    label_set = list(labels) if labels else sorted({x for pair in pairs for x in pair})
    matrix = {"{}|{}".format(g, p): 0 for g in label_set for p in label_set}
    for gold, pred in pairs:
        key = "{}|{}".format(gold, pred)
        matrix[key] = matrix.get(key, 0) + 1

    correct = sum(matrix.get("{}|{}".format(lbl, lbl), 0) for lbl in label_set)
    per_class: Dict[str, Any] = {}
    for lbl in label_set:
        tp = matrix.get("{}|{}".format(lbl, lbl), 0)
        predicted = sum(matrix.get("{}|{}".format(g, lbl), 0) for g in label_set)
        actual = sum(matrix.get("{}|{}".format(lbl, p), 0) for p in label_set)
        per_class[lbl] = {
            "support": actual,
            "precision": (tp / predicted) if predicted else None,
            "recall": (tp / actual) if actual else None,
        }
    return {
        "n": n,
        "labels": label_set,
        "accuracy": (correct / n) if n else None,
        "kappa": cohen_kappa(pairs),
        "matrix": matrix,
        "per_class": per_class,
    }


# ------------------------------------------------------------------ IO


def load_heldout_cases(path: Path) -> List[Dict[str, Any]]:
    cases: List[Dict[str, Any]] = []
    seen: set = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        for field in ("case_key", "source_id", "axis", "output"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                if field == "output" and isinstance(row.get("output"), str):
                    continue  # an empty candidate output is allowed
                raise CalibrationError("{}:{} missing {!r}".format(path, line_number, field))
        if row["axis"] not in AXES:
            raise CalibrationError("{}:{} axis must be one of {}".format(path, line_number, AXES))
        if row["case_key"] in seen:
            raise CalibrationError("{}: duplicate case_key {!r}".format(path, row["case_key"]))
        seen.add(row["case_key"])
        cases.append(row)
    if not cases:
        raise CalibrationError("no cases in {}".format(path))
    return cases


def load_gold(path: Path) -> Dict[str, str]:
    gold: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        case_key = row.get("case_key")
        label = row.get("label")
        if isinstance(case_key, str) and isinstance(label, str):
            gold[case_key] = label
    if not gold:
        raise CalibrationError("no gold labels in {}".format(path))
    return gold


def load_blocks(dataset: Path) -> Dict[str, Mapping[str, Any]]:
    blocks: Dict[str, Mapping[str, Any]] = {}
    for line in dataset.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if isinstance(row.get("id"), str) and isinstance(row.get("agentic"), Mapping):
            blocks[row["id"]] = row["agentic"]
    return blocks


def _atomic_write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    os.replace(tmp, path)


# ------------------------------------------------------------------ judging


def _judge_one_case(
    case: Mapping[str, Any],
    block: Mapping[str, Any],
    judge: LLMJudge,
    max_attempts: int,
    backoff_initial_seconds: float,
    backoff_max_seconds: float,
    limiter: RateLimiter,
    sleep: Callable[[float], None],
) -> Dict[str, Any]:
    facts = block.get("public_facts", [])
    axis = case["axis"]
    evidence = render_evidence(
        {"output": case.get("output", ""), "tool_calls": case.get("tool_calls", [])}
    )

    if axis == "boundary":
        boundary = block.get("ground_truth", {}).get("boundary", "")
        rubrics = collect_boundary_rubrics(block, "L1")
        verdict = _retry_call(
            lambda: judge.judge_boundary(facts, boundary, rubrics, evidence),
            "boundary",
            max_attempts,
            backoff_initial_seconds,
            backoff_max_seconds,
            limiter,
            sleep,
        )
    else:
        rubric = block.get("success", {}).get("rubric", "")
        verdict = _retry_call(
            lambda: judge.judge_success(facts, rubric, evidence),
            "success",
            max_attempts,
            backoff_initial_seconds,
            backoff_max_seconds,
            limiter,
            sleep,
        )
    return {
        "case_key": case["case_key"],
        "source_id": case["source_id"],
        "axis": axis,
        "judge_model": judge.model,
        "verdict": verdict.verdict,
        "rationale": verdict.rationale,
        "error": verdict.error,
    }


def run_judge_over_cases(
    cases: Sequence[Mapping[str, Any]],
    blocks_by_id: Mapping[str, Mapping[str, Any]],
    judge: LLMJudge,
    *,
    output: Optional[Path] = None,
    resume: bool = False,
    max_attempts: int = 3,
    backoff_initial_seconds: float = 1.0,
    backoff_max_seconds: float = 30.0,
    requests_per_second: Optional[float] = None,
    concurrency: int = 8,
    sleep: Callable[[float], None] = time.sleep,
    progress: Optional[Callable[[int, int], None]] = None,
) -> List[Dict[str, Any]]:
    results: Dict[str, Dict[str, Any]] = {}
    if resume and output is not None and output.exists():
        for line in output.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            ck = row.get("case_key")
            # Reuse a completed case only if it matches the current judge model and
            # did not error, so a fresh judge or a transient failure is redone.
            if (
                isinstance(ck, str)
                and row.get("judge_model") == judge.model
                and row.get("error") is None
            ):
                results[ck] = row

    order = {case["case_key"]: index for index, case in enumerate(cases)}
    for case in cases:
        if case["source_id"] not in blocks_by_id:
            raise CalibrationError("case source_id {!r} not in dataset".format(case["source_id"]))
    jobs = [case for case in cases if case["case_key"] not in results]

    limiter = RateLimiter(requests_per_second, sleep=sleep)
    lock = threading.Lock()
    done = 0

    def _ordered() -> List[Dict[str, Any]]:
        return [results[k] for k in sorted(results, key=lambda k: order.get(k, 1 << 30))]

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {
            pool.submit(
                _judge_one_case,
                case,
                blocks_by_id[case["source_id"]],
                judge,
                max_attempts,
                backoff_initial_seconds,
                backoff_max_seconds,
                limiter,
                sleep,
            ): case["case_key"]
            for case in jobs
        }
        for future in as_completed(futures):
            row = future.result()
            with lock:
                results[row["case_key"]] = row
                done += 1
                if output is not None:
                    _atomic_write_jsonl(output, _ordered())
                if progress is not None:
                    progress(done, len(jobs))
    return _ordered()


# ------------------------------------------------------------------ calibration


def calibrate(
    judged_cases: Sequence[Mapping[str, Any]],
    gold: Mapping[str, str],
    axis: str,
) -> Dict[str, Any]:
    """Judge-vs-gold confusion/accuracy/κ for one axis (excludes judge errors)."""

    pairs: List[Tuple[str, str]] = []
    judge_errors = 0
    for row in judged_cases:
        if row.get("axis") != axis:
            continue
        ck = row.get("case_key")
        if ck not in gold:
            continue
        if row.get("error") is not None or row.get("verdict") in (None, "error"):
            judge_errors += 1
            continue
        pairs.append((str(gold[ck]), str(row["verdict"])))
    report = confusion_matrix(pairs, labels=AXIS_LABELS[axis])
    report["axis"] = axis
    report["judge_errors_excluded"] = judge_errors
    return report


def inter_judge_agreement(
    judged_a: Sequence[Mapping[str, Any]],
    judged_b: Sequence[Mapping[str, Any]],
    axis: str,
) -> Dict[str, Any]:
    """Agreement + κ between the two judges' own verdicts (no gold), per axis."""

    a_by_key = {r["case_key"]: r for r in judged_a if r.get("axis") == axis}
    b_by_key = {r["case_key"]: r for r in judged_b if r.get("axis") == axis}
    pairs: List[Tuple[str, str]] = []
    for ck in sorted(set(a_by_key) & set(b_by_key)):
        ra, rb = a_by_key[ck], b_by_key[ck]
        if ra.get("error") is not None or rb.get("error") is not None:
            continue
        if ra.get("verdict") in (None, "error") or rb.get("verdict") in (None, "error"):
            continue
        pairs.append((str(ra["verdict"]), str(rb["verdict"])))
    report = confusion_matrix(pairs, labels=AXIS_LABELS[axis])
    report["axis"] = axis
    return report


def two_judge_calibration(
    cases: Sequence[Mapping[str, Any]],
    blocks_by_id: Mapping[str, Mapping[str, Any]],
    gold: Mapping[str, str],
    judge_a: LLMJudge,
    judge_b: Optional[LLMJudge] = None,
    *,
    output_a: Optional[Path] = None,
    output_b: Optional[Path] = None,
    sleep: Callable[[float], None] = time.sleep,
    **run_kwargs: Any,
) -> Dict[str, Any]:
    judged_a = run_judge_over_cases(
        cases, blocks_by_id, judge_a, output=output_a, sleep=sleep, **run_kwargs
    )
    report: Dict[str, Any] = {
        "judge_a": {
            "model": judge_a.model,
            **{axis: calibrate(judged_a, gold, axis) for axis in AXES},
        }
    }
    if judge_b is not None:
        judged_b = run_judge_over_cases(
            cases, blocks_by_id, judge_b, output=output_b, sleep=sleep, **run_kwargs
        )
        report["judge_b"] = {
            "model": judge_b.model,
            **{axis: calibrate(judged_b, gold, axis) for axis in AXES},
        }
        report["inter_judge"] = {
            axis: inter_judge_agreement(judged_a, judged_b, axis) for axis in AXES
        }
    return report


# ------------------------------------------------------------------ reporting / CLI


def _print_axis(label: str, block: Mapping[str, Any]) -> None:
    acc = block.get("accuracy")
    kap = block.get("kappa")
    acc_s = "{:.1%}".format(acc) if acc is not None else "n/a"
    kap_s = "{:.3f}".format(kap) if kap is not None else "n/a"
    print(
        "    {:<9} n={:<3} accuracy={:<6} kappa={:<6} errors_excl={}".format(
            label, block.get("n", 0), acc_s, kap_s, block.get("judge_errors_excluded", 0)
        )
    )
    print("      confusion(gold|pred): {}".format(block.get("matrix")))


def _print_report(report: Mapping[str, Any]) -> None:
    for judge_key in ("judge_a", "judge_b"):
        jr = report.get(judge_key)
        if not jr:
            continue
        print("{} = {}".format(judge_key, jr["model"]))
        for axis in AXES:
            _print_axis(axis, jr[axis])
    inter = report.get("inter_judge")
    if inter:
        print("inter-judge agreement (judge_a vs judge_b):")
        for axis in AXES:
            _print_axis(axis, inter[axis])


def _build_judge(config_path: Path) -> LLMJudge:
    from sudo_bench.benchmark import load_config

    config = load_config(config_path)
    client = OpenAIChatClient(
        model=config.model,
        base_url=config.base_url,
        api_key=config.api_key,
        timeout=config.timeout,
        temperature=config.temperature,
        reasoning_effort=config.reasoning_effort,
        require_parameters=config.require_parameters,
        max_tokens=config.max_tokens,
        system_prompt=JUDGE_SYSTEM_PROMPT,
    )
    return LLMJudge(client)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Calibrate one or two cross-vendor judges against frozen human gold."
    )
    parser.add_argument("judge_a", type=Path, help="Judge A config (YAML)")
    parser.add_argument("--judge-b", type=Path, default=None, help="Judge B config (cross-vendor)")
    parser.add_argument("--cases", type=Path, required=True, help="Held-out candidate JSONL")
    parser.add_argument("--gold", type=Path, required=True, help="Frozen gold labels.jsonl")
    parser.add_argument("--dataset", type=Path, required=True, help="agentic dataset JSONL")
    parser.add_argument("--out", type=Path, required=True, help="Where to write the report JSON")
    parser.add_argument("--out-a", type=Path, default=None, help="Judged cases JSONL for judge A")
    parser.add_argument("--out-b", type=Path, default=None, help="Judged cases JSONL for judge B")
    parser.add_argument("--allow-same-vendor", action="store_true")
    args = parser.parse_args(argv)

    cases = load_heldout_cases(args.cases)
    gold = load_gold(args.gold)
    blocks = load_blocks(args.dataset)
    judge_a = _build_judge(args.judge_a)
    judge_b = _build_judge(args.judge_b) if args.judge_b else None

    if judge_b is not None and not args.allow_same_vendor:
        if _vendor(judge_a.model) and _vendor(judge_a.model) == _vendor(judge_b.model):
            raise JudgePipelineError(
                "the two judges share vendor {!r}; a two-judge design needs different "
                "vendors (or pass --allow-same-vendor)".format(_vendor(judge_a.model))
            )

    def _progress(done: int, total: int) -> None:
        print("  {}/{} cases judged".format(done, total), end="\r", file=sys.stderr)

    report = two_judge_calibration(
        cases, blocks, gold, judge_a, judge_b,
        output_a=args.out_a, output_b=args.out_b, progress=_progress,
    )
    print(file=sys.stderr)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
