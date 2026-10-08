"""Integrity checks for the 120-case L1 real-output human calibration audit."""

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).parents[1]
GOLD = ROOT / "annotation/gold/agentic-l1-real-output-audit-120-v1"


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_real_output_gold_has_complete_unique_axis_coverage():
    rows = _jsonl(GOLD / "labels.jsonl")
    axes_by_case = defaultdict(set)
    for row in rows:
        axes_by_case[row["case_key"]].add(row["axis"])

    assert len(rows) == 360
    assert len(axes_by_case) == 120
    assert set(map(frozenset, axes_by_case.values())) == {
        frozenset({"refusal", "boundary", "success"})
    }
    assert Counter(row["group"] for row in rows) == {
        "core_random": 270,
        "risk_enriched": 90,
    }
    assert Counter(row["label_source"] for row in rows) == {
        "annotator_agreement": 337,
        "blind_arbitration": 23,
    }
    assert all(len(row["output_sha256"]) == 64 for row in rows)


def test_arbitration_and_final_counts_match_summary():
    rows = _jsonl(GOLD / "labels.jsonl")
    summary = json.loads((GOLD / "summary.json").read_text())
    arbitrated = [row for row in rows if row["arbitration_id"]]

    assert len(arbitrated) == 23
    assert len({row["case_key"] for row in arbitrated}) == 22
    assert Counter(row["axis"] for row in arbitrated) == {"success": 21, "boundary": 2}
    assert Counter((row["axis"], row["label"]) for row in arbitrated) == {
        ("success", "achieved"): 18,
        ("success", "not_achieved"): 3,
        ("boundary", "clean"): 2,
    }
    for group in ("all", "core_random", "risk_enriched"):
        selected = rows if group == "all" else [row for row in rows if row["group"] == group]
        for axis in ("refusal", "boundary", "success"):
            assert Counter(
                row["label"] for row in selected if row["axis"] == axis
            ) == summary["final_label_counts"][group][axis]


def test_predictions_align_without_publishing_candidate_text():
    labels = _jsonl(GOLD / "labels.jsonl")
    predictions = _jsonl(GOLD / "predictions.jsonl")
    labels_by_case = {row["case_key"]: row for row in labels}
    forbidden = {"output", "candidate_output", "task_prompt", "criterion", "rubric"}

    assert len(predictions) == 120
    assert len({row["case_key"] for row in predictions}) == 120
    assert {row["case_key"] for row in predictions} == set(labels_by_case)
    for row in predictions:
        label = labels_by_case[row["case_key"]]
        assert not forbidden & set(row)
        assert row["output_sha256"] == label["output_sha256"]
        assert row["model"] == label["model"]
        assert row["group"] == label["group"]


def test_sampling_hash_and_detector_audit_exclusion_are_preserved():
    labels = _jsonl(GOLD / "labels.jsonl")
    case_keys = sorted({row["case_key"] for row in labels})
    selected_hash = hashlib.sha256("\n".join(case_keys).encode()).hexdigest()
    provenance = json.loads((GOLD / "provenance.json").read_text())
    detector_rows = _jsonl(
        ROOT / "annotation/gold/agentic-l1-detector-audit-21-v1/labels.jsonl"
    )

    assert selected_hash == provenance["sampling"]["selected_case_keys_sha256"]
    assert not set(case_keys) & {row["case_key"] for row in detector_rows}


def test_frozen_hashes_and_weighted_analysis_are_self_consistent():
    summary = json.loads((GOLD / "summary.json").read_text())
    analysis = json.loads((GOLD / "analysis.json").read_text())

    assert summary["artifacts"]["labels_sha256"] == _sha256(GOLD / "labels.jsonl")
    assert summary["artifacts"]["predictions_sha256"] == _sha256(
        GOLD / "predictions.jsonl"
    )
    assert summary["artifacts"]["analysis_sha256"] == _sha256(GOLD / "analysis.json")
    weighted = analysis["eligible_pool_699_stratum_weighted"]["pipeline"]
    assert weighted["refusal"]["weighted_n"] == 699
    assert weighted["refusal"]["accuracy_resolved"] == 1
    assert weighted["boundary_human_engaged"]["weighted_n"] == 651
    assert weighted["boundary_human_engaged"]["per_class"]["violation"]["recall"] == 0.5
    assert weighted["success_all"]["coverage"] == 0.9220314735336195
    full_run = analysis["full_720_human_label_estimate"]
    assert full_run["population_n"] == 720
    assert full_run["refusal_counts"] == {"engaged": 670, "refusal": 50}
    assert full_run["boundary_human_engaged_counts"] == {
        "clean": 606,
        "violation": 64,
    }
    assert full_run["success_counts"] == {"achieved": 536, "not_achieved": 184}
    assert "must not be pooled" in analysis["reporting_guardrail"]


def test_normalization_is_narrow_and_documented():
    provenance = json.loads((GOLD / "provenance.json").read_text())

    assert provenance["normalizations"] == [
        {
            "annotator": "01",
            "workbook_row": 96,
            "axis": "refusal",
            "raw": "engaged+D98",
            "normalized": "engaged",
            "rule": (
                "Strip one accidental +cell-reference suffix only when the prefix is an "
                "otherwise valid label."
            ),
        }
    ]


def test_core_boundary_false_negatives_are_localized_to_question_003():
    labels = _jsonl(GOLD / "labels.jsonl")
    predictions = _jsonl(GOLD / "predictions.jsonl")
    gold = {(row["case_key"], row["axis"]): row["label"] for row in labels}
    false_negatives = [
        row
        for row in predictions
        if row["group"] == "core_random"
        and gold[(row["case_key"], "refusal")] == "engaged"
        and gold[(row["case_key"], "boundary")] == "violation"
        and row["combined"]["boundary"] != "violation"
    ]

    assert len(false_negatives) == 4
    assert {row["source_id"] for row in false_negatives} == {"question-003"}


def test_bootstrap_intervals_are_published_and_bracket_point_estimates():
    analysis = json.loads((GOLD / "analysis.json").read_text())
    summary = json.loads((GOLD / "summary.json").read_text())
    uncertainty = analysis["uncertainty"]

    assert uncertainty["replicates"] == 10_000
    assert "collapsed to 3 per-model variance strata" in uncertainty["method"]
    sections = [
        uncertainty["eligible_pool_699_pipeline"],
        uncertainty["full_720_human_label_estimate"],
        *uncertainty["full_720_human_label_estimate_by_model"].values(),
    ]
    for section in sections:
        for stat in section.values():
            low, high = stat["ci95"]
            assert 0 <= low <= high <= 1
            assert low <= stat["point"] <= high

    recall = uncertainty["eligible_pool_699_pipeline"][
        "boundary_human_engaged.violation_recall"
    ]
    assert recall["point"] == 0.5
    assert recall["ci95"][1] - recall["ci95"][0] > 0.5
    assert summary["uncertainty_ci95"]["full_720_human_label_estimate"] == {
        key: value["ci95"]
        for key, value in uncertainty["full_720_human_label_estimate"].items()
    }


def test_protocol_record_discloses_that_annotation_had_started():
    record = json.loads(
        (ROOT / "annotation/preregistration/l1-real-output-audit-120-v1.json").read_text()
    )

    assert record["status"] == "protocol_recorded_after_annotator_01_completed"
    assert record["status_as_originally_committed"] == "selection_frozen_annotation_not_started"
    assert "not a strict public preregistration" in record["amendment"]["correction"]
