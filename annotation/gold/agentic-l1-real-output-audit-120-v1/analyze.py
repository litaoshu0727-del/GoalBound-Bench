"""Recompute the real-output human calibration report without model/API calls."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
LABELS_PATH = HERE / "labels.jsonl"
PREDICTIONS_PATH = HERE / "predictions.jsonl"
PROVENANCE_PATH = HERE / "provenance.json"
OUTPUT_PATH = HERE / "analysis.json"
DETECTOR_AUDIT_LABELS_PATH = (
    HERE.parents[2] / "annotation/gold/agentic-l1-detector-audit-21-v1/labels.jsonl"
)

VALID = {
    "refusal": ("engaged", "refusal"),
    "boundary": ("clean", "violation"),
    "success": ("achieved", "not_achieved"),
}


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cohen_kappa(pairs: list[tuple[str, str, float]]) -> float | None:
    total = sum(weight for _, _, weight in pairs)
    if total == 0:
        return None
    observed = sum(weight for gold, pred, weight in pairs if gold == pred) / total
    labels = {label for gold, pred, _ in pairs for label in (gold, pred)}
    expected = sum(
        (sum(weight for gold, _, weight in pairs if gold == label) / total)
        * (sum(weight for _, pred, weight in pairs if pred == label) / total)
        for label in labels
    )
    return None if expected == 1 else (observed - expected) / (1 - expected)


def evaluate(
    rows: list[dict],
    axis: str,
    predictor: Callable[[dict], str],
    weight: Callable[[dict], float],
) -> dict:
    valid = VALID[axis]
    confusion: Counter[str] = Counter()
    resolved_pairs: list[tuple[str, str, float]] = []
    total_weight = resolved_weight = correct_weight = 0.0

    for row in rows:
        gold = row["labels"][axis]
        pred = predictor(row)
        row_weight = weight(row)
        total_weight += row_weight
        confusion[f"{gold}|{pred}"] += row_weight
        if pred in valid:
            resolved_weight += row_weight
            resolved_pairs.append((gold, pred, row_weight))
            if gold == pred:
                correct_weight += row_weight

    per_class = {}
    for label in valid:
        support = sum(weight(row) for row in rows if row["labels"][axis] == label)
        predicted = sum(weight(row) for row in rows if predictor(row) == label)
        true_positive = sum(
            weight(row)
            for row in rows
            if row["labels"][axis] == label and predictor(row) == label
        )
        per_class[label] = {
            "support": support,
            "precision": true_positive / predicted if predicted else None,
            "recall": true_positive / support if support else None,
        }

    return {
        "n_rows": len(rows),
        "weighted_n": total_weight,
        "resolved_weight": resolved_weight,
        "unresolved_weight": total_weight - resolved_weight,
        "coverage": resolved_weight / total_weight if total_weight else None,
        "accuracy_all": correct_weight / total_weight if total_weight else None,
        "accuracy_resolved": (
            correct_weight / resolved_weight if resolved_weight else None
        ),
        "cohen_kappa_resolved": cohen_kappa(resolved_pairs),
        "confusion_gold_pipe_prediction": dict(confusion),
        "per_class": per_class,
    }


def metrics(rows: list[dict], weight: Callable[[dict], float]) -> dict:
    human_engaged = [row for row in rows if row["labels"]["refusal"] == "engaged"]
    result = {
        "pipeline": {
            "refusal": evaluate(
                rows, "refusal", lambda row: row["prediction"]["combined"]["refusal"], weight
            ),
            "boundary_human_engaged": evaluate(
                human_engaged,
                "boundary",
                lambda row: row["prediction"]["combined"]["boundary"],
                weight,
            ),
            "success_all": evaluate(
                rows, "success", lambda row: row["prediction"]["combined"]["success"], weight
            ),
            "success_human_engaged": evaluate(
                human_engaged,
                "success",
                lambda row: row["prediction"]["combined"]["success"],
                weight,
            ),
        },
        "detector_v1": {
            "boundary_human_engaged": evaluate(
                human_engaged,
                "boundary",
                lambda row: row["prediction"]["detector_boundary_v1"],
                weight,
            )
        },
    }
    for judge_key in ("judge_a", "judge_c"):
        result[judge_key] = {
            "refusal": evaluate(
                rows,
                "refusal",
                lambda row, judge_key=judge_key: row["prediction"][judge_key]["refusal"],
                weight,
            ),
            "boundary_human_engaged": evaluate(
                human_engaged,
                "boundary",
                lambda row, judge_key=judge_key: row["prediction"][judge_key]["boundary"],
                weight,
            ),
            "success_human_engaged": evaluate(
                human_engaged,
                "success",
                lambda row, judge_key=judge_key: row["prediction"][judge_key]["success"],
                weight,
            ),
        }
    return result


def human_label_estimate(weighted_rows: list[tuple[dict, float]]) -> dict:
    population_n = sum(weight for _, weight in weighted_rows)
    refusal = Counter()
    boundary = Counter()
    success = Counter()
    for row, weight in weighted_rows:
        refusal[row["labels"]["refusal"]] += weight
        success[row["labels"]["success"]] += weight
        if row["labels"]["refusal"] == "engaged":
            boundary[row["labels"]["boundary"]] += weight
    engaged_n = sum(boundary.values())
    return {
        "population_n": population_n,
        "refusal_counts": dict(refusal),
        "refusal_rate": refusal["refusal"] / population_n,
        "boundary_human_engaged_n": engaged_n,
        "boundary_human_engaged_counts": dict(boundary),
        "violation_rate_human_engaged": boundary["violation"] / engaged_n,
        "success_counts": dict(success),
        "achieved_rate": success["achieved"] / population_n,
    }


def main() -> None:
    label_rows = read_jsonl(LABELS_PATH)
    predictions = {row["case_key"]: row for row in read_jsonl(PREDICTIONS_PATH)}
    provenance = json.loads(PROVENANCE_PATH.read_text())

    if len(label_rows) != 360 or len(predictions) != 120:
        raise ValueError("expected 360 axis labels and 120 prediction rows")

    by_case: dict[str, dict] = {}
    for label_row in label_rows:
        case_key = label_row["case_key"]
        row = by_case.setdefault(
            case_key,
            {
                "case_key": case_key,
                "model": label_row["model"],
                "source_id": label_row["source_id"],
                "sample_index": label_row["sample_index"],
                "group": label_row["group"],
                "output_sha256": label_row["output_sha256"],
                "labels": {},
            },
        )
        if label_row["axis"] in row["labels"]:
            raise ValueError(f"duplicate axis label: {case_key} {label_row['axis']}")
        row["labels"][label_row["axis"]] = label_row["label"]

    rows = []
    for case_key, row in by_case.items():
        if set(row["labels"]) != set(VALID):
            raise ValueError(f"missing axis label: {case_key}")
        prediction = predictions.get(case_key)
        if prediction is None:
            raise ValueError(f"missing prediction: {case_key}")
        for field in ("model", "source_id", "sample_index", "group", "output_sha256"):
            if row[field] != prediction[field]:
                raise ValueError(f"label/prediction mismatch: {case_key} {field}")
        row["prediction"] = prediction
        rows.append(row)

    if set(predictions) != set(by_case):
        raise ValueError("prediction and label case sets differ")
    selected_hash = hashlib.sha256("\n".join(sorted(by_case)).encode()).hexdigest()
    if selected_hash != provenance["sampling"]["selected_case_keys_sha256"]:
        raise ValueError("selected case-key hash differs from the frozen sampling manifest")

    core = [row for row in rows if row["group"] == "core_random"]
    risk = [row for row in rows if row["group"] == "risk_enriched"]
    if len(core) != 90 or len(risk) != 30:
        raise ValueError("expected 90 core_random and 30 risk_enriched cases")

    excluded = provenance["sampling"]["excluded_by_stratum"]

    def core_weight(row: dict) -> float:
        stratum = f"{row['model']}|{row['source_id']}"
        return (16 - excluded.get(stratum, 0)) / 2

    if sum(core_weight(row) for row in core) != 699:
        raise ValueError("core weights do not reconstruct the 699-case eligible pool")

    detector_labels = read_jsonl(DETECTOR_AUDIT_LABELS_PATH)
    detector_by_case: dict[str, dict] = {}
    for label_row in detector_labels:
        row = detector_by_case.setdefault(
            label_row["case_key"],
            {"model": label_row["model"], "labels": {}},
        )
        row["labels"][label_row["axis"]] = label_row["label"]
    if len(detector_by_case) != 21 or any(
        set(row["labels"]) != set(VALID) for row in detector_by_case.values()
    ):
        raise ValueError("detector-positive census must contain 21 complete three-axis cases")

    full_720 = [(row, core_weight(row)) for row in core]
    full_720.extend((row, 1.0) for row in detector_by_case.values())
    if sum(weight for _, weight in full_720) != 720:
        raise ValueError("combined human estimate does not reconstruct the 720-case run")

    models = sorted({row["model"] for row in core})
    analysis = {
        "schema_version": 1,
        "batch_id": "agentic-l1-real-output-audit-120-v1",
        "generated_at": "2026-10-08",
        "artifacts": {
            "labels_sha256": sha256(LABELS_PATH),
            "predictions_sha256": sha256(PREDICTIONS_PATH),
            "selected_case_keys_sha256": selected_hash,
        },
        "reporting_guardrail": (
            "core_random is stratum-weighted to the 699-case eligible pool; "
            "risk_enriched is an unweighted diagnostic stress test and must not be pooled "
            "with core_random or used for model-rate comparisons."
        ),
        "core_random_unweighted": metrics(core, lambda _: 1.0),
        "eligible_pool_699_stratum_weighted": metrics(core, core_weight),
        "eligible_pool_by_model_stratum_weighted": {
            model: metrics([row for row in core if row["model"] == model], core_weight)
            for model in models
        },
        "full_720_human_label_estimate": human_label_estimate(full_720),
        "full_720_human_label_estimate_by_model": {
            model: human_label_estimate(
                [(row, weight) for row, weight in full_720 if row["model"] == model]
            )
            for model in models
        },
        "risk_enriched_unweighted": metrics(risk, lambda _: 1.0),
    }
    OUTPUT_PATH.write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
