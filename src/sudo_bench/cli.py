import argparse
import json
import sys
from pathlib import Path
from typing import List, Mapping, Optional

from sudo_bench import __version__
from sudo_bench.api import ApiError, OpenAIChatClient
from sudo_bench.benchmark import (
    BenchmarkError,
    load_config,
    load_questions,
    run_benchmark,
    score_file,
)


def _progress(index: int, total: int, row: Mapping[str, object]) -> None:
    if row["error"]:
        status = "error"
    elif row["format_error"]:
        status = "format-error"
    elif row["correct"]:
        status = "correct"
    else:
        status = "incorrect"
    print("[{}/{}] {}: {}".format(index, total, row["id"], status), file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sudo-bench")
    parser.add_argument("--version", action="version", version="%(prog)s {}".format(__version__))
    commands = parser.add_subparsers(dest="command", required=True)

    eval_parser = commands.add_parser("eval", help="run the benchmark from YAML config")
    eval_parser.add_argument("config", nargs="?", type=Path, default=Path("config.yaml"))
    eval_parser.add_argument("--quiet", action="store_true")

    score_parser = commands.add_parser("score", help="score an existing result JSONL")
    score_parser.add_argument("results", type=Path)
    score_parser.add_argument("--case-sensitive", action="store_true")
    return parser


def _eval(config_path: Path, quiet: bool) -> int:
    config = load_config(config_path)
    questions = load_questions(config.dataset)
    client = OpenAIChatClient(
        model=config.model,
        base_url=config.base_url,
        api_key=config.api_key,
        timeout=config.timeout,
        temperature=config.temperature,
        require_parameters=config.require_parameters,
        max_tokens=config.max_tokens,
    )
    metrics = run_benchmark(
        questions,
        client,
        config.output,
        overwrite=config.overwrite,
        case_sensitive=config.case_sensitive,
        concurrency=config.concurrency,
        progress=None if quiet else _progress,
    )
    print(
        json.dumps(
            {"model": config.model, "output": str(config.output), "metrics": metrics.to_dict()},
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
        return _eval(args.config, args.quiet)
    except (BenchmarkError, ApiError, FileExistsError, OSError) as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
