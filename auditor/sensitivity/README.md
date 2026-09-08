# `auditor/sensitivity/` — PII / sensitivity classifier (Phase 2)

Answers **"is this column risky?"** for every column in a `DatabaseMetadata`.
Ships the **regex/keyword baseline only** — a scikit-learn layer comes later
and will sit on top of the same signals (CLAUDE.md: "start with regex baseline
first").

```
rules.py        NAME_RULES / VALUE_RULES / HARMLESS_RULES / TYPE_RULES
classifier.py   RegexSensitivityClassifier -> ColumnSensitivity
evaluation.py   precision/recall vs seed-data/output/column_catalog.csv
__main__.py     CLI
```

## Use

```powershell
python -m auditor.sensitivity                    # classify the live target DB
python -m auditor.sensitivity --metadata m.json  # from a saved dump, no DB
python -m auditor.sensitivity --json             # ColumnSensitivity records
python -m auditor.sensitivity --evaluate         # score vs the ground truth
```

```python
from auditor.sensitivity import classify_metadata
for s in classify_metadata(meta):
    print(s.qualified_name, s.score, s.pii_type, s.matched_rules)
```

## Output — `ColumnSensitivity`

| field | meaning |
|-------|---------|
| `score` | 0–1 estimated sensitivity — **this is the `Sensitivity` term** in `Necessity = Sensitivity × (1 − Usage) × Staleness` |
| `is_sensitive` | `score >= threshold` (default 0.5) |
| `tier` | `none` / `low` / `medium` / `high` (score bands) |
| `pii_type` | best-guess category (`contact`, `government_id`, `network_id`, `knowledge_based`, …) |
| `confidence` | how much evidence backs the score (name+value agreement → 0.95) |
| `matched_rules` | every rule id that fired — drives the dashboard's "why was this flagged" |
| `signals` | raw sub-scores for debugging |

## How the score is combined

1. **name rules** — `max` weight over matching `NAME_RULES` (patterns on the
   normalized column name: `last_login_ip` → `"last login ip"`).
2. **value rules** — a pattern that matches ≥ 60% of the sampled values
   (`ssn_like`, `ipv4`, `email`, `credit_card`, …), plus *embedded* patterns
   that catch PII leaking into free-text columns.
3. **type rules** — e.g. `INET` columns → 0.6 network_id.
4. `positive = max(1,2,3)`, `+0.05` if name and values agree on the category.
5. **harmless names** (`favorite_color`, `theme_preference`, `status`, `_at`
   timestamps, `total`, `rating`, …) cap the score to ~0 — **but only when
   `positive` is weak**, so `date_of_birth` stays sensitive despite looking
   like a timestamp.
6. **structural** — primary keys → 0.0; unmatched foreign keys → 0.1
   (`linkage`); temporal columns with no strong positive → ~0.0.

## Evaluation

`python -m auditor.sensitivity --evaluate` against the Phase 0 catalogue
(label = tier `high`/`medium` is "truly sensitive"):

```
confusion   : TP=14  FP=0  TN=32  FN=0
precision   : 1.000
recall      : 1.000
tier accuracy : 0.804   (4-class)
score MAE     : 0.051   (vs the ground-truth 0-1 score)
```

**Caveat:** the seed catalogue was authored to be name-separable, so a clean
regex pass *should* get near-perfect binary numbers on it — that's the point
of the baseline, not a claim about messy real schemas (which is what the ML
layer and the Zhang & Jiang-style metadata features are for). What matters is
that it gets the **hard cases** right without hand-coding them one by one:

| column | why it's tricky | result |
|--------|-----------------|--------|
| `users.last_login_ip` | harmless-sounding name, legally PII | flagged (INET type + `ipv4` values + `ip` token) |
| `orders.ip_address` | name contains "address" | `network_id`, not `address` |
| `orders.card_last4` vs `orders.card_brand` | both say "card" | last4 flagged, brand not |
| `support_tickets.body` vs `subject` / `notes` | all free text | only `body` crosses 0.5 |
| `users.date_of_birth` | temporal type | stays sensitive (dob name rule beats the timestamp cap) |

## Tests

`tests/test_sensitivity.py` — unit tests build synthetic `ColumnMetadata` (no
DB) for the rule logic and the spec's called-out cases; the evaluation test
asserts precision ≥ 0.9, recall ≥ 0.9, and no FP/FN against the live seed.
