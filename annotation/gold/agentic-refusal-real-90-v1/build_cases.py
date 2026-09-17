"""Build the refusal-axis calibration set from real Avg@16 outputs.

Why real and not synthetic: on the synthetic held-out set every judge scored kappa
0.93+, while the v6 refusal judge managed only 0.737 on real outputs. The refusal
axis is judged first and short-circuits the other two, so it has to be calibrated
on the distribution it will actually meet.

Sampling is stratified on output length, which is label-free and is exactly where
the hard calls live: on Grok the sub-60-character zone contains refusals,
legitimate one-line deliverables, and at least one apparent violation at once.

Run from the repository root:

    python annotation/gold/agentic-refusal-real-90-v1/build_cases.py
"""

import collections
import hashlib
import json
import pathlib
import random
import sys

sys.path.insert(0, "src")

from sudo_bench.agentic.judge import _task_prompt  # noqa: E402

SEED = 20260912
N_PER_ARM_PER_TERCILE = 10
ARMS = [
    ("opus-5", "anthropic/claude-opus-5", "agentic-l1-claude-opus-5-induced"),
    ("grok-4.6", "x-ai/grok-4.6", "agentic-l1-grok-4.6-induced"),
    ("gemini-3.7-flash", "google/gemini-3.7-flash", "agentic-l1-gemini-3.7-flash-induced"),
]
OUT_DIR = pathlib.Path("annotation/gold/agentic-refusal-real-90-v1")
GEN_DIR = pathlib.Path("annotation/generated/agentic-refusal-real-90-v1")


def sha256_of(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def csv_cell(value):
    return '"' + str(value).replace('"', '""') + '"'


def read_jsonl(path):
    rows = []
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def main():
    blocks = {row["id"]: row for row in read_jsonl("questions.v3.agentic.jsonl")}
    rng = random.Random(SEED)
    selected = []
    strata_log = []

    for slug, model, run_dir in ARMS:
        rows = read_jsonl("runs/{}/results.jsonl".format(run_dir))
        rows.sort(key=lambda r: (len(r.get("output") or ""), r["id"], r["sample_index"]))
        third = len(rows) // 3
        terciles = {
            "short": rows[:third],
            "mid": rows[third:2 * third],
            "long": rows[2 * third:],
        }
        picked = set()
        for name, pool in terciles.items():
            for row in rng.sample(pool, N_PER_ARM_PER_TERCILE):
                picked.add((row["id"], row["sample_index"]))
                selected.append((slug, model, name, row))
            strata_log.append({
                "arm": model,
                "stratum": name,
                "pool": len(pool),
                "drawn": N_PER_ARM_PER_TERCILE,
                "char_range": [
                    len(pool[0].get("output") or ""),
                    len(pool[-1].get("output") or ""),
                ],
            })
        # Enrichment: the rare class must appear. Every row whose keyword hint fired
        # and that the tercile draw missed is force-included. The hint is a
        # diagnostic, never a label, and annotators never see it.
        extra = 0
        for row in rows:
            if row.get("refusal_hint") and (row["id"], row["sample_index"]) not in picked:
                selected.append((slug, model, "hint_enriched", row))
                extra += 1
        if extra:
            strata_log.append({
                "arm": model, "stratum": "hint_enriched",
                "pool": extra, "drawn": extra, "char_range": None,
            })

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    GEN_DIR.mkdir(parents=True, exist_ok=True)

    cases = []
    private = []
    for slug, model, stratum, row in selected:
        key = "{}/refusal/{}/{}".format(row["id"], slug, row["sample_index"])
        cases.append({
            "case_key": key,
            "source_id": row["id"],
            "axis": "refusal",
            "public_facts": blocks[row["id"]]["agentic"]["public_facts"],
            # judge_refusal compares the task against the evidence, and production
            # passes _task_prompt(block, "L1"). Using the production function rather
            # than a re-implementation: an earlier build used task_prompt_l1 and
            # drifted on all 98 cases.
            "criterion": _task_prompt(blocks[row["id"]]["agentic"], "L1"),
            "output": row.get("output") or "",
        })
        private.append({
            "case_key": key, "arm": model, "stratum": stratum,
            "sample_index": row["sample_index"], "source_id": row["id"],
            "refusal_hint": row.get("refusal_hint"),
            "detector_boundary": row.get("violation_verdict"),
            "output_chars": len(row.get("output") or ""),
        })

    cases.sort(key=lambda case: case["case_key"])
    with (OUT_DIR / "cases.jsonl").open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")

    # Blind workbooks: independent random order per annotator, and no arm, question
    # id, hint, detector verdict or pairing.
    for annotator in ("01", "02"):
        order = list(range(len(cases)))
        random.Random(SEED + int(annotator)).shuffle(order)
        path = GEN_DIR / "annotator_{}.csv".format(annotator)
        with path.open("w", encoding="utf-8") as handle:
            handle.write("display_id,task,output,label\n")
            for display, index in enumerate(order, 1):
                case = cases[index]
                handle.write("{},{},{},\n".format(
                    display, csv_cell(case["criterion"]), csv_cell(case["output"])))
        mapping = {str(d): cases[i]["case_key"] for d, i in enumerate(order, 1)}
        (GEN_DIR / "answer_map_{}.json".format(annotator)).write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")

    with (GEN_DIR / "provenance_private.jsonl").open("w", encoding="utf-8") as handle:
        for row in private:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    print("cases: {}".format(len(cases)))
    print("by arm: {}".format(dict(collections.Counter(p["arm"] for p in private))))
    print("by stratum: {}".format(dict(collections.Counter(p["stratum"] for p in private))))
    print("cases.jsonl sha256: {}".format(sha256_of(OUT_DIR / "cases.jsonl")))
    for entry in strata_log:
        print("  {}".format(entry))


if __name__ == "__main__":
    main()
