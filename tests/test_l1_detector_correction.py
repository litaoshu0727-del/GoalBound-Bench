"""Offline checks for the frozen 21-case detector audit and correction rule."""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "reports/l1-mvp-induced-avg16/detector_correction.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("l1_detector_correction", SCRIPT)
correction = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(correction)


def test_frozen_gold_is_complete_and_matches_summary():
    path = ROOT / "annotation/gold/agentic-l1-detector-audit-21-v1/labels.jsonl"
    summary = json.loads((path.parent / "summary.json").read_text(encoding="utf-8"))
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == summary["labels_sha256"]
    cases = correction.load_gold(path)
    assert len(cases) == summary["n_cases"] == 21
    assert sum(c["labels"]["boundary"] == "clean" for c in cases.values()) == 21
    assert sum(c["labels"]["refusal"] == "refusal" for c in cases.values()) == 2


def test_v1_v2_offline_comparison_matches_frozen_audit():
    report = json.loads((ROOT / "reports/l1-mvp-induced-avg16"
                         / "detector-policy-compare-v1-v2.json").read_text(encoding="utf-8"))
    gold = correction.load_gold()
    assert report["legacy_reproduced_recorded_detector_verdicts"] == 720
    changed = {row["case_key"]: row for row in report["changed_cases"]}
    assert changed.keys() == gold.keys()
    for key, row in changed.items():
        assert row["legacy_v1"] == "violation"
        assert row["l1_present_semantic_v2"] == "unresolved"
        assert row["output_sha256"] == gold[key]["output_sha256"]


def test_correction_changes_boundary_only():
    original = {
        "n": 4,
        "counts": {
            "refusal": {"engaged": 3, "refusal": 1},
            "boundary": {"VIOLATION": 2, "CLEAN": 1, "REFUSAL": 1},
            "success": {"ACHIEVED": 2, "NOT_ACHIEVED": 2},
        },
        "violation_rate": {"resolved": 0.5},
        "violation_rate_engaged_only": {"resolved": 0.6667},
    }
    cases = [
        {"original_boundary": "VIOLATION", "labels": {"boundary": "clean"}},
        {"original_boundary": "REFUSAL", "labels": {"boundary": "clean"}},
    ]
    result = correction.corrected_arm(original, cases)
    assert result["violation_to_clean_corrections"] == 1
    assert result["previously_refusal_short_circuited"] == 1
    assert result["corrected_boundary_counts"] == {
        "VIOLATION": 1, "CLEAN": 2, "REFUSAL": 1,
    }
    assert result["corrected_violation_rate"] == 0.25
    assert result["corrected_engaged_only_violation_rate"] == 0.3333
    assert original["counts"]["boundary"]["VIOLATION"] == 2
    assert original["counts"]["refusal"] == {"engaged": 3, "refusal": 1}
    assert original["counts"]["success"] == {"ACHIEVED": 2, "NOT_ACHIEVED": 2}
