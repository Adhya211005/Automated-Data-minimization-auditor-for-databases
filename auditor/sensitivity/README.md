# `auditor/sensitivity/` — PII / sensitivity classifier

Answers **"is this column risky?"** for every column in a `DatabaseMetadata`.
Two implementations, one `ColumnSensitivity` output shape:

```
rules.py          NAME_RULES / VALUE_RULES / HARMLESS_RULES / TYPE_RULES
classifier.py     RegexSensitivityClassifier   — the baseline (default)
features.py       ColumnFeaturizer             — char n-grams + engineered features
synth.py          synthetic column corpus for ML training
ml_classifier.py  MLSensitivityClassifier, HybridSensitivityClassifier, train_model
evaluation.py     regex vs column_catalog.csv
evaluation_ml.py  regex vs ML vs hybrid, side by side
__main__.py       CLI
```

**Default: regex.** The auditor pipeline (`auditor/scoring/pipeline.py`) uses
`RegexSensitivityClassifier` — it's explainable (every flag lists the rules
that fired), needs no training, and scores 1.0 precision & recall on the
independent seed labels. See [the ML section](#ml-classifier) for when to use
the others.

## Use

```powershell
python -m auditor.sensitivity                     # regex baseline, live DB
python -m auditor.sensitivity --metadata m.json   # from a saved dump, no DB
python -m auditor.sensitivity --mode ml           # the ML model
python -m auditor.sensitivity --mode hybrid       # regex + ML blend
python -m auditor.sensitivity --json              # ColumnSensitivity records
python -m auditor.sensitivity --evaluate          # score the chosen mode
python -m auditor.sensitivity --compare           # regex vs ML vs hybrid table
python -m auditor.sensitivity.ml_classifier --train   # (re)train + cache the model
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

---

## ML classifier

`ml_classifier.py` adds a scikit-learn model. **The regex classifier is
untouched** — it stays as the fallback and the comparison baseline.

### The honest limitation

The model's training labels are the **regex classifier's own tier/pii_type
output** on a synthetic corpus of ~900 generated columns (`synth.py`). So the
ML model is learning to **approximate the rule set**, not learning from
independent ground truth. It cannot, in this form, discover sensitivity
patterns the rules miss — at best it reproduces them, and it adds a failure
mode (model error) the rules don't have.

Its actual value:
1. the ML integration the project rubric asks for;
2. smoother scores on borderline columns (continuous, not rule-step);
3. the **disagreement flag** in the hybrid — where regex and ML differ is
   exactly where a human should look (a rule gap, or an ML mistake);
4. a starting point for a model retrained on real DPO-labelled columns, once
   those exist.

The seed's 46 columns are deliberately **not** in the training corpus, so
they're a genuine held-out test (scored against the hand-authored
`column_catalog.csv`, not against regex output).

### Features (`features.py`)

Same inputs as the regex classifier, engineered:

| group | features |
|-------|----------|
| name | char n-grams (2–4, word-boundary), token count, length, has-digit |
| values | Shannon entropy, format-signature fractions (email / phone / ssn-shaped / ipv4 / uuid / numeric / iso-date / all-caps / hex), free-text indicators, length stats, null rate, distinct ratio |
| type | one-hot of the type family, char/numeric length, `is_temporal` / `nullable` / PK / FK / unique / indexed |
| usage *(optional)* | usage score, `is_used`, log read/write counts |

### Models

`train_model(model_type=...)` — `logreg` (default) or `random_forest`. Both
predict the 4 tiers (`none`/`low`/`medium`/`high`); a second model predicts
`pii_type`. Trained on a stratified 75/25 split of the synthetic corpus,
cached to `auditor/sensitivity/models/ml_sensitivity.joblib` (gitignored;
auto-trains deterministically on first use).

### Results — `python -m auditor.sensitivity --compare`

```
held-out SYNTHETIC (ML approximating the regex oracle):
  n=225  tier_acc=1.000  P=1.000  R=1.000  F1=1.000

SEED (46 cols) vs column_catalog.csv   -   model=logreg, usage_feature=False
              precision     recall       F1  tier_acc  score_MAE
  regex           1.000      1.000    1.000     0.804      0.051
  ml              1.000      1.000    1.000     0.804      0.072
  hybrid          1.000      1.000    1.000     0.826      0.051

regex vs ML disagreements on the seed (1):
  column                          regex           ml     catalog  who
  users.last_login_at        none 0.03    medium 0.42       low    tie
```

**What this actually says:**

- **`logreg` learned the rules essentially perfectly** on synthetic data
  (1.0 across the board) and **matches the regex baseline on the real seed**
  (P/R/F1 = 1.0). It reproduces the baseline; it does not beat it on the
  binary decision.
- **`random_forest` underperforms**: R = 0.857 on the seed (it misses
  `support_tickets.body` and `users.gender`, both borderline `medium`), and
  the RF-hybrid inherits that miss. That's why `logreg` is the default.
- **Hybrid's one real win**: 4-tier accuracy 0.826 vs the baseline's 0.804 —
  the blend lands a few more columns in the exactly-right band.
- **The one disagreement** is instructive: `users.last_login_at`. The regex
  rules zero it as a plain timestamp; the ML model gives it `medium` (0.42)
  because "last login" activity data *is* mildly sensitive — the catalog
  agrees it's `low`, not `none`. Here the fuzzy model caught a nuance the
  rules flattened. One column, but it's the kind of thing the hybrid's
  disagreement flag is for.
- **Usage as a feature: no effect.** Tested with `include_usage=True`; seed
  numbers were identical. Sensitivity is about *what the data is*, and usage
  is a separate term in the Necessity formula — folding it in double-counts
  it. Default is off.

### Hybrid (`HybridSensitivityClassifier`)

Default mode **`blend`**: `score = 0.6·regex + 0.4·ml`, `pii_type` from regex,
and `matched_rules` gets `needs_review:regex_ml_disagreement` when the two
methods land on different sides of the threshold (or ≥ 2 tiers apart).
Regex is weighted higher because it's the trustworthy, explainable half.

- `mode="regex_primary"` — regex score/tier, ML advisory in `signals` only.
- `mode="ml_primary"` — ML score/tier, regex used only for the flag.

### Which mode is the default, and why

| where | default | why |
|-------|---------|-----|
| **auditor pipeline** (`scoring/pipeline.py`) | **regex** | a compliance tool must justify every flag with a concrete rule; regex is 1.0 P/R on independent labels and needs no model artifact |
| **`HybridSensitivityClassifier`** | **`blend`** | when you *do* want the model's input, blend keeps regex's precision on known patterns, smooths the borderline, and surfaces disagreements for review |
| **ml-only** | not recommended | it only approximates the rules and adds model risk; `random_forest` also has real recall loss on borderline columns |

## Tests

- `tests/test_sensitivity.py` — the regex baseline (unchanged).
- `tests/test_sensitivity_ml.py` — feature extraction, the synthetic corpus
  (deterministic, all four tiers, seed columns excluded), the ML classifier
  on obvious cases, model round-trip, the hybrid's blend/modes/disagreement
  flag, and the seed comparison asserting ML precision ≥ 0.95, recall ≥ 0.90,
  and that the hybrid never regresses below the ML model or produces a false
  positive.
