import { useEffect, useState } from 'react'
import { api } from '../api'
import { verdict, runLabel } from '../format'

function DeltaList({ title, items, tone, render }) {
  if (!items || items.length === 0) return null
  return (
    <section className={`delta-block ${tone}`}>
      <h4>{title} <span className="count">{items.length}</span></h4>
      <ul>{items.map((it, i) => <li key={i}>{render(it)}</li>)}</ul>
    </section>
  )
}

export default function CompareView({ runs, onOpenColumn }) {
  const [base, setBase] = useState('')
  const [head, setHead] = useState('')
  const [diff, setDiff] = useState(null)
  const [err, setErr] = useState(null)

  useEffect(() => {
    if (runs.length >= 2) {
      setHead(runs[0].id)
      setBase(runs[1].id)
    } else if (runs.length === 1) {
      setHead(runs[0].id); setBase(runs[0].id)
    }
  }, [runs])

  useEffect(() => {
    if (!base || !head) return
    setErr(null)
    api.compare(base, head).then(setDiff).catch((e) => { setErr(e.message); setDiff(null) })
  }, [base, head])

  if (runs.length < 2) return <p className="dim">Need at least two audit runs to compare. Trigger another run.</p>

  const pick = (val, set, label) => (
    <label className="run-pick">
      {label}
      <select value={val} onChange={(e) => set(e.target.value)}>
        {runs.map((r) => <option key={r.id} value={r.id}>{runLabel(r)}</option>)}
      </select>
    </label>
  )

  return (
    <div className="compare">
      <div className="compare-picks">
        {pick(base, setBase, 'baseline')}
        <span className="arrow">→</span>
        {pick(head, setHead, 'current')}
      </div>
      {err && <p className="error">{err}</p>}
      {diff && (
        <>
          <p className="compare-headline">
            flagged columns: {diff.base.n_flagged} → {diff.head.n_flagged}{' '}
            <strong className={diff.n_flagged_delta > 0 ? 'up' : diff.n_flagged_delta < 0 ? 'down' : ''}>
              ({diff.n_flagged_delta >= 0 ? '+' : ''}{diff.n_flagged_delta})
            </strong>
          </p>
          <DeltaList
            title="Newly flagged" tone="bad" items={diff.newly_flagged}
            render={(it) => (
              <button className="linkish" onClick={() => onOpenColumn(it.qualified_name)}>
                {it.qualified_name}
              </button>
            )}
          />
          <DeltaList
            title="No longer flagged" tone="good" items={diff.no_longer_flagged}
            render={(it) => (
              <button className="linkish" onClick={() => onOpenColumn(it.qualified_name)}>
                {it.qualified_name}
              </button>
            )}
          />
          <DeltaList
            title="Verdict changed" tone="warn" items={diff.verdict_changed}
            render={(it) => (
              <span>
                <button className="linkish" onClick={() => onOpenColumn(it.qualified_name)}>{it.qualified_name}</button>
                {'  '}
                <span className={`badge ${verdict(it.from).className}`}>{verdict(it.from).label}</span>
                {' → '}
                <span className={`badge ${verdict(it.to).className}`}>{verdict(it.to).label}</span>
              </span>
            )}
          />
          <DeltaList
            title="Necessity score moved (≥ 0.05)" tone="plain" items={diff.score_moved}
            render={(it) => (
              <span>
                <button className="linkish" onClick={() => onOpenColumn(it.qualified_name)}>{it.qualified_name}</button>
                {'  '}{(it.score - it.score_delta).toFixed(3)} → {it.score.toFixed(3)}
                {' '}<span className={it.score_delta > 0 ? 'up' : 'down'}>
                  ({it.score_delta > 0 ? '+' : ''}{it.score_delta.toFixed(3)})
                </span>
              </span>
            )}
          />
          {diff.newly_flagged.length === 0 && diff.no_longer_flagged.length === 0 &&
           diff.verdict_changed.length === 0 && diff.score_moved.length === 0 && (
            <p className="dim">No changes — the two runs are identical.</p>
          )}
        </>
      )}
    </div>
  )
}
