import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sudo_bench.benchmark import BenchmarkError, Question, load_questions

ANNOTATION_SCHEMA_VERSION = 1
SEVERITIES = ("none", "low", "medium", "high")
CONFIDENCE_LEVELS = ("low", "medium", "high")
BEHAVIOR_TYPES = (
    "none",
    "deception",
    "fabrication",
    "credit_theft",
    "manipulation",
    "coercion",
    "privacy_or_confidentiality",
    "illegal_or_fraudulent",
    "harm_to_others",
    "conflict_of_interest",
    "other",
)
AMBIGUITY_FLAGS = (
    "insufficient_context",
    "multiple_problematic_options",
    "leading_stem",
    "unbalanced_options",
    "unnecessary_sensitive_attribute",
    "unclear_wording",
    "other",
)

_PACKET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class AnnotationError(BenchmarkError):
    pass


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _stable_hash(*parts: object) -> str:
    payload = "\0".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _option_label(index: int) -> str:
    return chr(ord("A") + index)


def _check_output_paths(paths: Iterable[Path], overwrite: bool) -> None:
    if overwrite:
        return
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise AnnotationError("output already exists: {}".format(existing[0]))


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=".{}-".format(path.name),
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        os.replace(str(temporary_path), str(path))
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    content = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    _atomic_write_text(path, content)


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AnnotationError("cannot read {}: {}".format(path, exc)) from exc
    except json.JSONDecodeError as exc:
        raise AnnotationError("invalid JSON in {}: {}".format(path, exc)) from exc
    if not isinstance(value, dict):
        raise AnnotationError("{} must contain a JSON object".format(path))
    return value


def _read_jsonl(path: Path) -> List[Tuple[int, Mapping[str, Any]]]:
    rows: List[Tuple[int, Mapping[str, Any]]] = []
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise AnnotationError("cannot read {}: {}".format(path, exc)) from exc
    with handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise AnnotationError(
                    "{}:{}: invalid JSON: {}".format(path, line_number, exc)
                ) from exc
            if not isinstance(row, dict):
                raise AnnotationError("{}:{}: expected a JSON object".format(path, line_number))
            rows.append((line_number, row))
    if not rows:
        raise AnnotationError("{} contains no annotation rows".format(path))
    return rows


def _structured_questions(dataset: Path) -> Tuple[List[Question], str]:
    try:
        dataset_content = dataset.read_bytes()
    except OSError as exc:
        raise AnnotationError("cannot read dataset {}: {}".format(dataset, exc)) from exc
    questions = load_questions(dataset)
    legacy = next((question.id for question in questions if not question.is_structured), None)
    if legacy is not None:
        raise AnnotationError(
            "blind annotation requires structured stem/options data; '{}' is legacy".format(legacy)
        )
    return questions, _sha256_bytes(dataset_content)


def _public_item(
    question: Question,
    packet_id: str,
    item_id: str,
    seed: int,
) -> Tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    options = sorted(
        question.options,
        key=lambda option: _stable_hash(seed, question.id, option.id, "annotation-option"),
    )
    public_options = []
    option_map: Dict[str, str] = {}
    for index, option in enumerate(options):
        label = _option_label(index)
        public_options.append({"label": label, "text": option.text})
        option_map[label] = option.id

    packet_row = {
        "schema_version": ANNOTATION_SCHEMA_VERSION,
        "packet_id": packet_id,
        "item_id": item_id,
        "stem": question.stem,
        "options": public_options,
    }
    template_row = {
        "schema_version": ANNOTATION_SCHEMA_VERSION,
        "packet_id": packet_id,
        "item_id": item_id,
        "annotator_id": "",
        "problematic_option_labels": [],
        "primary_risk_option_label": None,
        "behavior_types": [],
        "severity": "",
        "confidence": "",
        "ambiguity_flags": [],
        "missing_context": "",
        "rationale": "",
    }
    private_row = {
        "item_id": item_id,
        "question_id": question.id,
        "option_map": option_map,
        "original_target_option_id": question.target_option_id,
        "original_metadata": dict(question.metadata),
    }
    return packet_row, template_row, private_row


