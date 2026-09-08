import { verdict, when } from '../format'

export default function RunSummaryBar({ run }) {
  if (!run) return null
  const t = run.thresholds || {}
  const order = ['flag_for_deletion', 'review', 'keep', 'not_a_risk']
  return (
    <div className="summary-bar">
      <div className="summary-main">
        <strong>{run.n_flagged}</strong> of {run.n_columns} columns flagged
        <span className="summary-sep">·</span>
        <span className="summary-dim">{run.target}</span>
      </div>
      <div className="summary-meta">
        <span>policy {run.policy?.default_days ?? '?'}d</span>
        <span>window {run.window_days}d</span>
        <span>
          thresholds: sensitive ≥ {t.sensitive} · unused ≤ {t.unused} · stale ≥ {t.stale}
        </span>
        <span>{when(run.finished_at)}</span>
        {run.note && <span className="summary-note">“{run.note}”</span>}
      </div>
      <div className="verdict-counts">
        {order.map((v) => {
          const n = run.verdict_counts?.[v] ?? 0
          const m = verdict(v)
          return (
            <span key={v} className={`vc ${m.className}`} title={m.label}>
              {m.label}: {n}
            </span>
          )
        })}
      </div>
    </div>
  )
}
