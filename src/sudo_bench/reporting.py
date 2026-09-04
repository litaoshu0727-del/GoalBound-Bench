import hashlib
import json
import os
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from sudo_bench import __version__
from sudo_bench.benchmark import BenchmarkError, EvalConfig, Metrics


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _git_output(arguments: List[str]) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git"] + arguments,
            cwd=str(Path(__file__).resolve().parent),
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit or None


def _git_metadata() -> Dict[str, Any]:
    commit = _git_output(["rev-parse", "HEAD"])
    if commit is None:
        return {"commit": None, "dirty": None}
    status = _git_output(["status", "--short"])
    return {"commit": commit, "dirty": bool(status)}


def _percentile(values: List[float], fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 3)


def _read_rows(path: Path) -> List[Mapping[str, Any]]:
    rows: List[Mapping[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    return rows


def read_run_manifest(path: Path) -> Optional[Mapping[str, Any]]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError("cannot read run manifest {}: {}".format(path, exc)) from exc
    if not isinstance(data, dict) or not isinstance(data.get("run"), dict):
        raise BenchmarkError("invalid run manifest {}".format(path))
    return data


def validate_resume_manifest(manifest: Mapping[str, Any], config: EvalConfig) -> None:
    model = manifest.get("model")
    if isinstance(model, dict):
        previous_model = model.get("requested")
        previous_base_url = model.get("base_url")
        if previous_model is not None and previous_model != config.model:
            raise BenchmarkError("manifest model does not match the config")
        if previous_base_url is not None and previous_base_url != config.base_url:
            raise BenchmarkError("manifest base_url does not match the config")

    dataset = manifest.get("dataset")
    if isinstance(dataset, dict):
        previous_hash = dataset.get("sha256")
        if previous_hash is not None and previous_hash != _sha256_file(config.dataset):
            raise BenchmarkError("manifest dataset hash does not match the current dataset")

    prompt = manifest.get("prompt")
    if isinstance(prompt, dict):
        previous_prompt_hash = prompt.get("system_sha256")
        if previous_prompt_hash is not None and previous_prompt_hash != _sha256_text(
            config.system_prompt
        ):
            raise BenchmarkError("manifest system prompt does not match the config")

    parameters = manifest.get("parameters")
    if isinstance(parameters, dict):
        expected = {
            "temperature": config.temperature,
            "reasoning_effort": config.reasoning_effort,
            "require_parameters": config.require_parameters,
            "max_tokens": config.max_tokens,
            "samples_per_question": config.samples_per_question,
            "shuffle_options": config.shuffle_options,
            "shuffle_seed": config.shuffle_seed,
            "case_sensitive": config.case_sensitive,
        }
        for key, value in expected.items():
            if key in parameters and parameters[key] != value:
                raise BenchmarkError("manifest {} does not match the config".format(key))


def _aggregate_usage(rows: Iterable[Mapping[str, Any]]) -> Dict[str, float]:
    totals: Dict[str, float] = {}
    for row in rows:
        usage = row.get("usage")
        if not isinstance(usage, dict):
            continue
        for key, value in usage.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            totals[key] = totals.get(key, 0) + value
    return totals


def _sample_metrics(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[int, List[Mapping[str, Any]]] = {}
    for row in rows:
        sample_index = row.get("sample_index", 1)
        if isinstance(sample_index, int) and not isinstance(sample_index, bool):
            groups.setdefault(sample_index, []).append(row)

    summaries = []
    for sample_index, sample_rows in sorted(groups.items()):
        total = len(sample_rows)
        errors = sum(row.get("error") is not None for row in sample_rows)
        attempted = total - errors
        correct = sum(row.get("correct") is True for row in sample_rows)
        summaries.append(
            {
                "sample_index": sample_index,
                "total": total,
                "attempted": attempted,
                "correct": correct,
                "incorrect": attempted - correct,
                "errors": errors,
                "format_errors": sum(row.get("format_error") is not None for row in sample_rows),
                "accuracy": correct / total if total else 0.0,
                "target_choices": correct,
                "other_choices": attempted - correct,
                "target_choice_rate": correct / total if total else 0.0,
                "strict_avg_at_k": correct / total if total else 0.0,
                "behavioral_avg_at_k": correct / attempted if attempted else 0.0,
                "behavioral_target_choice_rate": (
                    correct / attempted if attempted else 0.0
                ),
            }
        )
    return summaries


def _label_confidence_metrics(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Mapping[str, Any]]] = {}
    for row in rows:
        metadata = row.get("metadata")
        confidence = metadata.get("label_confidence") if isinstance(metadata, dict) else None
        if not isinstance(confidence, str) or not confidence.strip():
            confidence = "unrated"
        groups.setdefault(confidence.strip().lower(), []).append(row)

    order = {"high": 0, "medium": 1, "low": 2, "unrated": 3}
    summaries = []
    for confidence, confidence_rows in sorted(
        groups.items(), key=lambda item: (order.get(item[0], 4), item[0])
    ):
        total = len(confidence_rows)
        errors = sum(row.get("error") is not None for row in confidence_rows)
        attempted = total - errors
        target_choices = sum(row.get("correct") is True for row in confidence_rows)
        summaries.append(
            {
                "label_confidence": confidence,
                "questions": len({str(row.get("id")) for row in confidence_rows}),
                "total": total,
                "attempted": attempted,
                "target_choices": target_choices,
                "other_choices": attempted - target_choices,
                "errors": errors,
                "format_errors": sum(
                    row.get("format_error") is not None for row in confidence_rows
                ),
                "target_choice_rate": target_choices / total if total else 0.0,
                "behavioral_target_choice_rate": (
                    target_choices / attempted if attempted else 0.0
                ),
            }
        )
    return summaries


def _reliability_summary(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    row_list = list(rows)
    attempt_counts = [
        row.get("attempt_count", 1)
        for row in row_list
        if isinstance(row.get("attempt_count", 1), int)
        and not isinstance(row.get("attempt_count", 1), bool)
    ]
    error_types = Counter(
        row["error_type"]
        for row in row_list
        if isinstance(row.get("error_type"), str) and row["error_type"]
    )
    return {
        "total_attempts": sum(attempt_counts),
        "retries": sum(max(0, count - 1) for count in attempt_counts),
        "recovered_samples": sum(
            row.get("error") is None
            and isinstance(row.get("attempt_count", 1), int)
            and not isinstance(row.get("attempt_count", 1), bool)
            and row.get("attempt_count", 1) > 1
            for row in row_list
        ),
        "final_error_types": dict(sorted(error_types.items())),
    }


def build_run_manifest(
    config: EvalConfig,
    config_path: Path,
    metrics: Optional[Metrics],
    run_id: str,
    started_at: str,
    completed_at: Optional[str],
    status: str = "completed",
    resume_count: int = 0,
    execution: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    config_path = config_path.resolve()
    root = config_path.parent
    rows = _read_rows(config.output) if config.output.exists() else []
    latencies = [
        float(row["latency_ms"])
        for row in rows
        if isinstance(row.get("latency_ms"), (int, float))
        and not isinstance(row.get("latency_ms"), bool)
    ]
    returned_models = sorted(
        {
            row["model"]
            for row in rows
            if row.get("error") is None
            and isinstance(row.get("model"), str)
            and row["model"].strip()
        }
    )
    git = _git_metadata()
    return {
        "schema_version": 3,
        "run": {
            "id": run_id,
            "status": status,
            "started_at": started_at,
            "updated_at": completed_at or started_at,
            "completed_at": completed_at,
            "resume_count": resume_count,
        },
        "benchmark": {
            "name": "sudo-bench",
            "version": __version__,
            "git_commit": git["commit"],
            "git_dirty": git["dirty"],
        },
        "model": {
            "requested": config.model,
            "returned": returned_models,
            "base_url": config.base_url,
        },
        "dataset": {
            "path": _display_path(config.dataset, root),
            "sha256": _sha256_file(config.dataset),
            "questions": metrics.questions if metrics is not None else None,
        },
        "prompt": {
            "system": config.system_prompt,
            "system_sha256": _sha256_text(config.system_prompt),
        },
        "parameters": {
            "timeout": config.timeout,
            "temperature": config.temperature,
            "reasoning_effort": config.reasoning_effort,
            "require_parameters": config.require_parameters,
            "max_tokens": config.max_tokens,
            "concurrency": config.concurrency,
            "samples_per_question": config.samples_per_question,
            "resume": config.resume,
            "retry_errors": config.retry_errors,
            "max_attempts": config.max_attempts,
            "backoff_initial_seconds": config.backoff_initial_seconds,
            "backoff_max_seconds": config.backoff_max_seconds,
            "requests_per_second": config.requests_per_second,
            "shuffle_options": config.shuffle_options,
            "shuffle_seed": config.shuffle_seed,
            "case_sensitive": config.case_sensitive,
        },
        "artifacts": {
            "config": _display_path(config_path, root),
            "config_sha256": _sha256_file(config_path),
            "results": _display_path(config.output, root),
            "results_sha256": _sha256_file(config.output) if config.output.exists() else None,
            "manifest": _display_path(config.manifest, root),
        },
        "metrics": metrics.to_dict() if metrics is not None else None,
        "sample_metrics": _sample_metrics(rows),
        "label_metrics": _label_confidence_metrics(rows),
        "execution": dict(execution or {}),
        "reliability": _reliability_summary(rows),
        "usage": _aggregate_usage(rows),
        "latency_ms": {
            "min": round(min(latencies), 3) if latencies else None,
            "median": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 3) if latencies else None,
        },
    }


def write_run_manifest(
    manifest: Mapping[str, Any],
    path: Path,
    overwrite: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise FileExistsError(path)

    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=".{}-".format(path.name),
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(str(temporary_path), str(path))
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
