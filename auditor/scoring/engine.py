"""Necessity scoring engine.

score  = sensitivity.score * (1 - usage.score) * retention.score      (0-1)

is_flagged  = is_sensitive AND is_unused AND is_stale

where each condition is an explicit cut on the 0-1 score the relevant phase
already produces:

    is_sensitive   sensitivity.score >= 0.50
    is_unused      usage.score       <= 0.20
    is_stale       retention.score   >= 0.50

Rationale for each threshold - see auditor/scoring/README.md:

  * 0.50 sensitivity  = Phase 2's own `is_sensitive` cutoff; sits mid-way
    through its "medium" tier (none <0.15, low <0.40, medium <0.70, high).
    On the seed it cleanly separates catalogue tier {high, medium} from
    {low, none}.
  * 0.20 usage        = top of Phase 3's "rare" tier (unused <1e-9,
    rare <0.20, occasional <0.45, ...). Captures "never queried" (0.0) and
    "queried a handful of times months ago" (ssn = 0.11) while excluding
    "occasionally queried" (gender = 0.25).
  * 0.50 staleness    = Phase 4 score 0.50 means the oldest data is overdue
    by half a full retention period - well past policy, not a rounding
    error. (score = clamp(days_overdue / policy_days, 0, 1).)

verdict:
    not is_sensitive                       -> "not_a_risk"     (favorite_color)
    is_sensitive, not is_unused            -> "keep"           (email: actively used)
    is_sensitive, is_unused, not is_stale  -> "review"         (dormant, not yet overdue)
    is_sensitive, is_unused, is_stale      -> "flag_for_deletion"
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional

from auditor.retention.checker import ColumnRetention
from auditor.sensitivity.classifier import ColumnSensitivity
from auditor.usage.analyzer import ColumnUsage

THRESHOLDS = {
    "sensitive": 0.50,   # sensitivity.score >= this
    "unused": 0.20,      # usage.score <= this
    "stale": 0.50,       # retention.score >= this
}

VERDICT_ORDER = ["flag_for_deletion", "review", "keep", "not_a_risk"]


@dataclass
class NecessityScore:
    qualified_name: str
    table: str
    column: str

    score: float                       # sensitivity * (1 - usage) * staleness
    is_flagged: bool                   # sensitive AND unused AND stale
    verdict: str                       # flag_for_deletion | review | keep | not_a_risk

    sensitivity: float
    usage: float
    retention: float

    is_sensitive: bool
    is_unused: bool
    is_stale: bool

    breakdown: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScoringResult:
    columns: list[NecessityScore]
    thresholds: dict
    n_columns: int
    n_flagged: int
    verdict_counts: dict

    def column(self, qualified_name: str) -> Optional[NecessityScore]:
        return next((c for c in self.columns if c.qualified_name == qualified_name), None)

    @property
    def by_name(self) -> dict[str, NecessityScore]:
        return {c.qualified_name: c for c in self.columns}

    @property
    def flagged(self) -> list[NecessityScore]:
        return [c for c in self.columns if c.is_flagged]

    def to_dict(self) -> dict:
        return {
            "thresholds": self.thresholds,
            "n_columns": self.n_columns,
            "n_flagged": self.n_flagged,
            "verdict_counts": self.verdict_counts,
            "columns": [c.to_dict() for c in self.columns],
        }

    def ranked_report(self, top: Optional[int] = None) -> str:
        rows = [
            f"{'#':>2}  {'column':<38} {'necessity':>9} {'sens':>5} {'1-use':>6} "
            f"{'stale':>6}  {'verdict':<17} why"
        ]
        rows.append("-" * 118)
        cols = self.columns if top is None else self.columns[:top]
        for i, c in enumerate(cols, 1):
            why = c.reasons[0] if c.reasons else ""
            rows.append(
                f"{i:>2}  {c.qualified_name:<38} {c.score:>9.3f} {c.sensitivity:>5.2f} "
                f"{1 - c.usage:>6.2f} {c.retention:>6.2f}  {c.verdict:<17} {why[:44]}"
            )
        rows.append("")
        rows.append(f"flagged for deletion: {self.n_flagged} of {self.n_columns} columns  "
                    f"({', '.join(f'{k}={v}' for k, v in self.verdict_counts.items())})")
        return "\n".join(rows)


class ScoringEngine:
    def __init__(
        self,
        *,
        t_sensitive: float = THRESHOLDS["sensitive"],
        t_unused: float = THRESHOLDS["unused"],
        t_stale: float = THRESHOLDS["stale"],
    ):
        self.t_sensitive = t_sensitive
        self.t_unused = t_unused
        self.t_stale = t_stale

    @property
    def thresholds(self) -> dict:
        return {"sensitive": self.t_sensitive, "unused": self.t_unused, "stale": self.t_stale}

    # ------------------------------------------------------------------ #

    def score_column(
        self,
        sensitivity: Optional[ColumnSensitivity],
        usage: Optional[ColumnUsage],
        retention: Optional[ColumnRetention],
        *,
        qualified_name: Optional[str] = None,
    ) -> NecessityScore:
        qn = qualified_name or _first_qn(sensitivity, usage, retention)
        table, _, column = qn.rpartition(".")

        s = sensitivity.score if sensitivity else 0.0
        u = usage.score if usage else 0.0
        r = retention.score if retention else 0.0

        score = round(s * (1.0 - u) * r, 4)

        is_sensitive = s >= self.t_sensitive
        is_unused = u <= self.t_unused
        is_stale = r >= self.t_stale
        is_flagged = is_sensitive and is_unused and is_stale

        if not is_sensitive:
            verdict = "not_a_risk"
        elif not is_unused:
            verdict = "keep"
        elif not is_stale:
            verdict = "review"
        else:
            verdict = "flag_for_deletion"

        breakdown = {
            "formula": "sensitivity * (1 - usage) * staleness",
            "sensitivity": {
                "score": round(s, 4), "threshold": self.t_sensitive,
                "is_sensitive": is_sensitive, "factor": round(s, 4),
            },
            "usage": {
                "score": round(u, 4), "threshold": self.t_unused,
                "is_unused": is_unused, "factor": round(1.0 - u, 4),
            },
            "staleness": {
                "score": round(r, 4), "threshold": self.t_stale,
                "is_stale": is_stale, "factor": round(r, 4),
            },
            "product": score,
        }

        return NecessityScore(
            qualified_name=qn, table=table, column=column,
            score=score, is_flagged=is_flagged, verdict=verdict,
            sensitivity=round(s, 4), usage=round(u, 4), retention=round(r, 4),
            is_sensitive=is_sensitive, is_unused=is_unused, is_stale=is_stale,
            breakdown=breakdown,
            reasons=self._reasons(verdict, is_sensitive, is_unused, is_stale,
                                  sensitivity, usage, retention),
            evidence=self._evidence(sensitivity, usage, retention),
        )

    def score_all(
        self,
        sensitivities: Iterable[ColumnSensitivity],
        usages: Iterable[ColumnUsage],
        retentions: Iterable[ColumnRetention],
    ) -> ScoringResult:
        sens = {c.qualified_name: c for c in sensitivities}
        use = {c.qualified_name: c for c in usages}
        ret = {c.qualified_name: c for c in retentions}
        names = list(dict.fromkeys([*sens, *use, *ret]))

        scored = [
            self.score_column(sens.get(n), use.get(n), ret.get(n), qualified_name=n)
            for n in names
        ]
        # action-first: flag_for_deletion, then review, then keep, then not_a_risk;
        # within each verdict, highest necessity score first
        scored.sort(key=lambda c: (VERDICT_ORDER.index(c.verdict), -c.score, c.qualified_name))

        counts = {v: 0 for v in VERDICT_ORDER}
        for c in scored:
            counts[c.verdict] = counts.get(c.verdict, 0) + 1

        return ScoringResult(
            columns=scored, thresholds=self.thresholds,
            n_columns=len(scored), n_flagged=sum(c.is_flagged for c in scored),
            verdict_counts=counts,
        )

    # ------------------------------------------------------------------ #

    @staticmethod
    def _reasons(verdict, is_sensitive, is_unused, is_stale, sens, usage, ret) -> list[str]:
        out: list[str] = []
        s = sens.score if sens else 0.0
        u = usage.score if usage else 0.0
        r = ret.score if ret else 0.0
        pii = f" ({sens.pii_type})" if sens and sens.pii_type else ""

        if verdict == "flag_for_deletion":
            out.append(
                f"sensitive{pii} (score {s:.2f}), not queried in the window "
                f"(usage {u:.2f}), and {_overdue_phrase(ret)} - minimization violation"
            )
        elif verdict == "keep":
            reads = usage.read_count if usage else 0
            out.append(
                f"sensitive{pii} but actively used ({reads:,} reads, usage {u:.2f} "
                f"> {THRESHOLDS['unused']}) - keep"
            )
        elif verdict == "review":
            out.append(
                f"sensitive{pii} and unused (usage {u:.2f}) but data not yet overdue "
                f"(staleness {r:.2f} < {THRESHOLDS['stale']}) - review, don't delete yet"
            )
        else:  # not_a_risk
            out.append(
                f"not sensitive (score {s:.2f} < {THRESHOLDS['sensitive']}) - "
                f"unused/stale but not a compliance risk"
            )

        out.append(f"sensitive={is_sensitive} (s={s:.2f}), "
                   f"unused={is_unused} (u={u:.2f}), stale={is_stale} (r={r:.2f})")
        return out

    @staticmethod
    def _evidence(sens, usage, ret) -> dict:
        ev: dict = {}
        if sens:
            ev["sensitivity"] = {
                "pii_type": sens.pii_type,
                "tier": sens.tier,
                "matched_rules": sens.matched_rules,
            }
        if usage:
            ev["usage"] = {
                "tier": usage.tier,
                "read_count": usage.read_count,
                "write_count": usage.write_count,
                "last_access_at": usage.last_access_at,
                "access_by_service": usage.access_by_service,
                "query_templates": usage.query_templates,
            }
        if ret:
            ev["retention"] = {
                "is_overdue": ret.is_overdue,
                "days_overdue": ret.days_overdue,
                "policy_applied": ret.policy_applied,
                "oldest_row_age_days": ret.oldest_row_age_days,
                "basis": ret.basis,
                "fraction_overdue": ret.fraction_overdue,
            }
        return ev


def _overdue_phrase(ret: Optional[ColumnRetention]) -> str:
    if not ret:
        return "past retention policy"
    if ret.days_overdue and ret.policy_applied.get("days"):
        return (f"{ret.days_overdue} days past the "
                f"{ret.policy_applied['days']}-day retention policy")
    return f"past retention policy (staleness {ret.score:.2f})"


def _first_qn(*objs) -> str:
    for o in objs:
        if o is not None:
            return o.qualified_name
    raise ValueError("score_column needs at least one of sensitivity/usage/retention")


def score_database(
    sensitivities: Iterable[ColumnSensitivity],
    usages: Iterable[ColumnUsage],
    retentions: Iterable[ColumnRetention],
    **engine_kwargs,
) -> ScoringResult:
    return ScoringEngine(**engine_kwargs).score_all(sensitivities, usages, retentions)
