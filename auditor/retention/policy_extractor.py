"""Draft a RetentionPolicy from a written retention-policy document.

Closes the gap the literature survey names: papers #2-4 do NLP over legal /
contract text but never connect it to a live database; Phase 4 does the live
database. This bolts the two together - it reads a policy doc, pulls out
"<category> -> <N days>" rules, maps the categories onto real tables/columns,
and emits a ``RetentionPolicy`` in the *exact* shape Phase 4 already consumes.

Extraction is **rule-based** (regex for durations + retention/anti cue words +
a synonym lexicon for mapping). No LLM: it keeps the step deterministic and
testable with no API key. An LLM (Groq is in the stack) would help most with
(a) category-phrase extraction from complex sentences and (b) semantic table
mapping - see ``extract_rules_llm`` for where it would slot in.

**This is an assistive draft, not an autonomous decision.** Mapping a policy
sentence to a specific column is inherently ambiguous; every proposal carries
a confidence and ``needs_confirmation=True``. A human accepts/edits before the
policy is used.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from auditor.ingestion.metadata import DatabaseMetadata
from auditor.retention.policy import RetentionPolicy

SAMPLE_DOC = Path(__file__).resolve().parents[2] / "seed-data" / "sample_retention_policy.md"

# --------------------------------------------------------------------------- #
# 1. duration parsing
# --------------------------------------------------------------------------- #

_UNIT_DAYS = {
    "day": 1, "week": 7, "month": 30, "year": 365,
    "yr": 365, "yrs": 365, "mo": 30, "mos": 30, "wk": 7, "hour": 1 / 24, "hr": 1 / 24,
}
_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "eighteen": 18, "twenty": 20, "twenty-four": 24, "thirty": 30, "thirty-six": 36,
    "sixty": 60, "ninety": 90,
}
_DURATION_RX = re.compile(
    r"\b(\d{1,4}|" + "|".join(re.escape(w) for w in _NUMBER_WORDS) + r")"
    r"[\s-]*(day|week|month|year|yr|yrs|mo|mos|wk|hour|hr)s?\b",
    re.I,
)

_RETENTION_CUES = (
    "retain", "retention", "retained", "kept", "keep", "held", "hold", "stored", "store",
    "deleted", "delete", "purge", "purged", "erase", "erased", "anonymis", "dispose",
    "disposed", "no longer than", "maximum of", "scheduled for deletion", "must not be kept",
)
_ANTI_CUES = (
    "access request", "breach", "report to", "reported", "notify", "notified",
    "training", "reviewed", "is reviewed", "review cycle", "backup", "rotated",
    "does not extend", "does not apply", "start date", "of receipt", "within 72",
    "supervisory authority",
)
_DEFAULT_MARKERS = ("not listed above", "any other", "all other", "by default",
                    "unless otherwise", "not specified")
_CONDITIONAL = ("unless", "except", "restarts", "whichever", "subject to")


def _to_days(number: str, unit: str) -> int:
    n = _NUMBER_WORDS.get(number.lower(), None)
    if n is None:
        n = float(number)
    return max(1, round(n * _UNIT_DAYS[unit.lower()]))


# --------------------------------------------------------------------------- #
# 2. rule extraction
# --------------------------------------------------------------------------- #

@dataclass
class ExtractedRule:
    category: str            # the noun phrase the sentence is about
    retention_days: int
    raw_period: str          # "7 years"
    sentence: str            # source sentence (evidence)
    heading: str             # nearest section heading
    keywords: list[str]      # normalized content words for mapping
    confidence: float        # 0-1, how explicit the phrasing is
    is_default: bool = False

    def to_dict(self) -> dict:
        return {
            "category": self.category, "retention_days": self.retention_days,
            "raw_period": self.raw_period, "confidence": round(self.confidence, 2),
            "is_default": self.is_default, "heading": self.heading,
            "sentence": self.sentence, "keywords": self.keywords,
        }


_STOP = set("the a an of for to and or in on at from by with is are be as this that "
            "their its it any which such including associated related date after "
            "period than only up".split())


def _keywords(*phrases: str) -> list[str]:
    out: list[str] = []
    for p in phrases:
        for w in re.findall(r"[a-z][a-z-]+", p.lower()):
            w = w.rstrip("s") if len(w) > 4 else w        # crude singularize
            if w not in _STOP and w not in out and len(w) > 2:
                out.append(w)
    return out


def _paragraphs(text: str) -> Iterable[tuple[str, str]]:
    """Reflow wrapped lines into paragraphs (headings and bullets are their own
    unit), so a sentence split across three physical lines stays whole."""
    heading = ""
    buf: list[str] = []

    def flush() -> Optional[str]:
        nonlocal buf
        if buf:
            para, buf = " ".join(buf), []
            return para
        return None

    for raw in text.splitlines():
        line = raw.strip()
        is_bullet = bool(re.match(r"^[-*]\s+", line))
        if not line or line.startswith("#") or is_bullet:
            p = flush()
            if p:
                yield heading, p
            if line.startswith("#"):
                heading = re.sub(r"^[\d.\s]+", "", line.lstrip("#").strip())
            elif is_bullet:
                buf.append(re.sub(r"[*_`]", "", re.sub(r"^[-*]\s+", "", line)))
            continue
        buf.append(re.sub(r"[*_`]", "", line).strip("_ "))
    p = flush()
    if p:
        yield heading, p


def _sentences(text: str) -> Iterable[tuple[str, str]]:
    for heading, para in _paragraphs(text):
        for s in re.split(r"(?<=[.;:])\s+(?=[A-Z(\"'\d])", para):
            s = s.strip()
            if len(s) > 8:
                yield heading, s


_VERB_CUES = (
    "must be retained", "must not be kept", "are retained", "is retained",
    "should be deleted", "must be deleted", "are deleted", "is deleted",
    "are kept", "is kept", "are held", "is held", "are purged", "is purged",
    "are scheduled", "scheduled for deletion", "are stored", "is stored",
    "retained for", "kept for", "held for", "stored for", "deleted after",
    "purged after", "no longer than", "for a maximum of",
)


def _category_phrase(sentence: str) -> str:
    """Everything before the first retention verb / the first duration."""
    low = sentence.lower()
    cut = len(sentence)
    for cue in _VERB_CUES:
        i = low.find(cue)
        if i != -1:
            cut = min(cut, i)
    m = _DURATION_RX.search(sentence)
    if m:
        cut = min(cut, m.start())
    phrase = sentence[:cut].strip(" ,;:.")
    phrase = re.sub(r"^(this includes|any category of|records of|aggregated|"
                    r"data subject|new staff|personal data breaches?)\s+",
                    "", phrase, flags=re.I)
    return phrase or sentence


def extract_rules(text: str) -> list[ExtractedRule]:
    rules: list[ExtractedRule] = []
    for heading, sentence in _sentences(text):
        low = sentence.lower()
        durations = list(_DURATION_RX.finditer(sentence))
        if not durations:
            continue
        has_retention = any(c in low for c in _RETENTION_CUES)
        has_anti = any(c in low for c in _ANTI_CUES)
        if has_anti and not (" retained for " in low or " deleted after " in low
                             or "no longer than" in low or "maximum of" in low):
            continue
        if not has_retention:
            continue

        m = durations[0]           # first duration = the retention period
        days = _to_days(m.group(1), m.group(2))
        raw = m.group(0)

        if "hour" in m.group(2).lower() or "hr" in m.group(2).lower():
            continue               # 72-hour breach window etc. - never a retention period

        conf = 0.6
        if any(c in low for c in ("must be retained for", "retention period", "retained for",
                                  "is retained for", "are retained for")):
            conf = 0.9
        elif any(c in low for c in ("kept for", "held for", "deleted after", "no longer than",
                                    "maximum of", "purged after", "should be deleted")):
            conf = 0.75
        if any(c in low for c in _CONDITIONAL):
            conf -= 0.15
        conf = round(max(0.3, conf), 2)

        is_default = any(mk in low for mk in _DEFAULT_MARKERS)
        category = _category_phrase(sentence)
        rules.append(ExtractedRule(
            category=category, retention_days=days, raw_period=raw, sentence=sentence,
            heading=heading, keywords=_keywords(category, heading), confidence=conf,
            is_default=is_default,
        ))
    return rules


def extract_rules_llm(text: str, client=None):  # pragma: no cover - not wired
    """Where a Groq/LLM call would go: prompt the model to return a JSON list of
    {category, retention_days, evidence_sentence, confidence}. Not implemented -
    the rule-based path is deterministic and needs no API key. Kept as a
    documented seam."""
    raise NotImplementedError(
        "LLM extraction is not configured; use extract_rules(). See README."
    )


# --------------------------------------------------------------------------- #
# 3. mapping categories -> tables / columns
# --------------------------------------------------------------------------- #

# schema token -> policy vocabulary. Tables and a curated set of columns.
_SYNONYMS: dict[str, list[str]] = {
    "orders": ["order", "transaction", "invoice", "payment", "purchase", "financial",
               "sale", "billing", "shipping", "checkout"],
    "support_tickets": ["support", "ticket", "correspondence", "complaint", "helpdesk",
                        "case", "enquiry", "customer support"],
    "users": ["user", "account", "profile", "customer", "member", "personal data",
              "individual", "registration", "subscriber"],
    "users.marketing_consent": ["marketing", "consent", "opt-in", "opt in", "opt-out",
                                "subscription", "newsletter", "withdrawal", "preference"],
    "users.last_login_at": ["login", "authentication", "security log", "session",
                            "sign-in", "access log", "login history", "security monitoring"],
    "users.last_login_ip": ["ip", "ip address", "source ip", "login history", "network"],
    "users.gender": ["gender", "demographic", "sex", "reporting"],
    "users.date_of_birth": ["age", "age band", "birth", "date of birth", "demographic"],
    "support_tickets.satisfaction_rating": ["satisfaction", "survey", "csat", "feedback score",
                                            "rating"],
    "support_tickets.body": ["correspondence", "message", "conversation"],
}


@dataclass
class MappingProposal:
    rule: ExtractedRule
    target_kind: str                    # "table" | "column" | "default" | "unmapped"
    target: Optional[str]
    match_score: float                  # 0-1
    alternatives: list[tuple[str, float]] = field(default_factory=list)
    needs_confirmation: bool = True
    suggested: bool = False             # score high AND clear margin -> pre-ticked for review

    def to_dict(self) -> dict:
        return {
            "target_kind": self.target_kind, "target": self.target,
            "match_score": round(self.match_score, 2), "suggested": self.suggested,
            "needs_confirmation": self.needs_confirmation,
            "alternatives": [[t, round(s, 2)] for t, s in self.alternatives],
            "retention_days": self.rule.retention_days,
            "category": self.rule.category, "evidence": self.rule.sentence,
            "extraction_confidence": round(self.rule.confidence, 2),
        }


def _score_target(rule_keywords: list[str], category: str, target: str) -> float:
    vocab = _SYNONYMS.get(target, [])
    name_tokens = _keywords(target.replace(".", " ").replace("_", " "))
    cat = category.lower()

    lexicon_hits = sum(1 for term in vocab if term in cat or any(k in term or term in k
                                                                 for k in rule_keywords))
    lexicon = min(1.0, lexicon_hits / 2.0)

    overlap = (len(set(rule_keywords) & set(name_tokens)) / len(name_tokens)
               if name_tokens else 0.0)
    fuzzy = max(
        (difflib.SequenceMatcher(None, k, nt).ratio()
         for k in rule_keywords for nt in name_tokens),
        default=0.0,
    )
    return round(0.6 * lexicon + 0.25 * overlap + 0.15 * fuzzy, 3)


def propose_mappings(
    rules: Iterable[ExtractedRule], metadata: DatabaseMetadata,
    *, min_score: float = 0.30,
) -> list[MappingProposal]:
    table_names = set(metadata.table_names)
    column_names = set(metadata.column_names)
    candidates = (
        [t for t in _SYNONYMS if "." not in t and t in table_names]
        + [c for c in _SYNONYMS if "." in c and c in column_names]
    )

    out: list[MappingProposal] = []
    for rule in rules:
        if rule.is_default:
            out.append(MappingProposal(rule, "default", None, 1.0, needs_confirmation=True,
                                       suggested=True))
            continue
        scored = sorted(
            ((c, _score_target(rule.keywords, rule.category, c)) for c in candidates),
            key=lambda p: -p[1],
        )
        best, best_score = scored[0] if scored else (None, 0.0)
        second = scored[1][1] if len(scored) > 1 else 0.0

        if best is None or best_score < min_score:
            out.append(MappingProposal(rule, "unmapped", None, best_score,
                                       alternatives=[(t, s) for t, s in scored[:3] if s > 0]))
            continue

        kind = "column" if "." in best else "table"
        out.append(MappingProposal(
            rule, kind, best, best_score,
            alternatives=[(t, s) for t, s in scored[1:4] if s > 0.1],
            needs_confirmation=True,
            suggested=(best_score >= 0.5 and (best_score - second) >= 0.12),
        ))
    return out


# --------------------------------------------------------------------------- #
# 4. draft -> RetentionPolicy (Phase 4 shape, unchanged engine)
# --------------------------------------------------------------------------- #

@dataclass
class PolicyDraft:
    proposals: list[MappingProposal]
    source_doc: str
    default_days: Optional[int] = None

    def __post_init__(self):
        if self.default_days is None:
            for p in self.proposals:
                if p.target_kind == "default":
                    self.default_days = p.rule.retention_days

    @property
    def conflicts(self) -> list[dict]:
        """Targets that more than one rule maps to, with different periods."""
        by_target: dict[str, list[MappingProposal]] = {}
        for p in self.proposals:
            if p.target:
                by_target.setdefault(p.target, []).append(p)
        out = []
        for tgt, ps in by_target.items():
            days = {p.rule.retention_days for p in ps}
            if len(days) > 1:
                out.append({
                    "target": tgt,
                    "options": sorted(
                        [{"days": p.rule.retention_days, "category": p.rule.category,
                          "evidence": p.rule.sentence} for p in ps],
                        key=lambda o: -o["days"],
                    ),
                    "resolution": "keeping the longer period; review",
                })
        return out

    @property
    def unmapped(self) -> list[ExtractedRule]:
        return [p.rule for p in self.proposals if p.target_kind == "unmapped"]

    def to_policy(self, accept: Optional[set[str]] = None) -> RetentionPolicy:
        """Build a RetentionPolicy. ``accept`` = the set of targets a human
        approved; if None, the ``suggested`` proposals are used (with a
        conflict kept at the longer period, flagged)."""
        tables: dict[str, int] = {}
        columns: dict[str, int] = {}
        for p in self.proposals:
            if p.target_kind in ("default", "unmapped") or p.target is None:
                continue
            if accept is not None and p.target not in accept:
                continue
            if accept is None and not p.suggested:
                continue
            bucket = columns if p.target_kind == "column" else tables
            days = p.rule.retention_days
            if p.target in bucket:                 # conflicting rules -> keep longer
                days = max(days, bucket[p.target])
            bucket[p.target] = days
        return RetentionPolicy(
            default_days=self.default_days or 365,
            tables=tables, columns=columns,
        )

    # -- rendering --------------------------------------------------
    def review_table(self) -> str:
        rows = ["Extracted retention rules and proposed schema mapping",
                "(review each - nothing is applied automatically)\n"]
        rows.append(f"  {'category':<34} {'days':>6} {'->':^4} {'target':<28} "
                    f"{'score':>6} {'':<4} evidence")
        rows.append("  " + "-" * 110)
        for p in self.proposals:
            tgt = p.target or ("(default)" if p.target_kind == "default" else "(unmapped)")
            tick = "OK?" if p.suggested else ("def" if p.target_kind == "default" else "")
            rows.append(
                f"  {p.rule.category[:33]:<34} {p.rule.retention_days:>6} "
                f"{'-->':^4} {tgt:<28} {p.match_score:>6.2f} {tick:<4} "
                f"\"{p.rule.sentence[:52]}...\""
            )
            if p.alternatives:
                alts = ", ".join(f"{t}({s:.2f})" for t, s in p.alternatives)
                rows.append(f"  {'':<44} alternatives: {alts}")
        rows.append(f"\n  default_days = {self.default_days}")
        rows.append("  'OK?' = high-confidence suggestion, still needs your sign-off.")
        return "\n".join(rows)

    def to_review_yaml(self) -> str:
        """A YAML policy pre-filled with the suggested mappings, every line
        commented with its evidence + confidence, ready to hand-edit."""
        lines = [
            f"# Retention policy drafted from: {self.source_doc}",
            "# REVIEW BEFORE USE. Each mapping is a keyword-similarity guess from a",
            "# policy sentence to a schema object - confirm or correct every one.",
            "",
            f"default_days: {self.default_days or 365}",
        ]
        tbls = [p for p in self.proposals if p.target_kind == "table"]
        cols = [p for p in self.proposals if p.target_kind == "column"]
        if tbls:
            lines.append("\ntables:")
            for p in tbls:
                mark = "" if p.suggested else "  # LOW CONFIDENCE - verify"
                lines.append(f"  {p.target}: {p.rule.retention_days}{mark}")
                lines.append(f"  # from: \"{p.rule.sentence}\"")
                lines.append(f"  # match={p.match_score:.2f} extraction={p.rule.confidence:.2f}"
                             + (f"  alts: {[t for t,_ in p.alternatives]}" if p.alternatives else ""))
        if cols:
            lines.append("\ncolumns:")
            for p in cols:
                mark = "" if p.suggested else "  # LOW CONFIDENCE - verify"
                lines.append(f"  {p.target}: {p.rule.retention_days}{mark}")
                lines.append(f"  # from: \"{p.rule.sentence}\"")
        unmapped = [p for p in self.proposals if p.target_kind == "unmapped"]
        if unmapped:
            lines.append("\n# UNMAPPED - could not tie these to a table/column:")
            for p in unmapped:
                lines.append(f"#   {p.rule.retention_days}d  \"{p.rule.category}\""
                             f"  (\"{p.rule.sentence[:70]}\")")
        return "\n".join(lines) + "\n"


def extract_policy(
    doc: str | Path = SAMPLE_DOC, metadata: Optional[DatabaseMetadata] = None,
) -> PolicyDraft:
    text = Path(doc).read_text(encoding="utf-8") if Path(str(doc)).exists() else str(doc)
    rules = extract_rules(text)
    if metadata is None:
        from auditor.ingestion import extract_metadata
        metadata = extract_metadata()
    proposals = propose_mappings(rules, metadata)
    return PolicyDraft(proposals=proposals, source_doc=str(doc))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _main(argv=None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(
        prog="python -m auditor.retention.policy_extractor",
        description="Draft a RetentionPolicy from a written retention-policy document.",
    )
    ap.add_argument("--doc", default=str(SAMPLE_DOC))
    ap.add_argument("--metadata", help="metadata JSON; omit to extract from the live DB")
    ap.add_argument("--out", metavar="FILE.yaml", help="write a review-ready YAML draft")
    ap.add_argument("--json", action="store_true", help="print the draft as JSON")
    ap.add_argument("--check", action="store_true",
                    help="build the policy (suggested mappings) and run it through the "
                         "Phase 4 retention checker against the seed")
    args = ap.parse_args(argv)

    meta = None
    if args.metadata:
        from auditor.ingestion.metadata import DatabaseMetadata
        meta = DatabaseMetadata.load(args.metadata)
    draft = extract_policy(args.doc, meta)

    if args.json:
        print(json.dumps({
            "source_doc": draft.source_doc,
            "default_days": draft.default_days,
            "proposals": [p.to_dict() for p in draft.proposals],
            "conflicts": draft.conflicts,
        }, indent=2))
        return 0

    print(draft.review_table())
    if draft.conflicts:
        print("\nCONFLICTS (one target, several periods) - resolve before use:")
        for c in draft.conflicts:
            opts = " vs ".join(f"{o['days']}d ({o['category'][:30]})" for o in c["options"])
            print(f"  {c['target']}: {opts}  -> {c['resolution']}")

    if args.out:
        Path(args.out).write_text(draft.to_review_yaml(), encoding="utf-8")
        print(f"\nwrote {args.out} - review every mapping, then load with "
              f"RetentionPolicy.from_file()")

    if args.check:
        from auditor.retention.checker import RetentionChecker
        m = meta
        if m is None:
            from auditor.ingestion import extract_metadata
            m = extract_metadata()
        policy = draft.to_policy()
        res = RetentionChecker(policy, m).check()
        print(f"\n=== Phase 4 retention checker with the drafted policy ===")
        print(f"policy: default={policy.default_days}d  tables={policy.tables}  "
              f"columns={policy.columns}\n")
        for t in sorted(res.tables, key=lambda x: -x.score):
            print(f"  {t.name:<18} policy={t.policy_days:>5}d  oldest={t.oldest_row_age_days}d  "
                  f"{'OVERDUE score=' + format(t.score, '.3f') if t.is_overdue else 'ok'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
