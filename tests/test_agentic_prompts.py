"""The L1 experimental arms must isolate outcome pressure as the manipulation."""

from pathlib import Path

import yaml

from sudo_bench.agentic.prompts import (
    L1_COMMON_INSTRUCTION,
    L1_INDUCED,
    L1_NEUTRAL,
    L1_OUTCOME_PRESSURE,
)

ROOT = Path(__file__).resolve().parents[1]


def _config(name: str):
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def test_induced_differs_from_neutral_only_by_pressure_prefix():
    assert L1_NEUTRAL == L1_COMMON_INSTRUCTION
    assert L1_INDUCED == L1_OUTCOME_PRESSURE + L1_NEUTRAL
    assert "利用他人" not in L1_INDUCED
    assert "不要补充背景中没有提供的事实" in L1_NEUTRAL


def test_smoke_configs_are_strictly_matched():
    induced = _config("config.agentic-l1-smoke.yaml")
    neutral = _config("config.agentic-l1-smoke-neutral.yaml")

    assert induced["system_prompt"] == L1_INDUCED
    assert neutral["system_prompt"] == L1_NEUTRAL

    for field in ("system_prompt", "output", "manifest"):
        induced.pop(field)
        neutral.pop(field)
    assert induced == neutral


def test_full_induced_config_uses_the_same_prompt_constant():
    config = _config("config.agentic-l1-opus-5-induced.yaml")
    assert config["system_prompt"] == L1_INDUCED


def test_smoke_judge_configs_are_strictly_matched_and_cross_vendor():
    induced = _config("config.agentic-l1-smoke-judge-induced.yaml")
    neutral = _config("config.agentic-l1-smoke-judge-neutral.yaml")

    assert induced["model"].split("/", 1)[0] != "anthropic"
    assert neutral["model"] == induced["model"]

    for field in ("output", "manifest"):
        induced.pop(field)
        neutral.pop(field)
    assert induced == neutral


def test_v6_recalibration_configs_are_matched_and_do_not_overwrite_v5():
    induced = _config("config.agentic-l1-smoke-judge-v6-induced.yaml")
    neutral = _config("config.agentic-l1-smoke-judge-v6-neutral.yaml")
    old_induced = _config("config.agentic-l1-smoke-judge-induced.yaml")
    old_neutral = _config("config.agentic-l1-smoke-judge-neutral.yaml")

    assert induced["model"].split("/", 1)[0] != "anthropic"
    assert induced["output"] != old_induced["output"]
    assert neutral["output"] != old_neutral["output"]
    assert induced["overwrite"] is False
    assert neutral["overwrite"] is False

    for field in ("output", "manifest"):
        induced.pop(field)
        neutral.pop(field)
    assert induced == neutral


# --- full-scale Avg@16 arms: the matched induced/neutral pairs ----------------


# Deviation 2026-10-09 (annotation/preregistration/agentic-l1-paired-induced-neutral-v1.json):
# no OpenRouter endpoint for Opus 5 accepts `temperature` any more, so the Opus neutral
# arm and the fresh Opus induced arm leave it unset. The 2026-09-12 Opus induced config
# keeps 1.0. This is the only field allowed to differ, and only for these Opus configs.
TEMPERATURE_UNSET = {"config.agentic-l1-opus-5-neutral.yaml",
                     "config.agentic-l1-opus-5-induced-r2.yaml"}


def _without_documented_deviation(path, config):
    if path in TEMPERATURE_UNSET:
        assert config.pop("temperature") is None, path
    elif "temperature" in config and path.startswith("config.agentic-l1-opus-5"):
        assert config.pop("temperature") == 1.0, path
    return config


FULL_ARM_PAIRS = [
    ("config.agentic-l1-opus-5-induced.yaml", "config.agentic-l1-opus-5-neutral.yaml"),
    ("config.agentic-l1-grok-4.6-induced.yaml", "config.agentic-l1-grok-4.6-neutral.yaml"),
    ("config.agentic-l1-gemini-3.7-flash-induced.yaml",
     "config.agentic-l1-gemini-3.7-flash-neutral.yaml"),
]


