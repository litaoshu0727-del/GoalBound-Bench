"""Offline sensitivity analysis from public historical counts; no model calls.

Does not read the halted paired session or change its frozen analysis plan.
Uses two-sided question sign flips and Holm adjustment across models. Smoothed
rates are alternative fixed assumptions, not posterior-predictive power estimates.
"""

import argparse
import hashlib
import json
import math
import random
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = 20261022
ALPHA = 0.05
NSIM = 3000
EFFECTS = (0.0, 0.25, 0.5, 0.75, 1.0)
SCENARIOS = {
    "empirical_plugin": 0.0,
    "jeffreys_mean_sensitivity": 0.5,
    "laplace_mean_sensitivity": 1.0,
}


def validate_input(data):
    """Reject missing/extra questions and invalid counts instead of filling zeros."""
    m = data["samples_per_question"]
    if type(m) is not int or m < 1:
        raise ValueError("samples_per_question must be a positive integer")
    questions = data["questions"]
    if (not isinstance(questions, list) or not questions
            or any(not isinstance(q, str) or not q for q in questions)
            or len(set(questions)) != len(questions)):
        raise ValueError("questions must contain distinct non-empty ids")
    models = data["violations_by_question"]
    if not isinstance(models, dict) or not models:
        raise ValueError("at least one model is required")
    for model, counts in models.items():
        if set(counts) != set(questions):
            raise ValueError("{}: missing or extra question counts".format(model))
        if any(type(c) is not int or not 0 <= c <= m for c in counts.values()):
            raise ValueError("{}: counts must be integers in [0, m]".format(model))
        if data["totals"].get(model) != sum(counts.values()):
            raise ValueError("{}: total does not match counts".format(model))


def minimum_two_sided_p(nonzero_questions):
    """Best possible p for k nonzero differences, all in the same direction."""
    if type(nonzero_questions) is not int or nonzero_questions < 0:
        raise ValueError("nonzero_questions must be a non-negative integer")
    return min(1.0, 2.0 / (2 ** nonzero_questions))


@lru_cache(maxsize=32768)
def _sign_flip_tail(magnitudes, observed):
    # Exact subset-sum counts: each subset selects the negative signs.
    # Signed sum = total - 2 * subset_sum. No questions are truncated.
    frequencies = [1]
    for value in magnitudes:
        previous = frequencies
        frequencies = previous + [0] * value
        for subtotal, frequency in enumerate(previous):
            frequencies[subtotal + value] += frequency
    total = sum(magnitudes)
    extreme = sum(f for s, f in enumerate(frequencies) if abs(total - 2 * s) >= observed)
    return extreme / (2 ** len(magnitudes))


def sign_flip_p(differences):
    """Exact two-sided sign-flip p for integer count differences at equal n."""
    differences = tuple(differences)
    if any(type(d) is not int for d in differences):
        raise ValueError("differences must be integer counts with equal denominators")
    magnitudes = tuple(sorted(abs(d) for d in differences if d))
    return _sign_flip_tail(magnitudes, abs(sum(differences)))


def holm(pvalues):
    """Holm-adjusted p-values for the full supplied hypothesis family."""
    if any(not math.isfinite(p) or not 0 <= p <= 1 for p in pvalues.values()):
        raise ValueError("p-values must be finite and in [0, 1]")
    adjusted, previous = {}, 0.0
    for index, (key, value) in enumerate(sorted(pvalues.items(), key=lambda item: item[1])):
        previous = max(previous, min(1.0, (len(pvalues) - index) * value))
        adjusted[key] = previous
    return adjusted


def fisher_two_sided(a, b, c, d):
    """Sum hypergeometric tables with probability <= the observed table's.

    Integer weights avoid underflow and floating-point ties. Used only for
    hypothetical examples, not outcomes of the halted paired session.
    """
    if any(type(x) is not int or x < 0 for x in (a, b, c, d)):
        raise ValueError("table entries must be non-negative integers")
    n, row1, col1 = a + b + c + d, a + b, a + c
    observed_weight = math.comb(col1, a) * math.comb(n - col1, row1 - a)
    weights = (
        math.comb(col1, x) * math.comb(n - col1, row1 - x)
        for x in range(max(0, row1 + col1 - n), min(row1, col1) + 1)
    )
    return sum(w for w in weights if w <= observed_weight) / math.comb(n, row1)


def simulation_interval(k, n):
    """Rejection frequency with a Monte Carlo Wilson 95% interval, not an effect CI."""
    z, rate = 1.959963984540054, k / n
    denominator = 1 + z * z / n
    centre = (rate + z * z / (2 * n)) / denominator
    half = z * math.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / denominator
    return {
        "rejections": k, "simulations": n, "rate": rate,
        "monte_carlo_wilson_ci95": [max(0.0, centre - half), min(1.0, centre + half)],
    }


def simulate_family(counts_by_model, m, effect, pseudocount, seed, nsim=NSIM):
    """Simulate all models before applying Holm in each replicate.

    p_i=(count+a)/(m+2a); p_n=p_i*(1-effect). Independent Bernoulli draws
    conditional on these fixed rates are an assumption. effect=0 checks the null.
    """
    if not 0 <= effect <= 1 or pseudocount < 0 or nsim < 1:
        raise ValueError("invalid simulation parameters")
    probabilities = {
        model: [(c + pseudocount) / (m + 2 * pseudocount) for c in counts]
        for model, counts in counts_by_model.items()
    }
    rng = random.Random(seed)
    wins = {model: {"unadjusted": 0, "holm": 0} for model in probabilities}
    for _ in range(nsim):
        pvalues = {}
        for model, rates in probabilities.items():
            differences = []
            for p_i in rates:
                p_n = p_i * (1 - effect)
                a = sum(rng.random() < p_i for _ in range(m))
                b = sum(rng.random() < p_n for _ in range(m))
                differences.append(a - b)
            pvalues[model] = sign_flip_p(differences)
        adjusted = holm(pvalues)
        for model in probabilities:
            wins[model]["unadjusted"] += pvalues[model] <= ALPHA
            wins[model]["holm"] += adjusted[model] <= ALPHA
    return {
        model: {name: simulation_interval(k, nsim) for name, k in counts.items()}
        for model, counts in wins.items()
    }


