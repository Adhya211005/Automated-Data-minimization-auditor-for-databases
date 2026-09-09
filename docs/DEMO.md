# 5-minute demo walkthrough

A click-by-click script. Times are cumulative. The `>` lines are what to say.

---

## Before you present (do this ~2 minutes ahead)

1. **Pause OneDrive sync** (right-click the tray icon → Pause syncing → 2
   hours). The project folder is OneDrive-synced; a sync pass right after the
   seed regenerates the query log makes audits take 10 s instead of 1 s.
2. Terminal 1 — backend:
   ```powershell
   .\venv\Scripts\Activate.ps1
   uvicorn auditor.api.main:app --port 8000
   ```
3. Terminal 2 — dashboard:
   ```powershell
   cd frontend ; npm run dev
   ```
   Open `http://localhost:5173`.
4. **Pre-load two runs so the Compare view has data.** Easiest: click
   **“+ New audit”** in the dashboard twice — once leaving *Retention policy*
   at `365`, once setting it to `1200` (add a note like "longer retention").
   Or from a terminal (note `curl.exe`, not PowerShell's `curl` alias):
   ```powershell
   curl.exe -s -X POST http://127.0.0.1:8000/audits -H "content-type: application/json" -d "{\"policy_days\":365}"
   curl.exe -s -X POST http://127.0.0.1:8000/audits -H "content-type: application/json" -d "{\"policy_days\":1200}"
   ```
   In the dashboard's run picker (top bar), select the **policy 365d** run.
5. Sanity check: the Report table shows **2 of 46 columns flagged**, with
   `users.mothers_maiden_name` and `users.ssn` at the top.

If something is off, `python seed-data\seed.py --reset` then re-do step 4.

---

## The script

### 0:00 — The problem (no screen, or the spec's fine example)

> Companies keep more personal data than they need — a column gets added
> "just in case" and nobody circles back. That's illegal under GDPR's storage
> limitation rule and it's a breach liability. Existing tools scan for PII and
> hand you a list of 200 columns. They can't tell you which ones actually
> matter. This tool answers a harder question: *is this sensitive data still
> worth keeping?* — which needs three signals, not one.

### 0:30 — The Report view

Point at the **thresholds line** under the header:
`sensitive ≥ 0.5 · unused ≤ 0.2 · stale ≥ 0.5`.

> It flags a column only when **all three** cross their threshold — sensitive
> *and* unused *and* stale. Two of 46 columns here. Everything else is
> "keep", "review", or "not a risk".

Point at the three mini-bars per row (sensitive / 1−usage / stale). Red = that
signal crossed its line.

### 1:00 — The worked example: `users.mothers_maiden_name`

Click the top row.

> Necessity score 0.900. The formula is spelled out:
> **sensitivity 0.90 × (1 − usage) 1.00 × staleness 1.00**.

Walk the three signal bars in the detail panel:

- **Sensitivity 0.90** — "knowledge_based PII, matched the rule
  `name:knowledge_based`."
- **Usage 0.00** — "**zero reads, zero writes** in the 90-day window. No
  application query touches this column."
- **Staleness 1.00** — "the oldest row is 900 days old — **535 days past** the
  365-day retention policy."

> Sensitive, never used, and overdue. That's a real minimization violation,
> and the tool shows *why*, not just *that*.

### 2:00 — The AND-gate: `users.date_of_birth` is NOT flagged

Close the panel. It's the **3rd row** — necessity score **≈ 0.50**, right
behind the two flagged columns. Click it (verdict **Keep**).

> Score 0.50 — that *looks* like a violation. It's sensitive (0.85, an
> identity attribute) and stale (1.00, same old rows). But look at the middle
> signal: **usage 0.42** — the app reads `date_of_birth` for age
> verification. That's above the **0.20** "unused" threshold, so `is_unused`
> is false, and the column is **not flagged** — verdict Keep.

