"""Feature engineering for the ML sensitivity classifier.

Same inputs as the regex baseline - column name, SQL type, sample values from
the Phase 1 metadata extractor (plus optional usage stats) - turned into real
features rather than raw text:

  name       char n-grams (2-4, word-boundary aware) + a few name statistics
  values     Shannon entropy, format-signature fractions (email / phone /
             ssn-shaped / ip-shaped / uuid / numeric / date / all-caps),
             free-text indicators, length stats, null rate, distinct ratio
  type       one-hot of the normalized type family + structural flags
  usage      (optional) usage score + log counts + is_used
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np
from scipy import sparse
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.feature_extraction.text import CountVectorizer

from auditor.ingestion.metadata import ColumnMetadata
from auditor.sensitivity.rules import normalize_name

_TYPE_VOCAB = [
    "string", "text", "integer", "numeric", "float", "boolean", "date",
    "timestamp", "timestamptz", "time", "interval", "uuid", "inet", "json",
    "bytes", "array", "other",
]

# value-format signatures (match against str(value))
_SIGNATURES = {
    "email": re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$"),
    "phone": re.compile(r"^\+?[\d][\d\s().-]{6,17}\d$"),
    "ssn_shaped": re.compile(r"^\d{3}-\d{2}-\d{4}$"),
    "ipv4": re.compile(r"^(\d{1,3}\.){3}\d{1,3}$"),
    "ipv6": re.compile(r"^[0-9a-fA-F:]+:[0-9a-fA-F:]+$"),
    "uuid": re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$"),
    "numeric": re.compile(r"^-?\d+(\.\d+)?$"),
    "iso_date": re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}.*)?$"),
    "all_caps_token": re.compile(r"^[A-Z0-9_]{2,}$"),
    "hex_blob": re.compile(r"^[0-9a-fA-F]{16,}$"),
}


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def engineered_features(
    col: ColumnMetadata, usage=None, *, include_usage: bool = True
) -> dict[str, float]:
    """Flat numeric feature dict for one column (excludes the name n-grams)."""
    f: dict[str, float] = {}
    name = normalize_name(col.name)
    tokens = [t for t in name.split(" ") if t]

    # --- name statistics ------------------------------------------------
    f["name_len"] = len(col.name)
    f["name_tokens"] = len(tokens)
    f["name_has_digit"] = float(any(ch.isdigit() for ch in col.name))
    f["name_avg_token_len"] = float(np.mean([len(t) for t in tokens])) if tokens else 0.0

    # --- type ---------------------------------------------------------
    for t in _TYPE_VOCAB:
        f[f"type_{t}"] = float(col.generic_type == t)
    f["type_char_max_len"] = float(col.char_max_length or 0)
    f["type_numeric_precision"] = float(col.numeric_precision or 0)

    # --- structural flags -------------------------------------------
    f["is_temporal"] = float(col.is_temporal)
    f["is_nullable"] = float(col.nullable)
    f["is_primary_key"] = float(col.is_primary_key)
    f["is_foreign_key"] = float(col.is_foreign_key)
    f["is_unique"] = float(col.is_unique)
    f["is_indexed"] = float(col.is_indexed)
    f["has_comment"] = float(bool(col.comment))

    # --- sample values --------------------------------------------
    samples = [str(v) for v in (col.sample_values or []) if v is not None]
    n = len(samples)
    f["n_samples"] = float(n)
    f["null_rate"] = float(col.sample_null_fraction or 0.0)
    f["distinct_ratio"] = float((col.distinct_in_sample or 0) / n) if n else 0.0

    if n:
        lengths = [len(s) for s in samples]
        word_counts = [len(s.split()) for s in samples]
        entropies = [_shannon_entropy(s) for s in samples]
        f["val_len_mean"] = float(np.mean(lengths))
        f["val_len_max"] = float(np.max(lengths))
        f["val_len_std"] = float(np.std(lengths))
        f["val_entropy_mean"] = float(np.mean(entropies))
        f["val_words_mean"] = float(np.mean(word_counts))
        f["val_is_freetext"] = float(np.mean(word_counts) >= 3.0)
        f["val_digit_ratio"] = float(
            np.mean([sum(c.isdigit() for c in s) / len(s) for s in samples if s])
        )
        f["val_space_frac"] = float(np.mean([(" " in s) for s in samples]))
        for sig, rx in _SIGNATURES.items():
            f[f"sig_{sig}"] = float(np.mean([bool(rx.match(s)) for s in samples]))
    else:
        for k in ("val_len_mean", "val_len_max", "val_len_std", "val_entropy_mean",
                  "val_words_mean", "val_is_freetext", "val_digit_ratio", "val_space_frac"):
            f[k] = 0.0
        for sig in _SIGNATURES:
            f[f"sig_{sig}"] = 0.0

    # --- usage (optional) ----------------------------------------
    if include_usage:
        if usage is not None:
            f["use_score"] = float(getattr(usage, "score", 0.0))
            f["use_is_used"] = float(getattr(usage, "is_used", False))
            f["use_log_reads"] = math.log1p(float(getattr(usage, "read_count", 0) or 0))
            f["use_log_writes"] = math.log1p(float(getattr(usage, "write_count", 0) or 0))
        else:
            f["use_score"] = 0.0
            f["use_is_used"] = 0.0
            f["use_log_reads"] = 0.0
            f["use_log_writes"] = 0.0

    return f


@dataclass
class _Row:
    col: ColumnMetadata
    usage: object = None


class ColumnFeaturizer(BaseEstimator, TransformerMixin):
    """sklearn transformer: list of (ColumnMetadata[, usage]) -> feature matrix.

    Char n-grams of the name (fit vocabulary) are hstacked with the engineered
    numeric features (fixed, ordered keys)."""

    def __init__(self, *, include_usage: bool = True, ngram_range=(2, 4), min_df=1):
        self.include_usage = include_usage
        self.ngram_range = ngram_range
        self.min_df = min_df

    # -- helpers ------------------------------------------------------
    @staticmethod
    def _as_rows(X) -> list[_Row]:
        rows: list[_Row] = []
        for item in X:
            if isinstance(item, _Row):
                rows.append(item)
            elif isinstance(item, ColumnMetadata):
                rows.append(_Row(item))
            elif isinstance(item, (tuple, list)):
                rows.append(_Row(item[0], item[1] if len(item) > 1 else None))
            else:
                raise TypeError(f"unexpected feature input: {type(item)}")
        return rows

    def _eng_dicts(self, rows: list[_Row]) -> list[dict]:
        return [
            engineered_features(r.col, r.usage, include_usage=self.include_usage)
            for r in rows
        ]

    # -- sklearn API ------------------------------------------------
    def fit(self, X, y=None):
        rows = self._as_rows(X)
        names = [normalize_name(r.col.name) for r in rows]
        self.vectorizer_ = CountVectorizer(
            analyzer="char_wb", ngram_range=self.ngram_range, min_df=self.min_df,
            lowercase=True,
        )
        self.vectorizer_.fit(names)
        self.feature_keys_ = list(self._eng_dicts(rows[:1])[0].keys()) if rows else []
        self.feature_names_ = (
            [f"ngram::{g}" for g in self.vectorizer_.get_feature_names_out()]
            + [f"eng::{k}" for k in self.feature_keys_]
        )
        return self

    def transform(self, X):
        rows = self._as_rows(X)
        names = [normalize_name(r.col.name) for r in rows]
        ngram = self.vectorizer_.transform(names)
        eng = self._eng_dicts(rows)
        mat = np.array([[d.get(k, 0.0) for k in self.feature_keys_] for d in eng], dtype=float)
        return sparse.hstack([ngram, sparse.csr_matrix(mat)]).tocsr()

    def fit_transform(self, X, y=None):
        return self.fit(X, y).transform(X)
