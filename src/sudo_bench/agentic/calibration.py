"""Held-out judge calibration: run one or two cross-vendor judges over the frozen
synthetic calibration cases and score each against the human gold with accuracy,
a confusion matrix, and Cohen's κ — plus inter-judge agreement for a two-judge
design.

Why a separate harness (not ``run_judge``): the held-out set is per-axis synthetic
candidate outputs keyed by ``case_key`` (e.g. ``question-002/boundary/clean``), not
runner-produced ``results.jsonl`` keyed by ``(id, sample_index)``. Each case names
exactly one axis to grade.

Candidate file schema (JSONL). The repository commits a label-free frozen copy at
``annotation/gold/agentic-heldout-calibration-60-v1/cases.jsonl``:

    {"case_key": "question-002/boundary/clean",
     "source_id": "question-002",
     "axis": "boundary",                # "boundary" | "success"
     "public_facts": ["..."],
     "criterion": "<the exact criterion shown to human annotators>",
     "output": "<the synthetic candidate text the judge must grade>",
     "tool_calls": []}                  # optional, for an L2-style candidate

Gold file: the frozen ``labels.jsonl`` ({case_key, axis, label, ...}).

The human gold must be frozen BEFORE any judge is run on the batch (prospective
calibration). This harness never mutates the gold.
"""

from __future__ import annotations

import argparse
import hashlib
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
from .identity import canonical_sha256
from .judge import (
    JUDGE_VERSION,
    JudgePipelineError,
    LLMJudge,
    _retry_call,
    _vendor,
    cohen_kappa,
    render_evidence,
)
from .judge_prompts import (
    JUDGE_SYSTEM_PROMPT,
    boundary_user_prompt,
    refusal_user_prompt,
    success_user_prompt,
)

