"""Offline entrypoint failure semantics, clean-checkout inputs and no-network checks."""

import json
import shutil
import socket
import urllib.request
from pathlib import Path

import pytest

from sudo_bench import offline
from sudo_bench.cli import main

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def public_checkout(tmp_path):
    # Copy only the documented public set: no .git, secrets or private runs.
    for relative in offline.PUBLIC_FILES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    return tmp_path


def test_public_inputs_and_calibrations_work_without_private_data(public_checkout, monkeypatch):
    monkeypatch.chdir(public_checkout.parent)
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-a-real-key-and-not-needed")
    ctx = offline.Context(public_checkout)
    with offline.network_disabled():
        assert offline.check_public_files(ctx)["required_public_files"] == len(offline.PUBLIC_FILES)
        assert offline.check_datasets(ctx)["core_items"] == 15
        assert offline.check_heldout_calibration(ctx)["cases"] == 60
        assert offline.check_refusal_calibration(ctx)["cases"] == 98
        agreement = offline.check_q003_automated(ctx)
    assert agreement["raw_pairs"] == 24
    assert agreement["engaged_pairs"] == 23
    assert agreement["raw_agreement"] == pytest.approx(21 / 24)
    assert agreement["engaged_agreement"] == pytest.approx(20 / 23)


def test_human_recompute_preserves_all_frozen_inputs_and_ignores_optional_private_file(
    public_checkout,
):
    ctx = offline.Context(public_checkout)
    frozen = {p: ctx.digest(p) for p in offline.PUBLIC_FILES}
    private = public_checkout / "annotation/generated/question-003-boundary-v2-human-24-v1"
    private.mkdir(parents=True)
    (private / "pre_arbitration_labels.json").write_text("not JSON; must never be read")
    with offline.network_disabled():
        result = offline.check_analysis_script(ctx, offline.Q3_HUMAN)
    assert result["private_inputs_used"] is False
    assert {p: ctx.digest(p) for p in frozen} == frozen


def test_missing_public_file_is_an_error(public_checkout):
    (public_checkout / "runs/heldout-calibration/judged-a.jsonl").unlink()
    with pytest.raises(FileNotFoundError):
        offline.check_public_files(offline.Context(public_checkout))


def test_rubric_drift_fails_even_if_model_prompts_stay_identical(public_checkout):
    path = public_checkout / "questions.v4.agentic.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["agentic"]["ground_truth"]["boundary"] = "changed rubric with unchanged prompt"
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n")
    with pytest.raises(offline.VerificationError, match="SHA-256 mismatch"):
        offline.check_datasets(offline.Context(public_checkout))


@pytest.mark.parametrize("value", [
    '{"id":"q1","id":"q2"}', '{"n":NaN}', '{"n":Infinity}', '{"n":1e9999}',
])
def test_nonstandard_or_duplicate_key_json_is_rejected(value):
    with pytest.raises(offline.VerificationError):
        offline.parse_json(value)


def test_duplicate_jsonl_rows_are_rejected(tmp_path):
    (tmp_path / "cases.jsonl").write_text('{"case_key":"q","axis":"boundary"}\n' * 2)
    with pytest.raises(offline.VerificationError, match="duplicate row"):
        offline.Context(tmp_path).rows("cases.jsonl")


@pytest.mark.parametrize("index", [True, 0, -1, "1", None])
def test_malformed_sample_indices_cannot_alias_valid_samples(tmp_path, index):
    (tmp_path / "results.jsonl").write_text(json.dumps({"id": "q1", "sample_index": index}))
    with pytest.raises(offline.VerificationError, match="invalid sample_index"):
        offline.Context(tmp_path).rows("results.jsonl")


def test_changed_artifact_fails_its_hash_check(tmp_path):
    source = tmp_path / "source.json"
    source.write_text('{"n":1}')
    ctx = offline.Context(tmp_path)
    committed = ctx.digest("source.json")
    source.write_text('{"n":2}')
    with pytest.raises(offline.VerificationError, match="SHA-256 mismatch"):
        ctx.hash_matches("source.json", committed)


@pytest.mark.parametrize("relative", ["../private.json", "/tmp/outside.json"])
def test_artifact_references_cannot_escape_checkout(tmp_path, relative):
    with pytest.raises(offline.VerificationError):
        offline.Context(tmp_path).path(relative)


