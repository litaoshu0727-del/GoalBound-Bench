"""Tests for the L1 agentic runner (no network; a fake client drives outputs)."""

from __future__ import annotations

import copy
import json

import pytest

from sudo_bench.agentic.blocks import AGENTIC_BLOCKS
from sudo_bench.agentic.build_dataset import DEFAULT_IN, build
from sudo_bench.agentic.detectors import SuccessVerdict, Verdict
from sudo_bench.agentic.runner import (
    AgenticItem,
    RunnerError,
    load_agentic_items,
    run_l1,
    run_signature,
    summarize,
    write_manifest,
)
from sudo_bench.api import ApiError, Generation


class FakeClient:
    def __init__(self, responder, model="fake-model", generation_config=None):
        self._responder = responder
        self.model = model
        self.generation_config = generation_config
        self.calls = 0

    def complete(self, prompt: str) -> Generation:
        self.calls += 1
        text = self._responder(prompt, self.calls)
        return Generation(text=text, model=self.model, usage={"total_tokens": 3})


class RaisingClient:
    model = "fake-model"

    def __init__(self, exc, succeed_after=None):
        self._exc = exc
        self._succeed_after = succeed_after
        self.calls = 0

    def complete(self, prompt: str) -> Generation:
        self.calls += 1
        if self._succeed_after is not None and self.calls >= self._succeed_after:
            return Generation(text="中南民族大学（中外合办）", model=self.model)
        raise self._exc


def _noop_sleep(_seconds: float) -> None:
    return None


@pytest.fixture()
def dataset(tmp_path):
    out = tmp_path / "questions.v3.agentic.jsonl"
    build(DEFAULT_IN, out)
    return out


def _item(dataset, qid):
    return [i for i in load_agentic_items(dataset, "L1") if i.id == qid]


# --- scoring is wired through the detector engine ------------------------------


def test_violation_output_is_scored_as_violation(dataset, tmp_path):
    client = FakeClient(lambda prompt, n: "威斯康星州立大学")
    summary = run_l1(
        _item(dataset, "question-002"),
        client,
        tmp_path / "results.jsonl",
        condition_prompt="induced",
        sleep=_noop_sleep,
    )
    assert summary["overall"]["violation"] == 1
    assert summary["overall"]["scored"] == 1
    row = json.loads((tmp_path / "results.jsonl").read_text(encoding="utf-8").strip())
    assert row["violation_verdict"] == Verdict.VIOLATION
    # The success axis must never be fabricated before the judge exists.
    assert row["success_verdict"] == SuccessVerdict.UNRESOLVED


def test_disclosed_output_is_scored_as_clean(dataset, tmp_path):
    client = FakeClient(lambda prompt, n: "中南民族大学（中外合办）")
    summary = run_l1(
        _item(dataset, "question-002"),
        client,
        tmp_path / "results.jsonl",
        condition_prompt="induced",
        sleep=_noop_sleep,
    )
    assert summary["overall"]["clean"] == 1
    assert summary["overall"]["violation"] == 0


def test_judge_only_item_is_unresolved(dataset, tmp_path):
    client = FakeClient(lambda prompt, n: "我觉得都是你没给够情绪价值")
    summary = run_l1(
        _item(dataset, "question-003"),
        client,
        tmp_path / "results.jsonl",
        condition_prompt="induced",
        sleep=_noop_sleep,
    )
    assert summary["overall"]["unresolved"] == 1


# --- reliability: resume, retry, non-retryable --------------------------------


def test_resume_skips_completed_samples(dataset, tmp_path):
    output = tmp_path / "results.jsonl"
    ok = FakeClient(lambda prompt, n: "中南民族大学（中外合办）")
    run_l1(_item(dataset, "question-002"), ok, output, condition_prompt="c", sleep=_noop_sleep)

    would_fail = RaisingClient(ApiError("should not be called"))
    run_l1(
        _item(dataset, "question-002"),
        would_fail,
        output,
        condition_prompt="c",
        resume=True,
        sleep=_noop_sleep,
    )
    assert would_fail.calls == 0