def test_full_arms_are_matched_pairs_differing_only_by_outcome_pressure():
    """The causal claim rests on this: the two arms of a model must differ in the
    system prompt and nowhere else. Anything else that drifts -- max_tokens, sampling,
    the dataset -- would confound outcome pressure with a generation change."""
    for induced_path, neutral_path in FULL_ARM_PAIRS:
        induced = _config(induced_path)
        neutral = _config(neutral_path)

        assert induced["system_prompt"] == L1_INDUCED, induced_path
        assert neutral["system_prompt"] == L1_NEUTRAL, neutral_path

        for field in ("system_prompt", "output", "manifest"):
            induced.pop(field)
            neutral.pop(field)
        induced = _without_documented_deviation(induced_path, induced)
        neutral = _without_documented_deviation(neutral_path, neutral)
        assert induced == neutral, "{} and {} diverged".format(induced_path, neutral_path)


def test_every_full_arm_shares_one_generation_setup():
    """Across models, only `model` may differ: the three induced arms are compared with
    each other, so a per-model max_tokens or sample count would break that too. The one
    documented exception is the unset temperature on the two newer Opus configs."""
    scored = []
    for induced_path, neutral_path in FULL_ARM_PAIRS:
        for path in (induced_path, neutral_path):
            config = _config(path)
            expected = None if path in TEMPERATURE_UNSET else 1.0
            assert config.get("temperature") == expected, path
            scored.append({k: config[k] for k in
                           ("max_tokens", "samples_per_question",
                            "require_parameters", "dataset", "reasoning_effort")
                           if k in config})
    assert all(s == scored[0] for s in scored), scored


def test_full_arm_models_are_the_three_preregistered_ones():
    models = {_config(p)["model"] for pair in FULL_ARM_PAIRS for p in pair}
    assert models == {"anthropic/claude-opus-5", "x-ai/grok-4.6", "google/gemini-3.7-flash"}


# --- paired session: fresh induced arms generated alongside the neutral arms --------

PAIRED_R2 = [
    ("config.agentic-l1-opus-5-induced.yaml", "config.agentic-l1-opus-5-induced-r2.yaml",
     "config.agentic-l1-opus-5-neutral.yaml"),
    ("config.agentic-l1-grok-4.6-induced.yaml", "config.agentic-l1-grok-4.6-induced-r2.yaml",
     "config.agentic-l1-grok-4.6-neutral.yaml"),
    ("config.agentic-l1-gemini-3.7-flash-induced.yaml",
     "config.agentic-l1-gemini-3.7-flash-induced-r2.yaml",
     "config.agentic-l1-gemini-3.7-flash-neutral.yaml"),
]


def test_fresh_induced_arms_copy_the_original_except_output_paths():
    for original_path, fresh_path, _ in PAIRED_R2:
        original = _config(original_path)
        fresh = _config(fresh_path)
        assert fresh["output"] != original["output"]
        assert fresh["manifest"] != original["manifest"]
        assert fresh["overwrite"] is False
        for field in ("output", "manifest"):
            original.pop(field)
            fresh.pop(field)
        original = _without_documented_deviation(original_path, original)
        fresh = _without_documented_deviation(fresh_path, fresh)
        assert fresh == original, fresh_path


def test_fresh_induced_and_neutral_arms_differ_only_by_outcome_pressure():
    for _, fresh_path, neutral_path in PAIRED_R2:
        fresh = _config(fresh_path)
        neutral = _config(neutral_path)
        assert fresh.get("temperature") == neutral.get("temperature"), fresh_path
        assert fresh["system_prompt"] == L1_INDUCED
        assert neutral["system_prompt"] == L1_NEUTRAL
        for field in ("system_prompt", "output", "manifest"):
            fresh.pop(field)
            neutral.pop(field)
        assert fresh == neutral, "{} and {} diverged".format(fresh_path, neutral_path)
