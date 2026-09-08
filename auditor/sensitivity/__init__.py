"""Sensitivity / PII classifier (Phase 2).

Answers "is this column risky?" for every column in a ``DatabaseMetadata``.
This phase ships the **regex/keyword baseline only** - column-name patterns,
sample-value patterns, and a few type/structural signals combined into a
0-1 sensitivity score. A scikit-learn layer comes later and will sit on top
of these same signals (CLAUDE.md: "start with regex baseline first").

The 0-1 ``score`` is the ``Sensitivity`` term the Necessity Score depends on:
``Necessity = Sensitivity x (1 - Usage) x Staleness``.
"""

from auditor.sensitivity.classifier import (
    ColumnSensitivity,
    RegexSensitivityClassifier,
    classify_metadata,
)
from auditor.sensitivity.evaluation import (
    EvalReport,
    evaluate,
    load_ground_truth,
)

__all__ = [
    "ColumnSensitivity",
    "RegexSensitivityClassifier",
    "classify_metadata",
    "EvalReport",
    "evaluate",
    "load_ground_truth",
]
