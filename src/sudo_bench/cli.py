import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Mapping, Optional
from uuid import uuid4

from sudo_bench import __version__
from sudo_bench.annotation import (
    AnnotationError,
    export_annotation_packet,
    merge_annotations,
)
from sudo_bench.api import ApiError, OpenAIChatClient
from sudo_bench.benchmark import (
    BenchmarkError,
    load_config,
    load_questions,
    read_result_run_id,
    run_benchmark,
    score_file,
)
from sudo_bench.reporting import (
    build_run_manifest,
    read_run_manifest,
    validate_resume_manifest,
    write_run_manifest,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _progress(index: int, total: int, row: Mapping[str, object]) -> None:
    if row["error"]:
        status = "error"
    elif row["format_error"]:
        status = "format-error"
    elif row["correct"]:
        status = "target-choice"
    else:
        status = "other-choice"
    sample = row.get("sample_index", 1)
    print(
        "[{}/{}] {}#{}: {}".format(index, total, row["id"], sample, status),
        file=sys.stderr,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sudo-bench")
    parser.add_argument("--version", action="version", version="%(prog)s {}".format(__version__))
    commands = parser.add_subparsers(dest="command", required=True)

    eval_parser = commands.add_parser("eval", help="run the benchmark from YAML config")
    eval_parser.add_argument("config", nargs="?", type=Path, default=Path("config.yaml"))
    eval_parser.add_argument("--quiet", action="store_true")
    eval_parser.add_argument("--resume", action="store_true", help="resume an existing result file")
    eval_parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="resume and replace samples whose final result is an API error",
    )

    score_parser = commands.add_parser("score", help="score an existing result JSONL")
    score_parser.add_argument("results", type=Path)
    score_parser.add_argument("--case-sensitive", action="store_true")

    annotation_parser = commands.add_parser(
        "annotation",
        help="export blind annotation packets and merge independent responses",
    )
    annotation_commands = annotation_parser.add_subparsers(
        dest="annotation_command",
        required=True,
    )
    export_parser = annotation_commands.add_parser(
        "export",
        help="create a public blind packet and a separate private mapping",
    )
    export_parser.add_argument("dataset", type=Path)
    export_parser.add_argument("output_dir", type=Path)
    export_parser.add_argument("--seed", type=int, required=True)
    export_parser.add_argument("--packet-id")
    export_parser.add_argument("--overwrite", action="store_true")

    merge_parser = annotation_commands.add_parser(
        "merge",
        help="validate and merge independent annotation response files",
    )
    merge_parser.add_argument("mapping", type=Path)
    merge_parser.add_argument("responses", nargs="+", type=Path)
    merge_parser.add_argument("--report", type=Path)
    merge_parser.add_argument("--adjudication", type=Path)
    merge_parser.add_argument("--min-annotators", type=int, default=3)
    merge_parser.add_argument("--overwrite", action="store_true")
    return parser


def _eval(
    config_path: Path,
    quiet: bool,
    resume_override: bool = False,
    retry_errors_override: bool = False,
) -> int:
    config = load_config(config_path)
    if resume_override or retry_errors_override:
        if config.overwrite:
            raise BenchmarkError("--resume/--retry-errors cannot be used with overwrite: true")
        config = replace(
            config,
            resume=True,
            retry_errors=config.retry_errors or retry_errors_override,
        )
    questions = load_questions(config.dataset)
    if config.output.exists() and not config.overwrite and not config.resume:
        raise FileExistsError(config.output)
    if config.manifest.exists() and not config.overwrite and not config.resume:
        raise FileExistsError(config.manifest)

    previous_manifest = read_run_manifest(config.manifest) if config.resume else None
    if previous_manifest is not None:
        validate_resume_manifest(previous_manifest, config)
    output_run_id = read_result_run_id(config.output) if config.resume else None
    previous_run = previous_manifest.get("run", {}) if previous_manifest else {}
    if (
        previous_manifest is not None
        and not config.output.exists()
        and previous_run.get("status") == "completed"
    ):
        raise BenchmarkError(
            "completed manifest exists but its result file is missing; use new artifact paths"
        )
    manifest_run_id = previous_run.get("id")
    if not isinstance(manifest_run_id, str) or not manifest_run_id.strip():
        manifest_run_id = None
    if output_run_id and manifest_run_id and output_run_id != manifest_run_id:
        raise BenchmarkError("result and manifest run ids do not match")

    continuing = config.resume and (config.output.exists() or previous_manifest is not None)
    run_id = output_run_id or manifest_run_id
    if run_id is None:
        run_id = "{}-{}".format(
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            uuid4().hex[:8],
        )
    previous_started_at = previous_run.get("started_at")
    started_at = (
        previous_started_at
        if continuing and isinstance(previous_started_at, str) and previous_started_at
        else _utc_now()
    )
    previous_resume_count = previous_run.get("resume_count", 0)
    if not isinstance(previous_resume_count, int) or isinstance(previous_resume_count, bool):
        previous_resume_count = 0
    resume_count = previous_resume_count + (1 if continuing else 0)
    session_started_at = _utc_now()

    client = OpenAIChatClient(
        model=config.model,
        base_url=config.base_url,
        api_key=config.api_key,
        timeout=config.timeout,
        temperature=config.temperature,
        require_parameters=config.require_parameters,
        max_tokens=config.max_tokens,
        system_prompt=config.system_prompt,
    )
    execution = {
        "session_started_at": session_started_at,
        "resume_enabled": config.resume,
        "continuing_existing_run": continuing,
    }
    running_manifest = build_run_manifest(
        config=config,
        config_path=config_path,
        metrics=None,
        run_id=run_id,
        started_at=started_at,
        completed_at=None,
        status="running",
        resume_count=resume_count,
        execution=execution,
    )
    write_run_manifest(running_manifest, config.manifest, overwrite=True)
    run_stats = dict(execution)
    try:
        metrics = run_benchmark(
            questions,
            client,
            config.output,
            overwrite=config.overwrite,
            case_sensitive=config.case_sensitive,
            concurrency=config.concurrency,
            samples_per_question=config.samples_per_question,
            run_id=run_id,
            resume=config.resume,
            retry_errors=config.retry_errors,
            max_attempts=config.max_attempts,
            backoff_initial_seconds=config.backoff_initial_seconds,
            backoff_max_seconds=config.backoff_max_seconds,
            requests_per_second=config.requests_per_second,
            shuffle_options=config.shuffle_options,
            shuffle_seed=config.shuffle_seed,
            run_stats=run_stats,
            progress=None if quiet else _progress,
        )
    except BaseException as exc:
        failed_at = _utc_now()
        run_stats["session_completed_at"] = failed_at
        run_stats["failure"] = "{}: {}".format(type(exc).__name__, exc)
        try:
            failed_manifest = build_run_manifest(
                config=config,
                config_path=config_path,
                metrics=None,
                run_id=run_id,
                started_at=started_at,
                completed_at=failed_at,
                status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                resume_count=resume_count,
                execution=run_stats,
            )
            write_run_manifest(failed_manifest, config.manifest, overwrite=True)
        except Exception:
            pass
        raise
    completed_at = _utc_now()
    run_stats["session_completed_at"] = completed_at
    manifest = build_run_manifest(
        config=config,
        config_path=config_path,
        metrics=metrics,
        run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
        status="completed",
        resume_count=resume_count,
        execution=run_stats,
    )
    write_run_manifest(manifest, config.manifest, overwrite=True)
    print(
        json.dumps(
            {
                "run_id": run_id,
                "model": config.model,
                "output": str(config.output),
                "manifest": str(config.manifest),
                "metrics": metrics.to_dict(),
            },
            ensure_ascii=False,
        )
    )
    return 2 if metrics.errors else 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "score":
            print(json.dumps(score_file(args.results, args.case_sensitive).to_dict()))
            return 0
        if args.command == "annotation":
            if args.annotation_command == "export":
                summary = export_annotation_packet(
                    args.dataset,
                    args.output_dir,
                    args.seed,
                    packet_id=args.packet_id,
                    overwrite=args.overwrite,
                )
            else:
                report_path = args.report or args.mapping.with_name("agreement-report.json")
                adjudication_path = args.adjudication or args.mapping.with_name(
                    "adjudication.jsonl"
                )
                summary = merge_annotations(
                    args.mapping,
                    args.responses,
                    report_path,
                    adjudication_path,
                    min_annotators=args.min_annotators,
                    overwrite=args.overwrite,
                )
            print(json.dumps(summary, ensure_ascii=False))
            return 0
        return _eval(args.config, args.quiet, args.resume, args.retry_errors)
    except (AnnotationError, BenchmarkError, ApiError, FileExistsError, OSError) as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
