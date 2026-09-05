"""Phase-2 agentic / open-ended extension for GoalBound-Bench.

This subpackage holds the open-ended (L1) and single-turn tool-calling (L2)
evaluation machinery described in ``docs/phase-2-agentic-design.md``.

Design principle: open-ended/agentic evaluation does **not** remove gaming; it
moves the gaming surface onto the evaluator, task templates, and tool rules. The
detector engine here favours programmatic checks (reproducible, no judge leakage)
and explicitly marks channels that can only be resolved by an LLM/human judge, so
that "detector coverage" is a reported quantity rather than a hidden assumption.
"""

from .detectors import (
    AgenticConfigError,
    AgenticEvaluation,
    ChannelResult,
    ItemVerdict,
    SuccessResult,
    SuccessVerdict,
    Verdict,
    evaluate_agentic,
    evaluate_channel,
    evaluate_item,
    evaluate_success,
)

__all__ = [
    "AgenticConfigError",
    "AgenticEvaluation",
    "ChannelResult",
    "ItemVerdict",
    "SuccessResult",
    "SuccessVerdict",
    "Verdict",
    "evaluate_agentic",
    "evaluate_channel",
    "evaluate_item",
    "evaluate_success",
]
