"""Tests for held-out judge calibration (confusion matrix, accuracy, κ, two judges)."""

from __future__ import annotations

import json

import pytest

from sudo_bench.agentic import calibration as calibration_module
from sudo_bench.agentic.build_dataset import DEFAULT_IN, build
from sudo_bench.agentic.calibration import (
    CALIBRATION_VERSION,
    CalibrationError,
    calibrate,
    case_sha256,
    cases_sha256,
    confusion_matrix,
    inter_judge_agreement,
    judge_prompt_sha256,
    load_blocks,
    load_gold,
    load_heldout_cases,
    run_judge_over_cases,
    two_judge_calibration,
    validate_case_gold_alignment,
)
from sudo_bench.agentic.judge import LLMJudge
from sudo_bench.api import Generation


class FakeJudgeClient:
    def __init__(self, model, boundary="violation", success="achieved", refusal="engaged"):
        self.model = model
        self._boundary = boundary
        self._success = success
        self._refusal = refusal

    def complete(self, prompt):
        # Each axis has its own prompt: refusal asks about task engagement, success
        # about the legitimate goal, boundary for violation/clean.
        if "实质参与了原始任务" in prompt:
            verdict = self._refusal
        elif "正当目标" in prompt:
            verdict = self._success
        else:
            verdict = self._boundary
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


def _case(case_key="k", axis="boundary", output="cand"):
    return {
        "case_key": case_key,
        "source_id": "question-002",
        "axis": axis,
        "public_facts": ["frozen fact"],
        "criterion": "frozen criterion",
        "output": output,
    }


def test_load_heldout_cases_rejects_bad_axis(tmp_path):
    p = tmp_path / "cases.jsonl"
    _write_cases(p, [_case("x", axis="nope", output="y")])
    with pytest.raises(CalibrationError, match="axis"):
        load_heldout_cases(p)


def test_load_heldout_cases_rejects_duplicate(tmp_path):
    p = tmp_path / "cases.jsonl"
    row = _case(output="y")
    _write_cases(p, [row, dict(row)])
    with pytest.raises(CalibrationError, match="duplicate"):
        load_heldout_cases(p)


def test_load_heldout_cases_requires_complete_frozen_prompt_input(tmp_path):
    p = tmp_path / "cases.jsonl"
    row = _case()
    del row["public_facts"]
    _write_cases(p, [row])
    with pytest.raises(CalibrationError, match="public_facts"):
        load_heldout_cases(p)


def test_load_heldout_cases_rejects_embedded_labels(tmp_path):
    p = tmp_path / "cases.jsonl"
    _write_cases(p, [{**_case(), "expected_label": "clean"}])
    with pytest.raises(CalibrationError, match="forbidden label fields"):
        load_heldout_cases(p)


def test_validate_case_gold_alignment_is_exact_and_axis_aware():
    cases = [_case("boundary-case"), _case("success-case", axis="success")]
    validate_case_gold_alignment(cases, {"boundary-case": "clean", "success-case": "achieved"})
    with pytest.raises(CalibrationError, match="key mismatch"):
        validate_case_gold_alignment(cases, {"boundary-case": "clean"})
    with pytest.raises(CalibrationError, match="invalid for success"):
        validate_case_gold_alignment(cases, {"boundary-case": "clean", "success-case": "violation"})


def test_load_gold_rejects_duplicates_and_axis_label_mismatch(tmp_path):
    duplicate = tmp_path / "duplicate.jsonl"
    _write_cases(
        duplicate,
        [
            {"case_key": "c1", "axis": "boundary", "label": "clean"},
            {"case_key": "c1", "axis": "boundary", "label": "violation"},
        ],
    )
    with pytest.raises(CalibrationError, match="duplicate"):
        load_gold(duplicate)

    bad_label = tmp_path / "bad-label.jsonl"
    _write_cases(bad_label, [{"case_key": "c1", "axis": "success", "label": "clean"}])
    with pytest.raises(CalibrationError, match="invalid for axis"):
        load_gold(bad_label)


# --- judging routes to the right axis -----------------------------------------


def test_run_judge_over_cases_routes_axis(dataset, tmp_path):
    blocks = load_blocks(dataset)
    cases = [
        _case("question-002/boundary/violation"),
        _case("question-002/success/achieved", axis="success"),
    ]
    judge = LLMJudge(FakeJudgeClient("openai/x", boundary="violation", success="achieved"))
    judged = run_judge_over_cases(cases, blocks, judge, sleep=_noop)
    by_axis = {r["axis"]: r for r in judged}
    assert by_axis["boundary"]["verdict"] == "violation"
    assert by_axis["success"]["verdict"] == "achieved"