def export_annotation_packet(
    dataset: Path,
    output_dir: Path,
    seed: int,
    packet_id: Optional[str] = None,
    overwrite: bool = False,
) -> Mapping[str, Any]:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise AnnotationError("seed must be an integer")
    questions, dataset_sha256 = _structured_questions(dataset.resolve())
    if packet_id is None:
        packet_id = "blind-{}".format(_stable_hash(dataset_sha256, seed)[:12])
    if not isinstance(packet_id, str) or not _PACKET_ID.fullmatch(packet_id):
        raise AnnotationError(
            "packet_id must be 1-64 letters, numbers, dots, underscores, or hyphens"
        )

    output_dir = output_dir.resolve()
    packet_path = output_dir / "public" / "packet.jsonl"
    template_path = output_dir / "public" / "response-template.jsonl"
    mapping_path = output_dir / "private" / "mapping.json"
    responses_dir = output_dir / "responses"
    _check_output_paths((packet_path, template_path, mapping_path), overwrite)

    ordered_questions = sorted(
        questions,
        key=lambda question: _stable_hash(seed, question.id, "annotation-item"),
    )
    packet_rows = []
    template_rows = []
    private_rows = []
    for question in ordered_questions:
        item_id = "item-{}".format(
            _stable_hash(dataset_sha256, seed, question.id, "anonymous-item")[:12]
        )
        packet_row, template_row, private_row = _public_item(
            question,
            packet_id,
            item_id,
            seed,
        )
        packet_rows.append(packet_row)
        template_rows.append(template_row)
        private_rows.append(private_row)

    mapping = {
        "schema_version": ANNOTATION_SCHEMA_VERSION,
        "packet_id": packet_id,
        "source_dataset_sha256": dataset_sha256,
        "source_question_count": len(questions),
        "seed": seed,
        "items": private_rows,
    }
    _write_jsonl(packet_path, packet_rows)
    _write_jsonl(template_path, template_rows)
    _write_json(mapping_path, mapping)
    responses_dir.mkdir(parents=True, exist_ok=True)

    return {
        "packet_id": packet_id,
        "items": len(questions),
        "packet": str(packet_path),
        "response_template": str(template_path),
        "private_mapping": str(mapping_path),
        "responses_dir": str(responses_dir),
        "source_dataset_sha256": dataset_sha256,
    }


def _required_string(row: Mapping[str, Any], key: str, source: Path, line_number: int) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AnnotationError(
            "{}:{}: '{}' must be a non-empty string".format(source, line_number, key)
        )
    return value.strip()


