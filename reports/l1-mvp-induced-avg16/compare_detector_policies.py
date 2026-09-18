"""Compare the frozen V1 and new V2 L1 detectors on existing outputs; no API calls.

Run from the repository root:

    PYTHONPATH=src python reports/l1-mvp-induced-avg16/compare_detector_policies.py
"""

import collections
import hashlib
import json
import pathlib

import aggregate

from sudo_bench.agentic.detectors import DetectorPolicy, evaluate_item

DATASET = pathlib.Path("questions.v3.agentic.jsonl")
OUTPUT = pathlib.Path("reports/l1-mvp-induced-avg16/detector-policy-compare-v1-v2.json")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_blocks():
    blocks = {}
    for line in DATASET.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            blocks[row["id"]] = row["agentic"]
    if len(blocks) != 15:
        raise ValueError("expected 15 unique agentic questions")
    return blocks


def build_report():
    blocks = load_blocks()
    judged = aggregate.load_inputs()  # Verify sample completeness and provenance.
    rows = []
    counts = {model: collections.Counter() for model in aggregate.ARMS.values()}
    question_counts = collections.Counter()
    for arm, model in aggregate.ARMS.items():
        for (qid, index), row in sorted(judged[arm]["A"].items()):
            if not isinstance(row.get("output"), str):
                raise ValueError(f"missing output: {model}|{qid}|{index}")
            verdicts = {}
            for policy in (DetectorPolicy.LEGACY_V1, DetectorPolicy.L1_PRESENT_SEMANTIC_V2):
                result = evaluate_item(
                    blocks[qid], "L1", output_text=row["output"],
                    tool_calls=row.get("tool_calls"), item_id=qid,
                    detector_policy=policy,
                )
                verdicts[policy] = result.verdict
            recorded = (row.get("evaluation") or {}).get("violation", {}).get("verdict")
            if verdicts[DetectorPolicy.LEGACY_V1] != recorded:
                raise ValueError(f"legacy V1 does not reproduce {model}|{qid}|{index}")
            old = verdicts[DetectorPolicy.LEGACY_V1]
            new = verdicts[DetectorPolicy.L1_PRESENT_SEMANTIC_V2]
            counts[model][f"{old}->{new}"] += 1
            if old != new:
                question_counts[qid] += 1
                rows.append({
                    "case_key": f"{model}|{qid}|{index}",
                    "legacy_v1": old,
                    "l1_present_semantic_v2": new,
                    "output_sha256": hashlib.sha256(row["output"].encode("utf-8")).hexdigest(),
                })
    return {
        "analysis_id": "l1-detector-policy-compare-v1-v2",
        "scope": "Existing L1 induced Avg@16 outputs only; no model or judge calls",
        "dataset_sha256": sha256(DATASET),
        "judged_a_sha256": {
            model: sha256(aggregate.JUDGED / f"{arm}.judge-A.jsonl")
            for arm, model in aggregate.ARMS.items()
        },
        "legacy_reproduced_recorded_detector_verdicts": 720,
        "counts_by_model": {model: dict(counter) for model, counter in counts.items()},
        "changed_by_question": dict(sorted(question_counts.items())),
        "changed_cases": rows,
        "interpretation": (
            "V2 changes L1 free-text present keyword hits from automatic VIOLATION to "
            "UNRESOLVED. It does not by itself supply a semantic verdict or revise the "
            "historical preregistered report."
        ),
    }


def main():
    result = build_report()
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"legacy reproduced {result['legacy_reproduced_recorded_detector_verdicts']} rows")
    print(f"changed {len(result['changed_cases'])} rows")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
