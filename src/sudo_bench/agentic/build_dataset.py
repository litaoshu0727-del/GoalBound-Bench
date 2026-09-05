"""Build and validate the 15-item L1/L2 agentic dataset.

Run: ``python -m sudo_bench.agentic.build_dataset``

The generated JSONL preserves every L0 field, adds a validated ``agentic`` block,
and materializes model-visible prompts from ``public_facts``. Hidden
``ground_truth`` and judge rubrics are never interpolated into those prompts.
"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Mapping, Set

from .blocks import AGENTIC_BLOCKS

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_IN = REPO_ROOT / "questions.v3.jsonl"
DEFAULT_OUT = REPO_ROOT / "questions.v3.agentic.jsonl"
KNOWN_DETECTORS = {"present", "absent", "present_and_absent", "tool_called", "requires_judge"}
SOURCE_KINDS = {"output", "tool_field", "tool_any_field"}


class DatasetValidationError(ValueError):
    """Raised when an authored block is incomplete or internally inconsistent."""


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetValidationError(
                "{}:{} is not valid JSON: {}".format(path, line_number, exc)
            ) from exc
        if not isinstance(row, dict):
            raise DatasetValidationError(
                "{}:{} must contain a JSON object".format(path, line_number)
            )
        rows.append(row)
    return rows


def _require_text(value: Any, location: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DatasetValidationError("{} must be a non-empty string".format(location))


def _require_text_list(value: Any, location: str) -> None:
    if not isinstance(value, list) or not value:
        raise DatasetValidationError("{} must be a non-empty list".format(location))
    for index, text in enumerate(value):
        _require_text(text, "{}[{}]".format(location, index))


def _validate_tool(tool: Any, qid: str, index: int) -> str:
    location = "{}.l2_tools[{}]".format(qid, index)
    if not isinstance(tool, Mapping) or tool.get("type") != "function":
        raise DatasetValidationError("{} must be a function tool".format(location))
    function = tool.get("function")
    if not isinstance(function, Mapping):
        raise DatasetValidationError("{}.function must be an object".format(location))
    name = function.get("name")
    _require_text(name, "{}.function.name".format(location))
    _require_text(function.get("description"), "{}.function.description".format(location))
    schema = function.get("parameters")
    if not isinstance(schema, Mapping) or schema.get("type") != "object":
        raise DatasetValidationError(
            "{}.function.parameters must be an object schema".format(location)
        )
    properties = schema.get("properties")
    if not isinstance(properties, Mapping) or not properties:
        raise DatasetValidationError("{}.properties must be non-empty".format(location))
    for field_name, field_schema in properties.items():
        if not isinstance(field_schema, Mapping) or field_schema.get("type") != "string":
            raise DatasetValidationError(
                "{}.properties.{} must be a string schema".format(location, field_name)
            )
    required = schema.get("required")
    if not isinstance(required, list) or not set(required).issubset(properties):
        raise DatasetValidationError(
            "{}.required must reference declared properties".format(location)
        )
    if schema.get("additionalProperties") is not False:
        raise DatasetValidationError("{}.additionalProperties must be false".format(location))
    return str(name)


def _validate_source(
    source: Any, qid: str, channel_id: str, tool_fields: Mapping[str, Set[str]]
) -> None:
    if not isinstance(source, Mapping):
        raise DatasetValidationError("{}.{} source must be an object".format(qid, channel_id))
    kind = source.get("kind", "output")
    if kind not in SOURCE_KINDS:
        raise DatasetValidationError(
            "{}.{} has unknown source kind {!r}".format(qid, channel_id, kind)
        )
    if kind == "output":
        return
    tool_name = source.get("tool")
    if tool_name not in tool_fields:
        raise DatasetValidationError(
            "{}.{} references unknown tool {!r}".format(qid, channel_id, tool_name)
        )
    if kind == "tool_field" and source.get("field") not in tool_fields[tool_name]:
        raise DatasetValidationError(
            "{}.{} references an unknown tool field".format(qid, channel_id)
        )


def validate_block(qid: str, block: Mapping[str, Any]) -> None:
    """Validate one block before it can enter the generated benchmark."""

    levels = block.get("levels_supported")
    if not isinstance(levels, list) or set(levels) != {"L1", "L2"}:
        raise DatasetValidationError(
            "{} must support both L1 and L2 in this 15-item release".format(qid)
        )
    facts = block.get("public_facts")
    if not isinstance(facts, list) or not facts:
        raise DatasetValidationError("{}.public_facts must be non-empty".format(qid))
    for index, fact in enumerate(facts):
        _require_text(fact, "{}.public_facts[{}]".format(qid, index))
    ground_truth = block.get("ground_truth")
    if not isinstance(ground_truth, Mapping):
        raise DatasetValidationError("{}.ground_truth must be an object".format(qid))
    _require_text(ground_truth.get("fact"), "{}.ground_truth.fact".format(qid))
    _require_text(ground_truth.get("boundary"), "{}.ground_truth.boundary".format(qid))
    _require_text(block.get("task_prompt_l1"), "{}.task_prompt_l1".format(qid))
    _require_text(block.get("task_prompt_l2"), "{}.task_prompt_l2".format(qid))

    tools = block.get("l2_tools")
    if not isinstance(tools, list) or not tools:
        raise DatasetValidationError("{}.l2_tools must be non-empty".format(qid))
    tool_names: List[str] = []
    tool_fields: Dict[str, Set[str]] = {}
    for index, tool in enumerate(tools):
        name = _validate_tool(tool, qid, index)
        if name in tool_names:
            raise DatasetValidationError("{} repeats tool name {!r}".format(qid, name))
        tool_names.append(name)
        properties = tool["function"]["parameters"]["properties"]
        tool_fields[name] = set(properties)

    channels = block.get("violation_channels")
    if not isinstance(channels, list) or not channels:
        raise DatasetValidationError("{}.violation_channels must be non-empty".format(qid))
    channel_ids: Set[str] = set()
    levels_with_channels: Set[str] = set()
    for channel in channels:
        if not isinstance(channel, Mapping):
            raise DatasetValidationError("{} has a non-object channel".format(qid))
        channel_id = channel.get("id")
        _require_text(channel_id, "{}.channel.id".format(qid))
        if channel_id in channel_ids:
            raise DatasetValidationError("{} repeats channel id {!r}".format(qid, channel_id))
        channel_ids.add(str(channel_id))
        channel_levels = channel.get("levels")
        if not isinstance(channel_levels, list) or not channel_levels:
            raise DatasetValidationError("{}.{} must declare levels".format(qid, channel_id))
        if not set(channel_levels).issubset(levels):
            raise DatasetValidationError(
                "{}.{} declares unsupported levels".format(qid, channel_id)
            )
        levels_with_channels.update(channel_levels)
        detector = channel.get("detector")
        if not isinstance(detector, Mapping) or detector.get("type") not in KNOWN_DETECTORS:
            raise DatasetValidationError("{}.{} has an unknown detector".format(qid, channel_id))
        dtype = detector["type"]
        if dtype == "requires_judge":
            _require_text(detector.get("rubric"), "{}.{}.detector.rubric".format(qid, channel_id))
        elif dtype == "tool_called":
            if set(channel_levels) != {"L2"}:
                raise DatasetValidationError(
                    "{}.{} tool_called channels must be L2-only".format(qid, channel_id)
                )
            if detector.get("tool") not in tool_fields:
                raise DatasetValidationError(
                    "{}.{} references an unknown tool".format(qid, channel_id)
                )
        else:
            if dtype in {"present", "present_and_absent"}:
                _require_text_list(
                    detector.get("present_any_of"),
                    "{}.{}.detector.present_any_of".format(qid, channel_id),
                )
            if dtype in {"absent", "present_and_absent"}:
                _require_text_list(
                    detector.get("absent_all_of"),
                    "{}.{}.detector.absent_all_of".format(qid, channel_id),
                )
            if detector.get("judge_on_miss"):
                _require_text(
                    detector.get("rubric"),
                    "{}.{}.detector.rubric".format(qid, channel_id),
                )
            source = channel.get("source", {"kind": "output"})
            _validate_source(source, qid, str(channel_id), tool_fields)
            source_kind = source.get("kind", "output")
            expected_level = "L1" if source_kind == "output" else "L2"
            if set(channel_levels) != {expected_level}:
                raise DatasetValidationError(
                    "{}.{} source kind {!r} must be {}-only".format(
                        qid, channel_id, source_kind, expected_level
                    )
                )
    if levels_with_channels != set(levels):
        raise DatasetValidationError(
            "{} must have at least one channel for every supported level".format(qid)
        )

    success = block.get("success")
    if not isinstance(success, Mapping) or success.get("type") != "requires_judge":
        raise DatasetValidationError("{}.success must currently be judge-backed".format(qid))
    _require_text(success.get("rubric"), "{}.success.rubric".format(qid))


def _model_prompt(block: Mapping[str, Any], level: str) -> str:
    facts = "\n".join("- {}".format(fact) for fact in block["public_facts"])
    task = block["task_prompt_{}".format(level.lower())]
    return "背景事实：\n{}\n\n任务：\n{}".format(facts, task)


def _supports(block: Mapping[str, Any], level: str) -> bool:
    return level in block.get("levels_supported", [])


def _programmatic_channels(block: Mapping[str, Any], level: str) -> List[Mapping[str, Any]]:
    return [
        channel
        for channel in block.get("violation_channels", [])
        if level in channel.get("levels", [])
        and channel.get("detector", {}).get("type") != "requires_judge"
    ]


def _needs_judge_fallback(block: Mapping[str, Any], level: str) -> bool:
    for channel in block.get("violation_channels", []):
        if level not in channel.get("levels", []):
            continue
        detector = channel.get("detector", {})
        if detector.get("type") == "requires_judge" or detector.get("judge_on_miss"):
            return True
    return False


def _coverage_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {"items": len(rows)}
    for level in ("L1", "L2"):
        supported = [row for row in rows if _supports(row["agentic"], level)]
        signalled = [row for row in supported if _programmatic_channels(row["agentic"], level)]
        judge_only = [row for row in supported if row not in signalled]
        judge_free = [
            row
            for row in supported
            if _programmatic_channels(row["agentic"], level)
            and not _needs_judge_fallback(row["agentic"], level)
        ]
        key = level.lower()
        summary["{}_supported".format(key)] = len(supported)
        summary["{}_programmatic_signal".format(key)] = len(signalled)
        summary["{}_judge_only".format(key)] = len(judge_only)
        summary["{}_declared_judge_free".format(key)] = len(judge_free)
        summary["{}_judge_only_ids".format(key)] = sorted(row["id"] for row in judge_only)
    summary["ground_truth_complete"] = sum(
        bool(row["agentic"].get("ground_truth", {}).get("boundary")) for row in rows
    )
    return summary


def _print_summary(out_path: Path, summary: Mapping[str, Any]) -> None:
    print("wrote {} items -> {}".format(summary["items"], out_path))
    print(
        "  ground truth complete:       {}/{}".format(
            summary["ground_truth_complete"], summary["items"]
        )
    )
    for level in ("L1", "L2"):
        key = level.lower()
        print(
            "  {} authored:                 {}/{}".format(
                level, summary["{}_supported".format(key)], summary["items"]
            )
        )
        print(
            "  {} programmatic signal:      {}/{}; judge-only: {}; declared judge-free: {}".format(
                level,
                summary["{}_programmatic_signal".format(key)],
                summary["{}_supported".format(key)],
                summary["{}_judge_only".format(key)],
                summary["{}_declared_judge_free".format(key)],
            )
        )
        print(
            "  {} judge-only items:         {}".format(
                level, ", ".join(summary["{}_judge_only_ids".format(key)]) or "-"
            )
        )
    print(
        "  Static counts describe authored detector capability, not empirical "
        "sample-level auto-resolution."
    )


def build(in_path: Path = DEFAULT_IN, out_path: Path = DEFAULT_OUT) -> Dict[str, Any]:
    rows = _load_jsonl(in_path)
    ids = {row.get("id") for row in rows}
    if None in ids or len(ids) != len(rows):
        raise DatasetValidationError("input question ids must be present and unique")
    missing_blocks = sorted(ids - set(AGENTIC_BLOCKS))
    orphan_blocks = sorted(set(AGENTIC_BLOCKS) - ids)
    if missing_blocks:
        raise DatasetValidationError(
            "questions without an agentic block: {}".format(missing_blocks)
        )
    if orphan_blocks:
        raise DatasetValidationError(
            "agentic blocks with no matching question: {}".format(orphan_blocks)
        )
    for row in rows:
        qid = row["id"]
        block = deepcopy(AGENTIC_BLOCKS[qid])
        validate_block(qid, block)
        block["prompt_l1"] = _model_prompt(block, "L1")
        block["prompt_l2"] = _model_prompt(block, "L2")
        row["agentic"] = block
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8"
    )
    summary = _coverage_summary(rows)
    _print_summary(out_path, summary)
    return summary


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="in_path", type=Path, default=DEFAULT_IN)
    parser.add_argument("--out", dest="out_path", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    build(args.in_path, args.out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
