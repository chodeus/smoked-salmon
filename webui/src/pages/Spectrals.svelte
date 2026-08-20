<script lang="ts">
  import { apiPost } from '../lib/api'
  import FolderPicker from '../lib/FolderPicker.svelte'
  import JobStatus from '../lib/JobStatus.svelte'
  import { jobStore, type Job } from '../lib/jobs.svelte'

  let path = $state('')
  let activeJobId = $state<string | null>(null)
  let uploadJobId = $state<string | null>(null)
  let error = $state('')
  let lightbox = $state<string | null>(null)

  const activeJob = $derived(activeJobId ? jobStore.get(activeJobId) : undefined)
  const uploadJob = $derived(uploadJobId ? jobStore.get(uploadJobId) : undefined)

  async function generate() {
    error = ''
    uploadJobId = null
    try {
      const job = await apiPost<Job>('/spectrals/generate', { path })
      jobStore.add(job)
      activeJobId = job.id
    } catch (e) {
      error = String(e)
    }
  }

  async function upload() {
    if (!activeJobId) return
    error = ''
    try {
      const job = await apiPost<Job>('/spectrals/upload', { job_id: activeJobId })
      jobStore.add(job)
      uploadJobId = job.id
    } catch (e) {
      error = String(e)
    }
  }

  function imageUrl(jobId: string, file: string): string {
    return `/api/spectrals/${jobId}/image/${encodeURIComponent(file)}`
  }
</script>

<h1>Spectrals</h1>
<p class="lead">Generate spectral images for an album and upload them, for lossy-master checks and reports.</p>

<div class="card">
  <FolderPicker bind:value={path} />
  <div style="margin-top: 0.7rem">
    <button class="btn" onclick={generate} disabled={!path || activeJob?.status === 'running'}>
      Spectrals generieren
    </button>
  </div>
  {#if error}<p class="muted">{error}</p>{/if}
</div>

{#if activeJob}
  <div class="card">
    <h2>{activeJob.title}</h2>
    <JobStatus job={activeJob} />

    {#if activeJob.status === 'done' && activeJob.result}
      <div class="row" style="margin: 0.8rem 0">
        <button class="btn secondary" onclick={upload} disabled={uploadJob?.status === 'running'}>
          Auf Image-Host hochladen
        </button>
      </div>
      {#if uploadJob}
        <JobStatus job={uploadJob} />
        {#if uploadJob.status === 'done' && uploadJob.result}
          <ul class="mono">
            {#each uploadJob.result.urls as url}
              <li><a href={url} target="_blank" rel="noreferrer">{url}</a></li>
            {/each}
          </ul>
        {/if}
      {/if}
      <div class="gallery">
        {#each activeJob.result.files as file}
          <figure>
            <button onclick={() => (lightbox = imageUrl(activeJob!.id, file))}>
              <img src={imageUrl(activeJob.id, file)} alt={file} loading="lazy" />
            </button>
            <figcaption class="muted mono">{file}</figcaption>
          </figure>
        {/each}
      </div>
    {/if}
  </div>
{/if}

{#if lightbox}
  <button class="lightbox" onclick={() => (lightbox = null)}>
    <img src={lightbox} alt="Spectral" />
  </button>
{/if}

<style>
  .gallery {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(300px, 100%), 1fr));
    gap: 0.8rem;
    margin-top: 1rem;
  }
  figure {
    margin: 0;
  }
  figure button {
    background: none;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0;
    cursor: zoom-in;
    width: 100%;
    overflow: hidden;
  }
  figure img {
    width: 100%;
    display: block;
  }
  figcaption {
    font-size: 0.75rem;
    margin-top: 0.2rem;
    text-align: center;
  }
  .lightbox {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.85);
    border: none;
    cursor: zoom-out;
    z-index: 50;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 2rem;
  }
  .lightbox img {
    max-width: 100%;
    max-height: 100%;
  }
</style>
