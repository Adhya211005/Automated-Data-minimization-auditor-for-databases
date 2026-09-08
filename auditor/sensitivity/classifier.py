"""Regex/keyword sensitivity classifier.

``RegexSensitivityClassifier.classify(column)`` -> ``ColumnSensitivity`` with a
0-1 ``score``, a boolean ``is_sensitive``, a coarse ``tier``, a best-guess
``pii_type``, and the list of rule ids that fired (``matched_rules``) so the
dashboard can explain every flag.

How the score is combined, per column:

  1. name rules      -> name_score  = max weight of matching NAME_RULES
  2. value rules      -> value_score = weight of the value pattern that matches
                        >= `value_hit_ratio` of the sampled values
  3. type rules       -> type_score  (e.g. INET columns)
  positive = max(1, 2, 3);  +0.05 if name and values agree on the pii_type
  4. harmless names cap the score to ~0 - but only when `positive` is weak
     (< strong_threshold), so `date_of_birth` survives looking like a timestamp
  5. structural: primary keys -> 0.0; unmatched foreign keys -> 0.1 (linkage);
     temporal columns with no strong positive -> ~0.0
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional

from auditor.ingestion.metadata import ColumnMetadata, DatabaseMetadata
from auditor.sensitivity.rules import (
    EMBEDDED_VALUE_RULES,
    HARMLESS_RULES,
    NAME_RULES,
    TYPE_RULES,
    VALUE_RULES,
    normalize_name,
)

# score -> tier bands
_TIERS = ((0.15, "none"), (0.40, "low"), (0.70, "medium"), (1.01, "high"))


def _tier(score: float) -> str:
    for hi, name in _TIERS:
        if score < hi:
            return name
    return "high"


@dataclass
class ColumnSensitivity:
    qualified_name: str
    table: str
    column: str
    score: float                       # 0-1 estimated sensitivity (the "Sensitivity" term)
    is_sensitive: bool                  # score >= threshold
    tier: str                          # none | low | medium | high
    pii_type: Optional[str]
    confidence: float                  # 0-1, how much evidence backs the score
    matched_rules: list[str] = field(default_factory=list)
    signals: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class RegexSensitivityClassifier:
    def __init__(
        self,
        *,
        threshold: float = 0.5,
        strong_threshold: float = 0.5,
        value_hit_ratio: float = 0.6,
        harmless_cap: float = 0.08,
    ):
        self.threshold = threshold
        self.strong_threshold = strong_threshold
        self.value_hit_ratio = value_hit_ratio
        self.harmless_cap = harmless_cap

    # ------------------------------------------------------------------ #

    def classify(self, col: ColumnMetadata) -> ColumnSensitivity:
        norm = normalize_name(col.name)
        matched: list[str] = []
        signals: dict = {}

        # 1. name rules -------------------------------------------------
        name_hits = [r for r in NAME_RULES if r.matches(norm)]
        name_score = max((r.weight for r in name_hits), default=0.0)
        name_type = None
        if name_hits:
            best = max(name_hits, key=lambda r: r.weight)
            name_type = best.pii_type
            matched += [f"name:{r.id}" for r in name_hits]
            signals["name_score"] = round(name_score, 3)

        # 2. value rules ---------------------------------------------------
        value_score, value_type, value_sig = self._score_values(col)
        if value_sig:
            signals["value"] = value_sig
            matched.append(f"value:{value_sig['rule']}")

        # 3. type rules --------------------------------------------------
        type_score, type_type = 0.0, None
        if col.generic_type in TYPE_RULES:
            type_type, type_score = TYPE_RULES[col.generic_type]
            matched.append(f"type:{col.generic_type}")
            signals["type_score"] = type_score

        positive = max(name_score, value_score, type_score)
        agreement = bool(name_type and value_type and name_type == value_type)
        if agreement and positive < 0.98:
            positive = min(1.0, positive + 0.05)
            signals["agreement_bonus"] = True

        pii_type = name_type or value_type or type_type

        # 4. harmless-name override -----------------------------------
        harmless_hits = [h for h in HARMLESS_RULES if h.matches(norm)]
        score = positive
        if harmless_hits and positive < self.strong_threshold:
            score = min(positive, self.harmless_cap)
            matched += [f"harmless:{h.id}" for h in harmless_hits]
            signals["capped_by_harmless"] = harmless_hits[0].id
            if positive < 0.05:
                pii_type = None

        # 5. structural signals ---------------------------------------
        if col.is_primary_key and score < self.strong_threshold:
            score, pii_type = 0.0, None
            matched.append("struct:primary_key")
        elif col.is_foreign_key and score < 0.2:
            score = max(score, 0.1)
            pii_type = pii_type or "linkage"
            matched.append("struct:foreign_key")

        if col.is_temporal and positive < self.strong_threshold:
            score = min(score, 0.03)
            if positive < self.strong_threshold:
                pii_type = None
            matched.append("struct:temporal")

        score = round(max(0.0, min(1.0, score)), 3)
        confidence = self._confidence(name_score, value_score, agreement,
                                      bool(harmless_hits), score)

        return ColumnSensitivity(
            qualified_name=col.qualified_name,
            table=col.table,
            column=col.name,
            score=score,
            is_sensitive=score >= self.threshold,
            tier=_tier(score),
            pii_type=pii_type,
            confidence=round(confidence, 3),
            matched_rules=matched,
            signals=signals,
        )

    def classify_all(self, columns: Iterable[ColumnMetadata]) -> list[ColumnSensitivity]:
        return [self.classify(c) for c in columns]

    # ------------------------------------------------------------------ #

    def _score_values(self, col: ColumnMetadata):
        samples = [str(v) for v in (col.sample_values or []) if v is not None]
        if not samples:
            return 0.0, None, None
        n = len(samples)

        # a) whole-value patterns: does one pattern match most of the column?
        counts: dict[str, int] = {}
        for val in samples:
            for rule in VALUE_RULES:
                if rule.hit(val):
                    counts[rule.id] = counts.get(rule.id, 0) + 1
                    break
        for rule in VALUE_RULES:
            hits = counts.get(rule.id, 0)
            if hits and hits / n >= self.value_hit_ratio:
                return rule.weight, rule.pii_type, {
                    "rule": rule.id, "hit_ratio": round(hits / n, 2), "mode": "value",
                }

        # b) embedded PII inside longer free text
        for rule in EMBEDDED_VALUE_RULES:
            hits = sum(1 for v in samples if rule.hit(v))
            if hits and hits / n >= 0.3:
                return rule.weight, rule.pii_type, {
                    "rule": rule.id, "hit_ratio": round(hits / n, 2), "mode": "embedded",
                }

        return 0.0, None, None

    @staticmethod
    def _confidence(name_score, value_score, agreement, harmless, final_score) -> float:
        if agreement:
            return 0.95
        if name_score >= 0.6 or value_score >= 0.7:
            return 0.85
        if final_score == 0.0 and (harmless or name_score == 0.0):
            return 0.8          # confidently NOT sensitive
        if name_score or value_score:
            return 0.65
        return 0.55


def classify_metadata(
    metadata: DatabaseMetadata, **kwargs
) -> list[ColumnSensitivity]:
    """Classify every column in a metadata document."""
    clf = RegexSensitivityClassifier(**kwargs)
    return clf.classify_all(metadata.iter_columns())
