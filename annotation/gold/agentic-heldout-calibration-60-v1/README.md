# Agentic held-out judge calibration human gold

This directory freezes the independent human labels for the 60-case synthetic
boundary/success judge calibration set. It is a judge-validation artifact, not a
model benchmark result or leaderboard.

## Protocol

- The set covers all 15 source items on two separately calibrated axes.
- Boundary has 30 cases; success has 30 cases.
- Two annotators reviewed independently randomized packets without source IDs,
  author-seeded labels, pair linkage, or the other annotator's responses.
- One accidental Excel cell-reference suffix attached to an otherwise valid label
  was normalized in the analysis layer; the raw workbook and hash remain preserved.
- Boundary agreement was 30/30 (100%, Cohen's kappa 1.000).
- Success agreement was 29/30 (96.7%, Cohen's kappa 0.933).
- The single success disagreement received a third independent blind decision.
- Human gold was frozen before any LLM judge was run on this batch.

## Files

- `labels.jsonl` contains one final human label per case.
- `summary.json` records class balance, agreement, arbitration, and the comparison
  with the author-seeded construction labels.
- `provenance.json` records SHA-256 commitments for source and frozen artifacts.

The final success distribution is 16 achieved / 14 not_achieved, rather than the
author-seeded 15/15 construction balance. The reviewed mismatch is retained as human
gold instead of being changed to preserve the original intended balance.

Raw workbooks, individual responses, normalization details, and blind-ID mappings
remain excluded by `.gitignore`; their hashes are retained for audit.
