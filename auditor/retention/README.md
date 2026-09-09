# `auditor/retention/` — Retention policy engine (Phase 4)

Answers **"is this data overdue?"** — the `Staleness` term of
`Necessity = Sensitivity × (1 − Usage) × Staleness`.

Deliberately the simplest of the three signals: a day-threshold policy vs the
oldest timestamp in each table.

```
policy.py            RetentionPolicy — default_days + per-table / per-column overrides
checker.py           RetentionChecker -> ColumnRetention / TableRetention
policy_extractor.py  draft a RetentionPolicy from a written policy document (below)
__main__.py          CLI
```

## Use

```powershell
python -m auditor.retention                                  # default 365d, metadata only
python -m auditor.retention --policy retention_policy.example.yaml
python -m auditor.retention --default-days 730
python -m auditor.retention --live                           # + count rows past policy
python -m auditor.retention --json
python -m auditor.retention --evaluate                       # seed sanity check
```

```python
from auditor.retention import RetentionChecker, RetentionPolicy
res = RetentionChecker(RetentionPolicy(default_days=365), metadata).check()
res.column("users.mothers_maiden_name").score        # 1.0 — inherits the stale user row
res.column("users.date_of_birth").basis              # "table:created_at", not "self"
```

## Policy format (YAML or JSON)

```yaml
default_days: 365
tables:
  orders: 2555              # 7 years for tax
  support_tickets: 365
columns:
  users.last_login_at: 730
anchors:
  users: created_at         # which timestamp marks a row's age (default: created_at)
exclude:
  - "*.updated_at"          # glob on table.column — never flag these
```

Resolution: **column override → table override → `default_days`**.

## Output — `ColumnRetention` (parallel to `ColumnSensitivity` / `ColumnUsage`)

| field | meaning |
|-------|---------|
| `score` | 0–1 staleness — the `Staleness` term |
| `is_overdue` | oldest row older than policy |
| `days_overdue` | `oldest_row_age − policy_days` (None if not overdue) |
| `policy_applied` | `{"days": 365, "source": "table", "anchor": "created_at"}` |
| `oldest_row_age_days` / `newest_row_age_days` | span of the anchor timestamp |
| `basis` | `self` (temporal col scored on its own values) · `table:<anchor>` (inherited) · `excluded` · `no-temporal-data` |
| `rows_total` / `rows_overdue` / `fraction_overdue` | only with `--live` |

`TableRetention` carries the same fields at table granularity.

### Score

```
oldest_age   = now − min(anchor timestamp)                (Phase 1 metadata)
days_overdue = oldest_age − policy_days
age_score    = clamp(days_overdue / policy_days, 0, 1)    # data 2× past policy → 1.0
score        = age_score                                   (metadata only)
             = age_score × (0.4 + 0.6 × volume)            (--live: volume =
               min(1, fraction_overdue / 0.10))
```

### Why columns inherit a table score

Staleness is a property of the **row**, not the column. `mothers_maiden_name`
has no timestamp of its own — but the user row it sits in can be 900 days
old. So every non-lifecycle column takes its table's score, computed from an
anchor column (`created_at` by default). Columns that *are* lifecycle
timestamps (`updated_at`, `resolved_at`, `last_login_at`) are scored on their
own values instead (`basis = "self"`).

**`date_of_birth` is temporal but not a lifecycle timestamp** — it's an
attribute date. The name heuristic (`is_retention_relevant`) excludes
`birth` / `dob` / `expiry` / `valid_from` / `*_start_date` etc., so DOB
inherits the row age instead of being (wrongly) scored on a 1950s value.

## Against the Phase 0 seed

~18% of `users` / `orders` / `support_tickets` rows were seeded 400+ days old.

| policy | result |
|--------|--------|
| default 365 d | all three tables **overdue, score 1.000** (oldest rows 836–900 days → >2× policy) |
| `retention_policy.example.yaml` (orders 2555 d, users 1095 d) | only `support_tickets` overdue; `orders` / `users` clear (score 0.000) |
| 3650 d | nothing overdue |

`--live` adds row counts: users 59% / support_tickets 32% / orders 18% of
rows past a 365-day policy — matching the seeding.

`users.date_of_birth` → `basis = table:created_at` (inherits, not scored on
its 1940 minimum). `users.mothers_maiden_name` → score 1.0, 535 days overdue
— the spec's worked example.

## Limitations (kept deliberately small)

- Threshold policy only — no "keep N most recent", no legal-basis modelling,
  no per-row exceptions.
