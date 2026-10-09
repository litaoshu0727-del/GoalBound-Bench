"""Integrity checks for the frozen question-003 boundary-v2 validation set."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sudo_bench.agentic.blocks import AGENTIC_BLOCKS
from sudo_bench.agentic.boundary_policies import QUESTION_003_BOUNDARY_V2
from sudo_bench.agentic.build_dataset import DEFAULT_OUT
from sudo_bench.agentic.calibration import case_sha256, judge_prompt_sha256
from sudo_bench.agentic.identity import canonical_sha256
from sudo_bench.agentic.q003_automated_validation import combine_boundary

ROOT = Path(__file__).resolve().parents[1]
BATCH = ROOT / "annotation/calibration/question-003-boundary-v2-validation-v1"
CASES = BATCH / "cases.jsonl"
PROTOCOL = BATCH / "protocol.json"
REPORT = BATCH / "automated-dual-judge-report.json"
JUDGED_A = BATCH / "judged-A-v2.jsonl"
JUDGED_C = BATCH / "judged-C-v2.jsonl"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def test_q003_v2_validation_cases_are_frozen_and_model_blinded():
    cases_bytes = CASES.read_bytes()
    rows = [json.loads(line) for line in cases_bytes.decode().splitlines() if line]
    protocol = json.loads(PROTOCOL.read_text())

    assert len(rows) == 24
    assert len({row["case_key"] for row in rows}) == 24
    assert [row["case_key"] for row in rows] == [
        "Q3V2-{:03d}".format(index) for index in range(1, 25)
    ]
    assert protocol["sampling"]["n"] == 24
    assert set(protocol["sampling"]["selected_per_model"].values()) == {8}
    assert protocol["artifacts"]["cases_sha256"] == _sha256(cases_bytes)

    block = AGENTIC_BLOCKS["question-003"]
    allowed_fields = {
        "case_key",
        "source_id",
        "axis",
        "boundary_policy_version",
        "public_facts",
        "task_prompt",
        "criterion",
        "output",
        "output_sha256",
    }
    for row in rows:
        assert set(row) == allowed_fields
        assert row["source_id"] == "question-003"
        assert row["axis"] == "boundary"
        assert row["boundary_policy_version"] == QUESTION_003_BOUNDARY_V2["policy_id"]
        assert row["public_facts"] == block["public_facts"]
        assert row["task_prompt"] == block["task_prompt_l1"]
        assert row["criterion"] == QUESTION_003_BOUNDARY_V2["rubric"]
        assert row["output_sha256"] == _sha256(row["output"].encode())


def test_q003_v2_validation_protocol_freezes_a_gold_free_automated_check():
    protocol = json.loads(PROTOCOL.read_text())

    assert protocol["status"] == "policy_frozen_automated_dual_judge_authorized"
    assert protocol["source"]["model_facing_prompt_changed"] is False
    assert protocol["policy"]["frozen_for_phase"] is True
    assert protocol["policy"]["canonical_sha256"] == canonical_sha256(
        QUESTION_003_BOUNDARY_V2
    )
    assert protocol["automated_validation"]["human_labels"] is False
    assert protocol["automated_validation"]["no_tuning_after_results"] is True
    assert protocol["automated_validation"]["judge_a"] == "openai/gpt-5.6-sol"
    assert protocol["automated_validation"]["judge_c"] == "z-ai/glm-5.3-flash"
    assert protocol["artifacts"]["private_selection_committed"] is False
    assert "do not report accuracy, recall or precision" in (
        protocol["reporting"]["guardrail"]
    )
    assert "cannot estimate the 720-case population rate" in (
        protocol["reporting"]["guardrail"]
    )


def test_q003_v2_dual_judge_disagreements_never_get_silently_resolved():
    assert combine_boundary("clean", "clean") == "clean"
    assert combine_boundary("violation", "violation") == "violation"
    assert combine_boundary("clean", "violation") == "unresolved"
    assert combine_boundary("violation", None) == "unresolved"


def test_q003_v2_is_published_in_v4_without_rewriting_frozen_v3_prompts():
    assert DEFAULT_OUT.name == "questions.v4.agentic.jsonl"
    datasets = {}
    for version in ("v3", "v4"):
        rows = [
            json.loads(line)
            for line in (ROOT / f"questions.{version}.agentic.jsonl")
            .read_text()
            .splitlines()
            if line
        ]
        datasets[version] = {row["id"]: row for row in rows}

    old = datasets["v3"]["question-003"]["agentic"]
    current = datasets["v4"]["question-003"]["agentic"]
    assert "boundary_policy" not in old
    assert current["boundary_policy"]["active_version"] == (
        QUESTION_003_BOUNDARY_V2["policy_id"]
    )
    assert current["ground_truth"]["boundary"] == QUESTION_003_BOUNDARY_V2["boundary"]
    assert old["prompt_l1"] == current["prompt_l1"]
    assert old["prompt_l2"] == current["prompt_l2"]


def test_q003_v2_automated_dual_judge_results_are_complete_and_auditable():
    cases = {
        row["case_key"]: row
        for row in (
            json.loads(line) for line in CASES.read_text().splitlines() if line
        )
    }
    judged = {
        "A": [json.loads(line) for line in JUDGED_A.read_text().splitlines() if line],
        "C": [json.loads(line) for line in JUDGED_C.read_text().splitlines() if line],
    }
    expected_models = {
        "A": "openai/gpt-5.6-sol",
        "C": "z-ai/glm-5.3-flash",
    }
    for judge, rows in judged.items():
        assert len(rows) == 24
        assert {row["case_key"] for row in rows} == set(cases)
        assert {row["judge_model"] for row in rows} == {expected_models[judge]}
        assert all(row["error"] is None for row in rows)
        assert all(row["judge_version"] == "7" for row in rows)
        for row in rows:
            case = cases[row["case_key"]]
            assert row["case_sha256"] == case_sha256(case)
            assert row["judge_prompt_sha256"] == judge_prompt_sha256(case)

    report = json.loads(REPORT.read_text())
    assert report["status"] == (
        "completed_automated_robustness_check_without_human_gold"
    )
    assert report["judges"]["A"]["output_sha256"] == _sha256(JUDGED_A.read_bytes())
    assert report["judges"]["C"]["output_sha256"] == _sha256(JUDGED_C.read_bytes())
    assert report["provenance"]["protocol_sha256"] == _sha256(PROTOCOL.read_bytes())
    assert report["v2_inter_judge"] == {
        "valid_pairs": 24,
        "agreements": 21,
        "agreement": 0.875,
        "cohen_kappa": 0.7272727272727273,
        "matrix_a_by_c": {
            "clean|clean": 14,
            "clean|violation": 1,
            "violation|clean": 2,
            "violation|violation": 7,
        },
    }
    assert report["combined_counts"]["v2"] == {
        "clean": 13,
        "refusal": 1,
        "unresolved": 3,
        "violation": 7,
    }
    assert report["v1_to_v2_transitions"] == {
        "clean->clean": 13,
        "clean->unresolved": 3,
        "clean->violation": 7,
        "refusal->refusal": 1,
    }
    assert report["changed_n"] == 10


def test_q003_v2_human_audit_protocol_matches_frozen_cases_and_policy():
    record = json.loads(
        (
            ROOT / "annotation/preregistration/question-003-boundary-v2-human-24-v1.json"
        ).read_text()
    )
    cases = [json.loads(line) for line in CASES.read_text().splitlines() if line]

    assert record["source"]["cases_sha256"] == _sha256(CASES.read_bytes())
    assert record["source"]["policy_canonical_sha256"] == canonical_sha256(
        QUESTION_003_BOUNDARY_V2
    )
    assert {case["criterion"] for case in cases} == {QUESTION_003_BOUNDARY_V2["rubric"]}
    assert record["source"]["n"] == len(cases) == 24
    assert set(record["workbooks"]["blank_workbook_sha256"]) == {
        "annotator_01.xlsx",
        "annotator_02.xlsx",
    }
    assert "adopt_v2" in record["decision_rule_for_final_rescoring"]
