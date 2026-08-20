<script lang="ts">
  import { apiGet } from '../lib/api'

  interface Release {
    id: string
    artist: string
    album: string
    year: number | string | null
    track_count: number | null
    source: string
    url: string
  }

  interface SearchResponse {
    query: string
    sources: Record<string, { active: boolean; releases: Release[] }>
  }

  let query = $state('')
  let searching = $state(false)
  let results = $state<SearchResponse | null>(null)
  let error = $state('')
  let metaUrl = $state('')
  let metaData = $state<any>(null)
  let metaLoading = $state(false)

  async function search() {
    if (!query.trim()) return
    searching = true
    error = ''
    results = null
    try {
      results = await apiGet<SearchResponse>(`/search?q=${encodeURIComponent(query)}&limit=8`)
    } catch (e) {
      error = String(e)
    } finally {
      searching = false
    }
  }

  async function loadMetadata(url: string) {
    metaUrl = url
    metaLoading = true
    metaData = null
    try {
      const res = await apiGet<{ metadata: any }>(`/metadata?url=${encodeURIComponent(url)}`)
      metaData = res.metadata
    } catch (e) {
      metaData = { error: String(e) }
    } finally {
      metaLoading = false
    }
  }

  function trackList(meta: any): { disc: string; num: string; title: string; artists: string }[] {
    if (!meta?.tracks) return []
    const out: any[] = []
    for (const [disc, tracks] of Object.entries<any>(meta.tracks)) {
      for (const [num, t] of Object.entries<any>(tracks)) {
        out.push({
          disc,
          num,
          title: t.title,
          artists: (t.artists ?? []).map((a: any) => a[0]).join(', '),
        })
      }
    }
    return out
  }
</script>

<h1>Metadata search</h1>

<div class="card">
  <form
    class="row"
    onsubmit={(e) => {
      e.preventDefault()
      search()
    }}
  >
    <input type="text" class="grow" bind:value={query} placeholder="Artist Album …" />
    <button class="btn" disabled={searching}>{searching ? 'Searching …' : 'Search'}</button>
  </form>
</div>

{#if error}
  <div class="card"><p class="muted">{error}</p></div>
{/if}

{#if results}
  {@const anyHits = Object.values(results.sources).some((s) => s.releases.length > 0)}
  {#if !anyHits}
    <div class="card"><p class="muted">No matches for "{results.query}".</p></div>
  {/if}
  {#each Object.entries(results.sources) as [source, data]}
    {#if data.releases.length > 0}
      <div class="card">
        <h2>{source}</h2>
        <table>
          <tbody>
            {#each data.releases as rls}
              <tr>
                <td>{rls.artist} – <strong>{rls.album}</strong></td>
                <td class="muted">{rls.year ?? ''}</td>
                <td class="muted">{rls.track_count ? `${rls.track_count} Tracks` : ''}</td>
                <td>
                  <a href={rls.url} target="_blank" rel="noreferrer">Link</a>
                  <button class="btn small secondary" onclick={() => loadMetadata(rls.url)}>Metadata</button>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else if !data.active}
      <div class="card"><h2>{source}</h2><p class="muted">Inactive — API token required in config.</p></div>
    {/if}
  {/each}
{/if}

{#if metaLoading}
  <div class="card"><p class="muted">Loading metadata from {metaUrl} …</p></div>
{:else if metaData}
  <div class="card">
    <h2>Release metadata</h2>
    {#if metaData.error}
      <p class="muted">{metaData.error}</p>
    {:else}
      <p>
        <strong>{(metaData.artists ?? []).map((a: any) => a[0]).join(', ')}</strong> – {metaData.title}
        <span class="muted">({metaData.year ?? '?'}{metaData.label ? `, ${metaData.label}` : ''}{metaData.catno ? `, ${metaData.catno}` : ''})</span>
      </p>
      {#if metaData.genres?.length}
        <p>
          {#each metaData.genres as g}
            <span class="chip" style="margin-right: 0.3rem">{g}</span>
          {/each}
        </p>
      {/if}
      <table>
        <tbody>
          {#each trackList(metaData) as t}
            <tr>
              <td class="muted">{t.disc}-{t.num}</td>
              <td>{t.title}</td>
              <td class="muted">{t.artists}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>
{/if}
