"""Recompute the L1 MVP wall clock from the MEASURED per-model RPM ceiling and the
MEASURED real-prompt latency, instead of the assumed 0.25 rps / 15 rpm in the configs.

Throughput of one stage on one model is whichever binds first:
    rpm = min(rpm_ceiling * safety, 60 * concurrency / latency_p50)
so the recompute reports which of the two binds, because the fix differs:
rate-bound -> only a higher account tier helps; latency-bound -> raise concurrency.
"""

import json
import math
import sys

QUESTIONS = 15
SAMPLES = 16
SUBJECTS = ["anthropic/claude-opus-5", "google/gemini-3.7-flash", "x-ai/grok-4.6"]
JUDGE_CALLS_PER_SAMPLE = 2.1   # measured on this repo's own matched smoke judged.jsonl
SAFETY = 0.9                    # stay under the ceiling so retries stay rare


def fmt_hm(seconds):
    m = seconds / 60.0
    if m < 90:
        return "{:.0f} min".format(m)
    return "{:.1f} h".format(m / 60.0)


def stage(n_calls, ceiling_rpm, latency_s, concurrency):
    rate_cap = ceiling_rpm * SAFETY
    conc_cap = 60.0 * concurrency / latency_s if latency_s else float("inf")
    rpm = min(rate_cap, conc_cap)
    return {
        "calls": n_calls,
        "ceiling_rpm": ceiling_rpm,
        "throttle_rpm": round(rate_cap, 1),
        "requests_per_second": round(rate_cap / 60.0, 3),
        "latency_p50_s": latency_s,
        "concurrency": concurrency,
        "concurrency_cap_rpm": round(conc_cap, 1),
        "effective_rpm": round(rpm, 1),
        "binding": "rate limit" if rate_cap <= conc_cap else "latency/concurrency",
        "wall_s": n_calls / rpm * 60.0,
        "wall": fmt_hm(n_calls / rpm * 60.0),
        "min_concurrency_to_saturate": (
            max(1, math.ceil(rate_cap * latency_s / 60.0)) if latency_s else 1),
    }


def main():
    probe = json.load(open(sys.argv[1], encoding="utf-8"))
    ceilings = json.load(open(sys.argv[2], encoding="utf-8")) if len(sys.argv) > 2 else None

    by_model = {}
    for m in probe["models"]:
        real = m["phases"]["real_l1"]
        disc = m["phases"]["discover"]
        conf = m["phases"]["confirm_at_declared_rate"]
        ceiling = disc.get("declared_limit_rpm")
        if ceilings:
            for w in ceilings["waves"]:
                if w["model"] == m["model"]:
                    ceiling = int(w["declared_limit"]) if w.get("declared_limit") else (
                        ceiling or w["ok"])
        by_model[m["model"]] = {
            "ceiling_rpm": ceiling,
            "confirm_ok": conf["ok"],
            "confirm_429": conf["http_429"],
            "confirm_achieved_rpm": conf["achieved_rpm"],
            "real_latency_p50_s": real["latency_p50_s"],
            "real_latency_max_s": real["latency_max_s"],
            "completion_tokens_p50": real["completion_tokens_p50"],
            "cost_usd_per_call": real["cost_usd_per_call"],
        }

    print(json.dumps(by_model, ensure_ascii=False, indent=2))
    print()

    n_per_subject = QUESTIONS * SAMPLES
    print("=== Stage 1: L1 runner, induced, Avg@16 (3 subject models in parallel buckets) ===")
    runner_walls = []
    runner_cost = 0.0
    for model in SUBJECTS:
        d = by_model[model]
        conc = 8
        s = stage(n_per_subject, d["ceiling_rpm"], d["real_latency_p50_s"], conc)
        cost = (d["cost_usd_per_call"] or 0) * n_per_subject
        runner_cost += cost
        runner_walls.append(s["wall_s"])
        print("  {:26s} {:4d} calls  ceiling {:>3} rpm  lat {:>5.1f}s  eff {:>5.1f} rpm"
              "  -> {:>8s}  ({}-bound)  ~${:.2f}".format(
                  model, s["calls"], s["ceiling_rpm"], s["latency_p50_s"], s["effective_rpm"],
                  s["wall"], s["binding"], cost))
        print("       throttle to requests_per_second: {}  concurrency>= {} to saturate".format(
            s["requests_per_second"], s["min_concurrency_to_saturate"]))
    print("  stage wall (parallel): {}   sequential: {}   cost ~${:.2f}".format(
        fmt_hm(max(runner_walls)), fmt_hm(sum(runner_walls)), runner_cost))
    print()

    print("=== Stage 2: two cross-vendor judges ({} calls/sample measured) ===".format(
        JUDGE_CALLS_PER_SAMPLE))
    judge_plan = [
        ("openai/gpt-5.6-sol", "judge A", 3 * n_per_subject, "all 3 subjects"),
        ("google/gemini-3.7-flash", "judge B", 2 * n_per_subject,
         "anthropic+x-ai only; cross-vendor guard blocks judging google subject"),
    ]
    judge_walls = []
    judge_cost = 0.0
    for model, role, samples, note in judge_plan:
        d = by_model[model]
        calls = round(samples * JUDGE_CALLS_PER_SAMPLE)
        s = stage(calls, d["ceiling_rpm"], d["real_latency_p50_s"], 8)
        cost = (d["cost_usd_per_call"] or 0) * calls
        judge_cost += cost
        judge_walls.append(s["wall_s"])
        print("  {:26s} {:5s} {:4d} calls  ceiling {:>3} rpm  eff {:>5.1f} rpm -> {:>8s}"
              "  ({}-bound)  ~${:.2f}".format(
                  model, role, calls, s["ceiling_rpm"], s["effective_rpm"], s["wall"],
                  s["binding"], cost))
        print("       {}".format(note))
        print("       throttle to requests_per_second: {}  concurrency>= {} to saturate".format(
            s["requests_per_second"], s["min_concurrency_to_saturate"]))
    print("  stage wall (parallel): {}   sequential: {}   cost ~${:.2f}".format(
        fmt_hm(max(judge_walls)), fmt_hm(sum(judge_walls)), judge_cost))
    print()

    total_par = max(runner_walls) + max(judge_walls)
    total_seq = sum(runner_walls) + sum(judge_walls)
    print("=== Total ===")
    print("  all buckets in parallel : {}".format(fmt_hm(total_par)))
    print("  everything sequential   : {}".format(fmt_hm(total_seq)))
    print("  API cost (excl. retries): ~${:.2f}".format(runner_cost + judge_cost))
    print()
    print("=== For comparison: the configs' current assumption ===")
    for model in SUBJECTS:
        d = by_model[model]
        s_old = stage(n_per_subject, 15, d["real_latency_p50_s"], 1)   # rps 0.25, concurrency 1
        print("  {:26s} at 0.25 rps / concurrency 1 -> {}".format(model, s_old["wall"]))


if __name__ == "__main__":
    main()
