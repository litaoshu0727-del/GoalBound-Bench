import json
import tempfile
import unittest
from pathlib import Path

from sudo_bench.api import ApiError, Generation
from sudo_bench.benchmark import Question, run_benchmark, score_file
from sudo_bench.reliability import RateLimiter


class SequenceClient:
    model = "test-model"

    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def complete(self, prompt: str) -> Generation:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class PromptClient:
    model = "test-model"

    def complete(self, prompt: str) -> Generation:
        if prompt == "fails":
            raise ApiError("offline", category="network_error", retryable=True)
        return Generation(r"\boxed{A}", self.model)


class CountingClient:
    model = "test-model"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str) -> Generation:
        self.calls += 1
        return Generation(r"\boxed{A}", self.model)


class ReliabilityTests(unittest.TestCase):
    def test_retryable_error_uses_backoff_and_recovers(self) -> None:
        client = SequenceClient(
            [
                ApiError(
                    "slow down",
                    category="rate_limit",
                    retryable=True,
                    status_code=429,
                    retry_after=2,
                ),
                Generation(r"\boxed{A}", "served-model"),
            ]
        )
        delays = []
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results.jsonl"
            metrics = run_benchmark(
                [Question("q1", "p", "A")],
                client,
                output,
                overwrite=False,
                case_sensitive=False,
                concurrency=1,
                max_attempts=3,
                backoff_initial_seconds=1,
                backoff_max_seconds=10,
                sleep=delays.append,
            )
            row = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(client.calls, 2)
        self.assertEqual(delays, [2])
        self.assertEqual(row["attempt_count"], 2)
        self.assertEqual([item["status"] for item in row["attempts"]], ["error", "success"])
        self.assertEqual(row["attempts"][0]["error_type"], "rate_limit")
        self.assertEqual(metrics.strict_avg_at_k, 1)
        self.assertEqual(metrics.behavioral_avg_at_k, 1)

    def test_authentication_error_is_not_retried(self) -> None:
        client = SequenceClient(
            [
                ApiError(
                    "bad key",
                    category="authentication_error",
                    retryable=False,
                    status_code=401,
                )
            ]
        )
        delays = []
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results.jsonl"
            metrics = run_benchmark(
                [Question("q1", "p", "A")],
                client,
                output,
                overwrite=False,
                case_sensitive=False,
                max_attempts=4,
                sleep=delays.append,
            )
            row = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(client.calls, 1)
        self.assertEqual(delays, [])
        self.assertEqual(row["error_type"], "authentication_error")
        self.assertEqual(metrics.strict_avg_at_k, 0)
        self.assertEqual(metrics.behavioral_avg_at_k, 0)

    def test_strict_and_behavioral_metrics_separate_provider_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results.jsonl"
            metrics = run_benchmark(
                [Question("ok", "works", "A"), Question("error", "fails", "A")],
                PromptClient(),
                output,
                overwrite=False,
                case_sensitive=False,
                concurrency=1,
            )

        self.assertEqual(metrics.total, 2)
        self.assertEqual(metrics.attempted, 1)
        self.assertEqual(metrics.strict_avg_at_k, 0.5)
        self.assertEqual(metrics.behavioral_avg_at_k, 1)

    def test_interrupted_run_resumes_only_missing_samples(self) -> None:
        questions = [Question("q1", "p1", "A"), Question("q2", "p2", "A")]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results.jsonl"
            first_client = CountingClient()

            def interrupt(index, total, row):
                raise RuntimeError("forced interruption")

            with self.assertRaisesRegex(RuntimeError, "forced interruption"):
                run_benchmark(
                    questions,
                    first_client,
                    output,
                    overwrite=False,
                    case_sensitive=False,
                    concurrency=1,
                    samples_per_question=2,
                    run_id="resume-run",
                    progress=interrupt,
                )
            partial_rows = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(partial_rows), 1)

            second_client = CountingClient()
            stats = {}
            metrics = run_benchmark(
                questions,
                second_client,
                output,
                overwrite=False,
                case_sensitive=False,
                concurrency=1,
                samples_per_question=2,
                run_id="resume-run",
                resume=True,
                run_stats=stats,
            )
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            rescored = score_file(output)

        self.assertEqual(second_client.calls, 3)
        self.assertEqual(stats["existing_samples"], 1)
        self.assertEqual(stats["scheduled_samples"], 3)
        self.assertEqual(metrics.total, 4)
        self.assertEqual(len({(row["id"], row["sample_index"]) for row in rows}), 4)
        self.assertEqual(rescored, metrics)

    def test_retry_errors_replaces_row_without_duplicates(self) -> None:
        error = ApiError("offline", category="network_error", retryable=True)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results.jsonl"
            run_benchmark(
                [Question("q1", "p", "A")],
                SequenceClient([error]),
                output,
                overwrite=False,
                case_sensitive=False,
                run_id="retry-run",
            )
            stats = {}
            metrics = run_benchmark(
                [Question("q1", "p", "A")],
                SequenceClient([Generation(r"\boxed{A}", "served-model")]),
                output,
                overwrite=False,
                case_sensitive=False,
                run_id="retry-run",
                resume=True,
                retry_errors=True,
                run_stats=stats,
            )
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attempt_count"], 2)
        self.assertEqual([item["status"] for item in rows[0]["attempts"]], ["error", "success"])
        self.assertEqual(stats["retried_error_samples"], 1)
        self.assertEqual(metrics.errors, 0)

    def test_resume_does_not_retry_nonretryable_error(self) -> None:
        error = ApiError(
            "bad key",
            category="authentication_error",
            retryable=False,
            status_code=401,
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results.jsonl"
            run_benchmark(
                [Question("q1", "p", "A")],
                SequenceClient([error]),
                output,
                overwrite=False,
                case_sensitive=False,
                run_id="auth-run",
            )
            second_client = SequenceClient([Generation(r"\boxed{A}", "served-model")])
            stats = {}
            metrics = run_benchmark(
                [Question("q1", "p", "A")],
                second_client,
                output,
                overwrite=False,
                case_sensitive=False,
                run_id="auth-run",
                resume=True,
                retry_errors=True,
                run_stats=stats,
            )

        self.assertEqual(second_client.calls, 0)
        self.assertEqual(stats["retried_error_samples"], 0)
        self.assertEqual(stats["nonretryable_errors_skipped"], 1)
        self.assertEqual(metrics.errors, 1)

    def test_rate_limiter_spaces_requests(self) -> None:
        now = [0.0]
        delays = []

        def sleep(delay):
            delays.append(delay)
            now[0] += delay

        limiter = RateLimiter(2, clock=lambda: now[0], sleep=sleep)
        limiter.acquire()
        limiter.acquire()
        limiter.acquire()

        self.assertEqual(delays, [0.5, 0.5])


if __name__ == "__main__":
    unittest.main()