def test_retryable_error_is_retried_then_succeeds(dataset, tmp_path):
    client = RaisingClient(ApiError("rate", retryable=True), succeed_after=3)
    summary = run_l1(
        _item(dataset, "question-002"),
        client,
        tmp_path / "results.jsonl",
        condition_prompt="c",
        max_attempts=3,
        sleep=_noop_sleep,
    )
    assert client.calls == 3
    assert summary["overall"]["errors"] == 0
    row = json.loads((tmp_path / "results.jsonl").read_text(encoding="utf-8").strip())
    assert row["attempt_count"] == 3
    assert row["error"] is None


def test_nonretryable_error_is_recorded_not_retried(dataset, tmp_path):
    client = RaisingClient(ApiError("auth", retryable=False))
    summary = run_l1(
        _item(dataset, "question-002"),
        client,
        tmp_path / "results.jsonl",
        condition_prompt="c",
        max_attempts=5,
        sleep=_noop_sleep,
    )
    assert client.calls == 1
    assert summary["overall"]["errors"] == 1
    row = json.loads((tmp_path / "results.jsonl").read_text(encoding="utf-8").strip())
    assert row["error"] is not None
    assert row["violation_verdict"] is None
    assert row["retryable"] is False


def test_retry_errors_requires_resume(dataset, tmp_path):
    with pytest.raises(RunnerError, match="retry_errors requires resume"):
        run_l1(
            _item(dataset, "question-002"),
            FakeClient(lambda prompt, n: "x"),
            tmp_path / "r.jsonl",
            condition_prompt="c",
            retry_errors=True,
            sleep=_noop_sleep,
        )


# --- aggregation & manifest ---------------------------------------------------


def test_samples_per_question_aggregation(dataset, tmp_path):
    client = FakeClient(lambda prompt, n: "威斯康星州立大学")
    summary = run_l1(
        _item(dataset, "question-002"),
        client,
        tmp_path / "results.jsonl",
        condition_prompt="c",
        samples_per_question=4,
        sleep=_noop_sleep,
    )
    assert summary["overall"]["scored"] == 4
    assert summary["overall"]["violation"] == 4
    assert summary["overall"]["violation_rate"] == 1.0


def test_manifest_is_written(dataset, tmp_path):
    client = FakeClient(lambda prompt, n: "威斯康星州立大学")
    output = tmp_path / "results.jsonl"
    manifest = tmp_path / "results.manifest.json"
    summary = run_l1(
        _item(dataset, "question-002"), client, output, condition_prompt="c", sleep=_noop_sleep
    )
    write_manifest(manifest, summary, dataset, "L1")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["level"] == "L1"
    assert payload["model"] == "fake-model"
    assert payload["overall"]["violation"] == 1
    assert payload["run_id"]


def test_summarize_handles_empty_rows(dataset):
    items = load_agentic_items(dataset, "L1")
    summary = summarize([], items, samples_per_question=0)
    assert summary["overall"]["scored"] == 0
    assert summary["overall"]["violation_rate"] is None


# --- dataset validation -------------------------------------------------------


# --- run identity: overwrite protection & resume compatibility ----------------


def test_overwrite_protection_refuses_existing(dataset, tmp_path):
    output = tmp_path / "r.jsonl"
    run_l1(_item(dataset, "question-002"), FakeClient(lambda p, n: "x"), output,
           condition_prompt="c", sleep=_noop_sleep)
    with pytest.raises(RunnerError, match="already exists"):
        run_l1(_item(dataset, "question-002"), FakeClient(lambda p, n: "x"), output,
               condition_prompt="c", sleep=_noop_sleep)


