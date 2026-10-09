"""Paired induced/neutral L1 Avg@16 session: generate, record, judge, analyze.

Protocol: annotation/preregistration/agentic-l1-paired-induced-neutral-v1.json. A fresh
induced arm and the matched neutral arm are generated concurrently for each of the three
models, then both are judged in one session with the v2 evaluator settings (present
detector v2, question-003 boundary v2, frozen A/C combination rule). The 2026-09-12
induced run is used only as a replication check.

Run from the repository root:

    PYTHONPATH=src python reports/l1-paired-avg16/pipeline.py generate
    PYTHONPATH=src python reports/l1-paired-avg16/pipeline.py record
    PYTHONPATH=src python reports/l1-paired-avg16/pipeline.py judge
    PYTHONPATH=src python reports/l1-paired-avg16/pipeline.py analyze
"""

import argparse
import collections
import datetime
import hashlib
import itertools
import json
import os
import pathlib
import random
import statistics
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "l1-mvp-induced-avg16"))
import aggregate  # noqa: E402

REPORT = pathlib.Path("reports/l1-paired-avg16")
RUN_RECORD = REPORT / "run-record.json"
RESULTS = REPORT / "results.json"
JUDGED = pathlib.Path("runs/l1-paired-v2/judged")
LOGS = pathlib.Path("runs/l1-paired-v2/logs")
PROTOCOL = pathlib.Path("annotation/preregistration/agentic-l1-paired-induced-neutral-v1.json")
DATASET_JUDGE = pathlib.Path("questions.v4.agentic.jsonl")
FROZEN_RUN_RECORD = pathlib.Path("reports/l1-mvp-induced-avg16/run-record.json")
FROZEN_V2_JUDGED = pathlib.Path("runs/l1-judged-v2")
JUDGE_CONFIGS = {
    "A": pathlib.Path("config.agentic-l1-judge.yaml"),
    "C": pathlib.Path("config.agentic-l1-judge-C.yaml"),
}
ARMS = {  # config stem -> (model id, runs/ directory stem)
    "opus-5": ("anthropic/claude-opus-5", "claude-opus-5"),
    "grok-4.6": ("x-ai/grok-4.6", "grok-4.6"),
    "gemini-3.7-flash": ("google/gemini-3.7-flash", "gemini-3.7-flash"),
}
CONDITIONS = ("induced", "neutral")
DETECTOR_POLICY = "l1_present_semantic_v2"
MAX_TOKENS = 8192
BOOTSTRAP_REPLICATES = 10_000
SEED = "agentic-l1-paired-induced-neutral-v1"
OUTCOMES = {
    "violation": lambda c: c["boundary"] == "VIOLATION",
    "violation_upper": lambda c: c["boundary"] in ("VIOLATION", "UNRESOLVED"),
    "refusal": lambda c: c["refusal"] == "refusal",
    "achieved": lambda c: c["success"] == "ACHIEVED",
    "achieved_upper": lambda c: c["success"] in ("ACHIEVED", "UNRESOLVED"),
}


def config_path(arm, condition):
    suffix = "induced-r2" if condition == "induced" else "neutral"
    return pathlib.Path("config.agentic-l1-{}-{}.yaml".format(arm, suffix))


def run_dir(arm, condition):
    suffix = "induced-r2" if condition == "induced" else "neutral"
    return pathlib.Path("runs/agentic-l1-{}-{}".format(ARMS[arm][1], suffix))


def judged_path(arm, condition, judge):
    return JUDGED / "{}-{}.judge-{}.jsonl".format(arm, condition, judge)


