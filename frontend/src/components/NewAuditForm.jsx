import { useState } from 'react'
import { api } from '../api'

export default function NewAuditForm({ onClose, onDone }) {
  const [policyDays, setPolicyDays] = useState(365)
  const [windowDays, setWindowDays] = useState(90)
  const [mode, setMode] = useState('hybrid')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  async function submit(e) {
    e.preventDefault()
    setBusy(true); setErr(null)
    try {
      const run = await api.createAudit({
        policy_days: Number(policyDays),
        window_days: Number(windowDays),
        mode,
        note,
      })
      onDone(run)
    } catch (e) {
      setErr(e.message)
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <form className="modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <h3>Run a new audit</h3>
        <p className="dim">
          Re-extracts schema + sample values from the target DB (read-only),
          re-parses the query log, re-scores every column, and stores the run.
        </p>
        <label>
          Retention policy (days)
          <input type="number" min="1" value={policyDays}
            onChange={(e) => setPolicyDays(e.target.value)} />
        </label>
        <label>
          Usage window (days)
          <input type="number" min="1" value={windowDays}
            onChange={(e) => setWindowDays(e.target.value)} />
        </label>
        <label>
          Query attribution
          <select value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="hybrid">hybrid (SQL parse + declared columns)</option>
            <option value="sql">sql (parse only)</option>
            <option value="declared">declared (log fields only)</option>
          </select>
        </label>
        <label>
          Note (optional)
          <input value={note} placeholder="e.g. Q3 review"
            onChange={(e) => setNote(e.target.value)} />
        </label>
        {err && <p className="error">{err}</p>}
        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onClose}>Cancel</button>
          <button type="submit" disabled={busy}>{busy ? 'Running…' : 'Run audit'}</button>
        </div>
      </form>
    </div>
  )
}
