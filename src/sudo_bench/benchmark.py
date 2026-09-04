import hashlib
import json
import math
import os
import re
import tempfile
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Protocol,
    Tuple,
)
from uuid import uuid4

import yaml

from sudo_bench.api import SYSTEM_PROMPT, Generation
from sudo_bench.reliability import RateLimiter, classify_exception, retry_delay

_BOX_START = re.compile(r"\\boxed\s*\{")
_ENV_VALUE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


class BenchmarkError(Exception):
    pass


@dataclass(frozen=True)
class EvalConfig:
    api_key: Optional[str]
    base_url: str
    model: str
    dataset: Path
    output: Path
    manifest: Path
    system_prompt: str = SYSTEM_PROMPT
    timeout: float = 60.0
    temperature: Optional[float] = None
    reasoning_effort: Optional[str] = None
    require_parameters: bool = False
    max_tokens: Optional[int] = None
    concurrency: int = 256
    samples_per_question: int = 1
    resume: bool = False
    retry_errors: bool = False
    max_attempts: int = 1
    backoff_initial_seconds: float = 1.0
    backoff_max_seconds: float = 30.0
    requests_per_second: Optional[float] = None
    shuffle_options: bool = False
    shuffle_seed: Optional[int] = None
    case_sensitive: bool = False
    overwrite: bool = False


@dataclass(frozen=True)
class Option:
    id: str
    text: str


@dataclass(frozen=True)
class Question:
    id: str
    prompt: str
    answer: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    stem: Optional[str] = None
    options: Tuple[Option, ...] = ()
    target_option_id: Optional[str] = None

    @property
    def is_structured(self) -> bool:
        return bool(self.stem and self.options and self.target_option_id)


@dataclass(frozen=True)
class RenderedQuestion:
    prompt: str
    answer: str
    option_order: Tuple[Tuple[str, str], ...] = ()
    target_option_id: Optional[str] = None


@dataclass(frozen=True)
class Metrics:
    questions: int
    samples_per_question: int
    total: int
    attempted: int
    correct: int
    incorrect: int
    errors: int
    format_errors: int
    accuracy: float
    behavioral_accuracy: float

    @property
    def metric_name(self) -> str:
        return "Avg@{}".format(self.samples_per_question)

    @property
    def avg_at_k(self) -> float:
        return self.accuracy

    @property
    def strict_avg_at_k(self) -> float:
        return self.accuracy

    @property
    def behavioral_avg_at_k(self) -> float:
        return self.behavioral_accuracy

    @property
    def target_choices(self) -> int:
        return self.correct

    @property
    def other_choices(self) -> int:
        return self.incorrect

    @property
    def target_choice_rate(self) -> float:
        return self.accuracy

    @property
    def behavioral_target_choice_rate(self) -> float:
        return self.behavioral_accuracy

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric_name,
            "questions": self.questions,
            "samples_per_question": self.samples_per_question,
            "total": self.total,
            "attempted": self.attempted,
            "correct": self.correct,
            "incorrect": self.incorrect,
            "target_choices": self.target_choices,
            "other_choices": self.other_choices,
            "errors": self.errors,
            "format_errors": self.format_errors,
            "accuracy": self.accuracy,
            "avg_at_k": self.avg_at_k,
            "strict_avg_at_k": self.strict_avg_at_k,
            "behavioral_avg_at_k": self.behavioral_avg_at_k,
            "target_choice_rate": self.target_choice_rate,
            "behavioral_target_choice_rate": self.behavioral_target_choice_rate,
        }


class CompletionClient(Protocol):
    model: str

    def complete(self, prompt: str) -> Generation:
        ...


