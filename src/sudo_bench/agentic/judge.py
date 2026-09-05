"""Cross-vendor LLM-judge pipeline: resolve the UNRESOLVED boundary verdicts and
the always-unresolved success axis left by the detector engine / L1 runner.

What it does NOT do: override a programmatic VIOLATION/CLEAN (those are
high-precision and stay authoritative), fabricate a verdict when the judge output
cannot be parsed (that stays UNRESOLVED and is flagged), or let the judge share a
vendor with the evaluated model (guarded, because same-family grading is biased).

It reuses the project reliability primitives (retry/backoff/rate-limit), is
resumable with atomic checkpoints, and ships a Cohen's-κ calibration against a
human-labelled subset — the reportable judge-vs-human agreement (validity ①).
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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from uuid import uuid4

from sudo_bench.api import OpenAIChatClient
from sudo_bench.reliability import RateLimiter, classify_exception, retry_delay

from .detectors import SuccessVerdict, Verdict
from .identity import canonical_generation_config, canonical_sha256
from .judge_prompts import JUDGE_SYSTEM_PROMPT, boundary_user_prompt, success_user_prompt

ResultKey = Tuple[str, int]
BOUNDARY_LABELS = {Verdict.VIOLATION, Verdict.CLEAN}
SUCCESS_LABELS = {SuccessVerdict.ACHIEVED, SuccessVerdict.NOT_ACHIEVED}

# Bump when judged-row schema / resolution semantics change.
JUDGE_VERSION = "2"


class JudgeError(Exception):
    """Raised when a judge response cannot be parsed into a valid verdict."""

    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class JudgePipelineError(Exception):
    """Raised for configuration problems that prevent the pipeline from running."""


@dataclass(frozen=True)
class JudgeVerdict:
    axis: str  # "boundary" | "success"
    verdict: str  # a label, or "error"
    rationale: str = ""
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "axis": self.axis,
            "verdict": self.verdict,
            "rationale": self.rationale,
            "error": self.error,
        }


# ------------------------------------------------------------------ JSON parse


def _json_candidates(text: str):
    t = text.strip()
    yield t
    if "```" in t:
        for part in t.split("```"):
            p = part.strip()
            if p.lower().startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                yield p
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        yield t[i : j + 1]


def _extract_json(text: str) -> Dict[str, Any]:
    for candidate in _json_candidates(text):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise JudgeError("judge output is not JSON: {!r}".format(text.strip()[:200]))


# ------------------------------------------------------------------ the judge


class LLMJudge:
    """One-shot judge over a completion client (raises; the driver retries)."""

    def __init__(self, client) -> None:
        self._client = client

    @property
    def model(self) -> str:
        return self._client.model

    @property
    def generation_config(self) -> Mapping[str, Any]:
        return canonical_generation_config(self._client)

    def _decide(self, user_prompt: str, axis: str, allowed: set) -> JudgeVerdict:
        generation = self._client.complete(user_prompt)
        data = _extract_json(generation.text)
        verdict = data.get("verdict")
        if not isinstance(verdict, str) or verdict.strip() not in allowed:
            raise JudgeError("judge returned invalid {} verdict: {!r}".format(axis, verdict))
        rationale = str(data.get("rationale", ""))[:500]
        return JudgeVerdict(axis=axis, verdict=verdict.strip(), rationale=rationale)

    def judge_boundary(
        self, public_facts: Sequence[str], boundary: str, rubrics: Sequence[str], output: str
    ) -> JudgeVerdict:
        prompt = boundary_user_prompt(public_facts, boundary, rubrics, output)
        return self._decide(prompt, "boundary", BOUNDARY_LABELS)

    def judge_success(
        self, public_facts: Sequence[str], rubric: str, output: str
    ) -> JudgeVerdict:
        prompt = success_user_prompt(public_facts, rubric, output)
        return self._decide(prompt, "success", SUCCESS_LABELS)


# ---------------------------------------------------------------- block helpers


def collect_boundary_rubrics(block: Mapping[str, Any], level: str) -> List[str]:
    rubrics: List[str] = []
    for channel in block.get("violation_channels", []):
        if level not in channel.get("levels", []):
            continue
        rubric = channel.get("detector", {}).get("rubric")
        if isinstance(rubric, str) and rubric.strip() and rubric not in rubrics:
            rubrics.append(rubric)
    return rubrics


def _vendor(model: Optional[str]) -> str:
    if not model:
        return ""
    return model.split("/", 1)[0].strip().lower()


def assert_cross_vendor(judge_model: str, evaluated_model: str, allow_same_vendor: bool) -> None:
    if allow_same_vendor:
        return
    jv, ev = _vendor(judge_model), _vendor(evaluated_model)
    if jv and ev and jv == ev:
        raise JudgePipelineError(
            "judge model {!r} shares vendor {!r} with the evaluated model {!r}; "
            "same-family grading is biased. Use a different vendor or pass "
            "allow_same_vendor=True to override.".format(judge_model, jv, evaluated_model)
        )


# ------------------------------------------------------------------- IO / resume


def _atomic_write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _load_existing(path: Path) -> Dict[ResultKey, Dict[str, Any]]:
    existing: Dict[ResultKey, Dict[str, Any]] = {}
    if not path.exists():
        return existing
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        key = (row.get("id"), row.get("sample_index"))
        if isinstance(key[0], str) and isinstance(key[1], int):
            existing[key] = row
    return existing


def load_results(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    if not rows:
        raise JudgePipelineError("no result rows in {}".format(path))
    return rows


def evaluated_models(rows: Sequence[Mapping[str, Any]]) -> List[str]:
    """Distinct evaluated-model ids present in a results file (sorted)."""

    return sorted(
        {r.get("model") for r in rows if isinstance(r.get("model"), str) and r.get("model")}
    )


def infer_evaluated_model(rows: Sequence[Mapping[str, Any]]) -> str:
    models = evaluated_models(rows)
    return models[0] if models else ""


def assert_single_evaluated_model(rows: Sequence[Mapping[str, Any]]) -> str:
    """Refuse to judge a results file that mixes several evaluated models."""

    models = evaluated_models(rows)
    if len(models) > 1:
        raise JudgePipelineError(
            "results mix multiple evaluated models {}; judge one model at a time".format(models)
        )
    return models[0] if models else ""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def results_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    """Content hash of the results being judged, so a resume onto a different
    results file (or a re-run of the runner) is refused rather than mixed.

    Includes the input verdicts, not just the output text: which axes need judging
    is decided by ``violation_verdict`` / ``success_verdict``, so a change there
    must invalidate a resume too.
    """

    payload = "\n".join(
        "{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}".format(
            r.get("id"),
            r.get("sample_index"),
            r.get("run_id"),
            r.get("condition_prompt_sha256"),
            r.get("generation_config_sha256"),
            r.get("violation_verdict"),
            r.get("success_verdict"),
            r.get("output"),
        )
        for r in sorted(rows, key=lambda r: (str(r.get("id")), r.get("sample_index") or 0))
    )
    return _sha256(payload)


def blocks_sha256(blocks_by_id: Mapping[str, Mapping[str, Any]]) -> str:
    """Hash the scoring rules the judge reads (boundary + rubrics + success), so a
    change to the adjudication standard invalidates a judge resume."""

    payload = "\n".join(
        "{}\t{}".format(qid, json.dumps(blocks_by_id[qid], sort_keys=True, ensure_ascii=False))
        for qid in sorted(blocks_by_id)
    )
    return _sha256(payload)


def judge_signature(
    judge_model: str,
    evaluated_model: str,
    results_hash: str,
    blocks_hash: str,
    generation_config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    request_config = dict(generation_config or {"model": judge_model})
    return {
        "component": "agentic-l1-judge",
        "judge_version": JUDGE_VERSION,
        "judge_model": judge_model,
        "generation_config": request_config,
        "generation_config_sha256": canonical_sha256(request_config),
        "evaluated_model": evaluated_model,
        "results_sha256": results_hash,
        "blocks_sha256": blocks_hash,
    }


def _read_manifest(manifest: Optional[Path]) -> Optional[Mapping[str, Any]]:
    if manifest is None or not manifest.exists():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, Mapping) else None


def _read_manifest_signature(manifest: Optional[Path]) -> Optional[Mapping[str, Any]]:
    data = _read_manifest(manifest)
    if data is None:
        return None
    sig = data.get("signature")
    return sig if isinstance(sig, Mapping) else None


def _recover_judge_run_id(
    manifest: Optional[Path], existing: Mapping[ResultKey, Mapping[str, Any]]
) -> Optional[str]:
    run_ids = {
        row.get("judge_run_id")
        for row in existing.values()
        if isinstance(row.get("judge_run_id"), str) and row.get("judge_run_id")
    }
    data = _read_manifest(manifest)
    if data is not None and isinstance(data.get("run_id"), str) and data.get("run_id"):
        run_ids.add(data["run_id"])
    if len(run_ids) > 1:
        raise JudgePipelineError(
            "judge resume refused: existing results/manifest contain multiple judge_run_id values"
        )
    return next(iter(run_ids), None)


def _assert_judge_resume_compatible(
    manifest: Optional[Path],
    signature: Mapping[str, Any],
    existing: Mapping[ResultKey, Mapping[str, Any]],
) -> None:
    prior = _read_manifest_signature(manifest)
    if manifest is not None and prior is None:
        reason = "missing" if not manifest.exists() else "invalid or missing its signature"
        raise JudgePipelineError(
            "judge resume refused: manifest {} is {}; use overwrite or restore the manifest".format(
                manifest, reason
            )
        )
    if prior is not None:
        for key in (
            "judge_version",
            "judge_model",
            "generation_config_sha256",
            "evaluated_model",
            "results_sha256",
            "blocks_sha256",
        ):
            if prior.get(key) != signature.get(key):
                raise JudgePipelineError(
                    "judge resume refused: {} changed (was {!r}, now {!r}); use overwrite "
                    "or a fresh output path".format(key, prior.get(key), signature.get(key))
                )
    for row in existing.values():
        judged = row.get("judge")
        if isinstance(judged, Mapping) and judged.get("judge_model") not in (
            None,
            signature["judge_model"],
        ):
            raise JudgePipelineError(
                "judge resume refused: existing rows judged by {!r}, current judge is {!r}".format(
                    judged.get("judge_model"), signature["judge_model"]
                )
            )
        if isinstance(judged, Mapping) and (
            judged.get("generation_config_sha256") != signature["generation_config_sha256"]
        ):
            raise JudgePipelineError(
                "judge resume refused: existing rows use different or unknown judge parameters"
            )


def _write_signature_manifest(manifest: Path, signature: Mapping[str, Any], run_id: str) -> None:
    manifest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "signature": signature,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ------------------------------------------------------------------- driver


def _retry_call(
    fn: Callable[[], JudgeVerdict],
    axis: str,
    max_attempts: int,
    backoff_initial_seconds: float,
    backoff_max_seconds: float,
    limiter: RateLimiter,
    sleep: Callable[[float], None],
) -> JudgeVerdict:
    last_message = "unknown error"
    for attempt in range(1, max_attempts + 1):
        limiter.acquire()
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - classified below
            last_message = str(exc)
            if isinstance(exc, JudgeError):
                retryable = exc.retryable
            else:
                retryable = classify_exception(exc).retryable
            if retryable and attempt < max_attempts:
                info_retry_after = getattr(exc, "retry_after", None)
                delay = retry_delay(
                    attempt, backoff_initial_seconds, backoff_max_seconds, info_retry_after
                )
                if delay:
                    sleep(delay)
                continue
            break
    return JudgeVerdict(axis=axis, verdict="error", error=last_message)


def _needs_boundary(row: Mapping[str, Any]) -> bool:
    return row.get("violation_verdict") == Verdict.UNRESOLVED


def _needs_success(row: Mapping[str, Any]) -> bool:
    return row.get("success_verdict") == SuccessVerdict.UNRESOLVED


def _judge_one(
    row: Mapping[str, Any],
    block: Mapping[str, Any],
    judge: LLMJudge,
    generation_config_sha: str,
    level: str,
    max_attempts: int,
    backoff_initial_seconds: float,
    backoff_max_seconds: float,
    limiter: RateLimiter,
    sleep: Callable[[float], None],
) -> Dict[str, Any]:
    facts = block.get("public_facts", [])
    boundary = block.get("ground_truth", {}).get("boundary", "")
    rubrics = collect_boundary_rubrics(block, level)
    success_rubric = block.get("success", {}).get("rubric", "")
    output = row.get("output") or ""

    judged = dict(row)
    boundary_v: Optional[JudgeVerdict] = None
    success_v: Optional[JudgeVerdict] = None

    if _needs_boundary(row):
        boundary_v = _retry_call(
            lambda: judge.judge_boundary(facts, boundary, rubrics, output),
            "boundary",
            max_attempts,
            backoff_initial_seconds,
            backoff_max_seconds,
            limiter,
            sleep,
        )
    if _needs_success(row):
        success_v = _retry_call(
            lambda: judge.judge_success(facts, success_rubric, output),
            "success",
            max_attempts,
            backoff_initial_seconds,
            backoff_max_seconds,
            limiter,
            sleep,
        )

    if not _needs_boundary(row):
        final_violation = row.get("violation_verdict")
    elif boundary_v is not None and boundary_v.error is None:
        final_violation = boundary_v.verdict
    else:
        final_violation = Verdict.UNRESOLVED

    if not _needs_success(row):
        final_success = row.get("success_verdict")
    elif success_v is not None and success_v.error is None:
        final_success = success_v.verdict
    else:
        final_success = SuccessVerdict.UNRESOLVED

    judged["judge"] = {
        "judge_model": judge.model,
        "generation_config_sha256": generation_config_sha,
        "boundary": boundary_v.to_dict() if boundary_v is not None else None,
        "success": success_v.to_dict() if success_v is not None else None,
    }
    judged["final_violation_verdict"] = final_violation
    judged["final_success_verdict"] = final_success
    judged["judge_error"] = bool(
        (boundary_v is not None and boundary_v.error is not None)
        or (success_v is not None and success_v.error is not None)
    )
    return judged


def _carry_runner_error(row: Mapping[str, Any]) -> Dict[str, Any]:
    judged = dict(row)
    judged["judge"] = None
    judged["final_violation_verdict"] = None
    judged["final_success_verdict"] = None
    judged["judge_error"] = False
    return judged


def _judged_complete(row: Mapping[str, Any]) -> bool:
    return "final_violation_verdict" in row and not row.get("judge_error", False)


def run_judge(
    rows: Sequence[Mapping[str, Any]],
    blocks_by_id: Mapping[str, Mapping[str, Any]],
    judge: LLMJudge,
    output: Path,
    *,
    level: str = "L1",
    manifest: Optional[Path] = None,
    max_attempts: int = 3,
    backoff_initial_seconds: float = 1.0,
    backoff_max_seconds: float = 30.0,
    requests_per_second: Optional[float] = None,
    concurrency: int = 8,
    resume: bool = False,
    overwrite: bool = False,
    run_id: Optional[str] = None,
    sleep: Callable[[float], None] = time.sleep,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, Any]:
    if resume and overwrite:
        raise JudgePipelineError("resume and overwrite cannot both be true")
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or not 1 <= concurrency <= 256
    ):
        raise JudgePipelineError("concurrency must be an integer from 1 to 256")
    explicit_run_id = run_id is not None
    run_id = run_id or uuid4().hex
    evaluated_model = assert_single_evaluated_model(rows)
    generation_config = judge.generation_config
    signature = judge_signature(
        judge.model,
        evaluated_model,
        results_sha256(rows),
        blocks_sha256(blocks_by_id),
        generation_config=generation_config,
    )
    order = {row.get("id"): index for index, row in enumerate(rows)}
    order_key = {
        (row.get("id"), row.get("sample_index")): (order.get(row.get("id"), 1 << 30), i)
        for i, row in enumerate(rows)
    }

    results: Dict[ResultKey, Dict[str, Any]] = {}
    if output.exists() and output.stat().st_size > 0:
        if resume:
            results = _load_existing(output)
            _assert_judge_resume_compatible(manifest, signature, results)
            recovered = _recover_judge_run_id(manifest, results)
            if recovered and explicit_run_id and run_id != recovered:
                raise JudgePipelineError(
                    "judge resume refused: explicit run_id {!r} differs from existing {!r}".format(
                        run_id, recovered
                    )
                )
            if recovered:
                run_id = recovered
        elif not overwrite:
            raise JudgePipelineError(
                "output {} already exists; pass resume=True to continue it or "
                "overwrite=True to replace it".format(output)
            )
    if manifest is not None:
        _write_signature_manifest(manifest, signature, run_id)

    jobs: List[Mapping[str, Any]] = []
    for row in rows:
        key = (row.get("id"), row.get("sample_index"))
        if row.get("error") is not None:
            results[key] = _carry_runner_error(row)
            continue
        if row.get("id") not in blocks_by_id:
            raise JudgePipelineError("result id {!r} not in dataset".format(row.get("id")))
        previous = results.get(key)
        if previous is not None and _judged_complete(previous):
            continue
        jobs.append(row)

    limiter = RateLimiter(requests_per_second, sleep=sleep)
    lock = threading.Lock()
    done = 0

    def _ordered() -> List[Dict[str, Any]]:
        return [results[k] for k in sorted(results, key=lambda k: order_key.get(k, (1 << 30, 0)))]

    _atomic_write_jsonl(output, _ordered())

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(
                _judge_one,
                row,
                blocks_by_id[row["id"]],
                judge,
                signature["generation_config_sha256"],
                level,
                max_attempts,
                backoff_initial_seconds,
                backoff_max_seconds,
                limiter,
                sleep,
            ): (row["id"], row["sample_index"])
            for row in jobs
        }
        for future in as_completed(futures):
            judged = future.result()
            judged["judge_run_id"] = run_id
            key = (judged["id"], judged["sample_index"])
            with lock:
                results[key] = judged
                done += 1
                _atomic_write_jsonl(output, _ordered())
                if progress is not None:
                    progress(done, len(jobs))

    summary = summarize_judged(_ordered())
    summary.update({"run_id": run_id, "judge_model": judge.model, "signature": signature})
    return summary


# ------------------------------------------------------------------ reporting


def summarize_judged(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    scored = [r for r in rows if r.get("error") is None]
    runner_errors = len(rows) - len(scored)
    v = {Verdict.VIOLATION: 0, Verdict.CLEAN: 0, Verdict.UNRESOLVED: 0}
    s = {SuccessVerdict.ACHIEVED: 0, SuccessVerdict.NOT_ACHIEVED: 0, SuccessVerdict.UNRESOLVED: 0}
    judge_errors = 0
    for r in scored:
        fv = r.get("final_violation_verdict")
        fs = r.get("final_success_verdict")
        v[fv if fv in v else Verdict.UNRESOLVED] += 1
        s[fs if fs in s else SuccessVerdict.UNRESOLVED] += 1
        if r.get("judge_error"):
            judge_errors += 1
    scored_n = len(scored)
    resolved_v = v[Verdict.VIOLATION] + v[Verdict.CLEAN]
    resolved_s = s[SuccessVerdict.ACHIEVED] + s[SuccessVerdict.NOT_ACHIEVED]
    return {
        "scored": scored_n,
        "runner_errors": runner_errors,
        "judge_errors": judge_errors,
        "violation": v[Verdict.VIOLATION],
        "clean": v[Verdict.CLEAN],
        "violation_unresolved": v[Verdict.UNRESOLVED],
        "achieved": s[SuccessVerdict.ACHIEVED],
        "not_achieved": s[SuccessVerdict.NOT_ACHIEVED],
        "success_unresolved": s[SuccessVerdict.UNRESOLVED],
        # `violation_rate` is the resolved-only rate (kept for back-compat). Because
        # unresolved samples bias it either way, ALSO report the strict lower bound
        # (unresolved counted as clean) and upper bound (unresolved counted as
        # violation). A trustworthy number needs the bounds to be tight.
        "violation_rate": (v[Verdict.VIOLATION] / resolved_v) if resolved_v else None,
        "violation_rate_resolved": (v[Verdict.VIOLATION] / resolved_v) if resolved_v else None,
        "violation_lower_bound": (v[Verdict.VIOLATION] / scored_n) if scored_n else None,
        "violation_upper_bound": (
            (v[Verdict.VIOLATION] + v[Verdict.UNRESOLVED]) / scored_n if scored_n else None
        ),
        "achieved_rate": (s[SuccessVerdict.ACHIEVED] / resolved_s) if resolved_s else None,
        "achieved_rate_resolved": (s[SuccessVerdict.ACHIEVED] / resolved_s) if resolved_s else None,
        "achieved_lower_bound": (s[SuccessVerdict.ACHIEVED] / scored_n) if scored_n else None,
        "achieved_upper_bound": (
            (s[SuccessVerdict.ACHIEVED] + s[SuccessVerdict.UNRESOLVED]) / scored_n
            if scored_n
            else None
        ),
    }


# ------------------------------------------------------------------ calibration


def cohen_kappa(pairs: Sequence[Tuple[str, str]]) -> Optional[float]:
    """Cohen's κ, or None when it is not computable.

    κ is undefined when there is no label variance to chance-correct against
    (both raters constant on a single label, i.e. expected agreement == 1). We
    return None there rather than a misleading 1.0 — a degenerate all-"clean"
    subset should be reported as "not computable", not as perfect agreement.
    """

    n = len(pairs)
    if n == 0:
        return None
    labels = sorted({a for a, _ in pairs} | {b for _, b in pairs})
    if len(labels) < 2:
        return None
    po = sum(1 for a, b in pairs if a == b) / n
    pe = 0.0
    for label in labels:
        pa = sum(1 for a, _ in pairs if a == label) / n
        pb = sum(1 for _, b in pairs if b == label) / n
        pe += pa * pb
    if pe >= 1.0:
        return None
    return (po - pe) / (1 - pe)


def load_human_labels(path: Path) -> Dict[ResultKey, Dict[str, str]]:
    labels: Dict[ResultKey, Dict[str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        key = (row.get("id"), row.get("sample_index"))
        if isinstance(key[0], str) and isinstance(key[1], int):
            labels[key] = row
    return labels


def _calibration_pred(row: Mapping[str, Any], axis: str, source: str) -> Optional[str]:
    """The verdict to compare against a human label, for one axis and one source.

    - ``pipeline``: the final resolved verdict (detector + judge combined).
    - ``detector``: the programmatic verdict only (None where the detector
      abstained; the success axis has no detector, so always None).
    - ``judge``: the judge's own verdict only (None where the judge was not asked
      or errored).

    Reporting these separately keeps "judge–human κ" honest: the pipeline number
    folds in programmatic decisions and is NOT the same as the judge's own κ.
    """

    if source == "pipeline":
        field = "final_violation_verdict" if axis == "boundary" else "final_success_verdict"
        value = row.get(field)
    elif source == "detector":
        if axis != "boundary":
            return None
        value = row.get("violation_verdict")
    elif source == "judge":
        judged = row.get("judge")
        if not isinstance(judged, Mapping):
            return None
        block = judged.get(axis)
        if not isinstance(block, Mapping) or block.get("error"):
            return None
        value = block.get("verdict")
    else:
        raise ValueError("source must be 'pipeline', 'detector' or 'judge'")

    unresolved = Verdict.UNRESOLVED if axis == "boundary" else SuccessVerdict.UNRESOLVED
    if value is None or value == unresolved or value == "error":
        return None
    return str(value)


def compute_calibration(
    judged_rows: Sequence[Mapping[str, Any]],
    human_labels: Mapping[ResultKey, Mapping[str, str]],
    axis: str,
    source: str = "pipeline",
) -> Dict[str, Any]:
    human_field = "boundary" if axis == "boundary" else "success"
    pairs: List[Tuple[str, str]] = []
    for row in judged_rows:
        key = (row.get("id"), row.get("sample_index"))
        human = human_labels.get(key)
        if not human or human.get(human_field) is None:
            continue
        pred = _calibration_pred(row, axis, source)
        if pred is None:
            continue
        pairs.append((pred, str(human[human_field])))

    n = len(pairs)
    agreement = (sum(1 for a, b in pairs if a == b) / n) if n else None
    return {
        "axis": axis,
        "source": source,
        "n": n,
        "agreement": agreement,
        "kappa": cohen_kappa(pairs),
    }


def compute_all_calibrations(
    judged_rows: Sequence[Mapping[str, Any]],
    human_labels: Mapping[ResultKey, Mapping[str, str]],
) -> Dict[str, Dict[str, Any]]:
    """Detector / judge / pipeline agreement vs human, per axis — reported separately."""

    return {
        "boundary": {
            src: compute_calibration(judged_rows, human_labels, "boundary", src)
            for src in ("detector", "judge", "pipeline")
        },
        "success": {
            src: compute_calibration(judged_rows, human_labels, "success", src)
            for src in ("judge", "pipeline")
        },
    }


def write_manifest(
    manifest: Path,
    summary: Mapping[str, Any],
    calibration: Optional[Mapping[str, Any]],
    results_path: Path,
) -> None:
    manifest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": summary.get("run_id"),
        "judge_model": summary.get("judge_model"),
        "results": str(results_path),
        # Top-level so a later resume can validate it (see _read_manifest_signature).
        "signature": summary.get("signature"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {k: v for k, v in summary.items() if k not in {"run_id", "judge_model"}},
        "calibration": calibration,
    }
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _print_summary(summary: Mapping[str, Any], calibration: Optional[Mapping[str, Any]]) -> None:
    print(
        "judge run {} | judge_model={} | scored={} runner_errors={} judge_errors={}".format(
            str(summary.get("run_id", "?"))[:8],
            summary.get("judge_model"),
            summary["scored"],
            summary["runner_errors"],
            summary["judge_errors"],
        )
    )
    print(
        "  boundary: violation={} clean={} unresolved={}".format(
            summary["violation"], summary["clean"], summary["violation_unresolved"]
        )
    )
    print(
        "  success:  achieved={} not_achieved={} unresolved={}".format(
            summary["achieved"], summary["not_achieved"], summary["success_unresolved"]
        )
    )
    if summary["violation_rate_resolved"] is not None:
        print(
            "  violation_rate: resolved={:.1%}  bounds=[{:.1%}, {:.1%}] "
            "(unresolved as clean / as violation)".format(
                summary["violation_rate_resolved"],
                summary["violation_lower_bound"],
                summary["violation_upper_bound"],
            )
        )
    if summary["achieved_rate_resolved"] is not None:
        print(
            "  achieved_rate:  resolved={:.1%}  bounds=[{:.1%}, {:.1%}]".format(
                summary["achieved_rate_resolved"],
                summary["achieved_lower_bound"],
                summary["achieved_upper_bound"],
            )
        )
    if calibration:
        for axis in ("boundary", "success"):
            for source, c in (calibration.get(axis) or {}).items():
                if c and c["kappa"] is not None:
                    print(
                        "  κ({}/{}) = {:.3f}  (n={}, agreement={:.1%})".format(
                            axis, source, c["kappa"], c["n"], c["agreement"]
                        )
                    )
                elif c and c["n"]:
                    print(
                        "  κ({}/{}) = n/a (not computable; n={}, agreement={:.1%})".format(
                            axis, source, c["n"], c["agreement"]
                        )
                    )


def main(argv: Optional[List[str]] = None) -> int:
    from sudo_bench.benchmark import load_config

    parser = argparse.ArgumentParser(
        description="Resolve L1 UNRESOLVED verdicts with an LLM judge."
    )
    parser.add_argument("config", type=Path, help="Judge YAML config (model/api = the judge)")
    parser.add_argument("--results", type=Path, required=True, help="Runner results JSONL to judge")
    parser.add_argument("--human", type=Path, default=None, help="Optional human-label JSONL for κ")
    parser.add_argument(
        "--allow-same-vendor",
        action="store_true",
        help="Permit a judge that shares a vendor with the evaluated model (biased).",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    rows = load_results(args.results)
    blocks_by_id = {
        row["id"]: row["agentic"]
        for row in (
            json.loads(line)
            for line in config.dataset.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if isinstance(row.get("agentic"), Mapping)
    }
    evaluated_model = assert_single_evaluated_model(rows)
    assert_cross_vendor(config.model, evaluated_model, args.allow_same_vendor)

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
    judge = LLMJudge(client)

    def _progress(done: int, total: int) -> None:
        print("  {}/{} judged".format(done, total), end="\r", file=sys.stderr)

    summary = run_judge(
        rows,
        blocks_by_id,
        judge,
        config.output,
        manifest=config.manifest,
        max_attempts=config.max_attempts,
        backoff_initial_seconds=config.backoff_initial_seconds,
        backoff_max_seconds=config.backoff_max_seconds,
        requests_per_second=config.requests_per_second,
        concurrency=config.concurrency,
        resume=config.resume,
        overwrite=config.overwrite,
        progress=_progress,
    )
    print(file=sys.stderr)

    calibration: Optional[Dict[str, Any]] = None
    if args.human is not None:
        human = load_human_labels(args.human)
        judged_list = list(_load_existing(config.output).values())
        calibration = compute_all_calibrations(judged_list, human)

    write_manifest(config.manifest, summary, calibration, args.results)
    _print_summary(summary, calibration)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
