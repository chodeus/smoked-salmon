<script lang="ts">
  import { apiPost } from '../lib/api'
  import FolderPicker from '../lib/FolderPicker.svelte'
  import JobStatus from '../lib/JobStatus.svelte'
  import VerdictRows from '../lib/VerdictRows.svelte'
  import { checksumChip, logScoreChip } from '../lib/verdicts'
  import { jobStore, type Job } from '../lib/jobs.svelte'

  const ALL_CHECKS = [
    { key: 'provenance', label: 'Provenance (encoder & source tags)' },
    { key: 'log', label: 'Rip-Log (Score & Checksum)' },
    { key: 'integrity', label: 'File integrity' },
    { key: 'mqa', label: 'MQA detection' },
    { key: 'upconvert', label: 'Upconvert detection' },
  ]

  let path = $state('')
  let selected = $state<string[]>(['provenance', 'log', 'integrity', 'mqa', 'upconvert'])
  let jobId = $state<string | null>(null)
  let error = $state('')

  const job = $derived(jobId ? jobStore.get(jobId) : undefined)

  function toggle(key: string) {
    selected = selected.includes(key) ? selected.filter((k) => k !== key) : [...selected, key]
  }

  async function run() {
    error = ''
    try {
      const created = await apiPost<Job>('/checks/run', { path, checks: selected })
      jobStore.add(created)
      jobId = created.id
    } catch (e) {
      error = String(e)
    }
  }
</script>

<h1>Checks</h1>
<p class="lead">Run the quality checks on any folder without uploading it — encoder and source tags, rip-log score, file integrity, MQA markers and upconversion.</p>

<div class="card">
  <FolderPicker bind:value={path} />
  <div class="row" style="margin-top: 0.7rem; flex-wrap: wrap">
    {#each ALL_CHECKS as c}
      <label class="row" style="gap: 0.3rem">
        <input type="checkbox" checked={selected.includes(c.key)} onchange={() => toggle(c.key)} />
        {c.label}
      </label>
    {/each}
  </div>
  <div style="margin-top: 0.7rem">
    <button class="btn" onclick={run} disabled={!path || selected.length === 0}>Run checks</button>
  </div>
  {#if error}<p class="muted">{error}</p>{/if}
</div>

{#if job}
  <div class="card">
    <h2>{job.title}</h2>
    <JobStatus {job} />

    {#if job.status === 'done' && job.result}
      <VerdictRows rows={job.result.rows} />

      {#if job.result.raw.provenance?.files?.length}
        <h3>Provenance</h3>
        {#each job.result.raw.provenance.contradictions as note}
          <p><span class="chip warn">{note}</span></p>
        {/each}
        <table>
          <tbody>
            {#each job.result.raw.provenance.files as f}
              <tr>
                <td class="mono">{f.file}</td>
                <td class="muted">{f.vendor ?? '–'}</td>
                <td class="muted">
                  {Object.entries(f.markers ?? {})
                    .map(([k, v]) => `${k}: ${v}`)
                    .join(' · ') || '–'}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}

      {#if job.result.raw.log}
        <h3>Rip-Logs</h3>
        {#if job.result.raw.log.logs.length === 0}
          <p class="muted">No .log files found.</p>
        {:else}
          <table>
            <tbody>
              {#each job.result.raw.log.logs as log}
                <tr>
                  <td class="mono">{log.file}</td>
                  {#if log.error}
                    <td><span class="chip err">Error</span> <span class="muted">{log.error}</span></td>
                  {:else}
                    <td>
                      <span class="chip {logScoreChip(log.score)}">Score {log.score}</span>
                      <span class="chip {checksumChip(log.checksum_integrity)}">
                        Checksum: {log.checksum_integrity}
                      </span>
                    </td>
                  {/if}
                </tr>
              {/each}
            </tbody>
          </table>
        {/if}
      {/if}

      {#if job.result.raw.integrity?.details}
        <h3>Integrity</h3>
        <pre class="mono muted">{job.result.raw.integrity.details}</pre>
      {/if}

      {#if job.result.raw.mqa?.detected}
        <h3>MQA</h3>
        <ul class="mono">
          {#each job.result.raw.mqa.files.filter((f: any) => f.detected) as f}
            <li>{f.file}</li>
          {/each}
        </ul>
      {/if}

      {#if job.result.raw.upconvert?.files.length}
        <h3>Upconvert</h3>
        <table>
          <tbody>
            {#each job.result.raw.upconvert.files as f}
              <tr>
                <td class="mono">{f.file}</td>
                {#if f.error}
                  <td><span class="chip warn">{f.error}</span></td>
                {:else if f.not_applicable}
                  <td><span class="chip">{f.not_applicable}</span></td>
                {:else}
                  <td class="muted">{f.bitdepth}bit, wasted bits: {f.wasted_bits}</td>
                {/if}
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    {/if}
  </div>
{/if}

<style>
  pre {
    white-space: pre-wrap;
    background: var(--bg);
    border-radius: 8px;
    padding: 0.7rem;
    font-size: 0.8rem;
  }
  h3 {
    margin-top: 1rem;
  }
</style>