def _required_string(data: Mapping[str, Any], key: str, source: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkError("{}: '{}' must be a non-empty string".format(source, key))
    return value.strip()


def load_config(path: Path) -> EvalConfig:
    path = path.resolve()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BenchmarkError("cannot read config {}: {}".format(path, exc)) from exc
    except yaml.YAMLError as exc:
        raise BenchmarkError("invalid YAML in {}: {}".format(path, exc)) from exc
    if not isinstance(data, dict):
        raise BenchmarkError("{}: config must be a YAML object".format(path))

    allowed = {
        "api_key",
        "base_url",
        "model",
        "dataset",
        "output",
        "manifest",
        "system_prompt",
        "timeout",
        "temperature",
        "reasoning_effort",
        "require_parameters",
        "max_tokens",
        "concurrency",
        "samples_per_question",
        "resume",
        "retry_errors",
        "max_attempts",
        "backoff_initial_seconds",
        "backoff_max_seconds",
        "requests_per_second",
        "shuffle_options",
        "shuffle_seed",
        "case_sensitive",
        "overwrite",
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise BenchmarkError("{}: unknown config key '{}'".format(path, unknown[0]))

    raw_key = data.get("api_key")
    if raw_key is not None and not isinstance(raw_key, str):
        raise BenchmarkError("{}: 'api_key' must be a string or null".format(path))
    api_key = raw_key
    if isinstance(raw_key, str):
        env_match = _ENV_VALUE.match(raw_key.strip())
        if env_match:
            env_name = env_match.group(1)
            api_key = os.environ.get(env_name)
            if not api_key:
                raise BenchmarkError("environment variable {} is not set".format(env_name))
        elif not raw_key.strip():
            raise BenchmarkError("{}: 'api_key' must not be empty".format(path))

    timeout = data.get("timeout", 60.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise BenchmarkError("{}: 'timeout' must be positive".format(path))
    temperature = data.get("temperature")
    if temperature is not None and (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not 0 <= temperature <= 2
    ):
        raise BenchmarkError("{}: 'temperature' must be from 0 to 2 or null".format(path))
    reasoning_effort = data.get("reasoning_effort")
    supported_reasoning_efforts = {
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    }
    if reasoning_effort is not None and reasoning_effort not in supported_reasoning_efforts:
        raise BenchmarkError(
            "{}: 'reasoning_effort' must be one of {} or null".format(
                path, ", ".join(sorted(supported_reasoning_efforts))
            )
        )
    require_parameters = data.get("require_parameters", False)
    if not isinstance(require_parameters, bool):
        raise BenchmarkError("{}: 'require_parameters' must be a boolean".format(path))
    max_tokens = data.get("max_tokens")
    if max_tokens is not None and (
        isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0
    ):
        raise BenchmarkError("{}: 'max_tokens' must be a positive integer or null".format(path))
    concurrency = data.get("concurrency", 256)
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or not 1 <= concurrency <= 256
    ):
        raise BenchmarkError("{}: 'concurrency' must be an integer from 1 to 256".format(path))
    samples_per_question = data.get("samples_per_question", 1)
    if (
        isinstance(samples_per_question, bool)
        or not isinstance(samples_per_question, int)
        or not 1 <= samples_per_question <= 256
    ):
        raise BenchmarkError(
            "{}: 'samples_per_question' must be an integer from 1 to 256".format(path)
        )
    resume = data.get("resume", False)
    retry_errors = data.get("retry_errors", False)
    if not isinstance(resume, bool) or not isinstance(retry_errors, bool):
        raise BenchmarkError("{}: resume and retry_errors must be booleans".format(path))
    if retry_errors and not resume:
        raise BenchmarkError("{}: retry_errors requires resume: true".format(path))
    raw_system_prompt = data.get("system_prompt", SYSTEM_PROMPT)
    if not isinstance(raw_system_prompt, str) or not raw_system_prompt.strip():
        raise BenchmarkError("{}: 'system_prompt' must be a non-empty string".format(path))
    system_prompt = raw_system_prompt.strip()
    max_attempts = data.get("max_attempts", 1)
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= 20
    ):
        raise BenchmarkError("{}: 'max_attempts' must be an integer from 1 to 20".format(path))
    backoff_initial_seconds = data.get("backoff_initial_seconds", 1.0)
    backoff_max_seconds = data.get("backoff_max_seconds", 30.0)
    for key, value in (
        ("backoff_initial_seconds", backoff_initial_seconds),
        ("backoff_max_seconds", backoff_max_seconds),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise BenchmarkError("{}: '{}' must be zero or positive".format(path, key))
    if backoff_max_seconds < backoff_initial_seconds:
        raise BenchmarkError(
            "{}: backoff_max_seconds must be at least backoff_initial_seconds".format(path)
        )
    requests_per_second = data.get("requests_per_second")
    if requests_per_second is not None and (
        isinstance(requests_per_second, bool)
        or not isinstance(requests_per_second, (int, float))
        or requests_per_second <= 0
    ):
        raise BenchmarkError(
            "{}: 'requests_per_second' must be positive or null".format(path)
        )
    shuffle_options = data.get("shuffle_options", False)
    shuffle_seed = data.get("shuffle_seed")
    if not isinstance(shuffle_options, bool):
        raise BenchmarkError("{}: 'shuffle_options' must be a boolean".format(path))
    if shuffle_seed is not None and (
        isinstance(shuffle_seed, bool) or not isinstance(shuffle_seed, int)
    ):
        raise BenchmarkError("{}: 'shuffle_seed' must be an integer or null".format(path))
    if shuffle_options and shuffle_seed is None:
        raise BenchmarkError("{}: shuffle_options requires shuffle_seed".format(path))
    case_sensitive = data.get("case_sensitive", False)
    overwrite = data.get("overwrite", False)
    if not isinstance(case_sensitive, bool) or not isinstance(overwrite, bool):
        raise BenchmarkError("{}: case_sensitive and overwrite must be booleans".format(path))
    if resume and overwrite:
        raise BenchmarkError("{}: resume and overwrite cannot both be true".format(path))

    root = path.parent
    dataset = (root / _required_string(data, "dataset", path)).resolve()
    output = (root / _required_string(data, "output", path)).resolve()
    raw_manifest = data.get("manifest")
    if raw_manifest is None:
        manifest = output.with_suffix(".manifest.json")
    elif not isinstance(raw_manifest, str) or not raw_manifest.strip():
        raise BenchmarkError("{}: 'manifest' must be a non-empty string or null".format(path))
    else:
        manifest = (root / raw_manifest.strip()).resolve()
    if manifest == output:
        raise BenchmarkError("{}: 'manifest' and 'output' must be different paths".format(path))

    return EvalConfig(
        api_key=api_key,
        base_url=_required_string(data, "base_url", path),
        model=_required_string(data, "model", path),
        dataset=dataset,
        output=output,
        manifest=manifest,
        system_prompt=system_prompt,
        timeout=float(timeout),
        temperature=float(temperature) if temperature is not None else None,
        reasoning_effort=reasoning_effort,
        require_parameters=require_parameters,
        max_tokens=max_tokens,
        concurrency=concurrency,
        samples_per_question=samples_per_question,
        resume=resume,
        retry_errors=retry_errors,
        max_attempts=max_attempts,
        backoff_initial_seconds=float(backoff_initial_seconds),
        backoff_max_seconds=float(backoff_max_seconds),
        requests_per_second=(
            float(requests_per_second) if requests_per_second is not None else None
        ),
        shuffle_options=shuffle_options,
        shuffle_seed=shuffle_seed,
        case_sensitive=case_sensitive,
        overwrite=overwrite,
    )


def _answer_string(value: Any, path: Path, line_number: int) -> str:
    if isinstance(value, str):
        answer = value.strip()
    elif isinstance(value, bool):
        answer = "true" if value else "false"
    elif isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise BenchmarkError("{}:{}: answer must be finite".format(path, line_number))
        answer = str(value)
    else:
        raise BenchmarkError(
            "{}:{}: answer must be a string, number, or boolean".format(path, line_number)
        )
    if not answer:
        raise BenchmarkError("{}:{}: answer must not be empty".format(path, line_number))
    return answer


def _option_label(index: int) -> str:
    if not 0 <= index < 26:
        raise BenchmarkError("structured questions support from 2 to 26 options")
    return chr(ord("A") + index)


def _render_options(stem: str, options: Iterable[Option]) -> str:
    lines = [stem.strip()]
    lines.extend(
        "{}. {}".format(_option_label(index), option.text)
        for index, option in enumerate(options)
    )
    return "\n".join(lines)


def _load_structured_question(
    item: Mapping[str, Any],
    question_id: str,
    metadata: Mapping[str, Any],
    path: Path,
    line_number: int,
) -> Question:
    if "prompt" in item or "answer" in item:
        raise BenchmarkError(
            "{}:{}: structured questions cannot mix prompt/answer with stem/options".format(
                path, line_number
            )
        )
    label_confidence = metadata.get("label_confidence")
    if label_confidence is not None and label_confidence not in {"high", "medium", "low"}:
        raise BenchmarkError(
            "{}:{}: label_confidence must be high, medium, or low".format(
                path, line_number
            )
        )
    stem = item.get("stem")
    raw_options = item.get("options")
    target_option_id = item.get("target_option_id")
    if not isinstance(stem, str) or not stem.strip():
        raise BenchmarkError("{}:{}: stem must be a non-empty string".format(path, line_number))
    if not isinstance(raw_options, list) or not 2 <= len(raw_options) <= 26:
        raise BenchmarkError("{}:{}: options must contain 2 to 26 items".format(path, line_number))
    if not isinstance(target_option_id, str) or not target_option_id.strip():
        raise BenchmarkError(
            "{}:{}: target_option_id must be a non-empty string".format(path, line_number)
        )

    options = []
    option_ids = set()
    option_texts = set()
    for option_index, raw_option in enumerate(raw_options, start=1):
        if not isinstance(raw_option, dict):
            raise BenchmarkError(
                "{}:{}: option {} must be an object".format(path, line_number, option_index)
            )
        option_id = raw_option.get("id")
        option_text = raw_option.get("text")
        if not isinstance(option_id, str) or not option_id.strip():
            raise BenchmarkError(
                "{}:{}: option {} id must be a non-empty string".format(
                    path, line_number, option_index
                )
            )
        if not isinstance(option_text, str) or not option_text.strip():
            raise BenchmarkError(
                "{}:{}: option {} text must be a non-empty string".format(
                    path, line_number, option_index
                )
            )
        option_id = option_id.strip()
        option_text = option_text.strip()
        if option_id in option_ids or option_text in option_texts:
            raise BenchmarkError(
                "{}:{}: option ids and text must be unique".format(path, line_number)
            )
        option_ids.add(option_id)
        option_texts.add(option_text)
        options.append(Option(option_id, option_text))

    target_option_id = target_option_id.strip()
    if target_option_id not in option_ids:
        raise BenchmarkError(
            "{}:{}: target_option_id does not match an option".format(path, line_number)
        )
    target_index = next(
        index for index, option in enumerate(options) if option.id == target_option_id
    )
    return Question(
        id=question_id,
        prompt=_render_options(stem, options),
        answer=_option_label(target_index),
        metadata=metadata,
        stem=stem.strip(),
        options=tuple(options),
        target_option_id=target_option_id,
    )


def load_questions(path: Path) -> List[Question]:
    questions: List[Question] = []
    seen_ids = set()
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise BenchmarkError("cannot read dataset {}: {}".format(path, exc)) from exc

    with handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                item = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise BenchmarkError(
                    "{}:{}: invalid JSON: {}".format(path, line_number, exc)
                ) from exc
            if not isinstance(item, dict):
                raise BenchmarkError("{}:{}: expected a JSON object".format(path, line_number))
            question_id = str(item.get("id", line_number)).strip()
            if not question_id or question_id in seen_ids:
                raise BenchmarkError("{}:{}: invalid or duplicate id".format(path, line_number))
            metadata = item.get("metadata", {})
            if not isinstance(metadata, dict):
                raise BenchmarkError("{}:{}: metadata must be an object".format(path, line_number))

            structured = any(
                key in item for key in ("stem", "options", "target_option_id")
            )
            if structured:
                question = _load_structured_question(
                    item,
                    question_id,
                    metadata,
                    path,
                    line_number,
                )
            else:
                prompt = item.get("prompt")
                if not isinstance(prompt, str) or not prompt.strip():
                    raise BenchmarkError(
                        "{}:{}: prompt must be a non-empty string".format(path, line_number)
                    )
                if "answer" not in item:
                    raise BenchmarkError("{}:{}: missing answer".format(path, line_number))
                question = Question(
                    id=question_id,
                    prompt=prompt.strip(),
                    answer=_answer_string(item["answer"], path, line_number),
                    metadata=metadata,
                )
            questions.append(question)
            seen_ids.add(question_id)
    if not questions:
        raise BenchmarkError("dataset {} contains no questions".format(path))
    return questions


def render_question(
    question: Question,
    sample_index: int,
    shuffle_options: bool = False,
    shuffle_seed: Optional[int] = None,
) -> RenderedQuestion:
    if isinstance(sample_index, bool) or not isinstance(sample_index, int) or sample_index <= 0:
        raise BenchmarkError("sample_index must be a positive integer")
    if not question.is_structured:
        if shuffle_options:
            raise BenchmarkError(
                "shuffle_options requires every dataset row to use stem/options"
            )
        return RenderedQuestion(prompt=question.prompt, answer=question.answer)

    options = list(question.options)
    if shuffle_options:
        if shuffle_seed is None:
            raise BenchmarkError("shuffle_options requires shuffle_seed")

        def order_key(option: Option) -> str:
            payload = "{}\0{}\0{}".format(
                shuffle_seed,
                question.id,
                option.id,
            )
            return hashlib.sha256(payload.encode("utf-8")).hexdigest()

        options.sort(key=order_key)
        rotation = (sample_index - 1) % len(options)
        options = options[rotation:] + options[:rotation]

    option_order = tuple(
        (_option_label(index), option.id) for index, option in enumerate(options)
    )
    answer = next(
        label for label, option_id in option_order if option_id == question.target_option_id
    )
    return RenderedQuestion(
        prompt=_render_options(question.stem or "", options),
        answer=answer,
        option_order=option_order,
        target_option_id=question.target_option_id,
    )


def _escaped(text: str, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def extract_boxed(text: str) -> Optional[str]:
    answers = []
    for match in _BOX_START.finditer(text):
        start = match.end()
        depth = 1
        for index in range(start, len(text)):
            if text[index] == "{" and not _escaped(text, index):
                depth += 1
            elif text[index] == "}" and not _escaped(text, index):
                depth -= 1
                if depth == 0:
                    answers.append(text[start:index].strip())
                    break
    return answers[-1] if answers else None


def _normalize(answer: str, case_sensitive: bool) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", answer).strip().split())
    return normalized if case_sensitive else normalized.casefold()


def _matches(expected: str, predicted: str, case_sensitive: bool) -> bool:
    boxed_expected = extract_boxed(expected)
    expected = boxed_expected if boxed_expected is not None else expected
    return _normalize(expected, case_sensitive) == _normalize(predicted, case_sensitive)


def _metrics(
    results: Iterable[Mapping[str, Any]],
    questions: int,
    samples_per_question: int,
) -> Metrics:
    rows = list(results)
    errors = sum(row["error"] is not None for row in rows)
    format_errors = sum(row.get("format_error") is not None for row in rows)
    attempted = len(rows) - errors
    correct = sum(row["correct"] is True for row in rows)
    return Metrics(
        questions=questions,
        samples_per_question=samples_per_question,
        total=len(rows),
        attempted=attempted,
        correct=correct,
        incorrect=attempted - correct,
        errors=errors,
        format_errors=format_errors,
        accuracy=correct / len(rows) if rows else 0.0,
        behavioral_accuracy=correct / attempted if attempted else 0.0,
    )


def _write_results_atomic(
    output: Path,
    rows: Iterable[Mapping[str, Any]],
    question_order: Mapping[str, int],
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(
        rows,
        key=lambda row: (
            int(row.get("sample_index", 1)),
            question_order.get(str(row.get("id")), len(question_order)),
        ),
    )
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(output.parent),
            prefix=".{}-".format(output.name),
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            for row in ordered:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(str(temporary_path), str(output))
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def read_result_run_id(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    run_ids = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BenchmarkError(
                    "{}:{}: invalid JSON: {}".format(path, line_number, exc)
                ) from exc
            if isinstance(row, dict) and isinstance(row.get("run_id"), str):
                if row["run_id"].strip():
                    run_ids.add(row["run_id"].strip())
    if len(run_ids) > 1:
        raise BenchmarkError("result file {} contains multiple run ids".format(path))
    return next(iter(run_ids), None)


def _load_resume_rows(
    path: Path,
    questions: Mapping[str, Question],
    samples_per_question: int,
    requested_model: str,
    run_id: str,
    shuffle_options: bool,
    shuffle_seed: Optional[int],
) -> Dict[tuple, Dict[str, Any]]:
    rows: Dict[tuple, Dict[str, Any]] = {}
    run_ids = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BenchmarkError(
                    "{}:{}: invalid JSON: {}".format(path, line_number, exc)
                ) from exc
            if not isinstance(row, dict):
                raise BenchmarkError("{}:{}: invalid result".format(path, line_number))
            question_id = str(row.get("id", ""))
            question = questions.get(question_id)
            if question is None:
                raise BenchmarkError(
                    "{}:{}: result contains unknown question id '{}'".format(
                        path, line_number, question_id
                    )
                )
            sample_count = row.get("samples_per_question", 1)
            sample_index = row.get("sample_index", 1)
            if sample_count != samples_per_question:
                raise BenchmarkError(
                    "{}:{}: samples_per_question does not match the config".format(
                        path, line_number
                    )
                )
            if (
                isinstance(sample_index, bool)
                or not isinstance(sample_index, int)
                or not 1 <= sample_index <= samples_per_question
            ):
                raise BenchmarkError("{}:{}: invalid sample_index".format(path, line_number))
            rendered = render_question(
                question,
                sample_index,
                shuffle_options=shuffle_options,
                shuffle_seed=shuffle_seed,
            )
            if row.get("prompt") != rendered.prompt or row.get("answer") != rendered.answer:
                raise BenchmarkError(
                    "{}:{}: result does not match the current dataset or option order".format(
                        path, line_number
                    )
                )
            if question.is_structured:
                expected_order = [
                    {"label": label, "option_id": option_id}
                    for label, option_id in rendered.option_order
                ]
                if (
                    row.get("target_option_id") != rendered.target_option_id
                    or row.get("option_order") != expected_order
                ):
                    raise BenchmarkError(
                        "{}:{}: result semantic options do not match the config".format(
                            path, line_number
                        )
                    )
            previous_model = row.get("requested_model")
            if previous_model is not None and previous_model != requested_model:
                raise BenchmarkError(
                    "{}:{}: requested model does not match the config".format(path, line_number)
                )
            previous_run_id = row.get("run_id")
            if isinstance(previous_run_id, str) and previous_run_id.strip():
                run_ids.add(previous_run_id.strip())
            key = (question_id, sample_index)
            if key in rows:
                raise BenchmarkError("{}:{}: duplicate question sample".format(path, line_number))
            rows[key] = row
    if len(run_ids) > 1 or (run_ids and run_id not in run_ids):
        raise BenchmarkError("result file {} does not match run id {}".format(path, run_id))
    return rows


def run_benchmark(
    questions: Iterable[Question],
    client: CompletionClient,
    output: Path,
    overwrite: bool,
    case_sensitive: bool,
    concurrency: int = 256,
    samples_per_question: int = 1,
    run_id: Optional[str] = None,
    resume: bool = False,
    retry_errors: bool = False,
    max_attempts: int = 1,
    backoff_initial_seconds: float = 1.0,
    backoff_max_seconds: float = 30.0,
    requests_per_second: Optional[float] = None,
    shuffle_options: bool = False,
    shuffle_seed: Optional[int] = None,
    run_stats: Optional[MutableMapping[str, Any]] = None,
    progress: Optional[Callable[[int, int, Mapping[str, Any]], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Metrics:
    question_list = list(questions)
    if not question_list:
        raise BenchmarkError("questions must not be empty")
    if isinstance(concurrency, bool) or not 1 <= concurrency <= 256:
        raise BenchmarkError("concurrency must be from 1 to 256")
    if isinstance(samples_per_question, bool) or not 1 <= samples_per_question <= 256:
        raise BenchmarkError("samples_per_question must be from 1 to 256")
    if isinstance(max_attempts, bool) or not 1 <= max_attempts <= 20:
        raise BenchmarkError("max_attempts must be from 1 to 20")
    if (
        isinstance(backoff_initial_seconds, bool)
        or isinstance(backoff_max_seconds, bool)
        or not isinstance(backoff_initial_seconds, (int, float))
        or not isinstance(backoff_max_seconds, (int, float))
        or backoff_initial_seconds < 0
        or backoff_max_seconds < backoff_initial_seconds
    ):
        raise BenchmarkError("invalid retry backoff")
    if requests_per_second is not None and (
        isinstance(requests_per_second, bool) or requests_per_second <= 0
    ):
        raise BenchmarkError("requests_per_second must be positive or None")
    if resume and overwrite:
        raise BenchmarkError("resume and overwrite cannot both be true")
    if retry_errors and not resume:
        raise BenchmarkError("retry_errors requires resume")
    if not isinstance(shuffle_options, bool):
        raise BenchmarkError("shuffle_options must be a boolean")
    if shuffle_seed is not None and (
        isinstance(shuffle_seed, bool) or not isinstance(shuffle_seed, int)
    ):
        raise BenchmarkError("shuffle_seed must be an integer or None")
    if shuffle_options and shuffle_seed is None:
        raise BenchmarkError("shuffle_options requires shuffle_seed")
    if shuffle_options and any(not question.is_structured for question in question_list):
        raise BenchmarkError("shuffle_options requires every question to be structured")

    run_id = run_id or uuid4().hex
    question_by_id = {question.id: question for question in question_list}
    question_order = {question.id: index for index, question in enumerate(question_list)}
    results_by_key: Dict[tuple, Dict[str, Any]] = {}
    if output.exists():
        if resume:
            results_by_key = _load_resume_rows(
                output,
                question_by_id,
                samples_per_question,
                client.model,
                run_id,
                shuffle_options,
                shuffle_seed,
            )
        elif not overwrite:
            raise FileExistsError(output)

    all_jobs = [
        (question, sample_index)
        for sample_index in range(1, samples_per_question + 1)
        for question in question_list
    ]
    jobs = []
    retried_error_samples = 0
    nonretryable_errors_skipped = 0
    for question, sample_index in all_jobs:
        previous = results_by_key.get((question.id, sample_index))
        if previous is None:
            jobs.append((question, sample_index, None))
        elif previous.get("error") is not None and retry_errors:
            if previous.get("retryable") is False:
                nonretryable_errors_skipped += 1
            else:
                retried_error_samples += 1
                jobs.append((question, sample_index, previous))

    stats = run_stats if run_stats is not None else {}
    stats.update(
        {
            "resume_enabled": resume,
            "existing_samples": len(results_by_key),
            "scheduled_samples": len(jobs),
            "skipped_samples": len(all_jobs) - len(jobs),
            "retried_error_samples": retried_error_samples,
            "nonretryable_errors_skipped": nonretryable_errors_skipped,
            "completed_this_session": 0,
        }
    )
    _write_results_atomic(output, results_by_key.values(), question_order)
    limiter = RateLimiter(requests_per_second, sleep=sleep)

    def evaluate(
        question: Question,
        sample_index: int,
        previous: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        rendered = render_question(
            question,
            sample_index,
            shuffle_options=shuffle_options,
            shuffle_seed=shuffle_seed,
        )
        option_order = [
            {"label": label, "option_id": option_id}
            for label, option_id in rendered.option_order
        ]
        previous_attempts = []
        previous_attempt_count = 0
        if previous is not None:
            raw_attempts = previous.get("attempts", [])
            if isinstance(raw_attempts, list):
                previous_attempts = [dict(item) for item in raw_attempts if isinstance(item, dict)]
            raw_count = previous.get("attempt_count", len(previous_attempts))
            if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count >= 0:
                previous_attempt_count = raw_count
            if previous.get("error") is not None and not previous_attempts:
                previous_attempt_count = max(1, previous_attempt_count)
                previous_attempts.append(
                    {
                        "attempt": previous_attempt_count,
                        "status": "error",
                        "error_type": previous.get("error_type", "legacy_error"),
                        "message": previous.get("error"),
                        "retryable": previous.get("retryable"),
                        "status_code": previous.get("status_code"),
                        "will_retry": False,
                        "backoff_seconds": 0.0,
                    }
                )
        attempts = list(previous_attempts)

        for local_attempt in range(1, max_attempts + 1):
            attempt_number = previous_attempt_count + local_attempt
            limiter.acquire()
            attempt_started = time.perf_counter()
            try:
                generation = client.complete(rendered.prompt)
            except Exception as exc:
                info = classify_exception(exc)
                will_retry = info.retryable and local_attempt < max_attempts
                delay = (
                    retry_delay(
                        local_attempt,
                        backoff_initial_seconds,
                        backoff_max_seconds,
                        info.retry_after,
                    )
                    if will_retry
                    else 0.0
                )
                message = "{}: {}".format(type(exc).__name__, exc)
                attempts.append(
                    {
                        "attempt": attempt_number,
                        "status": "error",
                        "error_type": info.category,
                        "message": message,
                        "retryable": info.retryable,
                        "status_code": info.status_code,
                        "latency_ms": round((time.perf_counter() - attempt_started) * 1000, 3),
                        "will_retry": will_retry,
                        "backoff_seconds": delay,
                    }
                )
                if will_retry:
                    sleep(delay)
                    continue
                return {
                    "id": question.id,
                    "run_id": run_id,
                    "sample_index": sample_index,
                    "samples_per_question": samples_per_question,
                    "requested_model": client.model,
                    "prompt": rendered.prompt,
                    "answer": rendered.answer,
                    "prediction": None,
                    "target_option_id": rendered.target_option_id,
                    "predicted_option_id": None,
                    "option_order": option_order,
                    "shuffle_options": shuffle_options,
                    "shuffle_seed": shuffle_seed,
                    "raw_output": None,
                    "correct": None,
                    "model": client.model,
                    "error": message,
                    "error_type": info.category,
                    "retryable": info.retryable,
                    "status_code": info.status_code,
                    "format_error": None,
                    "attempt_count": attempt_number,
                    "attempts": attempts,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    "usage": {},
                    "metadata": dict(question.metadata),
                }

            prediction = extract_boxed(generation.text)
            predicted_option_id = None
            if prediction is not None and rendered.option_order:
                predicted_option_id = next(
                    (
                        option_id
                        for label, option_id in rendered.option_order
                        if _matches(label, prediction, False)
                    ),
                    None,
                )
            attempts.append(
                {
                    "attempt": attempt_number,
                    "status": "success",
                    "latency_ms": round((time.perf_counter() - attempt_started) * 1000, 3),
                }
            )
            return {
                "id": question.id,
                "run_id": run_id,
                "sample_index": sample_index,
                "samples_per_question": samples_per_question,
                "requested_model": client.model,
                "prompt": rendered.prompt,
                "answer": rendered.answer,
                "prediction": prediction,
                "target_option_id": rendered.target_option_id,
                "predicted_option_id": predicted_option_id,
                "option_order": option_order,
                "shuffle_options": shuffle_options,
                "shuffle_seed": shuffle_seed,
                "raw_output": generation.text,
                "correct": (
                    predicted_option_id == rendered.target_option_id
                    if rendered.target_option_id is not None
                    else (
                        _matches(rendered.answer, prediction, case_sensitive)
                        if prediction is not None
                        else False
                    )
                ),
                "model": generation.model,
                "error": None,
                "error_type": None,
                "retryable": None,
                "status_code": None,
                "format_error": (
                    "missing_boxed_answer"
                    if prediction is None
                    else (
                        "invalid_option_label"
                        if rendered.option_order and predicted_option_id is None
                        else None
                    )
                ),
                "attempt_count": attempt_number,
                "attempts": attempts,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "usage": dict(generation.usage),
                "metadata": dict(question.metadata),
            }

        raise AssertionError("retry loop did not return")

    if jobs:
        executor = ThreadPoolExecutor(max_workers=min(concurrency, len(jobs)))
        futures = [
            executor.submit(evaluate, question, sample_index, previous)
            for question, sample_index, previous in jobs
        ]
        try:
            for completed, future in enumerate(as_completed(futures), start=1):
                row = future.result()
                key = (str(row["id"]), int(row["sample_index"]))
                results_by_key[key] = row
                _write_results_atomic(output, results_by_key.values(), question_order)
                stats["completed_this_session"] = completed
                if progress:
                    progress(completed, len(jobs), row)
        except BaseException:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)

    return _metrics(results_by_key.values(), len(question_list), samples_per_question)


def _result_matches_target(
    row: Mapping[str, Any],
    prediction: str,
    case_sensitive: bool,
) -> bool:
    target_option_id = row.get("target_option_id")
    option_order = row.get("option_order")
    if isinstance(target_option_id, str) and isinstance(option_order, list):
        predicted_option_id = next(
            (
                item.get("option_id")
                for item in option_order
                if isinstance(item, dict)
                and isinstance(item.get("label"), str)
                and _matches(item["label"], prediction, False)
            ),
            None,
        )
        return predicted_option_id == target_option_id
    return _matches(str(row["answer"]), prediction, case_sensitive)


def score_file(path: Path, case_sensitive: bool = False) -> Metrics:
    rows = []
    sample_counts = set()
    question_ids = set()
    samples = set()
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise BenchmarkError("cannot read results {}: {}".format(path, exc)) from exc
    with handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BenchmarkError(
                    "{}:{}: invalid JSON: {}".format(path, line_number, exc)
                ) from exc
            if not isinstance(row, dict) or not isinstance(row.get("answer"), str):
                raise BenchmarkError("{}:{}: invalid result".format(path, line_number))
            question_id = str(row.get("id", line_number))
            question_ids.add(question_id)
            sample_count = row.get("samples_per_question", 1)
            if (
                isinstance(sample_count, bool)
                or not isinstance(sample_count, int)
                or sample_count <= 0
            ):
                raise BenchmarkError(
                    "{}:{}: invalid samples_per_question".format(path, line_number)
                )
            sample_counts.add(sample_count)
            sample_index = row.get("sample_index", 1)
            if (
                isinstance(sample_index, bool)
                or not isinstance(sample_index, int)
                or not 1 <= sample_index <= sample_count
            ):
                raise BenchmarkError("{}:{}: invalid sample_index".format(path, line_number))
            sample = (question_id, sample_index)
            if sample in samples:
                raise BenchmarkError("{}:{}: duplicate question sample".format(path, line_number))
            samples.add(sample)
            error = row.get("error")
            raw_output = row.get("raw_output")
            prediction = (
                extract_boxed(raw_output)
                if error is None and isinstance(raw_output, str)
                else None
            )
            invalid_option = (
                error is None
                and prediction is not None
                and isinstance(row.get("target_option_id"), str)
                and isinstance(row.get("option_order"), list)
                and not any(
                    isinstance(item, dict)
                    and isinstance(item.get("label"), str)
                    and _matches(item["label"], prediction, False)
                    for item in row["option_order"]
                )
            )
            rows.append(
                {
                    "error": error,
                    "format_error": (
                        "missing_boxed_answer"
                        if error is None and prediction is None
                        else "invalid_option_label" if invalid_option else None
                    ),
                    "correct": (
                        _result_matches_target(row, prediction, case_sensitive)
                        if error is None and prediction is not None
                        else False if error is None else None
                    ),
                }
            )
    if not rows:
        raise BenchmarkError("result file {} is empty".format(path))
    if len(sample_counts) != 1:
        raise BenchmarkError("result file {} mixes different sample counts".format(path))
    samples_per_question = sample_counts.pop()
    expected_total = len(question_ids) * samples_per_question
    if len(rows) != expected_total:
        raise BenchmarkError(
            "result file {} is incomplete: expected {} rows, found {}".format(
                path, expected_total, len(rows)
            )
        )
    return _metrics(rows, len(question_ids), samples_per_question)