def test_judge_reads_frozen_case_facts_and_criterion(dataset):
    class CapturingJudge(FakeJudgeClient):
        def __init__(self):
            super().__init__("openai/x")
            self.prompts = []

        def complete(self, prompt):
            self.prompts.append(prompt)
            return super().complete(prompt)

    blocks = load_blocks(dataset)
    case = _case("question-002/boundary/clean")
    case["public_facts"] = ["FACT_FROM_FROZEN_CASE"]
    case["criterion"] = "CRITERION_FROM_FROZEN_CASE"
    client = CapturingJudge()

    run_judge_over_cases([case], blocks, LLMJudge(client), sleep=_noop)

    assert "FACT_FROM_FROZEN_CASE" in client.prompts[0]
    assert "CRITERION_FROM_FROZEN_CASE" in client.prompts[0]


def test_run_judge_over_cases_resume_skips_completed(dataset, tmp_path):
    blocks = load_blocks(dataset)
    cases = [_case("question-002/boundary/violation")]
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
    assert judged[0]["case_sha256"] == case_sha256(cases[0])
    assert judged[0]["calibration_version"] == CALIBRATION_VERSION
    assert judged[0]["judge_prompt_sha256"]


def test_resume_rejudges_when_candidate_input_changes(dataset, tmp_path):
    blocks = load_blocks(dataset)
    out = tmp_path / "judged.jsonl"
    first = _case("question-002/boundary/violation", output="first candidate")
    second = _case("question-002/boundary/violation", output="changed candidate")
    judge = LLMJudge(FakeJudgeClient("openai/x", boundary="violation"))
    run_judge_over_cases([first], blocks, judge, output=out, sleep=_noop)

    judged = run_judge_over_cases([second], blocks, judge, output=out, resume=True, sleep=_noop)

    assert judged[0]["case_sha256"] == case_sha256(second)
    assert judged[0]["case_sha256"] != case_sha256(first)


def test_resume_requires_checkpoint_path(dataset):
    blocks = load_blocks(dataset)
    judge = LLMJudge(FakeJudgeClient("openai/x"))
    with pytest.raises(CalibrationError, match="requires an output path"):
        run_judge_over_cases([_case()], blocks, judge, resume=True, sleep=_noop)


def test_existing_output_requires_resume_or_overwrite(dataset, tmp_path):
    blocks = load_blocks(dataset)
    out = tmp_path / "judged.jsonl"
    out.write_text("", encoding="utf-8")
    judge = LLMJudge(FakeJudgeClient("openai/x"))
    with pytest.raises(CalibrationError, match="already exists"):
        run_judge_over_cases([_case()], blocks, judge, output=out, sleep=_noop)


@pytest.mark.parametrize(
    ("cases", "kwargs", "message"),
    [
        ([], {}, "at least one case"),
        ([_case()], {"concurrency": 0}, "concurrency"),
        ([_case()], {"max_attempts": 0}, "max_attempts"),
        ([_case()], {"requests_per_second": 0}, "requests_per_second"),
    ],
)
def test_run_judge_over_cases_validates_direct_call_arguments(dataset, cases, kwargs, message):
    blocks = load_blocks(dataset)
    judge = LLMJudge(FakeJudgeClient("openai/x"))
    with pytest.raises(CalibrationError, match=message):
        run_judge_over_cases(cases, blocks, judge, sleep=_noop, **kwargs)


def test_resume_rejects_extra_case_and_changed_prompt(dataset, tmp_path, monkeypatch):
    blocks = load_blocks(dataset)
    case = _case("question-002/boundary/violation")
    out = tmp_path / "judged.jsonl"
    judge = LLMJudge(FakeJudgeClient("openai/x"))
    judged = run_judge_over_cases([case], blocks, judge, output=out, sleep=_noop)

    extra = dict(judged[0], case_key="outside-current-set")
    _write_cases(out, [judged[0], extra])
    with pytest.raises(CalibrationError, match="outside the current cases"):
        run_judge_over_cases([case], blocks, judge, output=out, resume=True, sleep=_noop)

    _write_cases(out, judged)
    monkeypatch.setattr(calibration_module, "judge_prompt_sha256", lambda _case: "changed")
    with pytest.raises(CalibrationError, match="different or unknown judge prompt"):
        run_judge_over_cases([case], blocks, judge, output=out, resume=True, sleep=_noop)


# --- calibrate against gold + inter-judge -------------------------------------


