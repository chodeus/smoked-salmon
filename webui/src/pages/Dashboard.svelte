<script lang="ts">
  import { apiGet, apiPost } from '../lib/api'
  import { jobStore } from '../lib/jobs.svelte'

  interface Health {
    version: string
    config_path: string
    binaries: {
      required: Record<string, string | null>
      optional: Record<string, string | null>
    }
    trackers: string[]
    default_tracker: string | null
    directories: Record<string, DirInfo>
  }

  interface DirInfo {
    path: string | null
    exists: boolean
    free_bytes: number | null
    total_bytes: number | null
  }

  interface TrackerCheck {
    tracker: string
    session_ok: boolean
    session_error: string | null
    api_key_configured: boolean
    api_key_ok: boolean | null
    api_key_error: string | null
  }

  let health = $state<Health | null>(null)
  let error = $state('')
  let checking = $state(false)
  let checks = $state<TrackerCheck[] | null>(null)
  let checkError = $state('')

  let cachedAge = $state<number | null>(null)

  const trackersOk = $derived(checks ? checks.filter((c) => c.session_ok && c.api_key_ok !== false).length : null)
  const binariesMissing = $derived(
    health ? Object.values(health.binaries.required).filter((v) => !v).length : null,
  )
  const jobsRunning = $derived(jobStore.jobs.filter((j) => j.status === 'running' || j.status === 'queued').length)

  function formatBytes(n: number): string {
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
    let i = 0
    while (n >= 1024 && i < units.length - 1) {
      n /= 1024
      i++
    }
    return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${units[i]}`
  }

  function usedPercent(d: DirInfo): number | null {
    if (!d.total_bytes || d.free_bytes === null) return null
    return Math.round(100 * (1 - d.free_bytes / d.total_bytes))
  }

  async function checkconf(force = false) {
    checking = true
    checkError = ''
    try {
      const res = await apiPost<{ trackers: TrackerCheck[]; cached: boolean; age_seconds: number }>(
        `/checkconf${force ? '?force=true' : ''}`,
      )
      checks = res.trackers
      cachedAge = res.cached ? res.age_seconds : 0
    } catch (e) {
      checkError = String(e)
    } finally {
      checking = false
    }
  }

  // Verified on load, like health - the result is cached server-side so
  // reloading the dashboard does not keep hitting the trackers.
  $effect(() => {
    checkconf()
  })

  $effect(() => {
    apiGet<Health>('/health')
      .then((h) => (health = h))
      .catch((e) => (error = String(e)))
  })
</script>

<h1>Dashboard</h1>
<p class="lead">Version, tracker connections and the command-line tools salmon depends on. Checked automatically when the page loads.</p>

<div class="tiles">
  <div class="tile {trackersOk === null ? '' : trackersOk === checks?.length ? 'ok' : 'err'}">
    <span class="figure">{trackersOk ?? '–'}<span class="of">/{checks?.length ?? '–'}</span></span>
    <span class="muted">trackers connected</span>
  </div>
  <div class="tile {binariesMissing === null ? '' : binariesMissing === 0 ? 'ok' : 'err'}">
    <span class="figure">{binariesMissing === null ? '–' : binariesMissing === 0 ? 'all' : binariesMissing}</span>
    <span class="muted">{binariesMissing === 0 ? 'required tools present' : 'required tools missing'}</span>
  </div>
  <div class="tile {jobsRunning > 0 ? 'run' : ''}">
    <span class="figure">{jobsRunning}</span>
    <span class="muted">jobs in flight</span>
  </div>
</div>

<div class="card">
  <div class="row">
    <h2 class="grow" style="margin: 0">Tracker connections</h2>
    {#if cachedAge !== null && cachedAge > 0}
      <span class="muted" style="margin-right: 0.6rem">checked {cachedAge}s ago</span>
    {/if}
    <button class="btn small" onclick={() => checkconf(true)} disabled={checking}>
      {checking ? 'Checking …' : 'Re-check'}
    </button>
  </div>
  {#if checkError}<p class="muted">{checkError}</p>{/if}
  {#if checking && !checks}<p class="muted">Testing tracker connections …</p>{/if}
  {#if checks}
    <table>
      <tbody>
        {#each checks as c}
          <tr>
            <td class="mono">{c.tracker}</td>
            <td>
              {#if c.session_ok}<span class="chip ok">session ok</span>
              {:else}<span class="chip err">session failed</span>{/if}
            </td>
            <td>
              {#if !c.api_key_configured}<span class="chip">no API key</span>
              {:else if c.api_key_ok}<span class="chip ok">API key ok</span>
              {:else}<span class="chip err">API key failed</span>{/if}
            </td>
            <td class="muted">{c.session_error ?? c.api_key_error ?? ''}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>

{#if error}
  <div class="card"><p class="muted">Backend unreachable: {error}</p></div>
{:else if !health}
  <p class="muted">Loading …</p>
{:else}
  <div class="card">
    <h2>smoked-salmon {health.version}</h2>
    <p class="muted mono">{health.config_path}</p>
    <p>
      Tracker:
      {#each health.trackers as t}
        <span class="chip ok" style="margin-right: 0.4rem">{t}{t === health.default_tracker ? ' (default)' : ''}</span>
      {:else}
        <span class="chip err">none configured</span>
      {/each}
    </p>
  </div>

  <div class="card">
    <h2>Binaries</h2>
    <table>
      <tbody>
        {#each Object.entries(health.binaries.required) as [name, path]}
          <tr>
            <td class="mono">{name}</td>
            <td>{#if path}<span class="chip ok">ok</span>{:else}<span class="chip err">missing</span>{/if}</td>
            <td class="mono muted">{path ?? ''}</td>
          </tr>
        {/each}
        {#each Object.entries(health.binaries.optional) as [name, path]}
          <tr>
            <td class="mono">{name} <span class="muted">(optional)</span></td>
            <td>{#if path}<span class="chip ok">ok</span>{:else}<span class="chip">missing</span>{/if}</td>
            <td class="mono muted">{path ?? ''}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h2>Directories</h2>
    <table>
      <tbody>
        {#each Object.entries(health.directories) as [name, dir]}
          <tr>
            <td>{name}</td>
            <td class="mono muted">{dir.path ?? '–'}</td>
            <td>
              {#if !dir.path}
                <span class="chip">unset</span>
              {:else if !dir.exists}
                <span class="chip err">missing</span>
              {:else if usedPercent(dir) !== null}
                <div class="disk" title="{formatBytes(dir.free_bytes ?? 0)} free of {formatBytes(dir.total_bytes ?? 0)}">
                  <div class="disk-bar"><div class="disk-fill {(usedPercent(dir) ?? 0) >= 90 ? 'err' : (usedPercent(dir) ?? 0) >= 75 ? 'warn' : ''}" style="width: {usedPercent(dir)}%"></div></div>
                  <span class="muted">{formatBytes(dir.free_bytes ?? 0)} free</span>
                </div>
              {:else}
                <span class="chip ok">ok</span>
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{/if}

<style>
  .tiles {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(160px, 100%), 1fr));
    gap: 0.7rem;
    margin-bottom: 0.9rem;
  }
  .tile {
    background: var(--bg-raised);
    border: 1px solid var(--border);
    border-left: 3px solid var(--border);
    border-radius: 10px;
    padding: 0.7rem 0.8rem;
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
  }
  .tile.ok {
    border-left-color: var(--ok);
  }
  .tile.err {
    border-left-color: var(--err);
  }
  .tile.run {
    border-left-color: var(--accent);
  }
  .figure {
    font-size: 1.5rem;
    font-weight: 700;
    line-height: 1.1;
  }
  .figure .of {
    font-size: 0.9rem;
    font-weight: 500;
    color: var(--text-dim);
  }
  .disk {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    min-width: 160px;
  }
  .disk-bar {
    flex: 1;
    height: 6px;
    border-radius: 3px;
    background: var(--border);
    overflow: hidden;
  }
  .disk-fill {
    height: 100%;
    background: var(--accent);
  }
  .disk-fill.warn {
    background: var(--warn);
  }
  .disk-fill.err {
    background: var(--err);
  }
</style>
