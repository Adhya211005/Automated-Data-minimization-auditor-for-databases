// Thin wrapper over the Phase 6 API. All calls go through the Vite dev proxy
// (/api -> http://127.0.0.1:8000). Override with VITE_API_BASE for a build.
const BASE = import.meta.env.VITE_API_BASE || '/api'

async function req(path, opts) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  const text = await res.text()
  const body = text ? JSON.parse(text) : null
  if (!res.ok) {
    const detail = body?.detail || res.statusText
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return body
}

export const api = {
  health: () => req('/health'),
  listRuns: (limit = 50) => req(`/audits?limit=${limit}`),
  createAudit: (payload) => req('/audits', { method: 'POST', body: JSON.stringify(payload) }),
  getRun: (runId, flaggedOnly = false) =>
    req(`/audits/${runId}${flaggedOnly ? '?flagged_only=true' : ''}`),
  getColumn: (runId, qname) =>
    req(`/audits/${runId}/columns/${encodeURIComponent(qname)}`),
  compare: (base, head) =>
    req(`/audits/compare?base=${encodeURIComponent(base)}&head=${encodeURIComponent(head)}`),
  columnHistory: (qname) => req(`/columns/${encodeURIComponent(qname)}/history`),
}
