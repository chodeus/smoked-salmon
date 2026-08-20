<script lang="ts">
  import { apiPost } from './api'
  import { jobStore, type Job } from './jobs.svelte'

  interface Row {
    id: string
    label: string
    verdict: 'ok' | 'warn' | 'block' | 'skip'
    detail: string
  }
  interface Result {
    rows: Row[]
    blocking: string[]
    warnings: string[]
    raw: { source?: { source: string | null; confidence: string } }
  }

  let {
    path,
    source,
    trackers,
    skips,
    cleared = $bindable(false),
    onUseSource,
  }: {
    path: string
    source: string
    trackers: string[]
    skips: Record<string, boolean>
    cleared?: boolean
    onUseSource?: (s: string) => void
  } = $props()

  let jobId = $state<string | null>(null)
  let error = $state('')
  let verifiedFor = $state('')
  let acked = $state<string[]>([])

  const job = $derived(jobId ? jobStore.get(jobId) : undefined)
  const result = $derived<Result | undefined>(job?.status === 'done' ? job.result : undefined)
  const running = $derived(job?.status === 'queued' || job?.status === 'running')
  // Any change to the inputs invalidates the verdict, so a stale green cannot gate an upload.
  const signature = $derived(JSON.stringify({ path, source, trackers, skips }))
  const stale = $derived(!!result && signature !== verifiedFor)
  const unacked = $derived(result ? result.warnings.filter((w) => !acked.includes(w)) : [])
  const ok = $derived(!!result && !stale && result.blocking.length === 0 && unacked.length === 0)
  const detected = $derived(result?.raw?.source?.source ?? null)

  const CHIP: Record<Row['verdict'], string> = { ok: 'ok', warn: 'warn', block: 'err', skip: '' }
  const MARK: Record<Row['verdict'], string> = { ok: '✓', warn: '!', block: '✕', skip: '–' }

  $effect(() => {
    cleared = ok
  })

  async function verify() {
    error = ''
    acked = []
    const captured = signature
    try {
      const created = await apiPost<Job>('/checks/preflight', {
        path,
        source: source || null,
        trackers,
        ...skips,
      })
      jobStore.add(created)
      jobId = created.id
      verifiedFor = captured
    } catch (e) {
      error = String(e)
    }
  }

  function toggleAck(id: string) {
    acked = acked.includes(id) ? acked.filter((a) => a !== id) : [...acked, id]
  }
</script>

<div class="preflight">
  <div class="row">
    <strong class="grow">Pre-flight</strong>
    {#if result && !stale}
      <span class="chip {ok ? 'ok' : result.blocking.length ? 'err' : 'warn'}">
        {#if ok}Ready to upload{:else if result.blocking.length}Blocked{:else}{unacked.length} to acknowledge{/if}
      </span>
    {/if}
    <button class="btn small" onclick={verify} disabled={!path || running}>
      {running ? 'Verifying…' : result ? 'Verify again' : 'Verify album'}
    </button>
  </div>

  {#if error}<p class="muted">{error}</p>{/if}

  {#if running}
    <p class="muted">Decoding every file and searching the trackers — this can take a few minutes.</p>
  {:else if job?.status === 'error'}
    <p class="muted">Verification failed: {job.error}</p>
  {:else if result && stale}
    <p class="muted">Settings changed since this album was verified. Verify again before uploading.</p>
  {:else if result}
    <ul class="rows">
      {#each result.rows as r}
        <li class="verdict-{r.verdict}">
          <span class="mark chip {CHIP[r.verdict]}">{MARK[r.verdict]}</span>
          <span class="label">{r.label}</span>
          <span class="detail">{r.detail}</span>
          {#if r.id === 'source' && detected && !source && onUseSource}
            <button class="btn small secondary" onclick={() => onUseSource(detected)}>Use {detected}</button>
          {/if}
          {#if r.verdict === 'warn'}
            <label class="ack">
              <input type="checkbox" checked={acked.includes(r.id)} onchange={() => toggleAck(r.id)} />
              Acknowledge
            </label>
          {/if}
        </li>
      {/each}
    </ul>
  {:else}
    <p class="muted">Verify the album to check its source, integrity, rip log and duplicates before uploading.</p>
  {/if}
</div>

<style>
  .preflight {
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 0.7rem;
    margin-top: 0.8rem;
  }
  .rows {
    list-style: none;
    margin: 0.6rem 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }
  .rows li {
    display: grid;
    grid-template-columns: auto 8.5rem 1fr auto;
    align-items: baseline;
    gap: 0.5rem;
    font-size: 0.85rem;
  }
  .mark {
    justify-self: start;
    min-width: 1.4rem;
    text-align: center;
  }
  .label {
    font-weight: 600;
  }
  .detail {
    color: var(--text-dim);
  }
  .verdict-skip .label,
  .verdict-skip .detail {
    opacity: 0.55;
  }
  .ack {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    white-space: nowrap;
    color: var(--text-dim);
  }
  @media (max-width: 640px) {
    .rows li {
      grid-template-columns: auto 1fr;
    }
    .detail,
    .ack {
      grid-column: 2;
    }
  }
</style>