> This is the whole point. Multiply the three signals and `date_of_birth`
> scores higher than most columns. A score cutoff would flag it. The AND-gate
> doesn't, because the data is still being used. A plain PII scanner flags
> `date_of_birth` and `mothers_maiden_name` identically; this tool only
> escalates the one that's genuinely a problem.

(If you want an even starker contrast: `users.phone` — sensitivity 0.90,
**3,700 reads**, verdict Keep.)

### 2:45 — The remediation draft

Back to `users.mothers_maiden_name`, scroll to the **Suggested fix** block.

> For every flagged column it drafts the SQL to actually fix it —
> archive the data to a `dma_archive` schema for legal hold, then
> `DROP COLUMN`. **Nothing runs** — it's a draft with a Copy button.

Point at the **comment header** in the SQL — the necessity evidence is baked
in as a comment. Then the **CAUTION lines**:

> "Confirm no legal or contractual retention obligation before dropping."
> "Take a backup, run in a maintenance window." And there's a commented-out
> ALTERNATIVE — if only old rows are the problem, delete just those.

Click **Copy SQL** (it says "Copied").

### 3:30 — Compare runs (the recurring-audit workflow)

Top nav → **Compare runs**. Baseline = the **365d** run, current = the
**1200d** run.

> A real audit is periodic, not one-shot. This diffs two runs.
> Here I changed the retention policy from 1 year to ~3 years.
> `mothers_maiden_name` and `ssn` move from **flag_for_deletion → review** —
> still sensitive and unused, but no longer past policy. The tool catches
> exactly when a column crosses a threshold, in either direction.

Click one of the columns in the diff → it jumps back to that column's
evidence.

### 4:15 — Wrap

> So: it connects **read-only** to a live database, cross-references
> sensitivity, real query-log usage, and retention age, and produces a
> prioritised list with an evidence trail and a ready-to-review fix for each
> finding. Everything you saw is backed by 213 passing tests.

---

## If they ask — optional talking points (don't click unless asked)

### "How do you know a column is sensitive?"

> Regex + keyword rules over the column name, sample values, and type —
> `python -m auditor.sensitivity --evaluate` scores **1.0 precision and
> recall** on the labelled catalogue. There's also an **ML classifier**
> (scikit-learn) and a **hybrid** mode — `--compare` shows them side by side.
> The ML model is trained on the regex classifier's own labels, so it
> *approximates* the rules rather than independently discovering sensitivity —
> that's an honest limitation, in the README. Regex stays the default because
> a compliance tool has to justify every flag with a concrete rule.

### "Where does the retention policy come from?"

> Normally a small YAML file — per-table day thresholds. There's also a
> **document extractor**: give it a written retention policy (we have a
> synthetic one in `seed-data/`), and it pulls out "financial records →
> 7 years", "support tickets → 1 year" by regex + cue words, maps the
> categories to tables, and produces the same config.
> `python -m auditor.retention.policy_extractor --check`. The mapping step is
> deliberately a *human-reviewed draft* — a policy sentence doesn't name a
> column, so it flags the ambiguities instead of guessing. This closes a gap
> our literature survey found: existing tools do document-NLP *or*
> live-database analysis, never both.

### "Is it really read-only?"

> Three independent layers: a `SELECT`-only PostgreSQL role, every session
> forced `transaction_read_only`, and a statement guard that rejects
> non-reads. The connector tries to write at startup and refuses to run if
> the database *lets* it. `python -m auditor.ingestion --check`.

### "How fast is it?"

> The full pipeline — schema extraction, three analysis engines, scoring — is
> about **one second** on the seed database.

---

## Reset between demos

```powershell
python seed-data\seed.py --reset            # back to a known DB + query log
Remove-Item auditor\audit_history.db        # clear run history (optional)
```
Then re-do "Before you present" step 4. Give OneDrive a minute to settle (or
keep sync paused) before the first audit.
