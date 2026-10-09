"""Analyse the frozen human gold of the paired audit (l1-paired-human-240-v1).

Written and committed before any label existed. Applies the paired contrasts of
pipeline.py to human labels, as preregistered in
annotation/preregistration/l1-paired-human-240-v1.json:

    python reports/l1-paired-avg16/human_analysis.py
"""

import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pipeline  # noqa: E402

GOLD = pathlib.Path("annotation/gold/l1-paired-human-240-v1")
PROTOCOL = pathlib.Path("annotation/preregistration/l1-paired-human-240-v1.json")
OUTPUT = GOLD / "analysis.json"
SEED = "l1-paired-human-240-v1"
MODELS = ("x-ai/grok-4.6", "google/gemini-3.7-flash")
COMBINED = {
    "refusal": {"engaged": "engaged", "refusal": "refusal"},
    "boundary": {"clean": "CLEAN", "violation": "VIOLATION", "refusal": "REFUSAL"},
    "success": {"achieved": "ACHIEVED", "not_achieved": "NOT_ACHIEVED"},
}


def combined_from_labels(rows):
    """Map gold rows to {(model, condition): {(question, index): combined labels}}."""
    arms = {}
    for row in rows:
        cell = arms.setdefault((row["model"], row["condition"]), {})
        labels = cell.setdefault((row["source_id"], row["sample_index"]), {})
        labels[row["axis"]] = COMBINED[row["axis"]][row["label"]]
    for cell in arms.values():
        for key, labels in cell.items():
            if set(labels) != {"refusal", "boundary", "success"}:
                raise ValueError("incomplete labels for {!r}".format(key))
    return arms


def analyse(rows, echo_keys):
    arms = combined_from_labels(rows)
    results, primary_p = {}, {}
    for model in MODELS:
        induced, neutral = arms[(model, "induced")], arms[(model, "neutral")]
        effect = pipeline.contrast(induced, neutral, "{}|{}".format(SEED, model))
        echoed = {(q, i) for m, c, q, i in echo_keys if m == model and c == "induced"}
        trimmed = {key: value for key, value in induced.items() if key not in echoed}
        effect["sensitivity_excluding_pressure_echo"] = {
            "excluded_induced_outputs": len(echoed),
            "violation": pipeline.paired_difference(
                pipeline.question_counts(trimmed, pipeline.OUTCOMES["violation"]),
                pipeline.question_counts(neutral, pipeline.OUTCOMES["violation"]),
                "{}|{}|echo".format(SEED, model), test=False),
        }
        primary_p[model] = effect["primary_violation"]["sign_flip_p"]
        results[model] = {"n_per_arm": {"induced": len(induced), "neutral": len(neutral)},
                          "induced_minus_neutral": effect}
    for model, adjusted in pipeline.holm(primary_p).items():
        results[model]["induced_minus_neutral"]["primary_violation"]["holm_adjusted_p"] = adjusted
    return results


def main():
    summary = json.loads((GOLD / "summary.json").read_text())
    labels_path = GOLD / "labels.jsonl"
    frozen_sha = summary["artifacts"]["labels_sha256"]
    if hashlib.sha256(labels_path.read_bytes()).hexdigest() != frozen_sha:
        raise ValueError("labels.jsonl differs from the frozen summary")
    protocol = json.loads(PROTOCOL.read_text())
    echo_keys = [tuple(key.split("|")[:3]) + (int(key.split("|")[3]),)
                 for key in protocol["blinding"]["pressure_echo_case_keys"]]
    rows = pipeline.read_jsonl(labels_path)
    analysis = {
        "batch_id": "l1-paired-human-240-v1",
        "protocol": str(PROTOCOL),
        "labels_sha256": summary["artifacts"]["labels_sha256"],
        "status": "human-labelled substudy; underpowered by design (see protocol power)",
        "models": analyse(rows, echo_keys),
    }
    OUTPUT.write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n")
    for model, data in analysis["models"].items():
        primary = data["induced_minus_neutral"]["primary_violation"]
        print("{:26s} 越界 induced {:.1%} neutral {:.1%} 差 {:+.1%} [{:+.1%},{:+.1%}] "
              "p={:.4f} Holm={:.4f}".format(
                  model, primary["first"], primary["second"], primary["difference"],
                  primary["ci95"][0], primary["ci95"][1], primary["sign_flip_p"],
                  primary["holm_adjusted_p"]))


if __name__ == "__main__":
    main()
