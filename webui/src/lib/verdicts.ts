/** A row of `salmon check all`, as the checks job returns it (checks/verdicts.py). */
export type Verdict = 'OK' | 'WARN' | 'BLOCK' | 'INFO'

export interface Row {
  verdict: Verdict
  check: string
  detail: string
  notes: string[]
}

export interface ChecksResult {
  folder: string
  // The trackers searched for a dupe; their rows are among the others.
  trackers: string[]
  rows: Row[]
  blocking: number
  warnings: number
  report: string | null
  // The source the files prove, else null; and every match of each tracker searched, by site code.
  detected_source: string | null
  dupes: Record<string, DupeDetail>
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
  remasterCatalogueNumber: string | null
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

export interface DupeDetail {
  searchstrs: string[]
  matches: DupeGroup[]
}

/** The matches behind a "Dupe (<tracker>)" row, when its search found any. */
export function dupeDetail(result: ChecksResult, row: Row): DupeDetail | null {
  const code = /^Dupe \((.+)\)$/.exec(row.check)?.[1]
  const detail = code ? result.dupes?.[code] : undefined
  return detail?.matches.length ? detail : null
}

// Ported from the fork's verdicts.ts (chodeus, 9bfdddc3), with check all's verdicts.
export const CHIP: Record<Verdict, string> = { OK: 'ok', WARN: 'warn', BLOCK: 'err', INFO: '' }
