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

  function select() {
    if (listing) value = listing.path
    browsing = false
  }
</script>

<div class="picker">
  <div class="row">
    <input type="text" class="mono grow" bind:value placeholder="/pfad/zum/album" />
    <button class="btn secondary" onclick={() => open(value || undefined)}>Durchsuchen</button>
  </div>

  {#if browsing && listing}
    <div class="listing card">
      <div class="row">
        <span class="mono grow">{listing.path}</span>
        <span class="muted">{listing.audio_files.length} Audio-Dateien</span>
        <button class="btn small" onclick={select}>Auswählen</button>
        <button class="btn small secondary" onclick={() => (browsing = false)}>Schließen</button>
      </div>
      <ul>
        {#if listing.parent}
          <li><button onclick={() => open(listing!.parent!)}>..</button></li>
        {/if}
        {#each listing.dirs as dir}
          <li><button onclick={() => open(dir.path)}>{dir.name}/</button></li>
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
  li button {
    background: none;
    border: none;
    color: var(--text);
    cursor: pointer;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.85rem;
    padding: 0.15rem 0.3rem;
    width: 100%;
    text-align: left;
    border-radius: 5px;
  }
  li button:hover {
    background: var(--bg-hover);
    color: var(--accent);
  }
</style>