def build_report(data, nsim=NSIM):
    validate_input(data)
    m, questions = data["samples_per_question"], data["questions"]
    by_model = {model: [counts[q] for q in questions]
                for model, counts in data["violations_by_question"].items()}
    report = {
        "schema_version": 2,
        "revision_date": "2026-10-31",
        "status": "exploratory_offline_sensitivity_not_a_paired_result",
        "inputs": {"samples_per_question": m, "questions": len(questions),
                   "models": len(by_model), "alpha": ALPHA, "seed": SEED,
                   "simulations_per_scenario_effect": nsim},
        "test": {
            "statistic": "absolute sum of question count differences; equal n per question",
            "alternative": "two-sided",
            "multiplicity": "Holm across all models within each simulated replicate",
            "protocol": "annotation/preregistration/agentic-l1-paired-induced-neutral-v1.json",
            "scope": "Matches the protocol p-value criteria only; CI and unresolved gates omitted.",
        },
        "observed_induced": {},
        "zero_count_uncertainty": {
            "count": 0, "n": m,
            "binomial_one_sided_95_upper": -math.expm1(math.log(0.05) / m),
            "assumption": "iid Bernoulli generation and error-free boundary labels for one item",
        },
        "scenarios": {},
    }
    for model, counts in by_model.items():
        positive = [q for q, c in zip(questions, counts) if c]
        report["observed_induced"][model] = {
            "violations": sum(counts), "samples": m * len(questions),
            "observed_positive_questions": positive,
            "observed_zero_questions": len(questions) - len(positive),
            "hypothetical_neutral_all_zero_two_sided_p": sign_flip_p(counts),
            "scope": "Historical induced counts held fixed; neutral all-zero is hypothetical.",
        }
    for name, pseudocount in SCENARIOS.items():
        curves = {}
        for effect in EFFECTS:
            curves["neutral_lower_by_{:.0%}".format(effect)] = simulate_family(
                by_model, m, effect, pseudocount, "{}|{}|{}".format(SEED, name, effect), nsim)
        report["scenarios"][name] = {
            "pseudocount_per_outcome": pseudocount,
            "induced_probability_rule": "(count + a) / (m + 2*a)",
            "neutral_probability_rule": "p_induced * (1 - relative_reduction)",
            "expected_induced_violations": {
                model: sum((c + pseudocount) / (m + 2 * pseudocount) * m for c in counts)
                for model, counts in by_model.items()
            },
            "interpretation": (
                "Fixed-rate sensitivity assumption, not posterior prediction. "
                "Zero effect reports null rejection; others report conditional power."
            ),
            "rejection_frequencies": curves,
        }
    report["hypothetical_fisher_examples"] = {
        "interpretation": (
            "Alternative hypothetical outcomes, not a testing family or evidence; "
            "unadjusted p-values do not establish power or replace the frozen primary."
        ),
        "tables": [
            {"induced_violations": a, "neutral_violations": b, "n_per_condition": m,
             "two_sided_p_unadjusted": fisher_two_sided(a, m - a, b, m - b)}
            for a, b in ((16, 8), (16, 4), (16, 0), (10, 5), (10, 2), (10, 0),
                         (7, 3), (7, 0), (4, 0), (3, 0)) if max(a, b) <= m
        ],
    }
    report["discreteness_only"] = {
        "interpretation": "Best-case p given k nonzero differences; future k is unknown.",
        "minimum_two_sided_p_by_k": {
            str(k): minimum_two_sided_p(k) for k in range(len(questions) + 1)
        },
        "allocation_options_without_power_claim": [
            {"questions": nq, "samples_per_question": 240 // nq, "power_estimated": False}
            for nq in (15, 30, 60, 120)
        ],
    }
    report["withdrawn_v1_claims"] = {
        "mathematical_impossibility": "Empirical zero counts do not establish zero probabilities.",
        "icc_design_effect_effective_n": (
            "Withdrawn: nonstandard estimator; no substitute ICC fitted."
        ),
        "expansion_guarantees_power": "Observed-positive share changes with samples per question.",
        "within_item_half_reduction_significant": "10/16 vs 5/16 is not significant at 0.05.",
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Recompute and compare without writing")
    args = parser.parse_args()
    source = HERE / "per-question-violations.json"
    report = build_report(json.loads(source.read_text(encoding="utf-8")))
    report["inputs"]["per_question_violations_sha256"] = hashlib.sha256(
        source.read_bytes()).hexdigest()
    output = HERE / "results.json"
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if output.read_text(encoding="utf-8") != payload:
            raise SystemExit("offline sensitivity report is stale")
        print("offline sensitivity report reproduced exactly")
    else:
        output.write_text(payload, encoding="utf-8")
        print("wrote", output)
    for name, scenario in report["scenarios"].items():
        print(name)
        for effect, models in scenario["rejection_frequencies"].items():
            print(" ", effect, {model: round(values["holm"]["rate"], 4)
                                for model, values in models.items()})


if __name__ == "__main__":
    main()
