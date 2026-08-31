import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from sudo_bench.api import SYSTEM_PROMPT, Generation
from sudo_bench.benchmark import BenchmarkError, EvalConfig, Question, run_benchmark
from sudo_bench.reporting import build_run_manifest, validate_resume_manifest, write_run_manifest


class UsageClient:
    model = "requested-model"

    def complete(self, prompt: str) -> Generation:
        return Generation(
            text=r"\boxed{A}",
            model="returned-model",
            usage={"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        )


class ReportingTests(unittest.TestCase):
    def test_manifest_is_auditable_and_does_not_contain_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "questions.jsonl"
            output = root / "results.jsonl"
            manifest_path = root / "results.manifest.json"
            config_path = root / "config.yaml"
            dataset.write_text('{"id":"q1","prompt":"p","answer":"A"}\n', encoding="utf-8")
            config_path.write_text("placeholder", encoding="utf-8")
            config = EvalConfig(
                api_key="top-secret",
                base_url="https://models.example/v1",
                model="requested-model",
                dataset=dataset,
                output=output,
                manifest=manifest_path,
                samples_per_question=2,
            )
            metrics = run_benchmark(
                [Question("q1", "p", "A", {"label_confidence": "high"})],
                UsageClient(),
                output,
                overwrite=False,
                case_sensitive=False,
                samples_per_question=2,
                run_id="run-123",
            )
            manifest = build_run_manifest(
                config=config,
                config_path=config_path,
                metrics=metrics,
                run_id="run-123",
                started_at="2026-08-31T00:00:00Z",
                completed_at="2026-08-31T00:00:01Z",
            )
            write_run_manifest(manifest, manifest_path, overwrite=False)
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))
            with self.assertRaisesRegex(BenchmarkError, "model"):
                validate_resume_manifest(saved, replace(config, model="different-model"))
            with self.assertRaisesRegex(BenchmarkError, "system prompt"):
                validate_resume_manifest(saved, replace(config, system_prompt="different prompt"))
            with self.assertRaisesRegex(BenchmarkError, "shuffle_options"):
                validate_resume_manifest(
                    saved,
                    replace(config, shuffle_options=True, shuffle_seed=42),
                )

        self.assertEqual(saved["run"]["id"], "run-123")
        self.assertEqual(saved["run"]["status"], "completed")
        self.assertEqual(saved["schema_version"], 3)
        self.assertEqual(saved["metrics"]["metric"], "Avg@2")
        self.assertEqual(saved["metrics"]["total"], 2)
        self.assertEqual([item["accuracy"] for item in saved["sample_metrics"]], [1, 1])
        self.assertEqual(saved["model"]["returned"], ["returned-model"])
        self.assertEqual(saved["usage"]["total_tokens"], 8)
        self.assertEqual(saved["reliability"]["total_attempts"], 2)
        self.assertEqual(saved["reliability"]["retries"], 0)
        self.assertEqual(saved["label_metrics"][0]["label_confidence"], "high")
        self.assertEqual(saved["label_metrics"][0]["target_choice_rate"], 1)
        self.assertEqual(saved["prompt"]["system"], SYSTEM_PROMPT)
        self.assertEqual(len(saved["artifacts"]["results_sha256"]), 64)
        self.assertNotIn("top-secret", json.dumps(saved))


if __name__ == "__main__":
    unittest.main()
