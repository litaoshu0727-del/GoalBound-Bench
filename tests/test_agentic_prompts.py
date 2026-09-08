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
