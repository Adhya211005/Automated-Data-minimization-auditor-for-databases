import { useState } from 'react'
import { verdict, num } from '../format'

function MiniBar({ value, hit }) {
  return (
    <div className="mini-track" title={value?.toFixed(2)}>
      <div className={`mini-fill ${hit ? 'hit' : ''}`} style={{ width: `${(value ?? 0) * 100}%` }} />
    </div>
  )
}

export default function ReportTable({ columns, selected, onSelect }) {
  const [flaggedOnly, setFlaggedOnly] = useState(false)
  const rows = flaggedOnly ? columns.filter((c) => c.is_flagged) : columns

  return (
    <div className="report">
      <label className="filter-toggle">
        <input
          type="checkbox"
          checked={flaggedOnly}
          onChange={(e) => setFlaggedOnly(e.target.checked)}
        />
        flagged only
      </label>
      <table className="report-table">
        <thead>
          <tr>
            <th>#</th>
            <th>column</th>
            <th className="num">necessity</th>
            <th>verdict</th>
            <th className="sig">sensitive</th>
            <th className="sig">unused (1−usage)</th>
            <th className="sig">stale</th>
            <th>why</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => {
            const m = verdict(c.verdict)
            return (
              <tr
                key={c.qualified_name}
                className={
                  (c.is_flagged ? 'row-flagged ' : '') +
                  (selected === c.qualified_name ? 'row-selected' : '')
                }
                onClick={() => onSelect(c.qualified_name)}
              >
                <td className="dim">{c.rank}</td>
                <td className="mono">{c.qualified_name}</td>
                <td className="num strong">{c.score.toFixed(3)}</td>
                <td>
                  <span className={`badge ${m.className}`}>{m.label}</span>
                </td>
                <td><MiniBar value={c.sensitivity} hit={c.is_sensitive} /></td>
                <td><MiniBar value={1 - c.usage} hit={c.is_unused} /></td>
                <td><MiniBar value={c.retention} hit={c.is_stale} /></td>
                <td className="why">{c.reasons?.[0]}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {rows.length === 0 && <p className="empty">No columns match.</p>}
      <p className="hint">
        Flagged = <em>sensitive AND unused AND stale</em>, all three — not a score cutoff.
        {' '}Click a row for the full evidence.
      </p>
    </div>
  )
}
