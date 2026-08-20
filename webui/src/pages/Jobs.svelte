<script lang="ts">
  import { apiPost } from '../lib/api'
  import JobActivity from '../lib/JobActivity.svelte'
  import JobStatus from '../lib/JobStatus.svelte'
  import QuestionPanel from '../lib/QuestionPanel.svelte'
  import { jobStore } from '../lib/jobs.svelte'

  let cancelError = $state('')
  let opened = $state<string[]>([])

  function toggle(id: string) {
    opened = opened.includes(id) ? opened.filter((o) => o !== id) : [...opened, id]
  }

  async function cancel(id: string) {
    cancelError = ''
    try {
      await apiPost(`/jobs/${id}/cancel`)
    } catch (e) {
      const msg = String(e)
      // A 409 just means the job finished in the meantime — the websocket
      // will deliver the final state; only surface unexpected errors.
      if (!msg.includes('already finished')) cancelError = msg
    }
  }
</script>

<h1>Jobs</h1>
<p class="lead">Everything running or finished in this session. Open a job to answer its prompts or read its log.</p>

{#if jobStore.loadError}
  <div class="card"><p class="muted">{jobStore.loadError}</p></div>
{/if}
{#if cancelError}
  <div class="card"><p class="muted">Cancel failed: {cancelError}</p></div>
{/if}

{#if jobStore.jobs.length === 0 && !jobStore.loadError}
  <div class="card"><p class="muted">No jobs in this session yet.</p></div>
{/if}

{#each jobStore.jobs as job (job.id)}
  <div class="card">
    <div class="row">
      <h2 class="grow" style="margin: 0">{job.title}</h2>
      <span class="muted mono">{job.id}</span>
      <button class="btn small secondary" onclick={() => toggle(job.id)}>
        {opened.includes(job.id) ? 'Hide log' : 'Open'}
      </button>
      {#if job.status === 'running' || job.status === 'queued'}
        <button class="btn small secondary" onclick={() => cancel(job.id)}>Cancel</button>
      {/if}
    </div>
    <JobStatus {job} />
    <QuestionPanel {job} />
    {#if job.question || opened.includes(job.id)}
      <!-- Context for answering, and the log for any job the user opens. -->
      <JobActivity {job} logTail={opened.includes(job.id) ? 200 : 15} />
    {/if}
  </div>
{/each}
