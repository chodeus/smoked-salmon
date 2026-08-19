<script lang="ts">
  import type { Job } from './jobs.svelte'

  let { job }: { job: Job } = $props()

  const chipClass: Record<string, string> = {
    queued: '',
    running: 'run',
    done: 'ok',
    error: 'err',
    cancelled: 'warn',
  }
</script>

<div class="row">
  <span class="chip {chipClass[job.status]}">{job.status}</span>
  {#if job.question}
    <span class="chip warn">waiting for answer</span>
  {/if}
  {#if job.status === 'running' && job.progress}
    <div class="progress grow">
      <div style="width: {job.progress.total ? (100 * job.progress.done) / job.progress.total : 0}%"></div>
    </div>
    <span class="muted">{job.progress.done}/{job.progress.total} — {job.progress.desc}</span>
  {/if}
  {#if job.error}
    <span class="muted">{job.error}</span>
  {/if}
</div>