@pytest.mark.parametrize("actual,expected", [
    (True, 1), (1, 2), ([1], [1, 2]), ({"a": 1}, {"b": 1}), (float("nan"), 1),
])
def test_report_comparison_detects_structural_and_material_changes(actual, expected):
    with pytest.raises(offline.VerificationError):
        offline.same(actual, expected)


def test_comparison_allows_only_roundoff_and_object_key_order():
    offline.same({"a": [1.0 + 1e-13], "b": None}, {"b": None, "a": [1.0]})
    with pytest.raises(offline.VerificationError):
        offline.same(0.125, 0.0625)


@pytest.mark.parametrize("connect", [
    lambda: socket.create_connection(("127.0.0.1", 9)),
    lambda: urllib.request.urlopen("https://example.invalid"),
])
def test_network_operations_fail_before_any_request(connect):
    with offline.network_disabled():
        with pytest.raises(offline.VerificationError, match="network access is disabled"):
            connect()


def test_absent_private_files_are_reported_separately(public_checkout):
    ctx = offline.Context(public_checkout)
    with pytest.raises(offline.MissingPrivateData) as failure:
        offline.check_private_l1(ctx)
    assert len(failure.value.paths) == 6
    assert failure.value.verified_present == 0
    with pytest.raises(offline.MissingPrivateData) as failure:
        offline.check_private_l0(ctx)
    assert len(failure.value.paths) == 12


def test_existing_private_corruption_is_not_hidden_by_other_missing_files(public_checkout):
    ctx = offline.Context(public_checkout)
    report = ctx.json(offline.L1 + "/rescoring-v2/judged-results-v2.json")
    path = public_checkout / next(iter(report["inputs"]["v2_judged_sha256"]))
    path.parent.mkdir(parents=True)
    path.write_text('{}\n')
    with pytest.raises(offline.VerificationError, match="SHA-256 mismatch"):
        offline.check_private_l1(ctx)


def test_failure_does_not_stop_other_checks_and_controls_exit_status(tmp_path, monkeypatch):
    def fail(_ctx):
        raise offline.VerificationError("changed published metric")

    def missing(_ctx):
        raise offline.MissingPrivateData(["runs/private/results.jsonl"])

    monkeypatch.setattr(offline, "checks", lambda: (
        ("public.ok", "public", lambda _: {"n": 3}),
        ("public.bad", "public", fail),
        ("private.absent", "private", missing),
    ))
    report = offline.verify(tmp_path)
    assert report["summary"] == {"PASS": 1, "FAIL": 1, "MISSING_PRIVATE_DATA": 1}
    assert report["status"] == "FAIL"
    assert offline.exit_code(report) == 1
    assert offline.exit_code(report, require_private=True) == 1


def test_unexpected_network_use_is_recorded_as_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(offline, "checks", lambda: (
        ("public.accidental_network", "public", lambda _: socket.create_connection(("x", 1))),
    ))
    report = offline.verify(tmp_path)
    assert report["summary"]["FAIL"] == 1
    assert "network access is disabled" in report["checks"][0]["error"]


@pytest.mark.parametrize("strict,expected", [(False, 0), (True, 2)])
def test_cli_emits_report_without_keys_and_marks_partial_reproduction(
    tmp_path, monkeypatch, capsys, strict, expected,
):
    def missing(_ctx):
        raise offline.MissingPrivateData(["runs/private/results.jsonl"])

    def forbid_api(*_args, **_kwargs):
        pytest.fail("offline command instantiated an API client")

    monkeypatch.setattr("sudo_bench.cli.OpenAIChatClient", forbid_api)
    monkeypatch.setattr(offline, "checks", lambda: (
        ("public.ok", "public", lambda _: {"n": 3}),
        ("private.absent", "private", missing),
    ))
    args = ["verify-offline", "--root", str(tmp_path)]
    if strict:
        args.append("--require-private")
    assert main(args) == expected
    report = json.loads((tmp_path / "outputs/offline-verification.json").read_text())
    assert report["status"] == "PASS_WITH_LIMITATIONS"
    assert report["network"] == "disabled"
    assert report["model_api_calls"] == 0
    assert "MISSING_PRIVATE_DATA" in capsys.readouterr().out


def test_report_cannot_overwrite_a_frozen_artifact(tmp_path):
    frozen = tmp_path / "gold.json"
    frozen.write_text('{"frozen":true}')
    with pytest.raises(offline.VerificationError, match="refusing to overwrite"):
        offline.write_report(frozen, {"kind": "goalbound-offline-verification"})
    assert frozen.read_text() == '{"frozen":true}'
