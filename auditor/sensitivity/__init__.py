"""Sensitivity / PII classifier.

Answers "is this column risky?" for every column in a ``DatabaseMetadata``.
The 0-1 ``score`` is the ``Sensitivity`` term of
``Necessity = Sensitivity x (1 - Usage) x Staleness``.

Two implementations, same ``ColumnSensitivity`` output shape:

  RegexSensitivityClassifier    the baseline - column-name / value / type
                                rules. Explainable, 1.0 precision & recall on
                                the seed catalogue. **This is the default the
                                auditor pipeline uses.**
  MLSensitivityClassifier       a scikit-learn model (in ml_classifier.py),
                                trained to approximate the rules on a synthetic
                                corpus. See that module's docstring / README
                                for the honest limitation.
  HybridSensitivityClassifier   regex + ML blended, with a disagreement flag.

ML pieces are imported lazily (they pull in scikit-learn); importing this
package does not require sklearn.
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
    # lazy ML exports (see __getattr__)
    "MLSensitivityClassifier",
    "HybridSensitivityClassifier",
    "train_model",
]


def __getattr__(name):  # PEP 562 - lazy so `import auditor.sensitivity` stays sklearn-free
    if name in ("MLSensitivityClassifier", "HybridSensitivityClassifier",
                "train_model", "load_model"):
        from auditor.sensitivity import ml_classifier
        return getattr(ml_classifier, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