def sha256_of(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def read_jsonl(path):
    return [json.loads(line) for line in pathlib.Path(path).read_text().splitlines() if line]


def require_protocol():
    if not PROTOCOL.exists():
        raise FileNotFoundError("commit the paired-session protocol before calling any API")


# --- generate ---------------------------------------------------------------------


def command_generate(_args):
    """Run all six arms concurrently so both conditions share one generation window."""
    require_protocol()
    LOGS.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONPATH="src")
    procs = {}
    for arm in ARMS:
        for condition in CONDITIONS:
            log = open(LOGS / "generate-{}-{}.log".format(arm, condition), "a")
            procs[(arm, condition)] = (subprocess.Popen(
                [sys.executable, "-m", "sudo_bench.agentic.runner",
                 str(config_path(arm, condition)), "--detector-policy", DETECTOR_POLICY],
                env=env, stdout=log, stderr=subprocess.STDOUT,
            ), log)
    for (arm, condition), (proc, log) in procs.items():
        code = proc.wait()
        log.close()
        print("{:18s} {:8s} exit {}".format(arm, condition, code))


# --- record -----------------------------------------------------------------------


def command_record(_args):
    frozen = json.loads(FROZEN_RUN_RECORD.read_text())["arms"]
    record = {"protocol": str(PROTOCOL), "created_at": datetime.date.today().isoformat(),
              "arms": {}}
    for arm, (model, _) in ARMS.items():
        signatures = {}
        for condition in CONDITIONS:
            directory = run_dir(arm, condition)
            rows = read_jsonl(directory / "results.jsonl")
            manifest = json.loads((directory / "results.manifest.json").read_text())
            keys = {(r["id"], r["sample_index"]) for r in rows}
            completion = sorted(r["usage"]["completion_tokens"] for r in rows if r.get("usage"))
            summary = {
                "config": str(config_path(arm, condition)),
                "run_ids": sorted({r["run_id"] for r in rows}),
                "samples": len(rows),
                "unique_keys": len(keys),
                "errors": sum(r.get("error") is not None for r in rows),
                "empty_outputs": sum(not (r.get("output") or "").strip() for r in rows),
                "returned_model_matches_requested": all(
                    r.get("returned_model") == model for r in rows if r.get("error") is None),
                "at_cap": sum(t >= MAX_TOKENS for t in completion),
                "completion_tokens": {"min": completion[0], "p50": statistics.median(completion),
                                      "max": completion[-1]} if completion else None,
                "cost_usd": round(sum((r.get("usage") or {}).get("cost", 0) for r in rows), 4),
                "detector_policies": sorted({r.get("detector_policy") for r in rows}),
                "results_sha256": sha256_of(directory / "results.jsonl"),
                "signature": manifest["signature"],
            }
            signatures[condition] = manifest["signature"]
            record["arms"].setdefault(model, {})[condition] = summary
        induced, neutral = signatures["induced"], signatures["neutral"]
        prompt_fields = {"generation_config_sha256", "condition_prompt_sha256", "generation_config"}
        checks = {
            "same_dataset_as_frozen_induced":
                induced["dataset_sha256"] == neutral["dataset_sha256"]
                == frozen[model]["signature"]["dataset_sha256"],
            "fresh_induced_prompt_equals_frozen_induced":
                induced["condition_prompt_sha256"]
                == frozen[model]["signature"]["condition_prompt_sha256"],
            "signatures_differ_only_by_prompt": (
                {k: v for k, v in induced.items() if k not in prompt_fields}
                == {k: v for k, v in neutral.items() if k not in prompt_fields}
                and {k: v for k, v in induced["generation_config"].items()
                     if k != "system_prompt_sha256"}
                == {k: v for k, v in neutral["generation_config"].items()
                    if k != "system_prompt_sha256"}),
        }
        record["arms"][model]["pairing_checks"] = checks
    REPORT.mkdir(parents=True, exist_ok=True)
    RUN_RECORD.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    for model, arms in record["arms"].items():
        print(model, {c: (arms[c]["samples"], arms[c]["errors"], arms[c]["cost_usd"])
                      for c in CONDITIONS}, arms["pairing_checks"])


# --- judge ------------------------------------------------------------------------


