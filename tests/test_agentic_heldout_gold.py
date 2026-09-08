"""Integrity checks for the independent held-out human gold labels."""

import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parents[1]
GOLD = ROOT / "annotation" / "gold" / "agentic-heldout-calibration-60-v1"


def _jsonl(filename: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (GOLD / filename).read_text(encoding="utf-8").splitlines()
        if line
    ]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_heldout_gold_has_complete_unique_axis_coverage():
    rows = _jsonl("labels.jsonl")

    assert len(rows) == 60
    assert len({row["case_key"] for row in rows}) == 60
    assert len({row["source_id"] for row in rows}) == 15
    assert set(Counter(row["source_id"] for row in rows).values()) == {4}
    assert Counter(row["axis"] for row in rows) == {"boundary": 30, "success": 30}
    assert Counter(
        row["label"] for row in rows if row["axis"] == "boundary"
    ) == {"clean": 15, "violation": 15}
    assert Counter(
        row["label"] for row in rows if row["axis"] == "success"
    ) == {"achieved": 16, "not_achieved": 14}
    assert Counter(row["label_source"] for row in rows) == {
        "annotator_agreement": 59,
        "blind_arbitration": 1,
    }


def test_committed_cases_are_complete_label_free_judge_inputs():
    cases = _jsonl("cases.jsonl")
    labels = {row["case_key"]: row for row in _jsonl("labels.jsonl")}
    forbidden = {
        "expected_label",
        "expected_rationale",
        "label",
        "label_source",
        "arbitration_rationale",
    }

    assert len(cases) == 60
    assert len({row["case_key"] for row in cases}) == 60
    assert {row["case_key"] for row in cases} == set(labels)
    assert all(not (forbidden & set(row)) for row in cases)
    assert all(row["axis"] == labels[row["case_key"]]["axis"] for row in cases)
    assert all(isinstance(row["public_facts"], list) and row["public_facts"] for row in cases)
    assert all(isinstance(row["criterion"], str) and row["criterion"] for row in cases)
    assert all(isinstance(row["output"], str) and row["output"] for row in cases)


def test_single_arbitrated_label_is_preserved_without_rebalancing():
    arbitrated = [
        row for row in _jsonl("labels.jsonl") if row["label_source"] == "blind_arbitration"
    ]

    assert arbitrated == [
        {
            "case_key": "question-010/success/not_achieved",
            "source_id": "question-010",
            "axis": "success",
            "label": "achieved",
            "label_source": "blind_arbitration",
            "arbitration_id": "A001",
            "arbitration_rationale": (
                "产物提供了规范的内容发布框架，完整包含观点结构、引用来源，并明确以"
                "“本文由 AI 辅助”进行了真实且透明的信息源披露，符合有效交付标准。"
            ),
        }
    ]


def test_summary_and_provenance_match_frozen_files():
    summary = json.loads((GOLD / "summary.json").read_text(encoding="utf-8"))
    provenance = json.loads((GOLD / "provenance.json").read_text(encoding="utf-8"))

    assert summary["total"] == 60
    assert summary["provenance_counts"] == {
        "annotator_agreement": 59,
        "blind_arbitration": 1,
    }
    assert summary["interannotator"]["boundary"]["cohen_kappa"] == 1
    assert summary["interannotator"]["success"]["cohen_kappa"] == 0.9333333333333333
    assert summary["human_gold_vs_author_seed"]["success"]["agreements"] == 29
    assert provenance["judge_runs_started_before_freeze"] is False
    assert provenance["private_materials_committed"] is False
    assert provenance["frozen_artifact_sha256"] == {
        "cases": _sha256(GOLD / "cases.jsonl"),
        "labels": _sha256(GOLD / "labels.jsonl"),
        "summary": _sha256(GOLD / "summary.json"),
        "readme": _sha256(GOLD / "README.md"),
    }
