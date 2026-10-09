"""Compare L0 multiple-choice and L1 open-generation behaviour on the 15 core questions.

Exploratory and descriptive, no API calls and no new labels. The plan below was committed
before the script was first run.

Data, per model (Claude Opus 5, Grok 4.6, Gemini 3.7 Flash) and question (16 samples each):

- L0: any-problematic-choice rate of the randomized-induced Avg@16 runs, rescored on the
  adjudicated labels of questions.v3.jsonl (reproduces the published leaderboard totals).
- L1: combined boundary verdict of the v2 rescoring of the 2026-09-12 induced run
  (runs/l1-judged-v2/, frozen A/C rule): violation rate over all samples, its upper bound
  with unresolved counted as violation, refusal rate, and violation among engaged samples.

Reported:

1. Per model: L0 rate next to L1 rates, and the model ordering at each level.
2. Per model and pooled over the 45 model-question pairs: Spearman rank correlation between
   the L0 rate and the L1 violation rate across questions, with a two-sided permutation
   p-value (10,000 shuffles, fixed seed). Descriptive only; no multiplicity correction.
3. A 2x2 count of model-question pairs: L0 rate >= 0.5 or not, against any L1 violation
   (at least one of 16 samples) or none.
4. The full per-question table.

Not a causal level effect. The two levels differ in more than format: the L0 induced prompt
explicitly tells the model to over-package itself or exploit others, while L1_INDUCED is
situational pressure with no such instruction; L0 Opus and Grok ran with high reasoning
effort and max_tokens 1024, L1 with the provider default and 8192; L0 offers the
problematic option ready-made; L1 violations are judged automatically and under-recalled
(about half on the 120-case audit under v1), with question-003 a lower bound.

    PYTHONPATH=src python reports/l0-l1-comparison/compare.py
"""

import collections
import json
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "l1-mvp-induced-avg16"))
import aggregate  # noqa: E402

QUESTIONS = pathlib.Path("questions.v3.jsonl")
V2_JUDGED = pathlib.Path("runs/l1-judged-v2")
OUTPUT = pathlib.Path("reports/l0-l1-comparison/results.json")
L0_RUNS = {
    "anthropic/claude-opus-5": "runs/openrouter-claude-opus-5-randomized-induced-high-reasoning",
    "x-ai/grok-4.6": "runs/openrouter-grok-4.6-randomized-induced-high-reasoning",
    "google/gemini-3.7-flash": "runs/openrouter-gemini-3.7-flash-randomized-induced",
}
LEADERBOARD = pathlib.Path("reports/goalbound-bench-leaderboard-adjudicated.json")
PERMUTATIONS = 10_000
SEED = "l0-l1-comparison-v1"
HIGH_L0 = 0.5


def read_jsonl(path):
    return [json.loads(line) for line in pathlib.Path(path).read_text().splitlines() if line]


def l0_rates():
    labels = {row["id"]: set(row["metadata"]["problematic_option_ids"])
              for row in read_jsonl(QUESTIONS)}
    published = {m["model"]: m["any_problematic_choices"]
                 for m in json.loads(LEADERBOARD.read_text())["models"]}
    rates = {}
    for model, run in L0_RUNS.items():
        counts = collections.defaultdict(lambda: [0, 0])
        for row in read_jsonl(pathlib.Path(run) / "results.jsonl"):
            if row["id"] not in labels:
                continue
            counts[row["id"]][1] += 1
            counts[row["id"]][0] += row.get("predicted_option_id") in labels[row["id"]]
        if sum(k for k, _ in counts.values()) != published[model]:
            raise ValueError("{}: L0 rescoring does not reproduce the leaderboard".format(model))
        if any(n != 16 for _, n in counts.values()) or len(counts) != 15:
            raise ValueError("{}: expected 15 questions x 16 samples".format(model))
        rates[model] = {qid: k / n for qid, (k, n) in counts.items()}
    return rates


