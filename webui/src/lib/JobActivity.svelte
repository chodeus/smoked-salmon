<script lang="ts">
  import type { Job } from './jobs.svelte'

  let { job, logTail = 0 }: { job: Job; logTail?: number } = $props()

  let logEl = $state<HTMLElement | null>(null)

  const lines = $derived(logTail > 0 ? job.log.slice(-logTail) : job.log)

  $effect(() => {
    job.log.length
    if (logEl) logEl.scrollTop = logEl.scrollHeight
  })
</script>

{#if job.spectrals?.length}
  <div class="gallery">
    {#each job.spectrals as file}
      <a href={`/api/jobs/${job.id}/spectral/${encodeURIComponent(file)}`} target="_blank" rel="noreferrer">
        <img src={`/api/jobs/${job.id}/spectral/${encodeURIComponent(file)}`} alt={file} loading="lazy" />
        <span class="muted mono">{file}</span>
      </a>
    {/each}
  </div>
{/if}

{#if lines.length}
  <pre class="log" bind:this={logEl}>{lines.join('\n')}</pre>
{/if}

<style>
  .log {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.7rem;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.78rem;
    white-space: pre-wrap;
    max-height: 420px;
    overflow-y: auto;
    margin-top: 0.8rem;
  }
  .gallery {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(280px, 100%), 1fr));
    gap: 0.7rem;
    margin-top: 0.8rem;
  }
  .gallery img {
    width: 100%;
    border: 1px solid var(--border);
    border-radius: 6px;
  }
  .gallery span {
    font-size: 0.72rem;
    display: block;
    text-align: center;
  }
</style>
