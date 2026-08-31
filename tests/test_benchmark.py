import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sudo_bench.api import SYSTEM_PROMPT, Generation
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
                        "manifest: audit/run.json",
                        "system_prompt: custom control prompt",
                        "temperature: 1.0",
                        "require_parameters: true",
                        "max_tokens: 8192",
                        "samples_per_question: 16",
                        "resume: true",
                        "retry_errors: true",
                        "max_attempts: 4",
                        "backoff_initial_seconds: 0.5",
                        "backoff_max_seconds: 8",
                        "requests_per_second: 2",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"TEST_KEY": "test-key"}):
                config = load_config(config_path)

        self.assertEqual(config.api_key, "test-key")
        self.assertEqual(config.dataset, root.resolve() / "questions.jsonl")
        self.assertEqual(config.output, root.resolve() / "results.jsonl")
        self.assertEqual(config.manifest, root.resolve() / "audit/run.json")
        self.assertEqual(config.system_prompt, "custom control prompt")
        self.assertEqual(config.temperature, 1.0)
        self.assertTrue(config.require_parameters)
        self.assertEqual(config.max_tokens, 8192)
        self.assertEqual(config.samples_per_question, 16)
        self.assertTrue(config.resume)
        self.assertTrue(config.retry_errors)
        self.assertEqual(config.max_attempts, 4)
        self.assertEqual(config.backoff_initial_seconds, 0.5)
        self.assertEqual(config.backoff_max_seconds, 8)
        self.assertEqual(config.requests_per_second, 2)

    def test_config_defaults_to_one_sample_and_derived_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text(
                "api_key: null\nbase_url: https://example.com\n"
                "model: m\ndataset: q.jsonl\noutput: results.jsonl\n",
                encoding="utf-8",
            )
            config = load_config(config_path)

        self.assertEqual(config.samples_per_question, 1)
        self.assertEqual(config.manifest, root.resolve() / "results.manifest.json")
        self.assertEqual(config.system_prompt, SYSTEM_PROMPT)

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

    def test_invalid_sample_count_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                "api_key: null\nbase_url: https://example.com\n"
                "model: m\ndataset: q.jsonl\noutput: r.jsonl\n"
                "samples_per_question: 0\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(BenchmarkError, "samples_per_question"):
                load_config(path)

    def test_resume_and_overwrite_are_mutually_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                "api_key: null\nbase_url: https://example.com\n"
                "model: m\ndataset: q.jsonl\noutput: r.jsonl\n"
                "resume: true\noverwrite: true\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(BenchmarkError, "cannot both be true"):
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
        self.assertEqual(metrics.format_errors, 1)
        self.assertEqual(metrics.metric_name, "Avg@1")
        self.assertEqual(rescored, metrics)
        self.assertEqual(rows_by_id["format"]["format_error"], "missing_boxed_answer")

    def test_multiple_samples_are_identifiable_and_score_as_avg_at_k(self) -> None:
        questions = [
            Question("one", "one", r"\frac{1}{2}"),
            Question("two", "two", r"\frac{1}{2}"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results.jsonl"
            metrics = run_benchmark(
                questions,
                FakeClient(),
                output,
                overwrite=False,
                case_sensitive=False,
                concurrency=2,
                samples_per_question=3,
                run_id="test-run",
            )
            rescored = score_file(output)
            rows = [json.loads(line) for line in output.read_text().splitlines()]

        self.assertEqual(metrics.metric_name, "Avg@3")
        self.assertEqual(metrics.questions, 2)
        self.assertEqual(metrics.samples_per_question, 3)
        self.assertEqual(metrics.total, 6)
        self.assertEqual(metrics.correct, 6)
        self.assertEqual(metrics.avg_at_k, 1)
        self.assertEqual(rescored, metrics)
        self.assertEqual({row["run_id"] for row in rows}, {"test-run"})
        self.assertEqual(
            {(row["id"], row["sample_index"]) for row in rows},
            {(question.id, sample) for question in questions for sample in range(1, 4)},
        )

    def test_rescore_rejects_incomplete_avg_at_k_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results.jsonl"
            output.write_text(
                json.dumps(
                    {
                        "id": "q1",
                        "answer": "A",
                        "raw_output": r"\boxed{A}",
                        "error": None,
                        "sample_index": 1,
                        "samples_per_question": 2,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(BenchmarkError, "incomplete"):
                score_file(output)


if __name__ == "__main__":
    unittest.main()
