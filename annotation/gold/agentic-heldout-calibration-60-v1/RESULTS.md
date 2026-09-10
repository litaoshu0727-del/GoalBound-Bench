# Held-out judge results

The human-gold `README.md`, labels, summary, and provenance remain byte-for-byte
frozen. This separate file records work performed only after that freeze.

## Reports

- `two-judge-calibration.json` is the prospective judge A/B calibration report.
- `judge-c-selection.json` reports every preregistered candidate and the cost
  tie-break that selected judge C.

## Published per-case decisions

| File | SHA-256 |
|---|---|
| `runs/heldout-calibration/judged-a.jsonl` | `942f17f14fe5e74f44af640653840f9547a715c5f37d3afdb72c8e79b2f3e3f5` |
| `runs/heldout-calibration/judged-b.jsonl` | `07189f34c7b2e65f3d0d6dd9c42d9ee0cb12a078711ac8feca38a0f23857a4c9` |
| `runs/judge-c-selection/judge-a.jsonl` | `5daa1be823bb7a81aa85ed235d45543f82817523c45c5b76eb43920a4ddd917d` |
| `runs/judge-c-selection/kimi-k3.jsonl` | `3dfc5c12c473d2bbb134d42c84a247fc54ff0fc13c33fdcf6f8f83fd7bbabba4` |
| `runs/judge-c-selection/minimax-m3.jsonl` | `4104200e4f8b9ec6b304ca0d130cf41d0746f76aaf3dae003f66464908792715` |
| `runs/judge-c-selection/glm-5.3-flash.jsonl` | `3d49c3c0f5cea3e1a64e1ae4dc9346f22bb525cf40ac37aeb220e5e00c8fb9d4` |

The first two files have the SHA-256 values recorded in
`two-judge-calibration.json`. The remaining four support the run-to-run and candidate
comparisons in `judge-c-selection.json`. They contain no API credentials.

The preregistration files are historical, immutable snapshots of what was committed
before each corresponding judge run. Current status and outcomes belong in result
files rather than being retroactively written into those snapshots. These published
JSONL files are historical calibration-v1 outputs; future resumable runs use
calibration schema v2 and must write to fresh paths rather than mixing versions.
