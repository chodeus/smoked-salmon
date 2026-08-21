<script lang="ts">
  import type { DupeDetail, DupeGroup, DupeTorrent } from './verdicts'

  let { detail }: { detail: DupeDetail } = $props()

  // A log score of 0 and a seeder count of 0 are real values, so no `||` fallbacks here.
  const or = (v: string | number | null | undefined, fallback = '–'): string =>
    v == null || v === '' ? fallback : String(v)

  function heading(g: DupeGroup): string {
    const artist = g.artist ? `${g.artist} – ` : ''
    const year = g.groupYear ? ` (${g.groupYear})` : ''
    return `${artist}${or(g.groupName, String(g.groupId))}${year}`
  }

  function edition(t: DupeTorrent): string {
    const parts = [t.remasterTitle, t.remasterYear, t.remasterRecordLabel].filter((p) => p != null && p !== '')
    return parts.length ? parts.join(' · ') : '–'
  }
</script>

<details class="matches">
  <summary>Show all {detail.matches.length} match{detail.matches.length === 1 ? '' : 'es'}</summary>
  <p class="muted mono searchstrs">searched: {detail.searchstrs.join(' / ')}</p>
  {#each detail.matches as g, i (g.groupId ?? `group-${i}`)}
    <div class="group">
      <a href={g.url} target="_blank" rel="noreferrer">{heading(g)}</a>
      {#if g.releaseType}<span class="chip">{g.releaseType}</span>{/if}
      {#if g.torrents.length}
        <table>
          <tbody>
            {#each g.torrents as t, j (t.torrentId ?? `torrent-${j}`)}
              <tr>
                <td class="mono">{or(t.format)} / {or(t.encoding)} / {or(t.media)}</td>
                <td class="muted">{edition(t)}</td>
                <td class="muted">
                  {#if t.hasLog}log {or(t.logScore)}{/if}
                  {#if t.seeders != null}<span class="seeders">{t.seeders} seeder{t.seeders === 1 ? '' : 's'}</span>{/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      {:else}
        <p class="muted">No torrents listed.</p>
      {/if}
    </div>
  {/each}
</details>

<style>
  .matches {
    margin-top: 0.3rem;
    font-size: 0.85rem;
  }
  summary {
    cursor: pointer;
    color: var(--text-dim);
  }
  .searchstrs {
    margin: 0.4rem 0 0.2rem;
    font-size: 0.75rem;
  }
  .group {
    margin: 0.5rem 0;
  }
  .group a {
    font-weight: 600;
    margin-right: 0.4rem;
  }
  .group table {
    margin-top: 0.2rem;
  }
  .seeders {
    margin-left: 0.5rem;
  }
</style>
