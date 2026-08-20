<script lang="ts">
  import { apiPost } from '../lib/api'
  import FolderPicker from '../lib/FolderPicker.svelte'
  import JobStatus from '../lib/JobStatus.svelte'
  import { jobStore, type Job } from '../lib/jobs.svelte'

  let path = $state('')
  let bitrate = $state<'V0' | '320'>('V0')
  let jobIds = $state<string[]>([])
  let error = $state('')

  const jobs = $derived(jobIds.map((id) => jobStore.get(id)).filter(Boolean) as Job[])

  async function transcode() {
    error = ''
    try {
      const job = await apiPost<Job>('/convert/transcode', { path, bitrate })
      jobStore.add(job)
      jobIds = [job.id, ...jobIds]
    } catch (e) {
      error = String(e)
    }
  }

  async function compress() {
    error = ''
    try {
      const job = await apiPost<Job>('/convert/compress', { path })
      jobStore.add(job)
      jobIds = [job.id, ...jobIds]
    } catch (e) {
      error = String(e)
    }
  }

  async function downconvert() {
    error = ''
    try {
      const job = await apiPost<Job>('/convert/downconvert', { path })
      jobStore.add(job)
      jobIds = [job.id, ...jobIds]
    } catch (e) {
      error = String(e)
    }
  }
</script>

<h1>Convert</h1>

<div class="card">
  <FolderPicker bind:value={path} />
  <div class="row" style="margin-top: 0.7rem">
    <select bind:value={bitrate} style="width: auto">
      <option value="V0">MP3 V0</option>
      <option value="320">MP3 320</option>
    </select>
    <button class="btn" onclick={transcode} disabled={!path}>Transcode</button>
    <button class="btn secondary" onclick={downconvert} disabled={!path}>24bit → 16bit</button>
    <button class="btn secondary" onclick={compress} disabled={!path}>Recompress FLACs</button>
  </div>
  {#if error}<p class="muted">{error}</p>{/if}
</div>

{#each jobs as job (job.id)}
  <div class="card">
    <h2>{job.title}</h2>
    <JobStatus {job} />
    {#if job.status === 'done' && job.result?.output_path}
      <p class="mono muted">→ {job.result.output_path}</p>
    {:else if job.status === 'done' && job.result?.recompressed !== undefined}
      <p class="mono muted">Recompressed {job.result.recompressed} FLACs in place.</p>
    {/if}
  </div>
{/each}
