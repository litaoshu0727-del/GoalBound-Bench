import json
import math
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol

import yaml

from sudo_bench.api import Generation

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
    timeout: float = 60.0
    temperature: Optional[float] = None
    require_parameters: bool = False
    max_tokens: Optional[int] = None
    concurrency: int = 256
    case_sensitive: bool = False
    overwrite: bool = False


@dataclass(frozen=True)
class Question:
    id: str
    prompt: str
    answer: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Metrics:
    total: int
    attempted: int
    correct: int
    incorrect: int
    errors: int
    accuracy: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "attempted": self.attempted,
            "correct": self.correct,
            "incorrect": self.incorrect,
            "errors": self.errors,
            "accuracy": self.accuracy,
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
        "timeout",
        "temperature",
        "require_parameters",
        "max_tokens",
        "concurrency",
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
    case_sensitive = data.get("case_sensitive", False)
    overwrite = data.get("overwrite", False)
    if not isinstance(case_sensitive, bool) or not isinstance(overwrite, bool):
        raise BenchmarkError("{}: case_sensitive and overwrite must be booleans".format(path))

    root = path.parent
    return EvalConfig(
        api_key=api_key,
        base_url=_required_string(data, "base_url", path),
        model=_required_string(data, "model", path),
        dataset=(root / _required_string(data, "dataset", path)).resolve(),
        output=(root / _required_string(data, "output", path)).resolve(),
        timeout=float(timeout),
        temperature=float(temperature) if temperature is not None else None,
        require_parameters=require_parameters,
        max_tokens=max_tokens,
        concurrency=concurrency,
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
            prompt = item.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise BenchmarkError(
                    "{}:{}: prompt must be a non-empty string".format(path, line_number)
                )
            if "answer" not in item:
                raise BenchmarkError("{}:{}: missing answer".format(path, line_number))

            question_id = str(item.get("id", line_number)).strip()
            if not question_id or question_id in seen_ids:
                raise BenchmarkError("{}:{}: invalid or duplicate id".format(path, line_number))
            metadata = item.get("metadata", {})
            if not isinstance(metadata, dict):
                raise BenchmarkError("{}:{}: metadata must be an object".format(path, line_number))

            questions.append(
                Question(
                    id=question_id,
                    prompt=prompt.strip(),
                    answer=_answer_string(item["answer"], path, line_number),
                    metadata=metadata,
                )
            )
            seen_ids.add(question_id)
    if not questions:
        raise BenchmarkError("dataset {} contains no questions".format(path))
    return questions


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


def _metrics(results: Iterable[Mapping[str, Any]]) -> Metrics:
    rows = list(results)
    errors = sum(row["error"] is not None for row in rows)
    attempted = len(rows) - errors
    correct = sum(row["correct"] is True for row in rows)
    return Metrics(
        total=len(rows),
        attempted=attempted,
        correct=correct,
        incorrect=attempted - correct,
        errors=errors,
        accuracy=correct / len(rows) if rows else 0.0,
    )


def run_benchmark(
    questions: Iterable[Question],
    client: CompletionClient,
    output: Path,
    overwrite: bool,
    case_sensitive: bool,
    concurrency: int = 256,
    progress: Optional[Callable[[int, int, Mapping[str, Any]], None]] = None,
) -> Metrics:
    question_list = list(questions)
    if not 1 <= concurrency <= 256:
        raise BenchmarkError("concurrency must be from 1 to 256")
    output.parent.mkdir(parents=True, exist_ok=True)
    results = []

    def evaluate(question: Question) -> Dict[str, Any]:
        started = time.perf_counter()
        try:
            generation = client.complete(question.prompt)
            prediction = extract_boxed(generation.text)
            return {
                "id": question.id,
                "prompt": question.prompt,
                "answer": question.answer,
                "prediction": prediction,
                "raw_output": generation.text,
                "correct": (
                    _matches(question.answer, prediction, case_sensitive)
                    if prediction is not None
                    else False
                ),
                "model": generation.model,
                "error": None,
                "format_error": None if prediction is not None else "missing_boxed_answer",
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "usage": dict(generation.usage),
                "metadata": dict(question.metadata),
            }
        except Exception as exc:
            return {
                "id": question.id,
                "prompt": question.prompt,
                "answer": question.answer,
                "prediction": None,
                "raw_output": None,
                "correct": None,
                "model": client.model,
                "error": "{}: {}".format(type(exc).__name__, exc),
                "format_error": None,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "usage": {},
                "metadata": dict(question.metadata),
            }

    with output.open("w" if overwrite else "x", encoding="utf-8") as handle:
        workers = min(concurrency, len(question_list))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(evaluate, question) for question in question_list]
            for completed, future in enumerate(as_completed(futures), start=1):
                row = future.result()
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
                results.append(row)
                if progress:
                    progress(completed, len(question_list), row)
    return _metrics(results)


def score_file(path: Path, case_sensitive: bool = False) -> Metrics:
    rows = []
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
            error = row.get("error")
            raw_output = row.get("raw_output")
            prediction = (
                extract_boxed(raw_output)
                if error is None and isinstance(raw_output, str)
                else None
            )
            rows.append(
                {
                    "error": error,
                    "correct": (
                        _matches(row["answer"], prediction, case_sensitive)
                        if error is None and prediction is not None
                        else False if error is None else None
                    ),
                }
            )
    if not rows:
        raise BenchmarkError("result file {} is empty".format(path))
    return _metrics(rows)
