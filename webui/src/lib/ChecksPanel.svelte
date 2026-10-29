<script lang="ts">
  import { apiPost } from './api'
  import DupeMatches from './DupeMatches.svelte'
  import JobStatus from './JobStatus.svelte'
  import VerdictRows from './VerdictRows.svelte'
  import { FINISHED, jobStore, type Job } from './jobs.svelte'
  import { dupeDetail, type ChecksResult, type Row } from './verdicts'

  // Advice only: the upload runs its own checks and asks its own questions.
  let {
    path,
    trackers,
    source,
    sources,
    onUseSource,
  }: {
    path: string
    trackers: string[]
    source: string
    sources: string[]
    onUseSource: (source: string) => void
  } = $props()

  let jobId = $state<string | null>(null)
  let checkedFor = $state('')
  let error = $state('')
  let starting = $state(false)

  // One shape for the form's inputs and a job's params, so a job found again compares too.
  const signatureOf = (p: Record<string, unknown>) => JSON.stringify({ path: p.path, trackers: p.trackers })
  const signature = $derived(signatureOf({ path, trackers }))
  const job = $derived(jobId ? jobStore.get(jobId) : undefined)
  const running = $derived(!!job && !FINISHED.includes(job.status))
  const result = $derived(job?.status === 'done' ? (job.result as ChecksResult | null) : null)
  const stale = $derived(!!job && signature !== checkedFor)
  const detected = $derived(result?.detected_source ?? null)

  // Back on the page: the newest checks of this folder and these trackers, unless they failed or were cancelled.
  $effect(() => {
    if (jobId || !path) return
    const earlier = jobStore.jobs.find(
      (j) =>
        j.kind === 'checks' &&
        signatureOf(j.params) === signature &&
        j.status !== 'failed' &&
        j.status !== 'cancelled',
    )
    if (!earlier) return
    jobId = earlier.id
    checkedFor = signature
  })

  async function run() {
    error = ''
    starting = true
    const captured = signature
    try {
      const started = await apiPost<Job>('/jobs', { kind: 'checks', params: { path, report: false, trackers } })
      jobId = started.id
      checkedFor = captured
    } catch (e) {
      error = String(e)
    } finally {
      starting = false
    }
  }
</script>

<div class="panel">
  <div class="row">
    <strong class="grow">Checks</strong>
    {#if result && !stale}
      <span class="chip {result.blocking ? 'err' : result.warnings ? 'warn' : 'ok'}">
        {#if result.blocking}{result.blocking} blocking{:else if result.warnings}{result.warnings} warning(s){:else}Nothing
          blocking, no warning{/if}
      </span>
    {/if}
    <button class="btn small secondary" onclick={run} disabled={!path || running || starting}>
      {running ? 'Checking…' : job ? 'Check again' : 'Check the album'}
    </button>
  </div>
  {#if error}<p class="error">{error}</p>{/if}

  {#if job && !result}
    <JobStatus {job} />
  {:else if result}
    {#if stale}
      <p class="hint">The folder or the trackers changed since these checks: check again.</p>
    {/if}
    {#if detected && !source && sources.includes(detected)}
      <p class="detected">
        The files prove <strong>{detected}</strong>.
        <button class="btn small secondary" onclick={() => onUseSource(detected)}>Use {detected}</button>
      </p>
    {/if}
    {#snippet dupes(row: Row)}
      {@const detail = result ? dupeDetail(result, row) : null}
      {#if detail}<DupeMatches {detail} />{/if}
    {/snippet}
    <VerdictRows rows={result.rows} rowDetail={dupes} />
  {:else}
    <p class="hint">
      What <span class="mono">salmon check all</span> finds in the folder, and a dupe search on each ticked tracker,
      before you start. Nothing in the folder is changed. The upload runs its own checks either way.
    </p>
  {/if}
</div>

<style>
  .panel {
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 0.7rem;
    margin-top: 0.9rem;
  }
  .hint {
    color: var(--text-dim);
    font-size: 0.78rem;
    line-height: 1.35;
    margin: 0.4rem 0 0;
    max-width: 60ch;
  }
  .detected {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin: 0.5rem 0 0;
    font-size: 0.85rem;
  }
  .error {
    color: var(--err);
    margin: 0.5rem 0 0;
  }
</style>
