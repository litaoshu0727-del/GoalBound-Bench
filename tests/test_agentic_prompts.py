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
