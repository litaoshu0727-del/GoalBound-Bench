"""Recompute the real-output human calibration report without model/API calls."""

from __future__ import annotations

import hashlib
import json
import random
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

BOOTSTRAP_REPLICATES = 10_000
BOOTSTRAP_SEED = "agentic-l1-real-output-audit-120-v1|rao-wu-bootstrap"

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


def percentile(sorted_values: list[float], q: float) -> float:
    position = q * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - lower
    return sorted_values[lower] * (1 - fraction) + sorted_values[upper] * fraction


def pool_statistics(rows: list[dict], weight: Callable[[dict], float]) -> dict:
    pipeline = metrics(rows, weight)["pipeline"]
    refusal = pipeline["refusal"]
    boundary = pipeline["boundary_human_engaged"]
    success = pipeline["success_all"]
    return {
        "refusal.accuracy_resolved": refusal["accuracy_resolved"],
        "boundary_human_engaged.coverage": boundary["coverage"],
        "boundary_human_engaged.accuracy_resolved": boundary["accuracy_resolved"],
        "boundary_human_engaged.cohen_kappa_resolved": boundary["cohen_kappa_resolved"],
        "boundary_human_engaged.violation_recall": boundary["per_class"]["violation"]["recall"],
        "boundary_human_engaged.violation_precision": boundary["per_class"]["violation"][
            "precision"
        ],
        "success_all.coverage": success["coverage"],
        "success_all.accuracy_resolved": success["accuracy_resolved"],
        "success_all.cohen_kappa_resolved": success["cohen_kappa_resolved"],
    }


def rate_statistics(weighted_rows: list[tuple[dict, float]]) -> dict:
    estimate = human_label_estimate(weighted_rows)
    return {
        key: estimate[key]
        for key in ("refusal_rate", "violation_rate_human_engaged", "achieved_rate")
    }


def summarize_draws(point: dict, draws: dict[str, list[float | None]]) -> dict:
    summary = {}
    for key, values in draws.items():
        defined = sorted(value for value in values if value is not None)
        summary[key] = {
            "point": point[key],
            "ci95": (
                [percentile(defined, 0.025), percentile(defined, 0.975)] if defined else None
            ),
            "undefined_replicates": len(values) - len(defined),
        }
    return summary


def collapsed_strata_bootstrap(
    core: list[dict],
    census: list[dict],
    core_weight: Callable[[dict], float],
    models: list[str],
) -> dict:
    """Rao-Wu rescaling bootstrap over collapsed (per-model) variance strata.

    The design has two rows per model-by-question stratum, which makes the
    within-stratum variance estimate zero whenever both rows agree. The 45 design
    strata are therefore collapsed to the three models for variance estimation only:
    each replicate draws m = n - 1 = 29 of a model's 30 core rows with replacement and
    rescales each drawn row's design weight by n / m. Collapsing adds between-question
    variance, so the intervals are conservative. Point estimates are unchanged.
    """
    variance_strata: dict[str, list[dict]] = {}
    for row in core:
        variance_strata.setdefault(row["model"], []).append(row)

    replicate_weights: dict[int, float] = {}

    def replicate_weight(row: dict) -> float:
        return replicate_weights[id(row)]

    def full_run(rows: list[dict]) -> list[tuple[dict, float]]:
        weighted = [(row, replicate_weight(row)) for row in rows]
        weighted.extend((row, 1.0) for row in census)
        return weighted

    def by_model(weighted: list[tuple[dict, float]]) -> dict[str, list[tuple[dict, float]]]:
        return {
            model: [(row, weight) for row, weight in weighted if row["model"] == model]
            for model in models
        }

    pool_point = pool_statistics(core, core_weight)
    full_point_rows = [(row, core_weight(row)) for row in core]
    full_point_rows.extend((row, 1.0) for row in census)
    full_point = rate_statistics(full_point_rows)
    model_point = {
        model: rate_statistics(rows) for model, rows in by_model(full_point_rows).items()
    }

    pool_draws: dict[str, list] = {key: [] for key in pool_point}
    full_draws: dict[str, list] = {key: [] for key in full_point}
    model_draws = {model: {key: [] for key in full_point} for model in models}

    rng = random.Random(BOOTSTRAP_SEED)
    for _ in range(BOOTSTRAP_REPLICATES):
        replicate_weights.clear()
        for model in models:
            rows = variance_strata[model]
            draws = len(rows) - 1
            for row in (rng.choice(rows) for _ in range(draws)):
                replicate_weights[id(row)] = replicate_weights.get(id(row), 0.0) + (
                    core_weight(row) * len(rows) / draws
                )
        picked = [row for row in core if id(row) in replicate_weights]
        for key, value in pool_statistics(picked, replicate_weight).items():
            pool_draws[key].append(value)
        weighted = full_run(picked)
        for key, value in rate_statistics(weighted).items():
            full_draws[key].append(value)
        for model, rows in by_model(weighted).items():
            for key, value in rate_statistics(rows).items():
                model_draws[model][key].append(value)

    return {
        "method": (
            "Rao-Wu rescaling bootstrap (m = n - 1) over the core rows, with the 45 "
            "model-by-question design strata collapsed to 3 per-model variance strata; "
            "95% percentile intervals."
        ),
        "replicates": BOOTSTRAP_REPLICATES,
        "seed": BOOTSTRAP_SEED,
        "notes": [
            "Only sampling variance from drawing 2 of each stratum's eligible outputs is "
            "captured. Annotator disagreement, arbitration and judge run-to-run variance "
            "are not.",
            "With 2 rows per design stratum the within-stratum variance is zero whenever "
            "both rows agree, so the design strata are collapsed to models for variance "
            "estimation. Collapsing and the absent finite-population correction "
            "(2 of up to 16 per stratum) both make the intervals conservative.",
            "The 21 detector-positive census cases are held fixed in every replicate.",
            "A degenerate interval (low equals high) means no event or disagreement was "
            "observed in the sampled rows, not that the rate is zero; by the rule of three "
            "the unobserved rate could still be up to about 10% for one model's 30 rows "
            "or about 3% for all 90.",
            "undefined_replicates counts replicates where a ratio had a zero denominator "
            "or kappa had no label variance; intervals use the defined replicates only.",
            "risk_enriched has no population weight and gets no interval.",
        ],
        "eligible_pool_699_pipeline": summarize_draws(pool_point, pool_draws),
        "full_720_human_label_estimate": summarize_draws(full_point, full_draws),
        "full_720_human_label_estimate_by_model": {
            model: summarize_draws(model_point[model], model_draws[model]) for model in models
        },
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
        "uncertainty": collapsed_strata_bootstrap(
            core, list(detector_by_case.values()), core_weight, models
        ),
    }
    OUTPUT_PATH.write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