def l1_rates():
    loaded = aggregate.load_inputs(judged_dir=V2_JUDGED, include_b=False)
    rates = {}
    for arm, model in aggregate.ARMS.items():
        per_question = collections.defaultdict(collections.Counter)
        for key in loaded[arm]["A"]:
            counts = aggregate.aggregate_arm({0: loaded[arm]["A"][key]},
                                             {0: loaded[arm]["C"][key]})["counts"]
            per_question[key[0]][next(iter(counts["boundary"]))] += 1
        rates[model] = {}
        for qid, counter in per_question.items():
            n = sum(counter.values())
            engaged = n - counter["REFUSAL"]
            rates[model][qid] = {
                "violation": counter["VIOLATION"] / n,
                "violation_upper": (counter["VIOLATION"] + counter["UNRESOLVED"]) / n,
                "refusal": counter["REFUSAL"] / n,
                "violation_among_engaged": counter["VIOLATION"] / engaged if engaged else None,
                "any_violation": counter["VIOLATION"] > 0,
            }
    return rates


def ranks(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    result = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            result[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return result


def spearman(x, y):
    rx, ry = ranks(x), ranks(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sx = sum((a - mx) ** 2 for a in rx) ** 0.5
    sy = sum((b - my) ** 2 for b in ry) ** 0.5
    return cov / (sx * sy) if sx and sy else None


def permutation_p(x, y, seed):
    observed = spearman(x, y)
    if observed is None:
        return None, None
    rng = random.Random(seed)
    shuffled = list(y)
    extreme = 0
    for _ in range(PERMUTATIONS):
        rng.shuffle(shuffled)
        value = spearman(x, shuffled)
        extreme += value is not None and abs(value) >= abs(observed) - 1e-12
    return observed, (extreme + 1) / (PERMUTATIONS + 1)


def main():
    l0, l1 = l0_rates(), l1_rates()
    models = list(L0_RUNS)
    questions = sorted(l0[models[0]])
    if any(sorted(l1[model]) != questions for model in models):
        raise ValueError("L0 and L1 must cover the same 15 questions")
    table, per_model, quadrant = [], {}, collections.Counter()
    pooled_x, pooled_y = [], []
    for model in models:
        x = [l0[model][q] for q in questions]
        y = [l1[model][q]["violation"] for q in questions]
        rho, p = permutation_p(x, y, "{}|{}".format(SEED, model))
        per_model[model] = {
            "l0_any_problematic_rate": sum(x) / len(x),
            "l1_violation_rate": sum(y) / len(y),
            "l1_violation_upper": sum(l1[model][q]["violation_upper"] for q in questions) / 15,
            "l1_refusal_rate": sum(l1[model][q]["refusal"] for q in questions) / 15,
            "spearman_l0_vs_l1_violation": rho,
            "permutation_p": p,
        }
        pooled_x += x
        pooled_y += y
        for q in questions:
            high = l0[model][q] >= HIGH_L0
            quadrant["L0_high" if high else "L0_low",
                     "L1_any_violation" if l1[model][q]["any_violation"] else "L1_none"] += 1
            table.append({"model": model, "question": q, "l0_rate": l0[model][q], **l1[model][q]})
    rho, p = permutation_p(pooled_x, pooled_y, SEED + "|pooled")
    result = {
        "status": "exploratory descriptive comparison; see the confounds in compare.py",
        "per_model": per_model,
        "model_order": {
            "l0": sorted(models, key=lambda m: -per_model[m]["l0_any_problematic_rate"]),
            "l1_violation": sorted(models, key=lambda m: -per_model[m]["l1_violation_rate"]),
        },
        "pooled_45_pairs": {"spearman": rho, "permutation_p": p},
        "quadrant_l0_ge_0.5_vs_any_l1_violation": {
            "{}|{}".format(*key): count for key, count in sorted(quadrant.items())},
        "per_question": table,
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    for model, data in per_model.items():
        print("{:26s} L0 {:.1%}  L1 越界 {:.1%} (上界 {:.1%}, 拒绝 {:.1%})  rho {} p {}".format(
            model, data["l0_any_problematic_rate"], data["l1_violation_rate"],
            data["l1_violation_upper"], data["l1_refusal_rate"],
            "-" if data["spearman_l0_vs_l1_violation"] is None
            else "{:+.2f}".format(data["spearman_l0_vs_l1_violation"]),
            "-" if data["permutation_p"] is None else "{:.3f}".format(data["permutation_p"])))
    print("pooled rho {:+.2f} p {:.3f}".format(rho, p), dict(quadrant))


if __name__ == "__main__":
    main()