def _string_list(
    value: Any,
    key: str,
    source: Path,
    line_number: int,
    allowed: Optional[Sequence[str]] = None,
) -> List[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise AnnotationError(
            "{}:{}: '{}' must be a list of non-empty strings".format(source, line_number, key)
        )
    normalized = [item.strip() for item in value]
    if len(set(normalized)) != len(normalized):
        raise AnnotationError(
            "{}:{}: '{}' must not contain duplicates".format(source, line_number, key)
        )
    if allowed is not None:
        unknown = next((item for item in normalized if item not in allowed), None)
        if unknown is not None:
            raise AnnotationError(
                "{}:{}: invalid {} '{}'".format(source, line_number, key, unknown)
            )
    return normalized


def _load_mapping(path: Path) -> Tuple[str, Mapping[str, Mapping[str, Any]], Mapping[str, Any]]:
    mapping = _read_json(path)
    if mapping.get("schema_version") != ANNOTATION_SCHEMA_VERSION:
        raise AnnotationError("{}: unsupported schema_version".format(path))
    packet_id = mapping.get("packet_id")
    if not isinstance(packet_id, str) or not _PACKET_ID.fullmatch(packet_id):
        raise AnnotationError("{}: invalid packet_id".format(path))
    raw_items = mapping.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise AnnotationError("{}: items must be a non-empty list".format(path))

    items: Dict[str, Mapping[str, Any]] = {}
    for index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            raise AnnotationError("{}: item {} must be an object".format(path, index))
        item_id = raw_item.get("item_id")
        question_id = raw_item.get("question_id")
        option_map = raw_item.get("option_map")
        target = raw_item.get("original_target_option_id")
        if not isinstance(item_id, str) or not item_id or item_id in items:
            raise AnnotationError("{}: invalid or duplicate item_id".format(path))
        if not isinstance(question_id, str) or not question_id:
            raise AnnotationError("{}: item {} has invalid question_id".format(path, item_id))
        if not isinstance(option_map, dict) or len(option_map) < 2:
            raise AnnotationError("{}: item {} has invalid option_map".format(path, item_id))
        if any(
            not isinstance(label, str)
            or not isinstance(option_id, str)
            or not label
            or not option_id
            for label, option_id in option_map.items()
        ):
            raise AnnotationError("{}: item {} has invalid option_map".format(path, item_id))
        if len(set(option_map.values())) != len(option_map):
            raise AnnotationError("{}: item {} maps duplicate options".format(path, item_id))
        if target not in option_map.values():
            raise AnnotationError("{}: item {} has invalid original target".format(path, item_id))
        items[item_id] = raw_item
    source_count = mapping.get("source_question_count")
    if source_count != len(items):
        raise AnnotationError("{}: source_question_count does not match items".format(path))
    dataset_hash = mapping.get("source_dataset_sha256")
    if (
        not isinstance(dataset_hash, str)
        or len(dataset_hash) != 64
        or any(character not in "0123456789abcdef" for character in dataset_hash)
    ):
        raise AnnotationError("{}: invalid source_dataset_sha256".format(path))
    return packet_id, items, mapping


def _validate_response_row(
    row: Mapping[str, Any],
    source: Path,
    line_number: int,
    packet_id: str,
    mapped_item: Mapping[str, Any],
) -> Mapping[str, Any]:
    allowed_fields = {
        "schema_version",
        "packet_id",
        "item_id",
        "annotator_id",
        "problematic_option_labels",
        "primary_risk_option_label",
        "behavior_types",
        "severity",
        "confidence",
        "ambiguity_flags",
        "missing_context",
        "rationale",
    }
    unknown_fields = sorted(set(row) - allowed_fields)
    if unknown_fields:
        raise AnnotationError(
            "{}:{}: unknown field '{}'".format(source, line_number, unknown_fields[0])
        )
    if row.get("schema_version") != ANNOTATION_SCHEMA_VERSION:
        raise AnnotationError("{}:{}: unsupported schema_version".format(source, line_number))
    if row.get("packet_id") != packet_id:
        raise AnnotationError("{}:{}: packet_id does not match".format(source, line_number))

    item_id = _required_string(row, "item_id", source, line_number)
    annotator_id = _required_string(row, "annotator_id", source, line_number)
    option_map = mapped_item["option_map"]
    option_labels = set(option_map)
    problematic_labels = _string_list(
        row.get("problematic_option_labels"),
        "problematic_option_labels",
        source,
        line_number,
    )
    unknown_label = next(
        (label for label in problematic_labels if label not in option_labels), None
    )
    if unknown_label is not None:
        raise AnnotationError(
            "{}:{}: unknown option label '{}'".format(source, line_number, unknown_label)
        )

    primary_label = row.get("primary_risk_option_label")
    if primary_label is not None:
        if not isinstance(primary_label, str) or primary_label not in option_labels:
            raise AnnotationError(
                "{}:{}: primary_risk_option_label is invalid".format(source, line_number)
            )
        if primary_label not in problematic_labels:
            raise AnnotationError(
                "{}:{}: primary risk option must also be problematic".format(source, line_number)
            )

    behavior_types = _string_list(
        row.get("behavior_types"),
        "behavior_types",
        source,
        line_number,
        BEHAVIOR_TYPES,
    )
    severity = _required_string(row, "severity", source, line_number)
    confidence = _required_string(row, "confidence", source, line_number)
    if severity not in SEVERITIES:
        raise AnnotationError("{}:{}: invalid severity '{}'".format(source, line_number, severity))
    if confidence not in CONFIDENCE_LEVELS:
        raise AnnotationError(
            "{}:{}: invalid confidence '{}'".format(source, line_number, confidence)
        )
    ambiguity_flags = _string_list(
        row.get("ambiguity_flags"),
        "ambiguity_flags",
        source,
        line_number,
        AMBIGUITY_FLAGS,
    )
    missing_context = row.get("missing_context")
    if not isinstance(missing_context, str):
        raise AnnotationError("{}:{}: missing_context must be a string".format(source, line_number))
    rationale = _required_string(row, "rationale", source, line_number)

    if problematic_labels:
        if severity == "none":
            raise AnnotationError(
                "{}:{}: problematic options require non-none severity".format(source, line_number)
            )
        if not behavior_types or "none" in behavior_types:
            raise AnnotationError(
                "{}:{}: problematic options require behavior types other than none".format(
                    source, line_number
                )
            )
    else:
        if primary_label is not None or severity != "none" or behavior_types != ["none"]:
            raise AnnotationError(
                "{}:{}: no problematic options requires primary=null, severity=none, "
                "and behavior_types=[none]".format(source, line_number)
            )

    return {
        "item_id": item_id,
        "annotator_id": annotator_id,
        "problematic_option_labels": problematic_labels,
        "problematic_option_ids": [option_map[label] for label in problematic_labels],
        "primary_risk_option_label": primary_label,
        "primary_risk_option_id": option_map.get(primary_label),
        "behavior_types": behavior_types,
        "severity": severity,
        "confidence": confidence,
        "ambiguity_flags": ambiguity_flags,
        "missing_context": missing_context.strip(),
        "rationale": rationale,
    }


def _load_responses(
    response_paths: Sequence[Path],
    packet_id: str,
    items: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Mapping[str, Mapping[str, Any]]]:
    by_annotator: Dict[str, Mapping[str, Mapping[str, Any]]] = {}
    expected_items = set(items)
    for path in response_paths:
        responses: Dict[str, Mapping[str, Any]] = {}
        annotator_ids = set()
        for line_number, row in _read_jsonl(path):
            item_id = row.get("item_id")
            if not isinstance(item_id, str) or item_id not in items:
                raise AnnotationError(
                    "{}:{}: unknown item_id '{}'".format(path, line_number, item_id)
                )
            if item_id in responses:
                raise AnnotationError(
                    "{}:{}: duplicate item_id '{}'".format(path, line_number, item_id)
                )
            validated = _validate_response_row(
                row,
                path,
                line_number,
                packet_id,
                items[item_id],
            )
            responses[item_id] = validated
            annotator_ids.add(validated["annotator_id"])
        if len(annotator_ids) != 1:
            raise AnnotationError("{} must contain exactly one annotator_id".format(path))
        missing = sorted(expected_items - set(responses))
        if missing:
            raise AnnotationError("{} is missing item '{}'".format(path, missing[0]))
        annotator_id = next(iter(annotator_ids))
        if annotator_id in by_annotator:
            raise AnnotationError("duplicate annotator_id '{}'".format(annotator_id))
        by_annotator[annotator_id] = responses
    return by_annotator


def _fleiss_kappa_binary(yes_counts: Sequence[int], annotators: int) -> Optional[float]:
    if not yes_counts or annotators < 2:
        return None
    observed_values = []
    for yes_count in yes_counts:
        no_count = annotators - yes_count
        observed_values.append(
            (yes_count * (yes_count - 1) + no_count * (no_count - 1))
            / (annotators * (annotators - 1))
        )
    observed = sum(observed_values) / len(observed_values)
    yes_rate = sum(yes_counts) / (len(yes_counts) * annotators)
    expected = yes_rate * yes_rate + (1 - yes_rate) * (1 - yes_rate)
    if math.isclose(expected, 1.0):
        return None
    return (observed - expected) / (1 - expected)


def _pairwise_agreement(
    annotator_ids: Sequence[str],
    responses: Mapping[str, Mapping[str, Mapping[str, Any]]],
    item_ids: Sequence[str],
) -> Tuple[List[Mapping[str, Any]], float]:
    pairs = []
    total_agreements = 0
    total_comparisons = 0
    for first, second in combinations(annotator_ids, 2):
        agreements = sum(
            responses[first][item_id]["primary_risk_option_id"]
            == responses[second][item_id]["primary_risk_option_id"]
            for item_id in item_ids
        )
        total_agreements += agreements
        total_comparisons += len(item_ids)
        pairs.append(
            {
                "annotator_a": first,
                "annotator_b": second,
                "agreements": agreements,
                "items": len(item_ids),
                "rate": agreements / len(item_ids),
            }
        )
    rate = total_agreements / total_comparisons if total_comparisons else 0.0
    return pairs, rate


def merge_annotations(
    mapping_path: Path,
    response_paths: Sequence[Path],
    report_path: Path,
    adjudication_path: Path,
    min_annotators: int = 3,
    overwrite: bool = False,
) -> Mapping[str, Any]:
    if (
        isinstance(min_annotators, bool)
        or not isinstance(min_annotators, int)
        or min_annotators < 2
    ):
        raise AnnotationError("min_annotators must be an integer of at least 2")
    if len(response_paths) < min_annotators:
        raise AnnotationError(
            "at least {} independent response files are required".format(min_annotators)
        )
    report_path = report_path.resolve()
    adjudication_path = adjudication_path.resolve()
    mapping_path = mapping_path.resolve()
    resolved_response_paths = [path.resolve() for path in response_paths]
    if report_path == adjudication_path:
        raise AnnotationError("report and adjudication paths must be different")
    _check_output_paths((report_path, adjudication_path), overwrite)

    packet_id, items, mapping = _load_mapping(mapping_path)
    responses = _load_responses(
        resolved_response_paths,
        packet_id,
        items,
    )
    if len(responses) < min_annotators:
        raise AnnotationError("at least {} unique annotators are required".format(min_annotators))

    annotator_ids = sorted(responses)
    item_ids = list(items)
    annotator_count = len(annotator_ids)
    majority_threshold = annotator_count // 2 + 1
    severity_counts: Counter[str] = Counter()
    confidence_counts: Counter[str] = Counter()
    yes_counts = []
    item_reports = []
    adjudication_rows = []

    for item_id in item_ids:
        mapped_item = items[item_id]
        item_responses = [responses[annotator][item_id] for annotator in annotator_ids]
        primary_counts = Counter(response["primary_risk_option_id"] for response in item_responses)
        most_common_primary, most_common_count = primary_counts.most_common(1)[0]
        majority_primary = most_common_primary if most_common_count >= majority_threshold else None
        exact_agreement = len(primary_counts) == 1
        option_vote_counts = {}
        for option_id in mapped_item["option_map"].values():
            count = sum(
                option_id in response["problematic_option_ids"] for response in item_responses
            )
            option_vote_counts[option_id] = count
            yes_counts.append(count)
        majority_problematic = sorted(
            option_id
            for option_id, count in option_vote_counts.items()
            if count >= majority_threshold
        )
        for response in item_responses:
            severity_counts[response["severity"]] += 1
            confidence_counts[response["confidence"]] += 1

        ambiguity_reporters = sum(bool(response["ambiguity_flags"]) for response in item_responses)
        low_confidence_count = sum(response["confidence"] == "low" for response in item_responses)
        target = mapped_item["original_target_option_id"]
        reasons = []
        if most_common_count < majority_threshold or majority_primary is None:
            reasons.append("no_primary_majority")
        elif majority_primary != target:
            reasons.append("consensus_differs_from_original_target")
        if len(majority_problematic) != 1:
            reasons.append("not_exactly_one_majority_problematic_option")
        if ambiguity_reporters >= majority_threshold:
            reasons.append("majority_reports_ambiguity")
        if low_confidence_count >= majority_threshold:
            reasons.append("majority_low_confidence")

        primary_votes = {
            (option_id if option_id is not None else "<none>"): count
            for option_id, count in sorted(primary_counts.items(), key=lambda item: str(item[0]))
        }
        item_report = {
            "item_id": item_id,
            "question_id": mapped_item["question_id"],
            "original_target_option_id": target,
            "primary_votes": primary_votes,
            "majority_primary_option_id": majority_primary,
            "majority_primary_votes": most_common_count,
            "exact_primary_agreement": exact_agreement,
            "majority_problematic_option_ids": majority_problematic,
            "problematic_vote_counts": option_vote_counts,
            "consensus_matches_original_target": majority_primary == target,
            "adjudication_reasons": reasons,
        }
        item_reports.append(item_report)
        if reasons:
            adjudication_rows.append(
                {
                    **item_report,
                    "original_metadata": mapped_item.get("original_metadata", {}),
                    "annotations": item_responses,
                }
            )

    pairwise, pairwise_rate = _pairwise_agreement(
        annotator_ids,
        responses,
        item_ids,
    )
    unanimous_items = sum(item["exact_primary_agreement"] for item in item_reports)
    majority_items = sum(item["majority_primary_option_id"] is not None for item in item_reports)
    target_matches = sum(item["consensus_matches_original_target"] for item in item_reports)
    summary = {
        "packet_id": packet_id,
        "source_dataset_sha256": mapping.get("source_dataset_sha256"),
        "annotators": annotator_ids,
        "annotator_count": annotator_count,
        "items": len(item_ids),
        "unanimous_primary_items": unanimous_items,
        "unanimous_primary_rate": unanimous_items / len(item_ids),
        "majority_primary_items": majority_items,
        "majority_primary_rate": majority_items / len(item_ids),
        "consensus_evaluable_items": majority_items,
        "consensus_matches_original_target": target_matches,
        "consensus_matches_original_target_rate": target_matches / len(item_ids),
        "consensus_matches_original_target_among_consensus_rate": (
            target_matches / majority_items if majority_items else None
        ),
        "items_requiring_adjudication": len(adjudication_rows),
        "primary_pairwise_agreement_rate": pairwise_rate,
        "option_binary_fleiss_kappa": _fleiss_kappa_binary(
            yes_counts,
            annotator_count,
        ),
        "severity_counts": dict(sorted(severity_counts.items())),
        "confidence_counts": dict(sorted(confidence_counts.items())),
    }
    report = {
        "schema_version": ANNOTATION_SCHEMA_VERSION,
        "provenance": {
            "mapping_sha256": _sha256_bytes(mapping_path.read_bytes()),
            "response_files": [
                {
                    "name": path.name,
                    "sha256": _sha256_bytes(path.read_bytes()),
                }
                for path in sorted(resolved_response_paths, key=lambda value: str(value))
            ],
        },
        "summary": summary,
        "pairwise_primary_agreement": pairwise,
        "items": item_reports,
    }
    _write_json(report_path, report)
    _write_jsonl(adjudication_path, adjudication_rows)
    return {
        **summary,
        "report": str(report_path),
        "adjudication": str(adjudication_path),
    }
