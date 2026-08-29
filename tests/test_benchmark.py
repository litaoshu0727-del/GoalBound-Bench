import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sudo_bench.api import Generation
from sudo_bench.benchmark import (
    BenchmarkError,
    Question,
    extract_boxed,
    load_config,
    load_questions,
    run_benchmark,
    score_file,
)


class FakeClient:
    model = "fake-model"

    def complete(self, prompt: str) -> Generation:
        if prompt == "error":
            raise RuntimeError("synthetic")
        if prompt == "unboxed":
            return Generation("B", self.model)
        return Generation(r"\boxed{\frac{1}{2}}", self.model)


class BenchmarkTests(unittest.TestCase):
    def test_load_config_expands_env_and_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "api_key: ${TEST_KEY}",
                        "base_url: https://models.example/v1",
                        "model: test-model",
                        "dataset: questions.jsonl",
                        "output: results.jsonl",
                        "temperature: 1.0",
                        "require_parameters: true",
                        "max_tokens: 8192",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"TEST_KEY": "test-key"}):
                config = load_config(config_path)

        self.assertEqual(config.api_key, "test-key")
        self.assertEqual(config.dataset, root.resolve() / "questions.jsonl")
        self.assertEqual(config.output, root.resolve() / "results.jsonl")
        self.assertEqual(config.temperature, 1.0)
        self.assertTrue(config.require_parameters)
        self.assertEqual(config.max_tokens, 8192)

    def test_missing_key_environment_variable_is_clear(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                "api_key: ${MISSING_KEY}\nbase_url: https://example.com\n"
                "model: m\ndataset: q.jsonl\noutput: r.jsonl\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(BenchmarkError, "MISSING_KEY"):
                    load_config(path)

    def test_load_questions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "questions.jsonl"
            path.write_text('{"prompt":"1+1?","answer":2}\n', encoding="utf-8")
            question = load_questions(path)[0]
        self.assertEqual(question.id, "1")
        self.assertEqual(question.answer, "2")

    def test_only_standard_boxed_and_nested_braces(self) -> None:
        self.assertEqual(extract_boxed(r"\boxed{\frac{1}{2}}"), r"\frac{1}{2}")
        self.assertIsNone(extract_boxed(r"\box{A}"))

    def test_run_and_rescore(self) -> None:
        questions = [
            Question("ok", "ok", r"\frac{1}{2}"),
            Question("format", "unboxed", "B"),
            Question("error", "error", "A"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results.jsonl"
            metrics = run_benchmark(questions, FakeClient(), output, False, False)
            rescored = score_file(output)
            rows = [json.loads(line) for line in output.read_text().splitlines()]
            rows_by_id = {row["id"]: row for row in rows}

        self.assertEqual(metrics.correct, 1)
        self.assertEqual(metrics.incorrect, 1)
        self.assertEqual(metrics.errors, 1)
        self.assertEqual(rescored, metrics)
        self.assertEqual(rows_by_id["format"]["format_error"], "missing_boxed_answer")


if __name__ == "__main__":
    unittest.main()
