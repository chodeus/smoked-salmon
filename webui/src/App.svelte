<script lang="ts">
  import Dashboard from './pages/Dashboard.svelte'
  import Upload from './pages/Upload.svelte'
  import Search from './pages/Search.svelte'
  import Spectrals from './pages/Spectrals.svelte'
  import Convert from './pages/Convert.svelte'
  import Checks from './pages/Checks.svelte'
  import Tools from './pages/Tools.svelte'
  import Jobs from './pages/Jobs.svelte'
  import Login from './pages/Login.svelte'
  import { jobStore } from './lib/jobs.svelte'
  import { checkAuth, onUnauthorized, type AuthState } from './lib/api'

  const routes: Record<string, { component: any; label: string }> = {
    '': { component: Dashboard, label: 'Dashboard' },
    upload: { component: Upload, label: 'Upload' },
    search: { component: Search, label: 'Search' },
    spectrals: { component: Spectrals, label: 'Spectrals' },
    convert: { component: Convert, label: 'Convert' },
    checks: { component: Checks, label: 'Checks' },
    tools: { component: Tools, label: 'Tools' },
    jobs: { component: Jobs, label: 'Jobs' },
  }

  let current = $state(location.hash.replace(/^#\/?/, ''))
  window.addEventListener('hashchange', () => {
    current = location.hash.replace(/^#\/?/, '')
  })

  const route = $derived(routes[current] ?? routes[''])
  const runningCount = $derived(jobStore.jobs.filter((j) => j.status === 'running').length)

  let authState = $state<AuthState | null>(null)
  const authed = $derived(!authState?.required || authState.authenticated)

  async function refreshAuth() {
    authState = await checkAuth()
  }

  onUnauthorized(() => {
    if (authState?.required) authState = { required: true, authenticated: false }
  })

  $effect(() => {
    refreshAuth()
  })

  $effect(() => {
    if (authed) jobStore.init()
  })
</script>

{#if authState === null}
  <div class="booting">Loading…</div>
{:else if !authed}
  <Login onLoggedIn={refreshAuth} />
{:else}
<div class="layout">
  <nav>
    <div class="brand">
      <span class="fish">🐟</span> salmon<span class="accent">web</span>
    </div>
    {#each Object.entries(routes) as [path, r]}
      <a href="#/{path}" class:active={route === r}>
        {r.label}
        {#if path === 'jobs' && runningCount > 0}
          <span class="chip run">{runningCount}</span>
        {/if}
      </a>
    {/each}
    <div class="spacer"></div>
    <span class="chip {jobStore.connected ? 'ok' : 'err'}">
      {jobStore.connected ? 'verbunden' : 'getrennt'}
    </span>
  </nav>

  <main>
    <route.component />
  </main>
</div>

{/if}

<style>
  .booting {
    padding: 2rem;
    color: var(--text-dim);
  }
  .layout {
    display: flex;
    min-height: 100vh;
  }
  nav {
    width: 200px;
    flex-shrink: 0;
    background: var(--bg-raised);
    border-right: 1px solid var(--border);
    padding: 1rem 0.8rem;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .brand {
    font-weight: 700;
    font-size: 1.1rem;
    margin-bottom: 1rem;
    padding: 0 0.5rem;
  }
  .brand .accent {
    color: var(--accent);
  }
  nav a {
    color: var(--text-dim);
    padding: 0.45rem 0.6rem;
    border-radius: 8px;
    font-weight: 500;
  }
  nav a:hover {
    background: var(--bg-hover);
    text-decoration: none;
  }
  nav a.active {
    background: var(--bg-hover);
    color: var(--text);
  }
  .spacer {
    flex: 1;
  }
  main {
    flex: 1;
    padding: 1.5rem 2rem;
    max-width: 1100px;
  }
  @media (max-width: 700px) {
    .layout {
      flex-direction: column;
    }
    nav {
      width: auto;
      flex-direction: row;
      flex-wrap: wrap;
      align-items: center;
      border-right: none;
      border-bottom: 1px solid var(--border);
    }
    .brand {
      margin-bottom: 0;
    }
    main {
      padding: 1rem;
    }
  }
</style>
