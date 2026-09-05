"""L1 (open-ended) runner: call a model on each item's ``prompt_l1``, sample it
``samples_per_question`` times, and score every sample on both axes with the
detector engine.

This is the first component that actually calls a model. It reuses the project's
existing reliability primitives (``classify_exception`` / ``retry_delay`` /
``RateLimiter``), the ``OpenAIChatClient`` transport, and the YAML ``load_config``
schema, so an L1 config looks like any other eval config — only the dataset is
``questions.v3.agentic.jsonl`` and the ``system_prompt`` is an L1 condition prompt
(see ``prompts.py``), with no ``\\boxed{}`` instruction.

Scope: L1 only. Many items resolve to UNRESOLVED (pending judge) by design, so the
headline is the (violation / unresolved / clean) triad, not a single "accuracy".
The legitimate-goal (success) axis is always UNRESOLVED until the judge pipeline
lands; this runner records it but never fabricates a value. Running this does NOT
by itself produce a finished agentic result — the UNRESOLVED share still needs a
cross-vendor judge + human calibration.
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

from sudo_bench.api import Generation, OpenAIChatClient
from sudo_bench.reliability import RateLimiter, classify_exception, retry_delay

from .detectors import SuccessVerdict, Verdict, evaluate_agentic
from .identity import canonical_generation_config, canonical_sha256

ResultKey = Tuple[str, int]

# Bump when the result-row schema or scoring semantics change, so a resume onto an
# older run is refused rather than silently mixed.
RUNNER_VERSION = "2"


class RunnerError(Exception):
    """Raised for configuration/dataset problems that prevent a run."""


@dataclass(frozen=True)
class AgenticItem:
    id: str
    block: Mapping[str, Any]
    prompt: str


class CompletionClient:
    """Structural type: anything with ``.model`` and ``.complete(prompt)``."""

    model: str

    def complete(self, prompt: str) -> Generation:  # pragma: no cover - protocol
        raise NotImplementedError


# --------------------------------------------------------------------------- IO


def load_agentic_items(dataset: Path, level: str = "L1") -> List[AgenticItem]:
    prompt_field = "prompt_{}".format(level.lower())
    items: List[AgenticItem] = []
    seen: set = set()
    for line_number, line in enumerate(dataset.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        qid = row.get("id")
        block = row.get("agentic")
        if not isinstance(qid, str) or not qid:
            raise RunnerError("{}:{} missing question id".format(dataset, line_number))
        if not isinstance(block, Mapping):
            raise RunnerError(
                "{}: {} has no agentic block; run build_dataset first".format(dataset, qid)
            )
        if level not in block.get("levels_supported", []):
            continue
        prompt = block.get(prompt_field)
        if not isinstance(prompt, str) or not prompt.strip():
            raise RunnerError(
                "{}: {} has no materialized {!r}; run build_dataset first".format(
                    dataset, qid, prompt_field
                )
            )
        if qid in seen:
            raise RunnerError("{}: duplicate question id {!r}".format(dataset, qid))
        seen.add(qid)
        items.append(AgenticItem(id=qid, block=block, prompt=prompt))
    if not items:
        raise RunnerError("no L1-supported items found in {}".format(dataset))
    return items


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


# ------------------------------------------------------------- run identity


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dataset_sha256(items: Sequence[AgenticItem]) -> str:
    # Hash the whole agentic block, not just the prompt: ground_truth, the
    # violation channels/detectors, and every rubric affect scoring, so changing
    # any of them must invalidate a resume even when the prompt is unchanged.
    payload = "\n".join(
        "{}\t{}".format(item.id, json.dumps(item.block, sort_keys=True, ensure_ascii=False))
        for item in items
    )
    return _sha256(payload)


def run_signature(
    model: str,
    condition_sha: str,
    samples_per_question: int,
    items: Sequence[AgenticItem],
    generation_config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """A fingerprint of everything that must match for a resume to be valid."""

    request_config = dict(generation_config or {"model": model})
    return {
        "component": "agentic-l1-runner",
        "runner_version": RUNNER_VERSION,
        "level": "L1",
        "model": model,
        "generation_config": request_config,
        "generation_config_sha256": canonical_sha256(request_config),
        "condition_prompt_sha256": condition_sha,
        "samples_per_question": samples_per_question,
        "dataset_sha256": _dataset_sha256(items),
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


def _recover_run_id(
    manifest: Optional[Path], existing: Mapping[ResultKey, Mapping[str, Any]]
) -> Optional[str]:
    """The run_id to keep when resuming, so a resumed file has ONE run_id.

    Prefer the manifest's run_id (source of truth); fall back to the run_id already
    stamped on existing rows when no manifest is present.
    """

    run_ids = {
        row.get("run_id")
        for row in existing.values()
        if isinstance(row.get("run_id"), str) and row.get("run_id")
    }
    data = _read_manifest(manifest)
    if data is not None and isinstance(data.get("run_id"), str) and data.get("run_id"):
        run_ids.add(data["run_id"])
    if len(run_ids) > 1:
        raise RunnerError(
            "resume refused: existing results/manifest contain multiple run_id values"
        )
    return next(iter(run_ids), None)


def _assert_resume_compatible(
    manifest: Optional[Path],
    signature: Mapping[str, Any],
    existing: Mapping[ResultKey, Mapping[str, Any]],
) -> None:
    """Refuse to resume onto results produced by a different run configuration.

    Two layers: the manifest signature (covers dataset/version/samples too) and a
    per-row guard on the requested model + condition prompt, which protects even
    when no manifest is present. Rows stamp the *requested* model, not the id the
    API echoes back, so a provider renaming the model cannot trigger a false miss.
    """

    prior = _read_manifest_signature(manifest)
    if manifest is not None and prior is None:
        reason = "missing" if not manifest.exists() else "invalid or missing its signature"
        raise RunnerError(
            "resume refused: manifest {} is {}; use overwrite or restore the manifest".format(
                manifest, reason
            )
        )
    if prior is not None:
        for key in (
            "runner_version",
            "level",
            "model",
            "generation_config_sha256",
            "condition_prompt_sha256",
            "samples_per_question",
            "dataset_sha256",
        ):
            if prior.get(key) != signature.get(key):
                raise RunnerError(
                    "resume refused: {} changed (was {!r}, now {!r}); use overwrite "
                    "or a fresh output path".format(key, prior.get(key), signature.get(key))
                )
    for (_qid, idx), row in existing.items():
        if row.get("model") not in (None, signature["model"]):
            raise RunnerError(
                "resume refused: existing results use model {!r}, current run is {!r}; "
                "use overwrite or a fresh output path".format(row.get("model"), signature["model"])
            )
        if row.get("condition_prompt_sha256") not in (None, signature["condition_prompt_sha256"]):
            raise RunnerError(
                "resume refused: existing results use a different condition prompt"
            )
        if row.get("generation_config_sha256") != signature["generation_config_sha256"]:
            raise RunnerError(
                "resume refused: existing results use different or unknown generation parameters"
            )
        if isinstance(idx, int) and idx > signature["samples_per_question"]:
            raise RunnerError(
                "resume refused: existing sample_index {} exceeds samples_per_question {}".format(
                    idx, signature["samples_per_question"]
                )
            )


def _write_signature_manifest(
    manifest: Path, signature: Mapping[str, Any], run_id: str
) -> None:
    manifest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "signature": signature,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ----------------------------------------------------------------- evaluation


def _score_row(
    item: AgenticItem,
    sample_index: int,
    run_id: str,
    condition_sha: str,
    generation_config_sha: str,
    requested_model: str,
    attempts: int,
    generation: Generation,
) -> Dict[str, Any]:
    evaluation = evaluate_agentic(item.block, "L1", output_text=generation.text, item_id=item.id)
    return {
        "id": item.id,
        "sample_index": sample_index,
        "level": "L1",
        "run_id": run_id,
        # `model` is the *requested* id (deterministic, used for resume identity);
        # `returned_model` is whatever the provider echoed back (informational).
        "model": requested_model,
        "returned_model": generation.model,
        "condition_prompt_sha256": condition_sha,
        "generation_config_sha256": generation_config_sha,
        "attempt_count": attempts,
        "error": None,
        "output": generation.text,
        "violation_verdict": evaluation.violation.verdict,
        "success_verdict": evaluation.success.verdict,
        "needs_judge": evaluation.needs_judge,
        "evaluation": evaluation.to_dict(),
        "usage": dict(generation.usage),
    }


def _error_row(
    item: AgenticItem,
    sample_index: int,
    run_id: str,
    condition_sha: str,
    generation_config_sha: str,
    model: str,
    attempts: int,
    exc: Exception,
) -> Dict[str, Any]:
    info = classify_exception(exc)
    return {
        "id": item.id,
        "sample_index": sample_index,
        "level": "L1",
        "run_id": run_id,
        "model": model,
        "condition_prompt_sha256": condition_sha,
        "generation_config_sha256": generation_config_sha,
        "attempt_count": attempts,
        "error": str(exc),
        "error_type": info.category,
        "retryable": info.retryable,
        "status_code": info.status_code,
        "output": None,
        "violation_verdict": None,
        "success_verdict": None,
        "needs_judge": None,
    }


def _run_one(
    item: AgenticItem,
    sample_index: int,
    client: CompletionClient,
    run_id: str,
    condition_sha: str,
    generation_config_sha: str,
    max_attempts: int,
    backoff_initial_seconds: float,
    backoff_max_seconds: float,
    limiter: RateLimiter,
    sleep: Callable[[float], None],
) -> Dict[str, Any]:
    last_exc: Optional[Exception] = None
    for local_attempt in range(1, max_attempts + 1):
        limiter.acquire()
        try:
            generation = client.complete(item.prompt)
        except Exception as exc:  # noqa: BLE001 - classified below
            last_exc = exc
            info = classify_exception(exc)
            if info.retryable and local_attempt < max_attempts:
                delay = retry_delay(
                    local_attempt, backoff_initial_seconds, backoff_max_seconds, info.retry_after
                )
                if delay:
                    sleep(delay)
                continue
            return _error_row(
                item,
                sample_index,
                run_id,
                condition_sha,
                generation_config_sha,
                client.model,
                local_attempt,
                exc,
            )
        return _score_row(
            item,
            sample_index,
            run_id,
            condition_sha,
            generation_config_sha,
            client.model,
            local_attempt,
            generation,
        )
    # Unreachable in practice; keeps the type checker happy.
    assert last_exc is not None
    return _error_row(
        item,
        sample_index,
        run_id,
        condition_sha,
        generation_config_sha,
        client.model,
        max_attempts,
        last_exc,
    )


# --------------------------------------------------------------------- driver


def run_l1(
    items: Sequence[AgenticItem],
    client: CompletionClient,
    output: Path,
    *,
    condition_prompt: str,
    manifest: Optional[Path] = None,
    samples_per_question: int = 1,
    concurrency: int = 8,
    resume: bool = False,
    retry_errors: bool = False,
    overwrite: bool = False,
    max_attempts: int = 1,
    backoff_initial_seconds: float = 1.0,
    backoff_max_seconds: float = 30.0,
    requests_per_second: Optional[float] = None,
    run_id: Optional[str] = None,
    sleep: Callable[[float], None] = time.sleep,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, Any]:
    if not items:
        raise RunnerError("items must not be empty")
    if retry_errors and not resume:
        raise RunnerError("retry_errors requires resume")
    if resume and overwrite:
        raise RunnerError("resume and overwrite cannot both be true")
    explicit_run_id = run_id is not None
    run_id = run_id or uuid4().hex
    condition_sha = _sha256(condition_prompt)
    generation_config = canonical_generation_config(client)
    signature = run_signature(
        client.model,
        condition_sha,
        samples_per_question,
        items,
        generation_config=generation_config,
    )
    order = {item.id: index for index, item in enumerate(items)}

    results: Dict[ResultKey, Dict[str, Any]] = {}
    if output.exists() and output.stat().st_size > 0:
        if resume:
            results = _load_existing(output)
            _assert_resume_compatible(manifest, signature, results)
            # Keep a single run_id across resumes. An explicitly supplied id is
            # allowed only when it agrees with the established run identity.
            recovered = _recover_run_id(manifest, results)
            if recovered and explicit_run_id and run_id != recovered:
                raise RunnerError(
                    "resume refused: explicit run_id {!r} differs from existing {!r}".format(
                        run_id, recovered
                    )
                )
            if recovered:
                run_id = recovered
        elif not overwrite:
            raise RunnerError(
                "output {} already exists; pass resume=True to continue it or "
                "overwrite=True to replace it".format(output)
            )
    # Stamp the run identity before any work, so a later resume can validate it.
    if manifest is not None:
        _write_signature_manifest(manifest, signature, run_id)

    def _pending(key: ResultKey) -> bool:
        previous = results.get(key)
        if previous is None:
            return True
        if previous.get("error") is not None and retry_errors:
            return previous.get("retryable") is not False
        return False

    jobs = [
        (item, sample_index)
        for sample_index in range(1, samples_per_question + 1)
        for item in items
        if _pending((item.id, sample_index))
    ]

    limiter = RateLimiter(requests_per_second, sleep=sleep)
    lock = threading.Lock()
    done = 0

    def _ordered_rows() -> List[Dict[str, Any]]:
        return [
            results[key]
            for key in sorted(results, key=lambda k: (order.get(k[0], 1 << 30), k[1]))
        ]

    _atomic_write_jsonl(output, _ordered_rows())

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {
            pool.submit(
                _run_one,
                item,
                sample_index,
                client,
                run_id,
                condition_sha,
                signature["generation_config_sha256"],
                max_attempts,
                backoff_initial_seconds,
                backoff_max_seconds,
                limiter,
                sleep,
            ): (item.id, sample_index)
            for item, sample_index in jobs
        }
        for future in as_completed(futures):
            row = future.result()
            key = (row["id"], row["sample_index"])
            with lock:
                results[key] = row
                done += 1
                _atomic_write_jsonl(output, _ordered_rows())
                if progress is not None:
                    progress(done, len(jobs))

    summary = summarize(_ordered_rows(), items, samples_per_question)
    summary.update(
        {
            "run_id": run_id,
            "condition_prompt_sha256": condition_sha,
            "model": client.model,
            "signature": signature,
        }
    )
    return summary


def summarize(
    rows: Sequence[Mapping[str, Any]],
    items: Sequence[AgenticItem],
    samples_per_question: int,
) -> Dict[str, Any]:
    def _bucket(subset: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        scored = [r for r in subset if r.get("error") is None]
        errors = len(subset) - len(scored)
        counts = {Verdict.VIOLATION: 0, Verdict.UNRESOLVED: 0, Verdict.CLEAN: 0}
        success_unresolved = 0
        for r in scored:
            counts[r.get("violation_verdict", Verdict.UNRESOLVED)] += 1
            if r.get("success_verdict") == SuccessVerdict.UNRESOLVED:
                success_unresolved += 1
        n = len(scored)
        return {
            "scored": n,
            "errors": errors,
            "violation": counts[Verdict.VIOLATION],
            "unresolved": counts[Verdict.UNRESOLVED],
            "clean": counts[Verdict.CLEAN],
            "violation_rate": (counts[Verdict.VIOLATION] / n) if n else None,
            "unresolved_rate": (counts[Verdict.UNRESOLVED] / n) if n else None,
            "clean_rate": (counts[Verdict.CLEAN] / n) if n else None,
            "success_unresolved": success_unresolved,
        }

    per_item = {
        item.id: _bucket([r for r in rows if r.get("id") == item.id]) for item in items
    }
    return {
        "items": len(items),
        "samples_per_question": samples_per_question,
        "overall": _bucket(rows),
        "per_item": per_item,
    }


def write_manifest(manifest: Path, summary: Mapping[str, Any], dataset: Path, level: str) -> None:
    manifest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": summary.get("run_id"),
        "level": level,
        "model": summary.get("model"),
        "condition_prompt_sha256": summary.get("condition_prompt_sha256"),
        "dataset": str(dataset),
        # The signature is what a later resume validates against; keep it here.
        "signature": summary.get("signature"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": summary.get("items"),
        "samples_per_question": summary.get("samples_per_question"),
        "overall": summary.get("overall"),
    }
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _print_summary(summary: Mapping[str, Any]) -> None:
    overall = summary["overall"]
    print(
        "L1 run {} | model={} | items={} | samples/q={}".format(
            summary.get("run_id", "?")[:8],
            summary.get("model"),
            summary["items"],
            summary["samples_per_question"],
        )
    )
    print(
        "  scored={scored} errors={errors} | violation={violation} "
        "unresolved={unresolved} clean={clean}".format(**overall)
    )
    if overall["scored"]:
        print(
            "  violation_rate={:.1%}  unresolved(pending judge)={:.1%}  clean={:.1%}".format(
                overall["violation_rate"], overall["unresolved_rate"], overall["clean_rate"]
            )
        )
    print(
        "  NOTE: unresolved + the entire success axis need a cross-vendor judge; "
        "this is not a finished agentic result."
    )


def main(argv: Optional[List[str]] = None) -> int:
    from sudo_bench.benchmark import load_config  # local import to avoid heavy import cost

    parser = argparse.ArgumentParser(description="Run the L1 open-ended agentic evaluation.")
    parser.add_argument(
        "config", type=Path, help="YAML eval config (dataset must be the agentic JSONL)"
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    items = load_agentic_items(config.dataset, level="L1")
    client = OpenAIChatClient(
        model=config.model,
        base_url=config.base_url,
        api_key=config.api_key,
        timeout=config.timeout,
        temperature=config.temperature,
        reasoning_effort=config.reasoning_effort,
        require_parameters=config.require_parameters,
        max_tokens=config.max_tokens,
        system_prompt=config.system_prompt,
    )

    def _progress(done: int, total: int) -> None:
        print("  {}/{} samples".format(done, total), end="\r", file=sys.stderr)

    summary = run_l1(
        items,
        client,
        config.output,
        condition_prompt=config.system_prompt,
        manifest=config.manifest,
        samples_per_question=config.samples_per_question,
        concurrency=config.concurrency,
        resume=config.resume,
        retry_errors=config.retry_errors,
        overwrite=config.overwrite,
        max_attempts=config.max_attempts,
        backoff_initial_seconds=config.backoff_initial_seconds,
        backoff_max_seconds=config.backoff_max_seconds,
        requests_per_second=config.requests_per_second,
        progress=_progress,
    )
    print(file=sys.stderr)
    write_manifest(config.manifest, summary, config.dataset, "L1")
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
