"""Offline statistical reference cases, exact-test oracles and report provenance."""

import copy
import hashlib
import importlib.util
import itertools
import json
import random
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports/outcome-pressure-power"
SPEC = importlib.util.spec_from_file_location("power_sensitivity", REPORT / "power_analysis.py")
power = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(power)


@pytest.mark.parametrize("differences,expected", [
    ([], 1.0), ([0, 0, 0], 1.0), ([1, 1, 1, 1], 0.125),
    ([-1, -1, -1, -1], 0.125), ([1, 1, 1, 1, 1, 1], 0.03125),
    ([1, -1], 1.0), ([1] * 19, 2 / 2**19),
])
def test_two_sided_sign_flip_reference_cases_and_no_truncation(differences, expected):
    assert power.sign_flip_p(differences) == expected


def test_subset_sum_matches_exhaustive_randomization():
    rng = random.Random(12)
    for n in range(1, 9):
        for _ in range(5):
            differences = [rng.randrange(-5, 6) for _ in range(n)]
            observed = abs(sum(differences))
            extreme = sum(
                abs(sum(s * d for s, d in zip(signs, differences))) >= observed
                for signs in itertools.product((-1, 1), repeat=n)
            )
            assert power.sign_flip_p(differences) == extreme / 2**n
            assert power.sign_flip_p(differences + [0, 0]) == extreme / 2**n


def test_fisher_matches_documented_probability_ordering_reference():
    # SciPy documentation's [[6, 2], [1, 4]] example, independent of this implementation.
    assert power.fisher_two_sided(6, 2, 1, 4) == pytest.approx(0.10256410256410257)
    assert power.fisher_two_sided(1, 4, 6, 2) == pytest.approx(0.10256410256410257)
    assert power.fisher_two_sided(10, 6, 5, 11) > 0.05
    assert power.fisher_two_sided(0, 0, 0, 0) == 1.0
    assert power.fisher_two_sided(0, 16, 0, 16) == 1.0


def test_holm_uses_the_entire_family_and_preserves_ties():
    assert power.holm({"a": 0.01, "b": 0.04, "c": 0.03}) == pytest.approx(
        {"a": 0.03, "b": 0.06, "c": 0.06})
    assert power.holm({"a": 0.02, "b": 0.02, "c": 0.5}) == pytest.approx(
        {"a": 0.06, "b": 0.06, "c": 0.5})
    assert power.minimum_two_sided_p(4) == 0.125
    assert power.minimum_two_sided_p(0) == 1.0


def test_simulation_null_and_large_shift_have_known_degenerate_outcomes():
    models = {"a": [16] * 15, "b": [16] * 15, "c": [16] * 15}
    null = power.simulate_family(models, 16, 0, 0, "null", nsim=4)
    shifted = power.simulate_family(models, 16, 1, 0, "shifted", nsim=4)
    for model in models:
        assert null[model]["holm"]["rejections"] == 0
        assert shifted[model]["holm"]["rejections"] == 4


def test_simulation_is_reproducible_and_holm_never_adds_rejections():
    args = ({"a": [4] * 8, "b": [2] * 8}, 8, 0.5, 0.5, "repeat")
    first = power.simulate_family(*args, nsim=20)
    assert first == power.simulate_family(*args, nsim=20)
    for model in first.values():
        assert model["holm"]["rejections"] <= model["unadjusted"]["rejections"]
        for result in model.values():
            lo, hi = result["monte_carlo_wilson_ci95"]
            assert 0 <= lo <= result["rate"] <= hi <= 1


@pytest.mark.parametrize("mutation", ["missing", "extra", "negative", "fractional", "total"])
def test_missing_or_invalid_counts_fail_instead_of_becoming_zero(mutation):
    data = {"samples_per_question": 16, "questions": ["q1", "q2"],
            "violations_by_question": {"model": {"q1": 0, "q2": 1}},
            "totals": {"model": 1}}
    counts = data["violations_by_question"]["model"]
    if mutation == "missing":
        counts.pop("q1")
    elif mutation == "extra":
        counts["q3"] = 0
    elif mutation == "negative":
        counts["q1"] = -1
    elif mutation == "fractional":
        counts["q1"] = 0.5
    else:
        data["totals"]["model"] = 99
    with pytest.raises(ValueError):
        power.validate_input(data)


def test_public_counts_match_frozen_v2_totals_and_report_input_hash():
    source = REPORT / "per-question-violations.json"
    data = json.loads(source.read_text())
    result = json.loads((REPORT / "results.json").read_text())
    frozen = json.loads((ROOT / "reports/l1-mvp-induced-avg16/rescoring-v2/"
                         "judged-results-v2.json").read_text())
    protocol = json.loads((ROOT / result["test"]["protocol"]).read_text())
    power.validate_input(data)
    assert result["inputs"]["per_question_violations_sha256"] == hashlib.sha256(
        source.read_bytes()).hexdigest()
    assert "two-sided" in protocol["analysis_plan"]["primary"]["test"]
    assert "Holm" in protocol["analysis_plan"]["primary"]["multiplicity"]
    assert result["test"]["alternative"] == "two-sided"
    for model, counts in data["violations_by_question"].items():
        assert sum(counts.values()) == frozen["arms"][model]["v2"]["counts"]["boundary"][
            "VIOLATION"]
        row = result["observed_induced"][model]
        assert row["violations"] == sum(counts.values())
        assert row["hypothetical_neutral_all_zero_two_sided_p"] == power.sign_flip_p(
            counts.values())
    assert result["zero_count_uncertainty"]["binomial_one_sided_95_upper"] == pytest.approx(
        1 - 0.05**(1 / 16))
    for scenario in result["scenarios"].values():
        for models in scenario["rejection_frequencies"].values():
            for model in models.values():
                assert model["holm"]["rejections"] <= model["unadjusted"]["rejections"]
                for row in model.values():
                    assert row["rate"] == row["rejections"] / row["simulations"]


def test_report_generation_accepts_clean_checkout_data_without_private_runs():
    data = {"samples_per_question": 16, "questions": ["q1", "q2"],
            "violations_by_question": {"model": {"q1": 0, "q2": 1}},
            "totals": {"model": 1}}
    before = copy.deepcopy(data)
    result = power.build_report(data, nsim=2)
    assert data == before
    assert result["status"] == "exploratory_offline_sensitivity_not_a_paired_result"
    assert set(result["scenarios"]) == set(power.SCENARIOS)
    assert result["scenarios"]["jeffreys_mean_sensitivity"]["expected_induced_violations"][
        "model"] > 1