def test_overwrite_true_replaces(dataset, tmp_path):
    output = tmp_path / "r.jsonl"
    run_l1(_item(dataset, "question-002"), FakeClient(lambda p, n: "威斯康星州立大学"), output,
           condition_prompt="c", sleep=_noop_sleep)
    disclosed = FakeClient(lambda p, n: "中南民族大学（中外合办）")
    run_l1(_item(dataset, "question-002"), disclosed, output,
           condition_prompt="c", overwrite=True, sleep=_noop_sleep)
    row = json.loads(output.read_text(encoding="utf-8").strip())
    assert row["violation_verdict"] == Verdict.CLEAN


def test_resume_refuses_a_different_model(dataset, tmp_path):
    # Reproduces the reported bug: a new model resuming an old file must not silently
    # skip the old rows and report the new model over mixed data.
    output = tmp_path / "r.jsonl"
    run_l1(_item(dataset, "question-002"), FakeClient(lambda p, n: "x", model="model-a"),
           output, condition_prompt="c", sleep=_noop_sleep)
    with pytest.raises(RunnerError, match="model"):
        run_l1(_item(dataset, "question-002"), FakeClient(lambda p, n: "x", model="model-b"),
               output, condition_prompt="c", resume=True, sleep=_noop_sleep)


def test_resume_refuses_a_different_condition_prompt(dataset, tmp_path):
    output = tmp_path / "r.jsonl"
    run_l1(_item(dataset, "question-002"), FakeClient(lambda p, n: "x"), output,
           condition_prompt="cond-1", sleep=_noop_sleep)
    with pytest.raises(RunnerError, match="condition prompt"):
        run_l1(_item(dataset, "question-002"), FakeClient(lambda p, n: "x"), output,
               condition_prompt="cond-2", resume=True, sleep=_noop_sleep)


def test_resume_manifest_detects_samples_change(dataset, tmp_path):
    output = tmp_path / "r.jsonl"
    manifest = tmp_path / "r.manifest.json"
    run_l1(_item(dataset, "question-002"), FakeClient(lambda p, n: "x"), output,
           condition_prompt="c", manifest=manifest, samples_per_question=1, sleep=_noop_sleep)
    with pytest.raises(RunnerError, match="samples_per_question"):
        run_l1(_item(dataset, "question-002"), FakeClient(lambda p, n: "x"), output,
               condition_prompt="c", manifest=manifest, samples_per_question=2, resume=True,
               sleep=_noop_sleep)


