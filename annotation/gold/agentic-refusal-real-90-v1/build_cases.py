"""Build the refusal-axis calibration set from REAL Avg@16 outputs.

Why real and not synthetic: the synthetic held-out set scored the judges at kappa
0.93+, while on real outputs the v6 refusal judge managed only 0.737. The refusal
axis is judged first and short-circuits the other two, so it has to be calibrated
on the distribution it will actually meet.

Sampling is stratified on output length, which is label-free and is exactly where
the hard calls live: on Grok the sub-60-character zone contains refusals, legitimate
one-line deliverables, and at least one apparent violation all at once.
"""
import hashlib, json, pathlib, random

SEED = 20260912
N_PER_ARM_PER_TERCILE = 10
ARMS = [("opus-5", "anthropic/claude-opus-5", "agentic-l1-claude-opus-5-induced"),
        ("grok-4.6", "x-ai/grok-4.6", "agentic-l1-grok-4.6-induced"),
        ("gemini-3.7-flash", "google/gemini-3.7-flash", "agentic-l1-gemini-3.7-flash-induced")]
OUT_DIR = pathlib.Path("annotation/gold/agentic-refusal-real-90-v1")
GEN_DIR = pathlib.Path("annotation/generated/agentic-refusal-real-90-v1")

blocks = {}
for line in pathlib.Path("questions.v3.agentic.jsonl").read_text(encoding="utf-8").splitlines():
    if line.strip():
        r = json.loads(line); blocks[r["id"]] = r

# Use the production function, not a re-implementation: judge_refusal receives
# _task_prompt(block, "L1"), which is the materialized prompt_l1 (facts + task),
# not the bare task_prompt_l1. A hand-rolled copy silently drifted here once.
import sys
sys.path.insert(0, "src")
from sudo_bench.agentic.judge import _task_prompt


def task_prompt(qid):
    return _task_prompt(blocks[qid]["agentic"], "L1")

rng = random.Random(SEED)
selected, strata_log = [], []
for slug, model, d in ARMS:
    rows = [json.loads(l) for l in
            pathlib.Path("runs/%s/results.jsonl" % d).read_text(encoding="utf-8").splitlines() if l.strip()]
    rows.sort(key=lambda r: (len(r.get("output") or ""), r["id"], r["sample_index"]))
    third = len(rows) // 3
    terciles = {"short": rows[:third], "mid": rows[third:2 * third], "long": rows[2 * third:]}
    picked_keys = set()
    for tname, pool in terciles.items():
        pick = rng.sample(pool, N_PER_ARM_PER_TERCILE)
        for r in pick:
            picked_keys.add((r["id"], r["sample_index"]))
            selected.append((slug, model, tname, r))
        strata_log.append({"arm": model, "stratum": tname, "pool": len(pool),
                           "drawn": N_PER_ARM_PER_TERCILE,
                           "char_range": [len(pool[0].get("output") or ""),
                                          len(pool[-1].get("output") or "")]})
    # Enrichment: the rare class must appear. Every row whose keyword hint fired is
    # force-included; the hint is a diagnostic, never a label, and the annotators
    # never see it.
    extra = 0
    for r in rows:
        if r.get("refusal_hint") and (r["id"], r["sample_index"]) not in picked_keys:
            selected.append((slug, model, "hint_enriched", r)); extra += 1
    if extra:
        strata_log.append({"arm": model, "stratum": "hint_enriched", "pool": extra,
                           "drawn": extra, "char_range": None})

OUT_DIR.mkdir(parents=True, exist_ok=True)
GEN_DIR.mkdir(parents=True, exist_ok=True)

cases, private = [], []
for slug, model, tname, r in selected:
    key = "{}/refusal/{}/{}".format(r["id"], slug, r["sample_index"])
    cases.append({
        "case_key": key,
        "source_id": r["id"],
        "axis": "refusal",
        "public_facts": blocks[r["id"]]["agentic"]["public_facts"],
        "criterion": task_prompt(r["id"]),   # judge_refusal compares task vs evidence
        "output": r.get("output") or "",
    })
    private.append({"case_key": key, "arm": model, "stratum": tname,
                    "sample_index": r["sample_index"], "source_id": r["id"],
                    "refusal_hint": r.get("refusal_hint"),
                    "detector_boundary": r.get("violation_verdict"),
                    "output_chars": len(r.get("output") or "")})

cases.sort(key=lambda c: c["case_key"])
with (OUT_DIR / "cases.jsonl").open("w", encoding="utf-8") as fh:
    for c in cases:
        fh.write(json.dumps(c, ensure_ascii=False) + "\n")

# Blind workbooks: independent random order per annotator, no arm, no question id,
# no hint, no detector verdict, no pairing.
display = list(range(len(cases)))
for annot in ("01", "02"):
    order = list(display)
    random.Random(SEED + int(annot)).shuffle(order)
    with (GEN_DIR / "annotator_{}.csv".format(annot)).open("w", encoding="utf-8") as fh:
        fh.write("display_id,task,output,label\n")
        for disp, idx in enumerate(order, 1):
            c = cases[idx]
            esc = lambda s: '"' + str(s).replace('"', '""') + '"'
            fh.write("{},{},{},\n".format(disp, esc(c["criterion"]), esc(c["output"])))
    with (GEN_DIR / "answer_map_{}.json".format(annot)).open("w", encoding="utf-8") as fh:
        json.dump({str(d): cases[i]["case_key"] for d, i in enumerate(order, 1)}, fh,
                  ensure_ascii=False, indent=2)
with (GEN_DIR / "provenance_private.jsonl").open("w", encoding="utf-8") as fh:
    for p in private:
        fh.write(json.dumps(p, ensure_ascii=False) + "\n")

sha = lambda p: hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
print("cases:", len(cases))
import collections
print("by arm:", dict(collections.Counter(p["arm"] for p in private)))
print("by stratum:", dict(collections.Counter(p["stratum"] for p in private)))
print("cases.jsonl sha256:", sha(OUT_DIR / "cases.jsonl"))
print("\nstrata:")
for s in strata_log:
    print("  ", s)
json.dump({"seed": SEED, "strata": strata_log,
           "cases_sha256": sha(OUT_DIR / "cases.jsonl"),
           "n": len(cases)},
          open("/private/tmp/claude-501/-Users-shulitao-Documents-Codex-2026-08-31-https-github-com-ichthyosaur-sudo-bench-work-SuDo-Bench/55af5f58-53b1-4676-816f-625ddfc8be75/scratchpad/refusal-set-meta.json", "w"), indent=2)
