import { useCallback, useEffect, useState } from 'react'
import { api } from './api'
import { runLabel } from './format'
import ReportTable from './components/ReportTable'
import RunSummaryBar from './components/RunSummaryBar'
import ColumnDetail from './components/ColumnDetail'
import CompareView from './components/CompareView'
import NewAuditForm from './components/NewAuditForm'
import './App.css'

export default function App() {
  const [runs, setRuns] = useState([])
  const [runId, setRunId] = useState(null)
  const [report, setReport] = useState(null)
  const [view, setView] = useState('report') // 'report' | 'compare'
  const [selectedCol, setSelectedCol] = useState(null)
  const [showNew, setShowNew] = useState(false)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  const loadRuns = useCallback(async () => {
    const list = await api.listRuns(100)
    setRuns(list)
    return list
  }, [])

  useEffect(() => {
    ;(async () => {
      try {
        const list = await loadRuns()
        if (list.length) setRunId(list[0].id)
        else setLoading(false)
      } catch (e) {
        setError(`Can't reach the API (${e.message}). Is uvicorn auditor.api.main:app running on :8000?`)
        setLoading(false)
      }
    })()
  }, [loadRuns])

  useEffect(() => {
    if (!runId) return
    setLoading(true)
    api.getRun(runId)
      .then((r) => { setReport(r); setError(null) })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [runId])

  function onAuditDone(newRun) {
    setShowNew(false)
    setSelectedCol(null)
    loadRuns().then(() => setRunId(newRun.id))
    setView('report')
  }

  return (
    <div className="app">
      <header>
        <div className="brand">
          <h1>Data Minimization Auditor</h1>
          <span className="tag">sensitive · unused · stale — all three</span>
        </div>
        <div className="header-controls">
          <nav className="tabs">
            <button className={view === 'report' ? 'on' : ''} onClick={() => setView('report')}>
              Report
            </button>
            <button className={view === 'compare' ? 'on' : ''} onClick={() => setView('compare')}>
              Compare runs
            </button>
          </nav>
          {runs.length > 0 && (
            <select className="run-select" value={runId || ''} onChange={(e) => setRunId(e.target.value)}>
              {runs.map((r) => <option key={r.id} value={r.id}>{runLabel(r)}</option>)}
            </select>
          )}
          <button className="primary" onClick={() => setShowNew(true)}>+ New audit</button>
        </div>
      </header>

      {error && <div className="banner error">{error}</div>}

      <main>
        {loading && !report && <p className="dim pad">Loading…</p>}

        {!loading && runs.length === 0 && !error && (
          <div className="empty-state">
            <p>No audit runs yet.</p>
            <button className="primary" onClick={() => setShowNew(true)}>Run the first audit</button>
          </div>
        )}

        {view === 'report' && report && (
          <div className={selectedCol ? 'split' : ''}>
            <div className="split-main">
              <RunSummaryBar run={report} />
              <ReportTable
                columns={report.columns}
                selected={selectedCol}
                onSelect={setSelectedCol}
              />
            </div>
            {selectedCol && (
              <ColumnDetail
                runId={runId}
                qname={selectedCol}
                onClose={() => setSelectedCol(null)}
              />
            )}
          </div>
        )}

        {view === 'compare' && (
          <CompareView
            runs={runs}
            onOpenColumn={(qn) => { setView('report'); setSelectedCol(qn) }}
          />
        )}
      </main>

      {showNew && <NewAuditForm onClose={() => setShowNew(false)} onDone={onAuditDone} />}
    </div>
  )
}
