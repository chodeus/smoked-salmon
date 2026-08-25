<script lang="ts">
  import { apiPost } from './api'
  import DupeMatches from './DupeMatches.svelte'
  import VerdictRows from './VerdictRows.svelte'
  import { jobStore, type Job } from './jobs.svelte'
  import type { ChecksResult, DupeDetail, Row } from './verdicts'

  let {
    path,
    source,
    trackers,
    checks,
    cleared = $bindable(false),
    onUseSource,
  }: {
    path: string
    source: string
    trackers: string[]
    checks: string[]
    cleared?: boolean
    onUseSource?: (s: string) => void
  } = $props()

  let jobId = $state<string | null>(null)
  let error = $state('')
  let verifiedFor = $state('')
  let acked = $state<string[]>([])

  const job = $derived(jobId ? jobStore.get(jobId) : undefined)
  const result = $derived<ChecksResult | undefined>(job?.status === 'done' ? job.result : undefined)
  const running = $derived(job?.status === 'queued' || job?.status === 'running')
  // Any change to the inputs invalidates the verdict, so a stale green cannot gate an upload.
  const signature = $derived(JSON.stringify({ path, source, trackers, checks }))
  const stale = $derived(!!result && signature !== verifiedFor)
  const unacked = $derived(result ? result.warnings.filter((w) => !acked.includes(w)) : [])
  const ok = $derived(!!result && !stale && result.blocking.length === 0 && unacked.length === 0)
  const detected = $derived(result?.raw?.source?.source ?? null)
  const md5Unset = $derived<string[]>(result?.raw?.integrity?.md5_unset ?? [])

  $effect(() => {
    cleared = ok
  })

  async function verify() {
    error = ''
    acked = []
    const captured = signature
    try {
      const created = await apiPost<Job>('/checks/run', {
        path,
        checks,
        source: source || null,
        trackers,
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

  // The dupe row names two matches; raw['dupe:<tracker>'] carries the rest.
  function dupeDetail(row: Row): DupeDetail | null {
    const detail = result?.raw?.[row.id] as DupeDetail | undefined
    return detail?.matches?.length ? detail : null
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
    {#if detected && !source && onUseSource}
      <p class="muted detected">
        Detected <strong>{detected}</strong>
        <button class="btn small secondary" onclick={() => onUseSource(detected)}>Use {detected}</button>
      </p>
    {/if}
    {#snippet rowDetail(row: Row)}
      {@const detail = dupeDetail(row)}
      {#if detail}<DupeMatches {detail} />{/if}
      {#if row.id === 'integrity' && md5Unset.length}
        <details class="files">
          <summary>Show the {md5Unset.length} file(s)</summary>
          <ul class="mono">
            {#each md5Unset as name}
              <li>{name}</li>
            {/each}
          </ul>
        </details>
      {/if}
    {/snippet}
    <VerdictRows rows={result.rows} {acked} onToggleAck={toggleAck} {rowDetail} />
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
  .detected {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin: 0.5rem 0 0;
  }
</style>
