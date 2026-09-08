import { useState } from 'react'

const STRATEGY_LABEL = {
  archive_then_drop: 'Archive → drop column',
  anonymize_in_place: 'Anonymize in place',
}

export default function SuggestedFix({ remediation }) {
  const [copied, setCopied] = useState(false)

  async function copy() {
    try {
      await navigator.clipboard.writeText(remediation.sql)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      setCopied(false)
    }
  }

  return (
    <section className="fix">
      <div className="fix-head">
        <h4>Suggested fix</h4>
        <span className="fix-strategy">
          {STRATEGY_LABEL[remediation.strategy] || remediation.strategy}
          {remediation.reversible ? ' · reversible' : ' · not reversible'}
        </span>
      </div>
      <p className="fix-summary">{remediation.summary}</p>

      {remediation.cautions?.length > 0 && (
        <ul className="fix-cautions">
          {remediation.cautions.map((c, i) => <li key={i}>{c}</li>)}
        </ul>
      )}

      <div className="fix-sql-wrap">
        <button className="copy-btn" onClick={copy}>
          {copied ? 'Copied' : 'Copy SQL'}
        </button>
        <pre className="fix-sql">{remediation.sql}</pre>
      </div>
      <p className="fix-note">
        Draft only — nothing has run. Review, then execute manually
        ({remediation.dialect}).
      </p>
    </section>
  )
}
