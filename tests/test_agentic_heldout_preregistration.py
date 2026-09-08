"""Integrity checks for the prospective held-out judge calibration set."""

import hashlib
import json
from pathlib import Path

PREREGISTRATION = (
    Path(__file__).parents[1]
    / "annotation"
    / "preregistration"
    / "agentic-heldout-calibration-60-v1.json"
)


def _load() -> dict:
    return json.loads(PREREGISTRATION.read_text(encoding="utf-8"))


def test_heldout_calibration_design_is_balanced_and_frozen_before_judging():
    prereg = _load()
    design = prereg["design"]

    assert prereg["batch_id"] == "agentic-heldout-calibration-60-v1"
    assert prereg["status"] == "human_gold_frozen"
    assert prereg["judge_runs_started"] is False
    assert prereg["human_gold_frozen"] is True
    assert design["total_examples"] == 60
    assert design["source_items"] == 15
    assert design["axes_are_separate"] is True
    assert design["axis_counts"] == {"boundary": 30, "success": 30}
    assert design["author_seed_label_counts"] == {
        "clean": 15,
        "violation": 15,
        "achieved": 15,
        "not_achieved": 15,
    }
    human = prereg["human_annotation"]
    assert human["annotator_01_complete"] is True
    assert human["annotator_02_complete"] is True
    assert human["normalization_count"] == 1
    assert human["substantive_disagreements"] == 1
    assert human["blind_adjudication_rows"] == 1
    assert human["pre_adjudication_agreement"]["boundary"] == {
        "n": 30,
        "agreements": 30,
        "agreement_rate": 1.0,
        "cohen_kappa": 1.0,
    }
    assert human["pre_adjudication_agreement"]["success"] == {
        "n": 30,
        "agreements": 29,
        "agreement_rate": 0.9666666666666667,
        "cohen_kappa": 0.9333333333333333,
    }


def test_heldout_calibration_artifact_commitments_are_complete():
    artifacts = _load()["artifacts"]
    hash_fields = (
        "seed_sha256",
        "annotator_01_workbook_sha256",
        "annotator_02_workbook_sha256",
        "annotator_01_completed_workbook_sha256",
        "annotator_02_completed_workbook_sha256",
        "blind_adjudication_workbook_sha256",
        "blind_adjudication_completed_workbook_sha256",
    )

    assert all(len(artifacts[field]) == 64 for field in hash_fields)
    assert all(
        all(character in "0123456789abcdef" for character in artifacts[field])
        for field in hash_fields
    )
    assert artifacts["private_seed_committed"] is False
    assert artifacts["blind_workbooks_committed"] is False
    assert artifacts["answer_mappings_committed"] is False


def test_frozen_human_gold_hashes_match_the_committed_artifacts():
    artifacts = _load()["artifacts"]["frozen_human_gold_sha256"]
    gold = PREREGISTRATION.parents[1] / "gold" / "agentic-heldout-calibration-60-v1"

    for key, filename in {
        "labels": "labels.jsonl",
        "summary": "summary.json",
        "readme": "README.md",
    }.items():
        assert artifacts[key] == hashlib.sha256((gold / filename).read_bytes()).hexdigest()