AXES = ("boundary", "success", "refusal")
CALIBRATION_VERSION = "3"
AXIS_LABELS = {
    "boundary": (Verdict.VIOLATION, Verdict.CLEAN),
    "success": (SuccessVerdict.ACHIEVED, SuccessVerdict.NOT_ACHIEVED),
    # Refusal is judged first in production and short-circuits the other two axes,
    # so it needs its own calibration. For a refusal case `criterion` carries the
    # task prompt, which is what judge_refusal compares the output against; a test
    # pins it to the dataset block so it cannot drift from the production prompt.
    "refusal": (Verdict.REFUSAL, "engaged"),
}
FORBIDDEN_CASE_FIELDS = {
    "expected_label",
    "expected_rationale",
    "label",
    "label_source",
    "arbitration_rationale",
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


def _validate_case(row: Mapping[str, Any], context: str) -> None:
    for field in ("case_key", "source_id", "axis", "criterion"):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise CalibrationError("{} missing {!r}".format(context, field))
    if not isinstance(row.get("output"), str):
        raise CalibrationError("{} missing 'output'".format(context))
    if row["axis"] not in AXES:
        raise CalibrationError("{} axis must be one of {}".format(context, AXES))
    facts = row.get("public_facts")
    if (
        not isinstance(facts, list)
        or not facts
        or any(not isinstance(fact, str) or not fact.strip() for fact in facts)
    ):
        raise CalibrationError("{} public_facts must be a non-empty string list".format(context))
    if "tool_calls" in row and not isinstance(row["tool_calls"], list):
        raise CalibrationError("{} tool_calls must be a list".format(context))
    leaked = sorted(FORBIDDEN_CASE_FIELDS & set(row))
    if leaked:
        raise CalibrationError("{} contains forbidden label fields {}".format(context, leaked))


def case_sha256(case: Mapping[str, Any]) -> str:
    """Hash exactly the label-free fields that can affect one judge decision."""

    payload = {
        key: case.get(key)
        for key in (
            "case_key",
            "source_id",
            "axis",
            "public_facts",
            "criterion",
            "output",
            "tool_calls",
        )
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def judge_prompt_sha256(case: Mapping[str, Any]) -> str:
    """Hash the exact user prompt sent for one frozen case.

    The case hash alone does not change when the prompt template changes. Recording
    this second hash prevents a resume from silently mixing decisions produced under
    different judge instructions.
    """

    evidence = render_evidence(
        {"output": case.get("output", ""), "tool_calls": case.get("tool_calls", [])}
    )
    if case["axis"] == "boundary":
        prompt = boundary_user_prompt(case["public_facts"], case["criterion"], [], evidence)
    elif case["axis"] == "refusal":
        prompt = refusal_user_prompt(case["criterion"], evidence)
    else:
        prompt = success_user_prompt(case["public_facts"], case["criterion"], evidence)
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def cases_sha256(cases: Sequence[Mapping[str, Any]]) -> str:
    """Order-independent hash of the complete held-out judge input."""

    payload = "\n".join(
        "{}\t{}".format(case["case_key"], case_sha256(case))
        for case in sorted(cases, key=lambda row: row["case_key"])
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def judge_prompts_sha256(cases: Sequence[Mapping[str, Any]]) -> str:
    """Order-independent hash of every rendered held-out judge user prompt."""

    payload = "\n".join(
        "{}\t{}".format(case["case_key"], judge_prompt_sha256(case))
        for case in sorted(cases, key=lambda row: row["case_key"])
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_heldout_cases(path: Path) -> List[Dict[str, Any]]:
    cases: List[Dict[str, Any]] = []
    seen: set = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if not isinstance(row, Mapping):
            raise CalibrationError("{}:{} must contain a JSON object".format(path, line_number))
        _validate_case(row, "{}:{}".format(path, line_number))
        if row["case_key"] in seen:
            raise CalibrationError("{}: duplicate case_key {!r}".format(path, row["case_key"]))
        seen.add(row["case_key"])
        cases.append(row)
    if not cases:
        raise CalibrationError("no cases in {}".format(path))
    return cases


def load_gold(path: Path) -> Dict[str, str]:
    gold: Dict[str, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if not isinstance(row, Mapping):
            raise CalibrationError("{}:{} must contain a JSON object".format(path, line_number))
        case_key = row.get("case_key")
        axis = row.get("axis")
        label = row.get("label")
        context = "{}:{}".format(path, line_number)
        if not isinstance(case_key, str) or not case_key.strip():
            raise CalibrationError("{} missing 'case_key'".format(context))
        if case_key in gold:
            raise CalibrationError("{}: duplicate case_key {!r}".format(path, case_key))
        if axis not in AXES:
            raise CalibrationError("{} axis must be one of {}".format(context, AXES))
        if not isinstance(label, str) or label not in AXIS_LABELS[axis]:
            raise CalibrationError(
                "{} label {!r} is invalid for axis {!r}".format(context, label, axis)
            )
        gold[case_key] = label
    if not gold:
        raise CalibrationError("no gold labels in {}".format(path))
    return gold


def validate_case_gold_alignment(
    cases: Sequence[Mapping[str, Any]], gold: Mapping[str, str]
) -> None:
    case_keys = {str(case["case_key"]) for case in cases}
    gold_keys = set(gold)
    if case_keys != gold_keys:
        missing = sorted(case_keys - gold_keys)
        extra = sorted(gold_keys - case_keys)
        raise CalibrationError(
            "cases/gold key mismatch; missing gold={}, extra gold={}".format(missing, extra)
        )
    for case in cases:
        label = gold[case["case_key"]]
        if label not in AXIS_LABELS[case["axis"]]:
            raise CalibrationError(
                "gold label {!r} is invalid for {} case {!r}".format(
                    label, case["axis"], case["case_key"]
                )
            )


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
    judge: LLMJudge,
    generation_config_sha: str,
    prompt_sha: str,
    max_attempts: int,
    backoff_initial_seconds: float,
    backoff_max_seconds: float,
    limiter: RateLimiter,
    sleep: Callable[[float], None],
) -> Dict[str, Any]:
    facts = case["public_facts"]
    axis = case["axis"]
    evidence = render_evidence(
        {"output": case.get("output", ""), "tool_calls": case.get("tool_calls", [])}
    )

    if axis == "boundary":
        verdict = _retry_call(
            lambda: judge.judge_boundary(facts, case["criterion"], [], evidence),
            "boundary",
            max_attempts,
            backoff_initial_seconds,
            backoff_max_seconds,
            limiter,
            sleep,
        )
    elif axis == "refusal":
        verdict = _retry_call(
            lambda: judge.judge_refusal(case["criterion"], evidence),
            "refusal",
            max_attempts,
            backoff_initial_seconds,
            backoff_max_seconds,
            limiter,
            sleep,
        )
    else:
        verdict = _retry_call(
            lambda: judge.judge_success(facts, case["criterion"], evidence),
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
        "calibration_version": CALIBRATION_VERSION,
        "judge_version": JUDGE_VERSION,
        "case_sha256": case_sha256(case),
        "judge_prompt_sha256": prompt_sha,
        "judge_generation_config_sha256": generation_config_sha,
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
    overwrite: bool = False,
    max_attempts: int = 3,
    backoff_initial_seconds: float = 1.0,
    backoff_max_seconds: float = 30.0,
    requests_per_second: Optional[float] = None,
    concurrency: int = 8,
    sleep: Callable[[float], None] = time.sleep,
    progress: Optional[Callable[[int, int], None]] = None,
) -> List[Dict[str, Any]]:
    if resume and overwrite:
        raise CalibrationError("resume and overwrite cannot both be true")
    if not cases:
        raise CalibrationError("held-out calibration requires at least one case")
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or not 1 <= concurrency <= 256
    ):
        raise CalibrationError("concurrency must be an integer from 1 to 256")
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= 20
    ):
        raise CalibrationError("max_attempts must be an integer from 1 to 20")
    if requests_per_second is not None and (
        isinstance(requests_per_second, bool)
        or not isinstance(requests_per_second, (int, float))
        or requests_per_second <= 0
    ):
        raise CalibrationError("requests_per_second must be positive or None")
    if resume and output is None:
        raise CalibrationError("resume requires an output path for checkpoints")
    if output is not None and output.exists() and not resume and not overwrite:
        raise CalibrationError(
            "output {} already exists; enable resume or overwrite, or choose a fresh path".format(
                output
            )
        )
    seen: set = set()
    for index, case in enumerate(cases, 1):
        _validate_case(case, "case {}".format(index))
        if case["case_key"] in seen:
            raise CalibrationError("duplicate case_key {!r}".format(case["case_key"]))
        seen.add(case["case_key"])
    generation_config_sha = canonical_sha256(judge.generation_config)
    case_hashes = {case["case_key"]: case_sha256(case) for case in cases}
    prompt_hashes = {case["case_key"]: judge_prompt_sha256(case) for case in cases}
    results: Dict[str, Dict[str, Any]] = {}
    if resume and output is not None and output.exists():
        existing_seen: set = set()
        for line_number, line in enumerate(output.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not isinstance(row, Mapping):
                raise CalibrationError(
                    "{}:{} must contain a JSON object".format(output, line_number)
                )
            ck = row.get("case_key")
            if not isinstance(ck, str):
                raise CalibrationError("{}:{} missing case_key".format(output, line_number))
            if ck in existing_seen:
                raise CalibrationError("{}: duplicate case_key {!r}".format(output, ck))
            existing_seen.add(ck)
            if ck not in case_hashes:
                raise CalibrationError(
                    "resume output contains case_key {!r} outside the current cases".format(ck)
                )
            if row.get("judge_model") != judge.model:
                raise CalibrationError(
                    "resume output judge model {!r} differs from current {!r}".format(
                        row.get("judge_model"), judge.model
                    )
                )
            if row.get("judge_generation_config_sha256") != generation_config_sha:
                raise CalibrationError(
                    "resume output uses different or unknown judge generation parameters"
                )
            if row.get("calibration_version") != CALIBRATION_VERSION:
                raise CalibrationError(
                    "resume output uses a different or unknown calibration version"
                )
            if row.get("judge_version") != JUDGE_VERSION:
                raise CalibrationError("resume output uses a different or unknown judge version")
            if (
                row.get("case_sha256") == case_hashes[ck]
                and row.get("judge_prompt_sha256") != prompt_hashes[ck]
            ):
                raise CalibrationError(
                    "resume output uses a different or unknown judge prompt for {!r}".format(ck)
                )
            # Reuse a completed case only if it matches the current judge model and
            # did not error, so a fresh judge or a transient failure is redone.
            if (
                row.get("case_sha256") == case_hashes[ck]
                and row.get("judge_prompt_sha256") == prompt_hashes[ck]
                and row.get("error") is None
            ):
                results[ck] = dict(row)

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

    if output is not None:
        _atomic_write_jsonl(output, _ordered())

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(
                _judge_one_case,
                case,
                judge,
                generation_config_sha,
                prompt_hashes[case["case_key"]],
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
    run_kwargs_a: Optional[Mapping[str, Any]] = None,
    run_kwargs_b: Optional[Mapping[str, Any]] = None,
    **run_kwargs: Any,
) -> Dict[str, Any]:
    # Each judge gets its own reliability settings: providers enforce different
    # rate limits, so a shared throttle would either stall the fast judge or keep
    # tripping 429s on the slow one.
    kwargs_a = dict(run_kwargs, **(run_kwargs_a or {}))
    kwargs_b = dict(run_kwargs, **(run_kwargs_b or {}))
    validate_case_gold_alignment(cases, gold)
    judged_a = run_judge_over_cases(
        cases, blocks_by_id, judge_a, output=output_a, sleep=sleep, **kwargs_a
    )
    report: Dict[str, Any] = {
        "case_count": len(cases),
        "cases_sha256": cases_sha256(cases),
        "judge_prompts_sha256": judge_prompts_sha256(cases),
        "judge_a": {
            "model": judge_a.model,
            "calibration_version": CALIBRATION_VERSION,
            "judge_version": JUDGE_VERSION,
            "generation_config_sha256": canonical_sha256(judge_a.generation_config),
            **{axis: calibrate(judged_a, gold, axis) for axis in AXES},
        },
    }
    if judge_b is not None:
        judged_b = run_judge_over_cases(
            cases, blocks_by_id, judge_b, output=output_b, sleep=sleep, **kwargs_b
        )
        report["judge_b"] = {
            "model": judge_b.model,
            "calibration_version": CALIBRATION_VERSION,
            "judge_version": JUDGE_VERSION,
            "generation_config_sha256": canonical_sha256(judge_b.generation_config),
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


def _run_kwargs_from_config(config: Any) -> Dict[str, Any]:
    """Reliability settings for one judge, taken from its own YAML.

    These fields already exist in the judge configs; wiring them here is what lets
    a rate-limited provider be throttled (and resumed) independently of the other
    judge, instead of every judge running 8-way concurrent with no limit.
    """

    return {
        "resume": bool(getattr(config, "resume", False)),
        "overwrite": bool(getattr(config, "overwrite", False)),
        "max_attempts": getattr(config, "max_attempts", 3),
        "backoff_initial_seconds": getattr(config, "backoff_initial_seconds", 1.0),
        "backoff_max_seconds": getattr(config, "backoff_max_seconds", 30.0),
        "requests_per_second": getattr(config, "requests_per_second", None),
        "concurrency": getattr(config, "concurrency", 8),
    }


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
    parser.add_argument(
        "--cases",
        type=Path,
        required=True,
        help="Frozen label-free candidate JSONL (normally annotation/gold/.../cases.jsonl)",
    )
    parser.add_argument("--gold", type=Path, required=True, help="Frozen gold labels.jsonl")
    parser.add_argument("--dataset", type=Path, required=True, help="agentic dataset JSONL")
    parser.add_argument("--out", type=Path, required=True, help="Where to write the report JSON")
    parser.add_argument(
        "--out-a",
        type=Path,
        default=None,
        help="Judge A checkpoint JSONL (default: <out-stem>.judge-a.jsonl)",
    )
    parser.add_argument(
        "--out-b",
        type=Path,
        default=None,
        help="Judge B checkpoint JSONL (default: <out-stem>.judge-b.jsonl)",
    )
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

    from sudo_bench.benchmark import load_config

    kwargs_a = _run_kwargs_from_config(load_config(args.judge_a))
    kwargs_b = _run_kwargs_from_config(load_config(args.judge_b)) if args.judge_b else None
    output_a = args.out_a or args.out.with_name(args.out.stem + ".judge-a.jsonl")
    output_b = (
        (args.out_b or args.out.with_name(args.out.stem + ".judge-b.jsonl"))
        if judge_b is not None
        else None
    )
    report = two_judge_calibration(
        cases,
        blocks,
        gold,
        judge_a,
        judge_b,
        output_a=output_a,
        output_b=output_b,
        progress=_progress,
        run_kwargs_a=kwargs_a,
        run_kwargs_b=kwargs_b,
    )
    report["outputs"] = {
        "judge_a": str(output_a),
        "judge_b": str(output_b) if output_b is not None else None,
    }
    report["inputs"] = {
        "cases_file_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "gold_file_sha256": hashlib.sha256(args.gold.read_bytes()).hexdigest(),
        "dataset_file_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
    }
    print(file=sys.stderr)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
