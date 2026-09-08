"""Integrity checks for the prospective held-out judge calibration set."""

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
    assert prereg["status"] == "awaiting_human_annotation"
    assert prereg["judge_runs_started"] is False
    assert prereg["human_gold_frozen"] is False
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


def test_heldout_calibration_artifact_commitments_are_complete():
    artifacts = _load()["artifacts"]
    hash_fields = (
        "seed_sha256",
        "annotator_01_workbook_sha256",
        "annotator_02_workbook_sha256",
    )

    assert all(len(artifacts[field]) == 64 for field in hash_fields)
    assert all(
        all(character in "0123456789abcdef" for character in artifacts[field])
        for field in hash_fields
    )
    assert artifacts["private_seed_committed"] is False
    assert artifacts["blind_workbooks_committed"] is False
    assert artifacts["answer_mappings_committed"] is False