- One anchor timestamp per table. A table with no lifecycle timestamp gets
  `basis = "no-temporal-data"` and score 0 (can't assess it).
- `age_score` saturates at 1.0 once data is 2× past policy — no gradient
  beyond that from age alone (`--live` volume still varies it).
- The metadata-only path trusts `min(anchor)` from Phase 1; it doesn't know
  if that one oldest row is an outlier. `--live` `fraction_overdue` is the
  corrective.

## Tests

`tests/test_retention.py` — `is_retention_relevant` heuristic, policy
resolution, the score curve, synthetic old/fresh tables (no DB), and the live
seed (all tables overdue at 365 d, clear at 3650 d, DOB inherits, example
policy discriminates).

---

# Policy extractor (`policy_extractor.py`)

Closes the gap the literature survey names: papers #2–4 in the spec do NLP
over legal/contract text but never connect it to a live database; Phase 4 is
the live-database side. This reads a **written retention policy** and drafts
the `RetentionPolicy` config Phase 4 otherwise expects hand-written — same
shape, **the checker is unchanged**.

```
seed-data/sample_retention_policy.md   a realistic synthetic policy doc (reproducible, like Phase 0's data)
policy_extractor.py                    extract rules -> map to schema -> PolicyDraft -> RetentionPolicy
```

## Use

```powershell
python -m auditor.retention.policy_extractor                    # review table for the sample doc
python -m auditor.retention.policy_extractor --doc mypolicy.md
python -m auditor.retention.policy_extractor --out draft.yaml   # review-ready YAML (every line commented)
python -m auditor.retention.policy_extractor --check            # build policy, run it through the Phase 4 checker
```

```python
from auditor.retention.policy_extractor import extract_policy
draft = extract_policy("company_policy.md", metadata)
for p in draft.proposals:            # each: category, days, target, match_score, evidence sentence
    ...
policy = draft.to_policy(accept={"orders", "support_tickets"})   # human picks which mappings to apply
```

## How it works — three steps, decreasing confidence

1. **Duration + rule extraction** (`extract_rules`) — regex for `N years /
   months / days` (digits and number-words), kept only when the sentence
   carries a *retention* cue (`retained for`, `deleted after`, `no longer
   than`, …) and not an *anti-cue* (`access request`, `breach`, `training`,
   `backup … cycle`, `reviewed every …`). Wrapped lines are reflowed into
   paragraphs first. Confidence 0.9 for `must be retained for`, 0.75 for
   `kept for` / `deleted after`, −0.15 for conditional phrasing (`unless`).
   This step is **reliable** — the numbers and periods come out cleanly.

2. **Category → schema mapping** (`propose_mappings`) — for each rule, score
   every table and a curated set of columns by keyword overlap against a
   synonym lexicon (`order/transaction/invoice/payment → orders`,
   `support/ticket/correspondence → support_tickets`, …) plus a `difflib`
   fuzzy name match. This step is the **weakest evidence in the whole
   pipeline** — a policy sentence ("financial records", "login history") does
   not name a column, so the match is a guess. Every proposal carries
   `needs_confirmation=True`; `suggested=True` only marks a high-score,
   clear-margin guess as pre-ticked for the reviewer.

3. **Assembly** (`PolicyDraft.to_policy`) — only proposals the human passed in
   `accept=` (or the `suggested` ones, with a warning) become policy entries.
   Where two rules map to one target with different periods
   (`PolicyDraft.conflicts`), the **longer** period is kept and the conflict
   is reported.

## Extraction: rule-based, not an LLM

Pure regex + cue-word rules + a synonym lexicon. **No LLM.** Why: it's
deterministic, testable with no API key, and the reliable part (the numeric
periods) doesn't need one. An LLM (Groq is in the stack) would help most with
category-phrase extraction from tangled sentences and with semantic table
mapping — `extract_rules_llm()` is a documented, unimplemented seam for that.

## Accuracy limitations — read before trusting a draft

**This is an assistive draft a human reviews, not an autonomous decision.**

- **The mapping step is inherently ambiguous.** "Customer support tickets …
  1 year" → `support_tickets` is easy. "Login history … 90 days" could be
  `users.last_login_at`, `users.last_login_ip`, or both — the extractor
  reports the tie and picks neither automatically. "Financial records" maps
  to `orders` here only because our lexicon says so; on a different schema it
  might be a `payments` or `invoices` table the lexicon doesn't know.
- **Category extraction is shallow.** It takes the noun phrase before the
  retention verb. Complex sentences ("X, except where Y, in which case Z")
  are truncated or mis-attributed.
- **One period per sentence.** "2 years unless renewed, then 3 years" keeps
  the first number only.
- **Silent misses.** A retention rule phrased without a recognised cue word,
  or with a written-out period the number-word map doesn't cover, is dropped
  with no error. Always diff the extracted rule count against the document.
- **The synonym lexicon is hand-built** for this schema's domain. A new
  target database needs its lexicon extended, or matches fall to fuzzy string
  similarity only.

On the sample doc the extractor gets all 9 periods and the default right, maps
the three tables cleanly, and correctly flags the two genuine ambiguities
(marketing-consent period conflict, survey-responses column vs table). That is
the *best* case — a policy written to be parseable, against a schema the
lexicon knows.

## Tests

`tests/test_policy_extractor.py` — duration parsing, the 9 expected periods
from the sample doc, the §3 non-retention noise correctly ignored, wrapped-line
handling, conflict detection, and the **end-to-end equivalence test**: a
policy built by the extractor drives `RetentionChecker` byte-identically
(every column's score / overdue / days-overdue / basis) to a hand-written
policy with the same numbers.
