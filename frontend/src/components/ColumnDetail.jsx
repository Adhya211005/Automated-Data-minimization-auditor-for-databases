import { useEffect, useState } from 'react'
import { api } from '../api'
import { verdict, num, when } from '../format'
import SignalBar from './SignalBar'
import SuggestedFix from './SuggestedFix'

function Row({ k, v }) {
  if (v == null || v === '') return null
  return (
    <div className="kv">
      <span className="k">{k}</span>
      <span className="v">{String(v)}</span>
    </div>
  )
}

export default function ColumnDetail({ runId, qname, onClose }) {
  const [col, setCol] = useState(null)
  const [history, setHistory] = useState([])
  const [err, setErr] = useState(null)

  useEffect(() => {
    let live = true
    setCol(null); setErr(null); setHistory([])
    api.getColumn(runId, qname).then((c) => live && setCol(c)).catch((e) => live && setErr(e.message))
    api.columnHistory(qname).then((h) => live && setHistory(h)).catch(() => {})
    return () => { live = false }
  }, [runId, qname])

  const m = col ? verdict(col.verdict) : null
  const b = col?.breakdown || {}
  const ev = col?.evidence || {}

  return (
    <aside className="detail">
      <div className="detail-head">
        <div>
          <span className="mono detail-title">{qname}</span>
          {m && <span className={`badge ${m.className}`}>{m.label}</span>}
        </div>
        <button className="close" onClick={onClose}>✕</button>
      </div>

      {err && <p className="error">{err}</p>}
      {!col && !err && <p className="dim">Loading evidence…</p>}

      {col && (
        <div className="detail-body">
          <div className="necessity-line">
            necessity <strong>{col.score.toFixed(3)}</strong>
            <span className="formula">
              sensitivity {b.sensitivity?.factor?.toFixed(2)} × (1 − usage) {b.usage?.factor?.toFixed(2)}
              {' '}× staleness {b.staleness?.factor?.toFixed(2)} = {b.product?.toFixed(3)}
            </span>
          </div>

          <section>
            <h4>Why this verdict</h4>
            <ul className="reasons">
              {col.reasons?.map((r, i) => <li key={i}>{r}</li>)}
            </ul>
          </section>

          {col.remediation && col.remediation.strategy !== 'none' && (
            <SuggestedFix remediation={col.remediation} />
          )}

          <section className="signals">
            <h4>The three signals</h4>
            <SignalBar
              label="Sensitivity" score={b.sensitivity?.score} threshold={b.sensitivity?.threshold}
              pass={b.sensitivity?.is_sensitive} direction="ge"
              hint={ev.sensitivity ? `${ev.sensitivity.pii_type || 'PII'} · tier ${ev.sensitivity.tier}` : ''}
            />
            <SignalBar
              label="Usage" score={b.usage?.score} threshold={b.usage?.threshold}
              pass={b.usage?.is_unused} direction="le"
              hint={ev.usage ? `${num(ev.usage.read_count)} reads / ${num(ev.usage.write_count)} writes in window` : ''}
            />
            <SignalBar
              label="Staleness" score={b.staleness?.score} threshold={b.staleness?.threshold}
              pass={b.staleness?.is_stale} direction="ge"
              hint={ev.retention?.days_overdue != null
                ? `${num(ev.retention.days_overdue)} days past the ${ev.retention.policy_applied?.days}-day policy`
                : ev.retention?.is_overdue ? 'overdue' : 'within policy'}
            />
          </section>

          {ev.sensitivity && (
            <section>
              <h4>Sensitivity evidence</h4>
              <Row k="PII type" v={ev.sensitivity.pii_type} />
              <Row k="tier" v={ev.sensitivity.tier} />
              <div className="chips">
                {ev.sensitivity.matched_rules?.map((r) => (
                  <span className="chip" key={r}>{r}</span>
                ))}
              </div>
            </section>
          )}

          {ev.usage && (
            <section>
              <h4>Usage evidence</h4>
              <Row k="reads" v={num(ev.usage.read_count)} />
              <Row k="writes" v={num(ev.usage.write_count)} />
              <Row k="last access" v={when(ev.usage.last_access_at)} />
              {ev.usage.access_by_service && Object.keys(ev.usage.access_by_service).length > 0 && (
                <div className="kv">
                  <span className="k">by service</span>
                  <span className="v">
                    {Object.entries(ev.usage.access_by_service)
                      .map(([s, n]) => `${s} (${num(n)})`).join(', ')}
                  </span>
                </div>
              )}
              {ev.usage.query_templates?.length > 0 && (
                <div className="chips">
                  {ev.usage.query_templates.map((q) => <span className="chip" key={q}>{q}</span>)}
                </div>
              )}
            </section>
          )}

          {ev.retention && (
            <section>
              <h4>Retention evidence</h4>
              <Row k="overdue" v={ev.retention.is_overdue ? 'yes' : 'no'} />
              <Row k="days overdue" v={ev.retention.days_overdue != null ? num(ev.retention.days_overdue) : null} />
              <Row k="oldest row age" v={ev.retention.oldest_row_age_days != null ? `${num(ev.retention.oldest_row_age_days)} days` : null} />
              <Row k="policy" v={ev.retention.policy_applied
                ? `${ev.retention.policy_applied.days}d (${ev.retention.policy_applied.source}, anchor ${ev.retention.policy_applied.anchor})`
                : null} />
              <Row k="basis" v={ev.retention.basis} />
              {ev.retention.fraction_overdue != null &&
                <Row k="rows past policy" v={`${Math.round(ev.retention.fraction_overdue * 100)}%`} />}
            </section>
          )}

          {history.length > 1 && (
            <section>
              <h4>History ({history.length} runs)</h4>
              <table className="mini-history">
                <tbody>
                  {history.map((h) => (
                    <tr key={h.run_id} className={h.run_id === runId ? 'current' : ''}>
                      <td>{when(h.finished_at)}</td>
                      <td className="num">{h.score.toFixed(3)}</td>
                      <td>
                        <span className={`badge ${verdict(h.verdict).className}`}>
                          {verdict(h.verdict).label}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}
        </div>
      )}
    </aside>
  )
}