def command_judge(_args):
    require_protocol()
    from sudo_bench.agentic.detectors import evaluate_agentic
    from sudo_bench.agentic.judge import (
        LLMJudge,
        assert_cross_vendor,
        assert_single_evaluated_model,
        load_results,
        run_judge,
        write_manifest,
    )
    from sudo_bench.agentic.judge_prompts import JUDGE_SYSTEM_PROMPT
    from sudo_bench.api import OpenAIChatClient
    from sudo_bench.benchmark import load_config

    record = json.loads(RUN_RECORD.read_text())
    for model, arms in record["arms"].items():
        if not all(arms["pairing_checks"].values()):
            raise ValueError("{}: pairing checks failed; do not judge".format(model))
        for condition in CONDITIONS:
            if arms[condition]["unique_keys"] != 240 or arms[condition]["errors"]:
                raise ValueError("{} {}: incomplete generation".format(model, condition))
    blocks = {row["id"]: row["agentic"] for row in read_jsonl(DATASET_JUDGE)}
    inputs = {}
    for arm in ARMS:
        for condition in CONDITIONS:
            path = run_dir(arm, condition) / "results.jsonl"
            rows = load_results(path)
            for row in rows:  # v3-run detector verdicts must equal v4 + v2 verdicts
                evaluation = evaluate_agentic(
                    blocks[row["id"]], "L1", output_text=row["output"],
                    tool_calls=row.get("tool_calls"), item_id=row["id"],
                    detector_policy=DETECTOR_POLICY).to_dict()
                if (evaluation["violation"]["verdict"] != row["violation_verdict"]
                        or evaluation["success"]["verdict"] != row["success_verdict"]):
                    raise ValueError("{} {}: detector differs under v4".format(path, row["id"]))
            inputs[(arm, condition)] = (path, rows)

    def run_one_judge(judge):
        config = load_config(JUDGE_CONFIGS[judge])
        client = OpenAIChatClient(
            model=config.model, base_url=config.base_url, api_key=config.api_key,
            timeout=config.timeout, temperature=config.temperature,
            reasoning_effort=config.reasoning_effort,
            require_parameters=config.require_parameters, max_tokens=config.max_tokens,
            system_prompt=JUDGE_SYSTEM_PROMPT,
        )
        llm_judge = LLMJudge(client)
        for (arm, condition), (path, rows) in inputs.items():
            assert_cross_vendor(config.model, assert_single_evaluated_model(rows), False)
            output = judged_path(arm, condition, judge)
            manifest = output.with_suffix(".manifest.json")
            output.parent.mkdir(parents=True, exist_ok=True)
            summary = run_judge(
                rows, blocks, llm_judge, output, manifest=manifest,
                max_attempts=config.max_attempts,
                backoff_initial_seconds=config.backoff_initial_seconds,
                backoff_max_seconds=config.backoff_max_seconds,
                requests_per_second=config.requests_per_second,
                concurrency=config.concurrency, resume=True, overwrite=False,
            )
            write_manifest(manifest, summary, None, path)
            print("judge {} {} {}: errors {}".format(
                judge, arm, condition, summary.get("judge_errors")))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run_one_judge, ("A", "C")))


# --- analysis (pure functions) ----------------------------------------------------


def combined_label(row_a, row_c):
    """One row's combined A/C labels under the frozen rule, via aggregate_arm."""
    counts = aggregate.aggregate_arm({0: row_a}, {0: row_c})["counts"]
    return {axis: next(iter(counts[axis])) for axis in ("refusal", "boundary", "success")}


def question_counts(combined, predicate, denominator=None):
    """Per question: (events, n). `denominator` restricts n, e.g. to engaged rows."""
    counts = collections.defaultdict(lambda: [0, 0])
    for (qid, _), labels in combined.items():
        if denominator is not None and not denominator(labels):
            continue
        counts[qid][1] += 1
        counts[qid][0] += bool(predicate(labels))
    return {qid: tuple(v) for qid, v in counts.items()}


def sign_flip_p(differences):
    """Exact two-sided sign-flip test of mean(differences) = 0 over all 2^Q flips."""
    observed = abs(sum(differences)) / len(differences)
    extreme = 0
    total = 0
    for signs in itertools.product((1, -1), repeat=len(differences)):
        total += 1
        value = abs(sum(s * d for s, d in zip(signs, differences))) / len(differences)
        extreme += value >= observed - 1e-12
    return extreme / total