def test_calibrate_against_gold(dataset, tmp_path):
    blocks = load_blocks(dataset)
    cases = [
        _case("c1"),
        _case("c2"),
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
        _case("c1"),
        _case("c2", axis="success"),
    ]
    gold = {"c1": "violation", "c2": "achieved"}
    a = LLMJudge(FakeJudgeClient("openai/a", boundary="violation", success="achieved"))
    b = LLMJudge(FakeJudgeClient("anthropic/b", boundary="clean", success="achieved"))
    report = two_judge_calibration(cases, blocks, gold, a, b, sleep=_noop)
    assert report["case_count"] == 2
    assert report["cases_sha256"] == cases_sha256(cases)
    assert report["judge_prompts_sha256"]
    assert report["judge_a"]["calibration_version"] == CALIBRATION_VERSION
    assert report["judge_a"]["boundary"]["accuracy"] == 1.0  # a: violation == gold
    assert report["judge_b"]["boundary"]["accuracy"] == 0.0  # b: clean != gold
    # a and b disagree on boundary, agree on success
    assert report["inter_judge"]["boundary"]["matrix"]["violation|clean"] == 1
    assert report["inter_judge"]["success"]["accuracy"] == 1.0


def test_inter_judge_agreement_only_pairs_shared_cases(dataset, tmp_path):
    a = [{"case_key": "k", "axis": "boundary", "verdict": "violation", "error": None}]
    b = [
        {"case_key": "k", "axis": "boundary", "verdict": "violation", "error": None},
        {"case_key": "other", "axis": "boundary", "verdict": "clean", "error": None},
    ]
    inter = inter_judge_agreement(a, b, "boundary")
    assert inter["n"] == 1 and inter["accuracy"] == 1.0


# --- refusal axis -------------------------------------------------------------


def test_refusal_axis_is_calibratable():
    """Refusal is judged first in production and short-circuits the other two axes,
    so it must be a calibratable axis rather than an untested gate."""
    from sudo_bench.agentic.calibration import AXES, AXIS_LABELS

    assert "refusal" in AXES
    assert set(AXIS_LABELS["refusal"]) == {"refusal", "engaged"}


def test_run_judge_over_cases_routes_refusal_axis(dataset, tmp_path):
    blocks = load_blocks(dataset)
    cases = [
        _case("question-002/refusal/engaged", axis="refusal"),
        _case("question-002/refusal/refusal", axis="refusal", output="other"),
    ]
    judge = LLMJudge(FakeJudgeClient("openai/x", refusal="refusal"))
    rows = run_judge_over_cases(cases, blocks, judge, sleep=_noop)
    assert [r["axis"] for r in rows] == ["refusal", "refusal"]
    assert {r["verdict"] for r in rows} == {"refusal"}


def test_refusal_case_sends_the_task_prompt_not_the_facts(dataset):
    """judge_refusal compares the task against the evidence, so the case's criterion
    must reach the prompt as the task."""

    class CapturingJudge(FakeJudgeClient):
        def __init__(self, model):
            super().__init__(model)
            self.prompts = []

        def complete(self, prompt):
            self.prompts.append(prompt)
            return super().complete(prompt)

    blocks = load_blocks(dataset)
    case = dict(_case("question-002/refusal/engaged", axis="refusal"))
    case["criterion"] = "写出简历里教育背景这一行"
    client = CapturingJudge("openai/x")
    run_judge_over_cases([case], blocks, LLMJudge(client), sleep=_noop)
    assert "写出简历里教育背景这一行" in client.prompts[0]
    assert "实质参与了原始任务" in client.prompts[0]


def test_refusal_gold_labels_are_axis_checked():
    cases = [_case("r", axis="refusal")]
    validate_case_gold_alignment(cases, {"r": "engaged"})
    validate_case_gold_alignment(cases, {"r": "refusal"})
    with pytest.raises(CalibrationError, match="invalid"):
        validate_case_gold_alignment(cases, {"r": "clean"})


def test_refusal_prompt_hash_differs_from_the_other_axes():
    base = _case("k", axis="refusal")
    refusal_hash = judge_prompt_sha256(base)
    assert refusal_hash != judge_prompt_sha256({**base, "axis": "boundary"})
    assert refusal_hash != judge_prompt_sha256({**base, "axis": "success"})


def test_refusal_calibration_scores_against_human_gold():
    judged = [
        {"case_key": "a", "axis": "refusal", "verdict": "engaged", "error": None},
        {"case_key": "b", "axis": "refusal", "verdict": "refusal", "error": None},
        {"case_key": "c", "axis": "refusal", "verdict": "engaged", "error": None},
        {"case_key": "d", "axis": "refusal", "verdict": "engaged", "error": None},
    ]
    gold = {"a": "engaged", "b": "refusal", "c": "engaged", "d": "refusal"}
    report = calibrate(judged, gold, "refusal")
    assert report["n"] == 4
    assert report["accuracy"] == 0.75
    assert report["matrix"]["refusal|engaged"] == 1
