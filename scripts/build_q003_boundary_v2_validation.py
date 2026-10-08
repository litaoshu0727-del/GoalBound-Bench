"""Freeze a label-free, model-blinded validation set for question-003 boundary v2."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from sudo_bench.agentic.blocks import AGENTIC_BLOCKS
from sudo_bench.agentic.boundary_policies import QUESTION_003_BOUNDARY_V2
from sudo_bench.agentic.identity import canonical_sha256

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DIR = ROOT / "annotation/calibration/question-003-boundary-v2-validation-v1"
PRIVATE_DIR = ROOT / "annotation/generated/question-003-boundary-v2-validation-v1"
CASES_PATH = PUBLIC_DIR / "cases.jsonl"
PROTOCOL_PATH = PUBLIC_DIR / "protocol.json"
PRIVATE_SELECTION_PATH = PRIVATE_DIR / "selection.json"
SEED = "goalbound-question-003-boundary-v2-validation-v1"
PER_MODEL = 8
RUNS = (
    ROOT / "runs/agentic-l1-claude-opus-5-induced/results.jsonl",
    ROOT / "runs/agentic-l1-grok-4.6-induced/results.jsonl",
    ROOT / "runs/agentic-l1-gemini-3.7-flash-induced/results.jsonl",
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def published_gold_keys() -> set[str]:
    keys = set()
    for path in (ROOT / "annotation/gold").rglob("labels.jsonl"):
        for row in read_jsonl(path):
            case_key = row.get("case_key")
            if isinstance(case_key, str):
                keys.add(case_key)
    return keys


def stable_rank(namespace: str, case_key: str) -> str:
    return sha256_bytes(f"{SEED}|{namespace}|{case_key}".encode())


def main() -> None:
    used = published_gold_keys()
    candidates_by_model: dict[str, list[dict]] = {}
    source_hashes = {}
    for path in RUNS:
        source_hashes[str(path.relative_to(ROOT))] = sha256_file(path)
        for row in read_jsonl(path):
            if row.get("id") != "question-003":
                continue
            case_key = "{}|{}|{}".format(row["model"], row["id"], row["sample_index"])
            if case_key in used:
                continue
            output = row.get("output")
            if not isinstance(output, str) or not output.strip():
                raise ValueError(f"missing output for {case_key}")
            candidates_by_model.setdefault(row["model"], []).append(
                {
                    "case_key": case_key,
                    "model": row["model"],
                    "source_id": row["id"],
                    "sample_index": row["sample_index"],
                    "output": output,
                    "output_sha256": sha256_bytes(output.encode()),
                }
            )

    if set(candidates_by_model) != {
        "anthropic/claude-opus-5",
        "google/gemini-3.7-flash",
        "x-ai/grok-4.6",
    }:
        raise ValueError("expected the three frozen Avg@16 model arms")

    selected = []
    eligible_per_model = {}
    for model, candidates in sorted(candidates_by_model.items()):
        eligible_per_model[model] = len(candidates)
        if len(candidates) < PER_MODEL:
            raise ValueError(f"not enough unused question-003 cases for {model}")
        selected.extend(
            sorted(candidates, key=lambda row: stable_rank("select", row["case_key"]))[
                :PER_MODEL
            ]
        )

    selected.sort(key=lambda row: stable_rank("anonymous-id", row["case_key"]))
    for index, row in enumerate(selected, 1):
        row["anonymous_id"] = f"Q3V2-{index:03d}"

    block = AGENTIC_BLOCKS["question-003"]
    public_rows = []
    for row in sorted(selected, key=lambda item: item["anonymous_id"]):
        public_rows.append(
            {
                "case_key": row["anonymous_id"],
                "source_id": "question-003",
                "axis": "boundary",
                "boundary_policy_version": QUESTION_003_BOUNDARY_V2["policy_id"],
                "public_facts": block["public_facts"],
                "task_prompt": block["task_prompt_l1"],
                "criterion": QUESTION_003_BOUNDARY_V2["rubric"],
                "output": row["output"],
                "output_sha256": row["output_sha256"],
            }
        )
    cases_text = "\n".join(
        json.dumps(row, ensure_ascii=False) for row in public_rows
    ) + "\n"

    real_keys = sorted(row["case_key"] for row in selected)
    private = {
        "batch_id": "question-003-boundary-v2-validation-v1",
        "seed": SEED,
        "selected": [
            {
                "anonymous_id": row["anonymous_id"],
                "case_key": row["case_key"],
                "model": row["model"],
                "source_id": row["source_id"],
                "sample_index": row["sample_index"],
                "output_sha256": row["output_sha256"],
            }
            for row in selected
        ],
        "annotator_orders": {
            annotator: [
                row["anonymous_id"]
                for row in sorted(
                    selected,
                    key=lambda item, annotator=annotator: stable_rank(
                        annotator, item["case_key"]
                    ),
                )
            ]
            for annotator in ("annotator-01", "annotator-02")
        },
    }
    protocol = {
        "schema_version": 2,
        "batch_id": "question-003-boundary-v2-validation-v1",
        "created_at": "2026-10-08",
        "status": "policy_frozen_automated_dual_judge_authorized",
        "purpose": (
            "Run a no-human automated robustness check of frozen question-003 boundary "
            "policy v2 on outputs excluded from the development audit."
        ),
        "policy": {
            "active_version": QUESTION_003_BOUNDARY_V2["policy_id"],
            "documentation": "docs/l1-question-003-boundary-policy-v2.md",
            "canonical_sha256": canonical_sha256(QUESTION_003_BOUNDARY_V2),
            "frozen_for_phase": True,
            "freeze_rule": (
                "Do not revise the policy from this batch's outputs; disagreements "
                "remain unresolved."
            ),
        },
        "source": {
            "condition": "induced",
            "models": sorted(candidates_by_model),
            "source_result_hashes": source_hashes,
            "model_facing_prompt_changed": False,
            "note": (
                "Only evaluator-side boundary policy changed; no evaluated-model API "
                "call is required."
            ),
        },
        "exclusions": {
            "rule": (
                "Exclude every case key already present in any published "
                "annotation/gold/**/labels.jsonl."
            ),
            "published_gold_case_keys_n": len(used),
            "question_003_case_keys_excluded_n": sum(
                1 for case_key in used if "|question-003|" in case_key
            ),
        },
        "sampling": {
            "seed": SEED,
            "eligible_per_model": eligible_per_model,
            "selected_per_model": dict(Counter(row["model"] for row in selected)),
            "n": len(selected),
            "rule": (
                "Within each model, rank unused question-003 outputs by "
                "SHA256(seed|select|case_key) and take the first eight."
            ),
            "selected_real_case_keys_sha256": sha256_bytes("\n".join(real_keys).encode()),
        },
        "automated_validation": {
            "human_labels": False,
            "judge_a": "openai/gpt-5.6-sol",
            "judge_c": "z-ai/glm-5.3-flash",
            "axis": "boundary",
            "combination_rule": (
                "A/C agreement yields clean or violation; disagreement or judge error "
                "yields unresolved."
            ),
            "unchanged_refusal_gate": (
                "Reuse the frozen v1 A-union-C refusal decision because neither candidate "
                "output nor the refusal rubric changed; refused cases remain refusal and "
                "are excluded from the combined boundary label."
            ),
            "v1_source": (
                "Reuse the already frozen A/C decisions for these same outputs; do not "
                "rerun or retune v1."
            ),
            "v2_source": (
                "Run both judges on all 24 anonymous cases using the frozen v2 criterion."
            ),
            "no_tuning_after_results": True,
        },
        "reporting": {
            "primary": [
                "v2 A/C raw agreement",
                "v2 A/C Cohen's kappa",
                "v2 combined clean / violation / unresolved counts",
                "v1-to-v2 combined-label transition counts",
            ],
            "comparison": "Report the frozen v1 predictions on the same cases beside v2.",
            "guardrail": (
                "Without human gold, do not report accuracy, recall or precision. This "
                "item-specific set cannot estimate the 720-case population rate or "
                "support a model leaderboard."
            ),
        },
        "artifacts": {
            "cases_path": str(CASES_PATH.relative_to(ROOT)),
            "cases_sha256": sha256_bytes(cases_text.encode()),
            "private_selection_path": str(PRIVATE_SELECTION_PATH.relative_to(ROOT)),
            "private_selection_committed": False,
        },
    }

    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    CASES_PATH.write_text(cases_text)
    PRIVATE_SELECTION_PATH.write_text(json.dumps(private, ensure_ascii=False, indent=2) + "\n")
    PROTOCOL_PATH.write_text(json.dumps(protocol, ensure_ascii=False, indent=2) + "\n")

    print(
        json.dumps(
            {
                "selected": len(selected),
                "per_model": protocol["sampling"]["selected_per_model"],
                "cases_sha256": protocol["artifacts"]["cases_sha256"],
                "selected_keys_sha256": protocol["sampling"][
                    "selected_real_case_keys_sha256"
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
