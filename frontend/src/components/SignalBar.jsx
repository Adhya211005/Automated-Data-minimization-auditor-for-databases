import { pct } from '../format'

// One 0-1 signal with its threshold marker. `pass` colors the fill.
// `direction` = 'ge' (sensitive/stale: high is bad) or 'le' (unused: low is bad)
export default function SignalBar({ label, score, threshold, pass, direction = 'ge', hint }) {
  return (
    <div className="signal">
      <div className="signal-head">
        <span className="signal-label">{label}</span>
        <span className={`signal-score ${pass ? 'is-hit' : ''}`}>{score?.toFixed(2)}</span>
      </div>
      <div className="signal-track">
        <div
          className={`signal-fill ${pass ? 'hit' : 'miss'}`}
          style={{ width: pct(score) }}
        />
        {threshold != null && (
          <div
            className="signal-threshold"
            style={{ left: pct(threshold) }}
            title={`threshold ${threshold} (${direction === 'le' ? '≤' : '≥'})`}
          />
        )}
      </div>
      <div className="signal-hint">
        {hint}
        {threshold != null && (
          <span className="signal-thresh-text">
            {' '}· {direction === 'le' ? 'unused if ≤' : 'if ≥'} {threshold}
          </span>
        )}
      </div>
    </div>
  )
}
