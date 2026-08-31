import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from sudo_bench.api import Generation
from sudo_bench.cli import main


class FakeOpenAIChatClient:
    calls = 0

    def __init__(self, model: str, **kwargs) -> None:
        self.model = model

    def complete(self, prompt: str) -> Generation:
        type(self).calls += 1
        return Generation(r"\boxed{A}", "served-model", {"total_tokens": 2})


class CliTests(unittest.TestCase):
    def test_eval_writes_results_manifest_and_machine_readable_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "questions.jsonl").write_text(
                '{"id":"q1","prompt":"p","answer":"A"}\n',
                encoding="utf-8",
            )
            config_path = root / "config.yaml"
            config_path.write_text(
                "api_key: null\n"
                "base_url: https://models.example/v1\n"
                "model: requested-model\n"
                "dataset: questions.jsonl\n"
                "output: results.jsonl\n"
                "manifest: results.manifest.json\n"
                "samples_per_question: 2\n",
                encoding="utf-8",
            )
            FakeOpenAIChatClient.calls = 0
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch("sudo_bench.cli.OpenAIChatClient", FakeOpenAIChatClient):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(
                        ["eval", str(config_path), "--quiet", "--resume", "--retry-errors"]
                    )

            summary = json.loads(stdout.getvalue())
            resumed_stdout = io.StringIO()
            with patch("sudo_bench.cli.OpenAIChatClient", FakeOpenAIChatClient):
                with redirect_stdout(resumed_stdout), redirect_stderr(stderr):
                    resumed_exit_code = main(
                        ["eval", str(config_path), "--quiet", "--resume", "--retry-errors"]
                    )
            resumed_summary = json.loads(resumed_stdout.getvalue())
            manifest = json.loads((root / "results.manifest.json").read_text(encoding="utf-8"))
            rows = [
                json.loads(line)
                for line in (root / "results.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(exit_code, 0)
        self.assertEqual(resumed_exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(summary["metrics"]["metric"], "Avg@2")
        self.assertEqual(summary["metrics"]["total"], 2)
        self.assertEqual(manifest["run"]["id"], summary["run_id"])
        self.assertEqual(resumed_summary["run_id"], summary["run_id"])
        self.assertEqual(manifest["run"]["status"], "completed")
        self.assertEqual(manifest["run"]["resume_count"], 1)
        self.assertEqual(manifest["execution"]["scheduled_samples"], 0)
        self.assertEqual(FakeOpenAIChatClient.calls, 2)
        self.assertEqual({row["run_id"] for row in rows}, {summary["run_id"]})

    def test_interruption_is_recorded_in_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "questions.jsonl").write_text(
                '{"id":"q1","prompt":"p","answer":"A"}\n',
                encoding="utf-8",
            )
            config_path = root / "config.yaml"
            config_path.write_text(
                "api_key: null\n"
                "base_url: https://models.example/v1\n"
                "model: requested-model\n"
                "dataset: questions.jsonl\n"
                "output: results.jsonl\n"
                "manifest: results.manifest.json\n"
                "resume: true\n",
                encoding="utf-8",
            )
            with patch("sudo_bench.cli.OpenAIChatClient", FakeOpenAIChatClient):
                with patch("sudo_bench.cli.run_benchmark", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        main(["eval", str(config_path), "--quiet"])
            manifest = json.loads((root / "results.manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["run"]["status"], "interrupted")
        self.assertIsNone(manifest["metrics"])
        self.assertIn("KeyboardInterrupt", manifest["execution"]["failure"])


if __name__ == "__main__":
    unittest.main()