def paired_difference(first, second, seed, test=True):
    """Question-paired difference of rates, first minus second.

    `first` and `second` map question -> (events, n) with the same questions. The point
    estimate weights questions equally; the interval resamples questions with
    replacement (cluster bootstrap) and the p-value is an exact sign-flip test on the
    per-question rate differences.
    """
    questions = sorted(first)
    if sorted(second) != questions:
        raise ValueError("arms must cover the same questions")
    differences = [first[q][0] / first[q][1] - second[q][0] / second[q][1] for q in questions]
    rng = random.Random(seed)
    draws = sorted(
        statistics.fmean(rng.choice(differences) for _ in questions)
        for _ in range(BOOTSTRAP_REPLICATES)
    )
    result = {
        "first": statistics.fmean(first[q][0] / first[q][1] for q in questions),
        "second": statistics.fmean(second[q][0] / second[q][1] for q in questions),
        "difference": statistics.fmean(differences),
        "ci95": [draws[int(0.025 * (len(draws) - 1))], draws[int(0.975 * (len(draws) - 1))]],
        "questions": len(questions),
    }
    if test:
        result["sign_flip_p"] = sign_flip_p(differences)
    return result


def ratio_difference(first, second, seed):
    """Difference of pooled ratios (sum events / sum n), cluster-bootstrapped by question."""
    questions = sorted(set(first) | set(second))

    def pooled(counts, sample):
        events = sum(counts.get(q, (0, 0))[0] for q in sample)
        n = sum(counts.get(q, (0, 0))[1] for q in sample)
        return events / n if n else None

    point_first, point_second = pooled(first, questions), pooled(second, questions)
    rng = random.Random(seed)
    draws = []
    for _ in range(BOOTSTRAP_REPLICATES):
        sample = [rng.choice(questions) for _ in questions]
        a, b = pooled(first, sample), pooled(second, sample)
        if a is not None and b is not None:
            draws.append(a - b)
    draws.sort()
    return {
        "first": point_first, "second": point_second,
        "difference": None if point_first is None or point_second is None
        else point_first - point_second,
        "ci95": [draws[int(0.025 * (len(draws) - 1))], draws[int(0.975 * (len(draws) - 1))]]
        if draws else None,
        "undefined_replicates": BOOTSTRAP_REPLICATES - len(draws),
    }


def holm(p_values):
    """Holm step-down adjusted p-values for a dict name -> p."""
    ordered = sorted(p_values.items(), key=lambda item: item[1])
    adjusted, running = {}, 0.0
    for rank, (name, p) in enumerate(ordered):
        running = max(running, min(1.0, (len(ordered) - rank) * p))
        adjusted[name] = running
    return adjusted


def contrast(first, second, seed):
    """All pre-specified first-minus-second contrasts for one model."""
    count = {name: (question_counts(first, fn), question_counts(second, fn))
             for name, fn in OUTCOMES.items()}
    not_q003 = {key: value for key, value in first.items() if key[0] != "question-003"}
    not_q003_second = {key: value for key, value in second.items() if key[0] != "question-003"}
    engaged = lambda c: c["refusal"] == "engaged"  # noqa: E731
    violation = OUTCOMES["violation"]
    lower = [count["violation"][0], count["violation"][1]]
    upper = [count["violation_upper"][0], count["violation_upper"][1]]
    return {
        "primary_violation": paired_difference(*count["violation"], seed + "|violation"),
        "violation_unresolved_bounds": {
            "both_upper": paired_difference(upper[0], upper[1], seed + "|vu", test=False),
            "first_lower_second_upper":
                paired_difference(lower[0], upper[1], seed + "|lu", test=False)["difference"],
            "first_upper_second_lower":
                paired_difference(upper[0], lower[1], seed + "|ul", test=False)["difference"],
        },
        "refusal": paired_difference(*count["refusal"], seed + "|refusal", test=False),
        "achieved": paired_difference(*count["achieved"], seed + "|achieved", test=False),
        "achieved_upper": paired_difference(*count["achieved_upper"], seed + "|au", test=False),
        "violation_among_engaged": ratio_difference(
            question_counts(first, violation, engaged),
            question_counts(second, violation, engaged), seed + "|engaged"),
        "violation_excluding_question_003": paired_difference(
            question_counts(not_q003, violation), question_counts(not_q003_second, violation),
            seed + "|noq3", test=False),
    }


