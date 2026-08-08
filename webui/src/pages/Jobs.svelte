<script lang="ts">
  import { apiPost } from '../lib/api'
  import JobStatus from '../lib/JobStatus.svelte'
  import { jobStore } from '../lib/jobs.svelte'

  let cancelError = $state('')

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

{#if jobStore.loadError}
  <div class="card"><p class="muted">{jobStore.loadError}</p></div>
{/if}
{#if cancelError}
  <div class="card"><p class="muted">Abbrechen fehlgeschlagen: {cancelError}</p></div>
{/if}

{#if jobStore.jobs.length === 0 && !jobStore.loadError}
  <div class="card"><p class="muted">Noch keine Jobs in dieser Sitzung.</p></div>
{/if}

{#each jobStore.jobs as job (job.id)}
  <div class="card">
    <div class="row">
      <h2 class="grow" style="margin: 0">{job.title}</h2>
      <span class="muted mono">{job.id}</span>
      {#if job.status === 'running' || job.status === 'queued'}
        <button class="btn small secondary" onclick={() => cancel(job.id)}>Abbrechen</button>
      {/if}
    </div>
    <JobStatus {job} />
  </div>
{/each}
