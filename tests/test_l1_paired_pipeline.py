"""Synthetic-data checks for the paired induced/neutral analysis, written before any data."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "reports" / "l1-mvp-induced-avg16"))
SPEC = importlib.util.spec_from_file_location(
    "l1_paired_pipeline", ROOT / "reports" / "l1-paired-avg16" / "pipeline.py")
pipeline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pipeline)

QUESTIONS = ["question-{:03d}".format(i) for i in range(2, 18) if i not in (5,)]


def combined(violations_per_question, refusals_per_question=0):
    """15 questions x 16 samples of combined labels with the given counts per question."""
    rows = {}
    for q_index, qid in enumerate(QUESTIONS):
        k = violations_per_question[q_index] if isinstance(
            violations_per_question, list) else violations_per_question
        for i in range(1, 17):
            refused = i <= refusals_per_question
            rows[(qid, i)] = {
                "refusal": "refusal" if refused else "engaged",
                "boundary": "REFUSAL" if refused else ("VIOLATION" if i > 16 - k else "CLEAN"),
                "success": "NOT_ACHIEVED" if refused else "ACHIEVED",
            }
    return rows


def test_fifteen_questions_match_the_dataset():
    assert len(QUESTIONS) == 15


def test_sign_flip_p_is_exact():
    assert pipeline.sign_flip_p([0.1, 0.1, 0.1, 0.1]) == pytest.approx(2 / 16)
    assert pipeline.sign_flip_p([0.0, 0.0, 0.0]) == 1.0


def test_identical_arms_give_zero_difference_and_p_one():
    rows = combined(2)
    result = pipeline.contrast(rows, rows, "seed")
    primary = result["primary_violation"]
    assert primary["difference"] == 0
    assert primary["ci95"] == [0, 0]
    assert primary["sign_flip_p"] == 1.0


def test_uniform_shift_is_detected_with_the_minimum_p():
    result = pipeline.contrast(combined(4), combined(2), "seed")
    primary = result["primary_violation"]
    assert primary["difference"] == pytest.approx(2 / 16)
    assert primary["ci95"] == [pytest.approx(2 / 16), pytest.approx(2 / 16)]
    assert primary["sign_flip_p"] == pytest.approx(2 / 2 ** 15)
    excluded = result["violation_excluding_question_003"]
    assert excluded["questions"] == 14


def test_refusal_and_engaged_ratio_are_computed_separately():
    result = pipeline.contrast(combined(2, refusals_per_question=4), combined(2), "seed")
    assert result["refusal"]["difference"] == pytest.approx(4 / 16)
    engaged = result["violation_among_engaged"]
    assert engaged["first"] == pytest.approx(2 / 12)
    assert engaged["second"] == pytest.approx(2 / 16)


def test_unresolved_bounds_bracket_the_primary_difference():
    first = combined(2)
    second = combined(2)
    first[("question-002", 1)]["boundary"] = "UNRESOLVED"
    bounds = pipeline.contrast(first, second, "seed")["violation_unresolved_bounds"]
    assert bounds["first_lower_second_upper"] <= 0 <= bounds["first_upper_second_lower"]


def test_holm_adjustment():
    adjusted = pipeline.holm({"a": 0.01, "b": 0.04, "c": 0.03})
    assert adjusted == {"a": pytest.approx(0.03), "c": pytest.approx(0.06),
                        "b": pytest.approx(0.06)}


def test_every_arm_config_exists_and_paths_are_fresh():
    for arm in pipeline.ARMS:
        for condition in pipeline.CONDITIONS:
            assert (ROOT / pipeline.config_path(arm, condition)).exists()
        assert pipeline.run_dir(arm, "induced").name.endswith("-induced-r2")
