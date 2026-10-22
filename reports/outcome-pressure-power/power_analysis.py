"""Power analysis for the outcome-pressure causal test. Pure computation, no API calls.

The paired induced/neutral design is sound -- L1_INDUCED is exactly
L1_OUTCOME_PRESSURE + L1_NEUTRAL and every other field is held equal -- but a design
can be valid and still be unable to answer its question. This script asks whether
15 questions x 16 samples can detect an outcome-pressure effect at all, using the
violation distribution the induced run actually produced.

Reads only the committed per-question counts. Run from the repository root:

    python reports/outcome-pressure-power/power_analysis.py
"""

import itertools
import json
import pathlib
import random
from math import comb

HERE = pathlib.Path("reports/outcome-pressure-power")
SEED = 20261022
ALPHA = 0.05
NSIM = 3000


def fisher_one_sided(a, b, c, d):
    """P(induced violations >= a) under equal rates, for a 2x2 of counts."""
    n, row1, col1 = a + b + c + d, a + b, a + c
    total = comb(n, row1)
    return sum(comb(col1, x) * comb(n - col1, row1 - x) / total
               for x in range(a, min(row1, col1) + 1))


def icc_and_design_effect(rates, m):
    """Between-question variance against the within-question binomial part."""
    n = len(rates)
    mu = sum(rates) / n
    between = sum((r - mu) ** 2 for r in rates) / (n - 1)
    within = mu * (1 - mu) / m if 0 < mu < 1 else 0.0
    if between + within == 0:
        return 0.0, 1.0, mu
    icc = max(0.0, (between - within) / (between + within))
    return icc, 1 + (m - 1) * icc, mu


def sign_permutation_p(diffs, observed):
    """Exact sign-flip permutation p over the non-tied question differences."""
    nonzero = [d for d in diffs if d != 0]
    if not nonzero:
        return 1.0
    if len(nonzero) > 18:                     # exact enumeration stays cheap below this
        nonzero = nonzero[:18]
    hit = 0
    total = 0
    for signs in itertools.product((1, -1), repeat=len(nonzero)):
        total += 1
        if sum(s * abs(d) for s, d in zip(signs, nonzero)) >= observed:
            hit += 1
    return hit / total


def simulate_power(counts, m, effect, rng, nsim=NSIM):
    """Neutral rate = induced rate x (1 - effect); test is the sign-flip permutation."""
    base = list(counts)
    wins = 0
    for _ in range(nsim):
        induced, neutral = [], []
        for c in base:
            p_i = c / m
            p_n = p_i * (1 - effect)
            induced.append(sum(rng.random() < p_i for _ in range(m)))
            neutral.append(sum(rng.random() < p_n for _ in range(m)))
        diffs = [a - b for a, b in zip(induced, neutral)]
        if sign_permutation_p(diffs, sum(diffs)) <= ALPHA:
            wins += 1
    return wins / nsim


def main():
    data = json.loads((HERE / "per-question-violations.json").read_text(encoding="utf-8"))
    m = data["samples_per_question"]
    questions = data["questions"]
    by_model = data["violations_by_question"]
    rng = random.Random(SEED)

    report = {
        "what": ("Can 15 questions x 16 samples detect an outcome-pressure effect on the "
                 "boundary axis? Computed from the induced run's own violation distribution."),
        "inputs": {"per_question_violations": "per-question-violations.json",
                   "samples_per_question": m, "alpha": ALPHA, "seed": SEED,
                   "simulations_per_cell": NSIM},
        "models": {},
    }

    for model, counts in by_model.items():
        series = [counts.get(q, 0) for q in questions]
        live = [q for q in questions if counts.get(q, 0)]
        rates = [c / m for c in series]
        icc, deff, mu = icc_and_design_effect(rates, m)
        n_total = len(questions) * m
        entry = {
            "total_violations": sum(series),
            "live_questions": live,
            "n_live": len(live),
            # A tied question contributes nothing to a paired test, so the number of
            # live questions caps how small the permutation p can ever be.
            "min_attainable_one_sided_p": 0.5 ** len(live) if live else None,
            "can_ever_reach_alpha": bool(live) and 0.5 ** len(live) <= ALPHA,
            "pooled_rate": round(mu, 4),
            "between_question_icc": round(icc, 3),
            "design_effect": round(deff, 1),
            "effective_sample_size": round(n_total / deff),
            "nominal_sample_size": n_total,
            "power_by_effect": {},
        }
        for effect in (0.25, 0.5, 0.75, 1.0):
            entry["power_by_effect"]["neutral_lower_by_{:.0%}".format(effect)] = round(
                simulate_power(series, m, effect, rng), 3)
        report["models"][model] = entry

    report["within_question_fisher"] = [
        {"induced": "{}/{}".format(a, m), "neutral": "{}/{}".format(b, m),
         "one_sided_p": round(fisher_one_sided(a, m - a, b, m - b), 4),
         "significant": fisher_one_sided(a, m - a, b, m - b) < ALPHA}
        for a, b in ((16, 8), (16, 4), (16, 0), (10, 5), (10, 2), (10, 0),
                     (7, 3), (7, 0), (4, 0), (3, 0))]

    # The share of questions capable of producing a violation at all, taken from the
    # most violation-prone arm so the projection is not pessimistic by construction.
    most_live = max(len(e["live_questions"]) for e in report["models"].values())
    observed_live_share = most_live / len(questions)
    report["design_tradeoff"] = {
        "budget_samples_per_model_per_condition": 240,
        "observed_live_question_share": round(observed_live_share, 3),
        "note": ("The unit of a paired test is the question, not the sample. Spreading the "
                 "same budget over more questions raises the number of live units, which is "
                 "what the attainable p depends on."),
        "rows": [],
    }
    for nq in (15, 30, 60, 120):
        live = round(nq * observed_live_share)
        report["design_tradeoff"]["rows"].append({
            "questions": nq, "samples_per_question": 240 // nq,
            "expected_live_questions": live,
            "min_attainable_one_sided_p": 0.5 ** live if live else None})

    out = HERE / "results.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("{:26s} {:>6s} {:>6s} {:>8s} {:>8s} {:>9s}".format(
        "model", "viol", "live", "min p", "DEFF", "eff. n"))
    for model, e in report["models"].items():
        print("{:26s} {:6d} {:6d} {:>8s} {:>8.1f} {:>9d}".format(
            model, e["total_violations"], e["n_live"],
            "{:.4f}".format(e["min_attainable_one_sided_p"]) if e["n_live"] else "n/a",
            e["design_effect"], e["effective_sample_size"]))
    print("\npower (sign-flip permutation, alpha={}):".format(ALPHA))
    for model, e in report["models"].items():
        print("  {:26s} {}".format(model, e["power_by_effect"]))
    print("\nwrote", out)


if __name__ == "__main__":
    main()
