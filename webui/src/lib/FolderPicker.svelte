<script lang="ts">
  import { apiGet } from './api'

  let { value = $bindable('') }: { value: string } = $props()

  interface BrowseResult {
    path: string
    parent: string | null
    dirs: { name: string; path: string }[]
    audio_files: string[]
  }

  let browsing = $state(false)
  let listing = $state<BrowseResult | null>(null)
  let error = $state('')

  async function open(path?: string) {
    browsing = true
    error = ''
    try {
      const query = path ? `?path=${encodeURIComponent(path)}` : ''
      listing = await apiGet<BrowseResult>(`/browse${query}`)
    } catch (e) {
      error = String(e)
    }
  }

  function select(path?: string) {
    value = path ?? listing?.path ?? value
    browsing = false
  }

  function shortName(p: string): string {
    return p.split('/').filter(Boolean).pop() ?? p
  }
</script>

<div class="picker">
  <div class="row">
    <input type="text" class="mono grow" bind:value placeholder="/path/to/album" />
    <button class="btn secondary" onclick={() => open(value || undefined)}>Browse</button>
  </div>

  {#if browsing && listing}
    <div class="listing card">
      <div class="row">
        <span class="mono grow">{listing.path}</span>
        <span class="muted">{listing.audio_files.length} audio files</span>
        <button class="btn small" onclick={() => select()}>Select "{shortName(listing.path)}"</button>
        <button class="btn small secondary" onclick={() => (browsing = false)}>Close</button>
      </div>
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
  {#if error}
    <p class="muted">{error}</p>
  {/if}
</div>

<style>
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
