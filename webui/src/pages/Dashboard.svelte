<script lang="ts">
  import { apiGet } from '../lib/api'

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

  let health = $state<Health | null>(null)
  let error = $state('')

  $effect(() => {
    apiGet<Health>('/health')
      .then((h) => (health = h))
      .catch((e) => (error = String(e)))
  })
</script>

<h1>Dashboard</h1>

{#if error}
  <div class="card"><p class="muted">Backend nicht erreichbar: {error}</p></div>
{:else if !health}
  <p class="muted">Lade …</p>
{:else}
  <div class="card">
    <h2>smoked-salmon {health.version}</h2>
    <p class="muted mono">{health.config_path}</p>
    <p>
      Tracker:
      {#each health.trackers as t}
        <span class="chip ok" style="margin-right: 0.4rem">{t}{t === health.default_tracker ? ' (default)' : ''}</span>
      {:else}
        <span class="chip err">keine konfiguriert</span>
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
            <td>{#if path}<span class="chip ok">ok</span>{:else}<span class="chip err">fehlt</span>{/if}</td>
            <td class="mono muted">{path ?? ''}</td>
          </tr>
        {/each}
        {#each Object.entries(health.binaries.optional) as [name, path]}
          <tr>
            <td class="mono">{name} <span class="muted">(optional)</span></td>
            <td>{#if path}<span class="chip ok">ok</span>{:else}<span class="chip">fehlt</span>{/if}</td>
            <td class="mono muted">{path ?? ''}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h2>Verzeichnisse</h2>
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
