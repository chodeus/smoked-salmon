<script lang="ts">
  import { apiPost } from '../lib/api'
  import FolderPicker from '../lib/FolderPicker.svelte'
  import JobStatus from '../lib/JobStatus.svelte'
  import { jobStore, type Job } from '../lib/jobs.svelte'

  const ALL_CHECKS = [
    { key: 'log', label: 'Rip-Log (Score & Checksum)' },
    { key: 'integrity', label: 'File integrity' },
    { key: 'mqa', label: 'MQA detection' },
    { key: 'upconvert', label: 'Upconvert detection' },
  ]

  let path = $state('')
  let selected = $state<string[]>(['log', 'integrity', 'mqa', 'upconvert'])
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
<p class="lead">Run the quality checks on any folder without uploading it — rip-log score, file integrity, MQA markers and upconversion.</p>

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
      {#if job.result.log}
        <h3>Rip-Logs</h3>
        {#if job.result.log.logs.length === 0}
          <p class="muted">No .log files found.</p>
        {:else}
          <table>
            <tbody>
              {#each job.result.log.logs as log}
                <tr>
                  <td class="mono">{log.file}</td>
                  {#if log.error}
                    <td><span class="chip err">Error</span> <span class="muted">{log.error}</span></td>
                  {:else}
                    <td>
                      <span class="chip {log.score === 100 ? 'ok' : 'warn'}">Score {log.score}</span>
                      <span class="chip {log.checksum_integrity === 'Match' ? 'ok' : 'warn'}">
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

      {#if job.result.integrity}
        <h3>Integrity</h3>
        <p>
          <span class="chip {job.result.integrity.passed ? 'ok' : 'err'}">
            {job.result.integrity.passed ? 'passed' : 'failed'}
          </span>
        </p>
        {#if job.result.integrity.details}
          <pre class="mono muted">{job.result.integrity.details}</pre>
        {/if}
      {/if}

      {#if job.result.mqa}
        <h3>MQA</h3>
        <p>
          <span class="chip {job.result.mqa.detected ? 'err' : 'ok'}">
            {job.result.mqa.detected ? 'MQA detected!' : 'no MQA'}
          </span>
        </p>
        {#if job.result.mqa.detected}
          <ul class="mono">
            {#each job.result.mqa.files.filter((f: any) => f.detected) as f}
              <li>{f.file}</li>
            {/each}
          </ul>
        {/if}
      {/if}

      {#if job.result.upconvert}
        <h3>Upconvert</h3>
        {#if job.result.upconvert.files.length === 0}
          <p class="muted">No 24-bit FLACs found.</p>
        {:else}
          <table>
            <tbody>
              {#each job.result.upconvert.files as f}
                <tr>
                  <td class="mono">{f.file}</td>
                  {#if f.error}
                    <td><span class="chip warn">{f.error}</span></td>
                  {:else}
                    <td>
                      <span class="chip {f.is_upconverted ? 'err' : 'ok'}">
                        {f.is_upconverted ? 'Upconvert!' : 'ok'}
                      </span>
                      <span class="muted">{f.bitdepth}bit, wasted bits: {f.wasted_bits}</span>
                    </td>
                  {/if}
                </tr>
              {/each}
            </tbody>
          </table>
        {/if}
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
