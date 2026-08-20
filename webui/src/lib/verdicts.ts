export type Verdict = 'ok' | 'warn' | 'block' | 'skip'

export interface Row {
  id: string
  label: string
  verdict: Verdict
  detail: string
}

export interface ChecksResult {
  rows: Row[]
  raw: Record<string, any>
  blocking: string[]
  warnings: string[]
}

export const CHIP: Record<Verdict, string> = { ok: 'ok', warn: 'warn', block: 'err', skip: '' }
export const MARK: Record<Verdict, string> = { ok: '✓', warn: '!', block: '✕', skip: '–' }

// Per-file chip colouring in detail tables. The upload gate's verdict is the
// backend's (checks/preflight.py); these only tint the per-file breakdown.
export const logScoreChip = (score: number): string => (score === 100 ? 'ok' : 'warn')
export const checksumChip = (integrity: string): string => (integrity === 'Match' ? 'ok' : 'warn')
