<script lang="ts">
  import { apiDelete, apiPost } from '../lib/api'
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
  // The images outlive the page: leaving and coming back should find them again
  // rather than silently stranding a folder in tmp_dir. Newest job first.
  const restorable = $derived(
    jobStore.jobs.find((j) => j.type === 'spectrals' && j.status === 'done' && !j.result?.discarded),
  )
  const assessment = $derived(activeJob?.result?.assessment as { level: string; notes: string[] } | undefined)
  const ASSESSMENT_CHIP: Record<string, string> = { ok: 'ok', look: 'warn', suspect: 'err' }

  $effect(() => {
    if (!activeJobId && restorable) activeJobId = restorable.id
  })

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

  async function discard() {
    if (!activeJobId) return
    error = ''
    try {
      await apiDelete(`/spectrals/${activeJobId}`)
      activeJobId = null
      uploadJobId = null
    } catch (e) {
      error = String(e)
    }
  }

  async function copyReport() {
    const report = activeJob?.result?.report
    if (report) await navigator.clipboard.writeText(report)
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
      Generate spectrals
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
          Upload to image host
        </button>
        <button class="btn small secondary" onclick={discard}>Delete these spectrals</button>
      </div>

      {#if assessment}
        <div class="assessment">
          <span class="chip {ASSESSMENT_CHIP[assessment.level] ?? ''}">{assessment.level}</span>
          <ul>
            {#each assessment.notes as note}<li class="muted">{note}</li>{/each}
          </ul>
        </div>
      {/if}

      {#if activeJob.result.report}
        <details class="report">
          <summary>Report</summary>
          <pre class="mono">{activeJob.result.report}</pre>
          <button class="btn small secondary" onclick={copyReport}>Copy report</button>
        </details>
      {/if}
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
        {#each [...activeJob.result.files, ...activeJob.result.frequency.filter((f: { image: string }) => f.image).map((f: { image: string }) => f.image)] as file}
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
  .assessment {
    margin: 0.6rem 0;
    display: flex;
    gap: 0.6rem;
    align-items: baseline;
  }
  .assessment ul {
    margin: 0;
    padding-left: 1rem;
    font-size: 0.85rem;
  }
  .report {
    margin: 0.6rem 0;
  }
  .report summary {
    cursor: pointer;
    color: var(--text-dim);
  }
  .report pre {
    white-space: pre-wrap;
    background: var(--bg);
    border-radius: 8px;
    padding: 0.7rem;
    font-size: 0.78rem;
  }
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
