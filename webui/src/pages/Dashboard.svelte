<script lang="ts">
  import { apiGet, apiPost } from '../lib/api'

  interface Health {
    version: string
    config_path: string
    binaries: {
      required: Record<string, string | null>
      optional: Record<string, string | null>
    }
    trackers: string[]
    default_tracker: string | null
    directories: Record<string, string | null>
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
        {#each Object.entries(health.directories) as [name, path]}
          <tr>
            <td>{name}</td>
            <td class="mono muted">{path ?? '–'}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{/if}
