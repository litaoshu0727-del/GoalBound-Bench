"""Where do judges A and C disagree on the 720 L1 induced outputs? No API calls, no labels.

Descriptive diagnostics, planned and committed before the first run. Data: the per-judge
verdicts of the v2 pipeline (runs/l1-judged-v2/, private) for Claude Opus 5, Grok 4.6 and
Gemini 3.7 Flash.

For each axis (refusal, boundary, success):

1. Per model and per question, among outputs where both judges gave a verdict on that axis
   (boundary and success are only asked when that judge saw the output as engaged and the
   detector was not decisive): number compared, disagreements, rate and direction
   ("A=x|C=y"), plus Cohen's kappa between A and C per model.
2. Questions ranked by disagreement rate pooled over the three models, as a pointer to the
   rubrics most worth clarifying.
3. On disagreement outputs that already have a human label (the 120-case real-output audit,
   the 24 question-003 audit outputs, the 21 detector-audit outputs), how often A, C or
   neither matched the human label.

Pointers only: the counts are small, the human-labelled subset is in-sample, and nothing
here changes any frozen rule.

    PYTHONPATH=src python reports/l1-judge-disagreement/diagnose.py
"""

import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "l1-mvp-induced-avg16"))
import aggregate  # noqa: E402

V2_JUDGED = pathlib.Path("runs/l1-judged-v2")
AUDIT = pathlib.Path("annotation/gold/agentic-l1-real-output-audit-120-v1/labels.jsonl")
DETECTOR_AUDIT = pathlib.Path("annotation/gold/agentic-l1-detector-audit-21-v1/labels.jsonl")
Q003_GOLD = pathlib.Path("annotation/gold/question-003-boundary-v2-human-24-v1/labels.jsonl")
Q003_SELECTION = pathlib.Path(
    "annotation/generated/question-003-boundary-v2-validation-v1/selection.json")
OUTPUT = pathlib.Path("reports/l1-judge-disagreement/results.json")
AXES = ("refusal", "boundary", "success")
TOP = 5


def read_jsonl(path):
    return [json.loads(line) for line in pathlib.Path(path).read_text().splitlines() if line]


def verdict(row, axis):
    block = (row.get("judge") or {}).get(axis)
    if not isinstance(block, dict) or block.get("error") is not None:
        return None
    return block.get("verdict")


def cohen_kappa(pairs):
    if not pairs:
        return None
    n = len(pairs)
    observed = sum(a == b for a, b in pairs) / n
    labels = {label for pair in pairs for label in pair}
    expected = sum((sum(a == x for a, _ in pairs) / n) * (sum(b == x for _, b in pairs) / n)
                   for x in labels)
    return None if expected == 1 else (observed - expected) / (1 - expected)


def human_labels():
    labels = collections.defaultdict(dict)
    for path in (AUDIT, DETECTOR_AUDIT):
        for row in read_jsonl(path):
            labels[row["case_key"]][row["axis"]] = row["label"]
    if Q003_SELECTION.exists():
        mapping = {s["anonymous_id"]: s["case_key"]
                   for s in json.loads(Q003_SELECTION.read_text())["selected"]}
        for row in read_jsonl(Q003_GOLD):
            labels[mapping[row["case_key"]]][row["axis"]] = row["label"]
    return labels


def main():
    loaded = aggregate.load_inputs(judged_dir=V2_JUDGED, include_b=False)
    humans = human_labels()
    cells = collections.defaultdict(lambda: {"compared": 0, "disagree": 0,
                                             "directions": collections.Counter()})
    pairs = collections.defaultdict(list)
    arbitration = {axis: collections.Counter() for axis in AXES}
    for arm, model in aggregate.ARMS.items():
        for key in sorted(loaded[arm]["A"]):
            row_a, row_c = loaded[arm]["A"][key], loaded[arm]["C"][key]
            case_key = "{}|{}|{}".format(model, key[0], key[1])
            for axis in AXES:
                a, c = verdict(row_a, axis), verdict(row_c, axis)
                if a is None or c is None:
                    continue
                pairs[(axis, model)].append((a, c))
                cell = cells[(axis, model, key[0])]
                cell["compared"] += 1
                if a != c:
                    cell["disagree"] += 1
                    cell["directions"]["A={}|C={}".format(a, c)] += 1
                    human = humans.get(case_key, {}).get(axis)
                    if human is not None:
                        arbitration[axis]["A_matches_human" if a == human
                                          else "C_matches_human" if c == human
                                          else "neither"] += 1

    per_model = {}
    for (axis, model), values in pairs.items():
        per_model.setdefault(axis, {})[model] = {
            "compared": len(values),
            "disagreements": sum(a != c for a, c in values),
            "rate": sum(a != c for a, c in values) / len(values),
            "cohen_kappa": cohen_kappa(values),
        }
    per_question, ranking = {}, {}
    for axis in AXES:
        pooled = collections.defaultdict(lambda: [0, 0])
        for (cell_axis, model, qid), cell in cells.items():
            if cell_axis != axis:
                continue
            per_question.setdefault(axis, {}).setdefault(qid, {})[model] = {
                "compared": cell["compared"], "disagree": cell["disagree"],
                "directions": dict(cell["directions"])}
            pooled[qid][0] += cell["disagree"]
            pooled[qid][1] += cell["compared"]
        ranking[axis] = sorted(
            ({"question": qid, "disagree": d, "compared": n, "rate": d / n}
             for qid, (d, n) in pooled.items() if n),
            key=lambda item: (-item["rate"], item["question"]))
    result = {
        "status": "descriptive pointers; small counts; nothing here changes a frozen rule",
        "per_model": per_model,
        "questions_ranked_by_pooled_disagreement": ranking,
        "human_labelled_disagreements": {axis: dict(c) for axis, c in arbitration.items()},
        "per_question": per_question,
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    for axis in AXES:
        print("==", axis)
        for model, data in per_model.get(axis, {}).items():
            print("  {:26s} {:3d}/{:3d} = {:.1%}  kappa {}".format(
                model, data["disagreements"], data["compared"], data["rate"],
                "-" if data["cohen_kappa"] is None else "{:.2f}".format(data["cohen_kappa"])))
        print("  top:", ", ".join("{} {}/{}".format(r["question"], r["disagree"], r["compared"])
                                  for r in ranking[axis][:TOP]))
        print("  human-labelled disagreements:", dict(arbitration[axis]))


if __name__ == "__main__":
    main()