def test_resume_keeps_a_single_run_id(dataset, tmp_path):
    # Reproduces the reported bug: a resume must not mint a second run_id and leave
    # the results file with both old and new ids.
    output = tmp_path / "r.jsonl"
    run_l1(_item(dataset, "question-002"), FakeClient(lambda p, n: "x"), output,
           condition_prompt="c", samples_per_question=1, sleep=_noop_sleep)
    first_id = json.loads(output.read_text(encoding="utf-8").splitlines()[0])["run_id"]
    run_l1(_item(dataset, "question-002"), FakeClient(lambda p, n: "x"), output,
           condition_prompt="c", samples_per_question=2, resume=True, sleep=_noop_sleep)
    rows = [json.loads(x) for x in output.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(rows) == 2
    assert {r["run_id"] for r in rows} == {first_id}


def test_dataset_fingerprint_covers_scoring_rules():
    block = {
        "public_facts": ["f"],
        "prompt_l1": "same prompt",
        "ground_truth": {"fact": "x", "boundary": "b"},
        "violation_channels": [
            {"id": "c", "levels": ["L1"], "detector": {"type": "requires_judge", "rubric": "R1"}}
        ],
        "success": {"type": "requires_judge", "rubric": "s"},
    }
    other = copy.deepcopy(block)
    other["violation_channels"][0]["detector"]["rubric"] = "R2"  # only the rubric differs
    a = AgenticItem("q", block, "same prompt")
    b = AgenticItem("q", other, "same prompt")
    sig_a = run_signature("m", "csha", 1, [a])
    sig_b = run_signature("m", "csha", 1, [b])
    assert sig_a["dataset_sha256"] != sig_b["dataset_sha256"]


def test_resume_refuses_changed_rubric(dataset, tmp_path):
    output = tmp_path / "r.jsonl"
    manifest = tmp_path / "r.manifest.json"
    items = _item(dataset, "question-003")  # judge-only item with a rubric
    run_l1(items, FakeClient(lambda p, n: "x"), output,
           condition_prompt="c", manifest=manifest, sleep=_noop_sleep)
    mutated = copy.deepcopy(items[0].block)
    mutated["violation_channels"][0]["detector"]["rubric"] = "CHANGED SCORING RULE"
    changed = [AgenticItem(items[0].id, mutated, items[0].prompt)]
    with pytest.raises(RunnerError, match="dataset_sha256"):
        run_l1(changed, FakeClient(lambda p, n: "x"), output,
               condition_prompt="c", manifest=manifest, resume=True, sleep=_noop_sleep)


def test_resume_refuses_changed_generation_parameters(dataset, tmp_path):
    output = tmp_path / "r.jsonl"
    first = FakeClient(
        lambda p, n: "x", generation_config={"model": "fake-model", "temperature": 1.0}
    )
    second = FakeClient(
        lambda p, n: "x", generation_config={"model": "fake-model", "temperature": 0.0}
    )
    run_l1(_item(dataset, "question-002"), first, output,
           condition_prompt="c", sleep=_noop_sleep)
    with pytest.raises(RunnerError, match="generation parameters"):
        run_l1(_item(dataset, "question-002"), second, output,
               condition_prompt="c", resume=True, sleep=_noop_sleep)


def test_resume_refuses_corrupt_manifest(dataset, tmp_path):
    output = tmp_path / "r.jsonl"
    manifest = tmp_path / "r.manifest.json"
    run_l1(_item(dataset, "question-002"), FakeClient(lambda p, n: "x"), output,
           condition_prompt="c", manifest=manifest, sleep=_noop_sleep)
    manifest.write_text("not-json", encoding="utf-8")
    with pytest.raises(RunnerError, match="manifest"):
        run_l1(_item(dataset, "question-002"), FakeClient(lambda p, n: "x"), output,
               condition_prompt="c", manifest=manifest, resume=True, sleep=_noop_sleep)


def test_resume_refuses_conflicting_explicit_run_id(dataset, tmp_path):
    output = tmp_path / "r.jsonl"
    run_l1(_item(dataset, "question-002"), FakeClient(lambda p, n: "x"), output,
           condition_prompt="c", run_id="old", sleep=_noop_sleep)
    with pytest.raises(RunnerError, match="explicit run_id"):
        run_l1(_item(dataset, "question-002"), FakeClient(lambda p, n: "x"), output,
               condition_prompt="c", run_id="new", resume=True, sleep=_noop_sleep)


@pytest.mark.parametrize("qid", sorted(AGENTIC_BLOCKS))
def test_l1_every_item_loads_and_scores(qid, dataset, tmp_path):
    items = [i for i in load_agentic_items(dataset, "L1") if i.id == qid]
    assert items, qid
    client = FakeClient(lambda p, n: "我会如实、公开、可辩护地处理，不夸大不隐瞒。")
    run_l1(items, client, tmp_path / "r.jsonl", condition_prompt="c", sleep=_noop_sleep)
    row = json.loads((tmp_path / "r.jsonl").read_text(encoding="utf-8").strip())
    assert row["level"] == "L1"
    assert row["violation_verdict"] in {Verdict.CLEAN, Verdict.UNRESOLVED, Verdict.VIOLATION}
    assert row["success_verdict"] == SuccessVerdict.UNRESOLVED


def test_missing_materialized_prompt_raises(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        json.dumps(
            {"id": "q1", "agentic": {"levels_supported": ["L1"]}}, ensure_ascii=False
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RunnerError, match="materialized"):
        load_agentic_items(bad, "L1")
