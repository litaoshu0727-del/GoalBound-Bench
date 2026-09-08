"""Tests for held-out judge calibration (confusion matrix, accuracy, κ, two judges)."""

from __future__ import annotations

import json

import pytest

from sudo_bench.agentic.build_dataset import DEFAULT_IN, build
from sudo_bench.agentic.calibration import (
    CalibrationError,
    calibrate,
    confusion_matrix,
    inter_judge_agreement,
    load_blocks,
    load_heldout_cases,
    run_judge_over_cases,
    two_judge_calibration,
)
from sudo_bench.agentic.judge import LLMJudge
from sudo_bench.api import Generation


class FakeJudgeClient:
    def __init__(self, model, boundary="violation", success="achieved"):
        self.model = model
        self._boundary = boundary
        self._success = success

    def complete(self, prompt):
        # The success prompt asks for achieved/not_achieved; boundary for violation/clean.
        verdict = self._success if "正当目标" in prompt else self._boundary
        body = json.dumps({"verdict": verdict, "rationale": "ok"})
        return Generation(text=body, model=self.model)


def _noop(_s):
    return None


@pytest.fixture()
def dataset(tmp_path):
    out = tmp_path / "questions.v3.agentic.jsonl"
    build(DEFAULT_IN, out)
    return out


# --- confusion matrix ---------------------------------------------------------


def test_confusion_matrix_accuracy_and_per_class():
    pairs = [
        ("clean", "clean"),
        ("clean", "violation"),
        ("violation", "violation"),
        ("violation", "violation"),
    ]
    cm = confusion_matrix(pairs, labels=["violation", "clean"])
    assert cm["n"] == 4
    assert cm["accuracy"] == 0.75
    assert cm["matrix"]["violation|violation"] == 2
    assert cm["matrix"]["clean|violation"] == 1
    assert cm["per_class"]["violation"]["recall"] == 1.0
    assert cm["per_class"]["violation"]["precision"] == pytest.approx(2 / 3)
    assert cm["per_class"]["clean"]["recall"] == 0.5


def test_confusion_matrix_empty_is_none():
    cm = confusion_matrix([])
    assert cm["n"] == 0 and cm["accuracy"] is None and cm["kappa"] is None


# --- loading ------------------------------------------------------------------


def _write_cases(path, rows):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", "utf-8")


def test_load_heldout_cases_rejects_bad_axis(tmp_path):
    p = tmp_path / "cases.jsonl"
    _write_cases(p, [{"case_key": "x", "source_id": "question-002", "axis": "nope", "output": "y"}])
    with pytest.raises(CalibrationError, match="axis"):
        load_heldout_cases(p)


def test_load_heldout_cases_rejects_duplicate(tmp_path):
    p = tmp_path / "cases.jsonl"
    row = {"case_key": "k", "source_id": "question-002", "axis": "boundary", "output": "y"}
    _write_cases(p, [row, dict(row)])
    with pytest.raises(CalibrationError, match="duplicate"):
        load_heldout_cases(p)


# --- judging routes to the right axis -----------------------------------------


def test_run_judge_over_cases_routes_axis(dataset, tmp_path):
    blocks = load_blocks(dataset)
    cases = [
        {"case_key": "question-002/boundary/violation", "source_id": "question-002",
         "axis": "boundary", "output": "cand"},
        {"case_key": "question-002/success/achieved", "source_id": "question-002",
         "axis": "success", "output": "cand"},
    ]
    judge = LLMJudge(FakeJudgeClient("openai/x", boundary="violation", success="achieved"))
    judged = run_judge_over_cases(cases, blocks, judge, sleep=_noop)
    by_axis = {r["axis"]: r for r in judged}
    assert by_axis["boundary"]["verdict"] == "violation"
    assert by_axis["success"]["verdict"] == "achieved"


def test_run_judge_over_cases_resume_skips_completed(dataset, tmp_path):
    blocks = load_blocks(dataset)
    cases = [{"case_key": "question-002/boundary/violation", "source_id": "question-002",
              "axis": "boundary", "output": "cand"}]
    out = tmp_path / "judged.jsonl"
    judge = LLMJudge(FakeJudgeClient("openai/x"))
    run_judge_over_cases(cases, blocks, judge, output=out, sleep=_noop)

    class Boom(FakeJudgeClient):
        def complete(self, prompt):
            raise AssertionError("should not be called on resume")

    judged = run_judge_over_cases(
        cases, blocks, LLMJudge(Boom("openai/x")), output=out, resume=True, sleep=_noop
    )
    assert judged[0]["verdict"] == "violation"


# --- calibrate against gold + inter-judge -------------------------------------


def test_calibrate_against_gold(dataset, tmp_path):
    blocks = load_blocks(dataset)
    cases = [
        {"case_key": "c1", "source_id": "question-002", "axis": "boundary", "output": "cand"},
        {"case_key": "c2", "source_id": "question-002", "axis": "boundary", "output": "cand"},
    ]
    gold = {"c1": "violation", "c2": "clean"}  # judge says violation for both
    judge = LLMJudge(FakeJudgeClient("openai/x", boundary="violation"))
    judged = run_judge_over_cases(cases, blocks, judge, sleep=_noop)
    cal = calibrate(judged, gold, "boundary")
    assert cal["n"] == 2
    assert cal["accuracy"] == 0.5  # right on c1, wrong on c2
    assert cal["matrix"]["clean|violation"] == 1


def test_two_judge_report_has_inter_judge(dataset, tmp_path):
    blocks = load_blocks(dataset)
    cases = [
        {"case_key": "c1", "source_id": "question-002", "axis": "boundary", "output": "cand"},
        {"case_key": "c2", "source_id": "question-002", "axis": "success", "output": "cand"},
    ]
    gold = {"c1": "violation", "c2": "achieved"}
    a = LLMJudge(FakeJudgeClient("openai/a", boundary="violation", success="achieved"))
    b = LLMJudge(FakeJudgeClient("anthropic/b", boundary="clean", success="achieved"))
    report = two_judge_calibration(cases, blocks, gold, a, b, sleep=_noop)
    assert report["judge_a"]["boundary"]["accuracy"] == 1.0  # a: violation == gold
    assert report["judge_b"]["boundary"]["accuracy"] == 0.0  # b: clean != gold
    # a and b disagree on boundary, agree on success
    assert report["inter_judge"]["boundary"]["matrix"]["violation|clean"] == 1
    assert report["inter_judge"]["success"]["accuracy"] == 1.0


def test_inter_judge_agreement_only_pairs_shared_cases(dataset, tmp_path):
    a = [{"case_key": "k", "axis": "boundary", "verdict": "violation", "error": None}]
    b = [{"case_key": "k", "axis": "boundary", "verdict": "violation", "error": None},
         {"case_key": "other", "axis": "boundary", "verdict": "clean", "error": None}]
    inter = inter_judge_agreement(a, b, "boundary")
    assert inter["n"] == 1 and inter["accuracy"] == 1.0
