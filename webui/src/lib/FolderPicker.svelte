<script lang="ts">
  import { apiGet } from './api'

  let {
    value = $bindable(''),
    writable = false,
    readOnlySource = $bindable(false),
  }: {
    value: string
    /** Set on pickers whose operation writes to the folder; library roots are
     *  read-only sources, so offering them would only produce a 403. */
    writable?: boolean
    /** True while `value` names a read-only library source — bind it to keep the
     *  form's submit disabled, since a typed path never passes through select(). */
    readOnlySource?: boolean
  } = $props()

  interface BrowseResult {
    path: string
    parent: string | null
    dirs: { name: string; path: string }[]
    audio_files: string[]
    roots: { path: string; name: string; library: boolean }[]
  }

  let browsing = $state(false)
  let listing = $state<BrowseResult | null>(null)
  let error = $state('')
  let roots = $state<BrowseResult['roots']>([])
  let classified = $state(false)

  // Loaded once so a path typed straight into the field can be judged without
  // browsing. Until it succeeds nothing can be classified, and an unclassified
  // path must not be treated as writable.
  $effect(() => {
    if (classified) return
    apiGet<BrowseResult>('/browse')
      .then((r) => {
        roots = r.roots ?? []
        classified = true
      })
      .catch(() => {})
  })

  /** Lexical resolution of '.', '..' and separators — what the server's realpath
   *  would do for a path containing no symlinks. Symlinks stay the server's job. */
  function resolve(p: string): string {
    const parts = p.trim().replace(/\\/g, '/').split('/')
    const out: string[] = []
    for (const part of parts) {
      if (part === '' || part === '.') continue
      if (part === '..') out.pop()
      else out.push(part)
    }
    return (p.trim().startsWith('/') ? '/' : '') + out.join('/')
  }

  function within(root: string, path: string): boolean {
    const r = resolve(root)
    const c = resolve(path)
    return c === r || c.startsWith(r === '/' ? '/' : r + '/')
  }

  async function open(path?: string) {
    browsing = true
    error = ''
    try {
      const query = path ? `?path=${encodeURIComponent(path)}` : ''
      listing = await apiGet<BrowseResult>(`/browse${query}`)
      if (listing.roots?.length) roots = listing.roots
    } catch (e) {
      error = String(e)
    }
  }

  function libraryRootFor(path: string): string | null {
    for (const r of roots) if (r.library && within(r.path, path)) return r.path
    return null
  }

  const readOnlyRoot = $derived(writable && value && classified ? libraryRootFor(value) : null)
  const unclassified = $derived(writable && !!value && !classified)

  $effect(() => {
    readOnlySource = readOnlyRoot !== null || unclassified
  })

  function select(path?: string) {
    const chosen = path ?? listing?.path ?? value
    const root = writable && libraryRootFor(chosen)
    if (root) {
      error = `${root} is a read-only library source — this operation writes to the folder, so pick a staging directory instead.`
      return
    }
    value = chosen
    browsing = false
  }

  function shortName(p: string): string {
    return p.split('/').filter(Boolean).pop() ?? p
  }

  const navRoots = $derived(roots.filter((r) => !writable || !r.library))
</script>

<div class="picker">
  <div class="row">
    <input type="text" class="mono grow" bind:value placeholder="/path/to/album" />
    <button class="btn secondary" onclick={() => open(value || undefined)}>Browse</button>
  </div>
  {#if unclassified}
    <p class="pick-error">Checking the folder against your configured directories…</p>
  {:else if readOnlyRoot}
    <p class="pick-error">
      {readOnlyRoot} is a read-only library source — this operation writes to the folder, so pick a
      staging directory instead.
    </p>
  {/if}

  {#if browsing && listing}
    <div class="listing card">
      {#if error}<p class="pick-error">{error}</p>{/if}
      <div class="row">
        <span class="mono grow">{listing.path}</span>
        <span class="muted">{listing.audio_files.length} audio files</span>
        <button class="btn small" onclick={() => select()}>Select "{shortName(listing.path)}"</button>
        <button class="btn small secondary" onclick={() => (browsing = false)}>Close</button>
      </div>
      {#if navRoots.length > 1}
        <div class="roots">
          {#each navRoots as r}
            <button
              class="root"
              class:current={within(r.path, listing!.path)}
              onclick={() => open(r.path)}
            >
              {r.name}{r.library ? ' (library)' : ''}
            </button>
          {/each}
        </div>
      {/if}
      <ul>
        {#if listing.parent}
          <li><button class="nav" onclick={() => open(listing!.parent!)}>..</button></li>
        {/if}
        {#each listing.dirs as dir}
          <li>
            <button class="nav" onclick={() => open(dir.path)}>{dir.name}/</button>
            <button class="pick" title="Select this folder" onclick={() => select(dir.path)}>select</button>
          </li>
        {/each}
      </ul>
    </div>
  {/if}
  {#if error && !(browsing && listing)}
    <p class="muted">{error}</p>
  {/if}
</div>

<style>
  .pick-error {
    margin: 0 0 0.5rem;
    font-size: 0.82rem;
    color: var(--err);
  }
  .roots {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    margin: 0.5rem 0 0.2rem;
  }
  .root {
    font-size: 0.78rem;
    padding: 0.25rem 0.55rem;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text-dim);
    cursor: pointer;
  }
  .root.current {
    border-color: var(--accent);
    color: var(--text);
  }
  .listing {
    margin-top: 0.5rem;
    max-height: 300px;
    overflow-y: auto;
  }
  ul {
    list-style: none;
    padding: 0;
    margin: 0.5rem 0 0;
  }
  li {
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }
  li button.nav {
    background: none;
    border: none;
    color: var(--text);
    cursor: pointer;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.85rem;
    padding: 0.15rem 0.3rem;
    flex: 1;
    text-align: left;
    border-radius: 5px;
  }
  li button.nav:hover {
    background: var(--bg-hover);
    color: var(--accent);
  }
  li button.pick {
    background: var(--bg-hover);
    border: 1px solid var(--border);
    color: var(--text-dim);
    cursor: pointer;
    font-size: 0.7rem;
    padding: 0.1rem 0.5rem;
    border-radius: 5px;
    visibility: hidden;
  }
  li:hover button.pick {
    visibility: visible;
  }
  li button.pick:hover {
    color: var(--accent);
    border-color: var(--accent-dim);
  }
</style>
