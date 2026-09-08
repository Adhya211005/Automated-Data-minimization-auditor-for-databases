export const VERDICTS = {
  flag_for_deletion: { label: 'Flag for deletion', className: 'v-flag' },
  review: { label: 'Review', className: 'v-review' },
  keep: { label: 'Keep', className: 'v-keep' },
  not_a_risk: { label: 'Not a risk', className: 'v-none' },
}

export const verdict = (v) => VERDICTS[v] || { label: v, className: 'v-none' }

export const pct = (x) => `${Math.round((x ?? 0) * 100)}%`
export const num = (x) => (x ?? 0).toLocaleString()

export function when(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

export const runLabel = (r) =>
  `${when(r.finished_at)} · policy ${r.policy?.default_days ?? '?'}d · ${r.n_flagged} flagged`
