"""End-to-end Runner -> Judge integration (no network; fakes for model and judge)."""

from __future__ import annotations

import json

import pytest

from sudo_bench.agentic.build_dataset import DEFAULT_IN, build
from sudo_bench.agentic.detectors import SuccessVerdict, Verdict
from sudo_bench.agentic.judge import LLMJudge, run_judge
from sudo_bench.agentic.runner import load_agentic_items, run_l1, run_l2
from sudo_bench.api import Generation


def _noop(_seconds: float) -> None:
    return None


class FakeL1Client:
    model = "evalvendor/model"

    def complete(self, prompt: str) -> Generation:
        text = "威斯康星州立大学" if "本科" in prompt else "我会如实、可辩护地处理。"
        return Generation(text=text, model=self.model)


class FakeL2Client:
    model = "evalvendor/model"

    def complete_with_tools(self, prompt, tools) -> Generation:
        name = tools[0]["function"]["name"]
        if name == "set_resume_field" and "本科" in prompt:
            args = {"school": "威斯康星州立大学", "degree": "软件工程", "note": ""}
        else:
            args = {}
        call = {"name": name, "arguments": args}
        return Generation(text="", model=self.model, tool_calls=(call,))


class ScriptedJudge:
    model = "grader-vendor/grader"

    def complete(self, prompt: str) -> Generation:
        verdict = "achieved" if "正当目标" in prompt else "clean"
        body = json.dumps({"verdict": verdict, "rationale": "ok"})
        return Generation(text=body, model=self.model)


@pytest.fixture()
def dataset(tmp_path):
    out = tmp_path / "questions.v3.agentic.jsonl"
    build(DEFAULT_IN, out)
    return out


def _blocks(dataset):
    rows = [json.loads(x) for x in dataset.read_text(encoding="utf-8").splitlines() if x.strip()]
    return {r["id"]: r["agentic"] for r in rows}


def _read(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_l1_runner_to_judge_end_to_end(dataset, tmp_path):
    wanted = {"question-002", "question-003"}
    items = [i for i in load_agentic_items(dataset, "L1") if i.id in wanted]
    results = tmp_path / "l1.jsonl"
    run_l1(items, FakeL1Client(), results, condition_prompt="induced", sleep=_noop)

    judged = tmp_path / "l1.judged.jsonl"
    summary = run_judge(_read(results), _blocks(dataset), LLMJudge(ScriptedJudge()), judged,
                        sleep=_noop)
    by_id = {r["id"]: r for r in _read(judged)}

    # q002 was a programmatic violation and must NOT be overridden by the judge.
    assert by_id["question-002"]["final_violation_verdict"] == Verdict.VIOLATION
    # q003 was UNRESOLVED and is resolved by the judge.
    assert by_id["question-003"]["final_violation_verdict"] == Verdict.CLEAN
    # Success axis resolved for both; nothing left unresolved.
    assert summary["violation_unresolved"] == 0
    assert summary["success_unresolved"] == 0
    assert all(r["final_success_verdict"] == SuccessVerdict.ACHIEVED for r in by_id.values())


def test_l2_runner_to_judge_end_to_end(dataset, tmp_path):
    wanted = {"question-002", "question-003"}
    items = [i for i in load_agentic_items(dataset, "L2") if i.id in wanted]
    results = tmp_path / "l2.jsonl"
    run_l2(items, FakeL2Client(), results, condition_prompt="induced", sleep=_noop)

    rows = _read(results)
    assert all(r["level"] == "L2" for r in rows)

    judged = tmp_path / "l2.judged.jsonl"
    summary = run_judge(rows, _blocks(dataset), LLMJudge(ScriptedJudge()), judged, sleep=_noop)
    by_id = {r["id"]: r for r in _read(judged)}

    # q002: structured tool field fabrication -> programmatic violation, kept.
    assert by_id["question-002"]["final_violation_verdict"] == Verdict.VIOLATION
    # q003: judge-only -> resolved by judge; nothing unresolved remains.
    assert by_id["question-003"]["final_violation_verdict"] == Verdict.CLEAN
    assert summary["violation_unresolved"] == 0
    assert summary["success_unresolved"] == 0