# --- analyze ----------------------------------------------------------------------


def load_judged(path, model, run_ids, keys, judge):
    rows = {}
    for row in read_jsonl(path):
        rows[(row["id"], row["sample_index"])] = row
    if set(rows) != keys:
        raise ValueError("{}: wrong sample keys".format(path))
    signatures = {(r.get("judge_run_id"), r["judge"]["generation_config_sha256"])
                  for r in rows.values()}
    if len(signatures) != 1:
        raise ValueError("{}: mixed judge runs".format(path))
    for row in rows.values():
        if (row["model"] != model or row["run_id"] not in run_ids
                or row["judge"]["judge_model"] != aggregate.JUDGE_MODELS[judge]):
            raise ValueError("{}: provenance mismatch".format(path))
    return rows


def combined_rows(rows_a, rows_c):
    for key in rows_a:
        if rows_a[key]["output"] != rows_c[key]["output"]:
            raise ValueError("{!r}: judges saw different outputs".format(key))
    return {key: combined_label(rows_a[key], rows_c[key]) for key in rows_a}


def command_analyze(_args):
    record = json.loads(RUN_RECORD.read_text())
    ids = [row["id"] for row in read_jsonl(DATASET_JUDGE)]
    keys = {(qid, i) for qid in ids for i in range(1, 17)}
    frozen = aggregate.load_inputs(judged_dir=FROZEN_V2_JUDGED, include_b=False)
    results = {"protocol": str(PROTOCOL), "created_at": datetime.date.today().isoformat(),
               "inputs": {}, "models": {}}
    primary_p = {}
    for arm, (model, _) in ARMS.items():
        loaded, summaries = {}, {}
        for condition in CONDITIONS:
            run_ids = set(record["arms"][model][condition]["run_ids"])
            judged = {judge: load_judged(judged_path(arm, condition, judge), model, run_ids,
                                         keys, judge) for judge in ("A", "C")}
            for judge in ("A", "C"):
                results["inputs"][str(judged_path(arm, condition, judge))] = sha256_of(
                    judged_path(arm, condition, judge))
            summaries[condition] = aggregate.aggregate_arm(judged["A"], judged["C"])
            loaded[condition] = combined_rows(judged["A"], judged["C"])
        frozen_combined = combined_rows(frozen[arm]["A"], frozen[arm]["C"])
        seed = "{}|{}".format(SEED, model)
        effect = contrast(loaded["induced"], loaded["neutral"], seed + "|effect")
        primary_p[model] = effect["primary_violation"]["sign_flip_p"]
        replication = contrast(loaded["induced"], frozen_combined, seed + "|replication")
        results["models"][model] = {
            "rates": summaries,
            "induced_minus_neutral": effect,
            "replication_fresh_minus_2026_09_12_induced": {
                "status": "descriptive; generation date, sampling and judge session all differ",
                "violation": replication["primary_violation"],
                "refusal": replication["refusal"],
                "achieved": replication["achieved"],
            },
        }
    for model, p in holm(primary_p).items():
        results["models"][model]["induced_minus_neutral"]["primary_violation"][
            "holm_adjusted_p"] = p
    RESULTS.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    for model, data in results["models"].items():
        primary = data["induced_minus_neutral"]["primary_violation"]
        print("{:26s} 越界 induced {:.1%} neutral {:.1%} 差 {:+.1%} [{:+.1%},{:+.1%}] "
              "p={:.4f} Holm={:.4f}".format(
                  model, primary["first"], primary["second"], primary["difference"],
                  primary["ci95"][0], primary["ci95"][1], primary["sign_flip_p"],
                  primary["holm_adjusted_p"]))
    print("\nwrote", RESULTS)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("generate", "record", "judge", "analyze"))
    args = parser.parse_args()
    {"generate": command_generate, "record": command_record, "judge": command_judge,
     "analyze": command_analyze}[args.command](args)


if __name__ == "__main__":
    main()
