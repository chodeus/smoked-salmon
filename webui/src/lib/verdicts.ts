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

export interface DupeTorrent {
  torrentId: number | null
  format: string | null
  encoding: string | null
  media: string | null
  hasLog: boolean | null
  logScore: number | null
  remasterTitle: string | null
  remasterYear: number | null
  remasterRecordLabel: string | null
  seeders: number | null
}

export interface DupeGroup {
  groupId: number | null
  groupName: string | null
  artist: string | null
  groupYear: number | null
  releaseType: string | null
  url: string
  torrents: DupeTorrent[]
}

// raw['dupe:<tracker>'] — every match behind the row, which only names the first two.
export interface DupeDetail {
  searchstrs: string[]
  matches: DupeGroup[]
}

export const CHIP: Record<Verdict, string> = { ok: 'ok', warn: 'warn', block: 'err', skip: '' }
export const MARK: Record<Verdict, string> = { ok: '✓', warn: '!', block: '✕', skip: '–' }

// Per-file chip colouring in detail tables. The upload gate's verdict is the
// backend's (checks/preflight.py); these only tint the per-file breakdown.
export const logScoreChip = (score: number): string => (score === 100 ? 'ok' : 'warn')
export const checksumChip = (integrity: string): string => (integrity === 'Match' ? 'ok' : 'warn')
