<script lang="ts">
  import { apiGet, apiPost } from '../lib/api'
  import FolderPicker from '../lib/FolderPicker.svelte'
  import JobActivity from '../lib/JobActivity.svelte'
  import JobStatus from '../lib/JobStatus.svelte'
  import Preflight from '../lib/Preflight.svelte'
  import QuestionPanel from '../lib/QuestionPanel.svelte'
  import { jobStore, type Job } from '../lib/jobs.svelte'

  let trackers = $state<string[]>([])
  let sources = $state<string[]>([])
  let encodings = $state<string[]>([])

  let path = $state('')
  let tracker = $state('')
  let source = $state('')
  let groupId = $state('')
  let request = $state('')
  let sourceUrl = $state('')
  let lossy = $state<'auto' | 'yes' | 'no'>('auto')
  let spectralsAfter = $state(false)
  let autoRename = $state(true)
  let compress = $state(false)
  let scene = $state(false)
  let skipUp = $state(false)
  let skipMqa = $state(false)
  let skipLogCheck = $state(false)
  let skipIntegrityCheck = $state(false)
  let essentialOnly = $state(false)
  let dryRun = $state(false)
  let overwrite = $state(false)
  let encoding = $state('')
  let spectrals = $state('')
  let skipInitialReview = $state(false)
  let applyAiSuggestions = $state(false)

  let preflightCleared = $state(false)
  let activeJobId = $state<string | null>(null)
  let error = $state('')
  let starting = $state(false)

  const activeJob = $derived(activeJobId ? jobStore.get(activeJobId) : undefined)
  const skips = $derived({
    skip_log_check: skipLogCheck,
    skip_integrity_check: skipIntegrityCheck,
    skip_mqa: skipMqa,
    skip_up: skipUp,
  })
  // Dry runs post nothing, so they are exempt from the pre-flight gate.
  const gated = $derived(!dryRun && !preflightCleared)

  function parseGroupId(value: string): number | null | undefined {
    const trimmed = value.trim()
    if (!trimmed) return null
    const fromUrl = trimmed.match(/torrents\.php\?id=(\d+)/)
    const digits = fromUrl ? fromUrl[1] : trimmed
    if (!/^\d+$/.test(digits)) return undefined
    return Number(digits)
  }

  $effect(() => {
    apiGet<{ trackers: string[]; sources: string[]; encodings: string[] }>('/upload/options')
      .then((o) => {
        trackers = o.trackers
        sources = o.sources
        encodings = o.encodings ?? []
        if (!tracker && o.trackers.length) tracker = o.trackers[0]
      })
      .catch((e) => {
        error = `Failed to load tracker options: ${e}`
      })
  })

  async function start() {
    if (starting) return
    error = ''
    if (gated) {
      error = 'Verify the album before uploading.'
      return
    }
    const parsedGroupId = parseGroupId(groupId)
    if (parsedGroupId === undefined) {
      error = 'Invalid group ID — provide a number or a torrents.php permalink.'
      return
    }
    const spectralTracks = spectrals
      .split(/[,\s]+/)
      .filter(Boolean)
      .map(Number)
    if (spectralTracks.some((n) => !Number.isInteger(n) || n < 1)) {
      error = 'Spectral track numbers must be positive whole numbers.'
      return
    }
    if (essentialOnly && scene) {
      error = 'Essential-only and scene cannot be combined.'
      return
    }
    starting = true
    try {
      const job = await apiPost<Job>('/upload', {
        path,
        tracker,
        source: source || null,
        group_id: parsedGroupId,
        request: request || null,
        source_url: sourceUrl || null,
        lossy: lossy === 'auto' ? null : lossy === 'yes',
        spectrals_after: spectralsAfter,
        auto_rename: autoRename,
        compress,
        scene,
        skip_up: skipUp,
        skip_mqa: skipMqa,
        skip_log_check: skipLogCheck,
        skip_integrity_check: skipIntegrityCheck,
        essential_only: essentialOnly,
        dry_run: dryRun,
        overwrite,
        encoding: encoding || null,
        spectrals: spectralTracks,
        skip_initial_review: skipInitialReview,
        apply_ai_suggestions: applyAiSuggestions,
      })
      jobStore.add(job)
      activeJobId = job.id
    } catch (e) {
      error = String(e)
    } finally {
      starting = false
    }
  }

  async function cancel() {
    if (activeJobId) await apiPost(`/jobs/${activeJobId}/cancel`).catch(() => {})
  }
</script>

<h1>Upload</h1>
<p class="lead">Verify an album, then upload it to a tracker. Anything the CLI would ask you is asked here instead, as the job runs.</p>

{#if !activeJob || activeJob.status !== 'running'}
  <div class="card">
    <FolderPicker bind:value={path} />
    <div class="grid">
      <label title="Which site to upload to. While upload.multi_tracker_upload is on (the default), the remaining trackers are offered after the first upload completes.">
        Tracker
        <select bind:value={tracker}>
          {#each trackers as t}<option value={t}>{t}</option>{/each}
        </select>
      </label>
      <label title="Media the files came from. Leave as ask to be prompted during the upload.">
        Source
        <select bind:value={source}>
          <option value="">— ask —</option>
          {#each sources as s}<option value={s}>{s}</option>{/each}
        </select>
      </label>
      <label title="Whether the master itself is lossy-sourced. Check automatically inspects the spectrals and asks if unsure.">
        Lossy Master
        <select bind:value={lossy}>
          <option value="auto">check automatically</option>
          <option value="yes">yes</option>
          <option value="no">no</option>
        </select>
      </label>
      <label title="Add to an existing torrent group instead of creating a new one. Accepts an ID or a torrents.php?id= URL.">
        Group-ID (optional)
        <input type="text" bind:value={groupId} placeholder="existing group" />
      </label>
      <label title="Fill a request when the upload completes. Accepts a request URL or ID.">
        Request (optional)
        <input type="text" bind:value={request} placeholder="Request URL or ID" />
      </label>
      <label title="For WEB uploads: the store or streaming URL the files came from, added to the release description.">
        Source-URL (optional, WEB)
        <input type="text" bind:value={sourceUrl} placeholder="https://…" />
      </label>
      <label title="Required when the files are lossy; ignored for lossless. Leave as ask to be prompted.">
        Encoding (lossy sources)
        <select bind:value={encoding}>
          <option value="">— ask —</option>
          {#each encodings as e}<option value={e}>{e}</option>{/each}
        </select>
      </label>
      <label title="Track numbers whose spectrals go into the release description, e.g. 1 4 7. Leave blank to choose during the upload.">
        Spectral tracks (optional)
        <input type="text" bind:value={spectrals} placeholder="e.g. 1 4 7" />
      </label>
    </div>
    <div class="row" style="flex-wrap: wrap; margin-top: 0.6rem">
      <label class="check" title="Rename files and folders to salmon's templates without asking to confirm each one."><input type="checkbox" bind:checked={autoRename} /> Auto-Rename</label>
      <label class="check" title="Generate, review and report spectrals after the torrent is uploaded instead of before it."><input type="checkbox" bind:checked={spectralsAfter} /> Spectrals after upload</label>
      <label class="check" title="Re-encode FLACs to the configured compression level before uploading. Slower, smaller files, audio unchanged."><input type="checkbox" bind:checked={compress} /> Recompress FLACs</label>
      <label class="check" title="Mark as a scene release: the folder and file names are left untouched, tags are not standardised and the cover is not compressed. Cannot be combined with essential-files-only."><input type="checkbox" bind:checked={scene} /> Scene-Release</label>
      <label class="check" title="Skip the upconversion check, which looks for 16-bit audio padded out to 24-bit."><input type="checkbox" bind:checked={skipUp} /> Skip upconvert check</label>
      <label class="check" title="Skip the MQA marker check. Only the first file is tested anyway."><input type="checkbox" bind:checked={skipMqa} /> Skip MQA check</label>
      <label class="check" title="Skip scoring CD rip logs and verifying their checksums against the audio."><input type="checkbox" bind:checked={skipLogCheck} /> Skip log check</label>
      <label class="check" title="Skip verifying that every audio file decodes cleanly (flac -wt / mp3val)."><input type="checkbox" bind:checked={skipIntegrityCheck} /> Skip integrity check</label>
      <label class="check" title="Upload only audio, logs, cues and artwork; strip nfo, sfv, md5, txt and other extras. Cannot be combined with scene."><input type="checkbox" bind:checked={essentialOnly} /> Essential files only</label>
      <label class="check" title="Ignore the artists, year, label, catalogue number and genres already in the file tags and take them from the scraped sources instead."><input type="checkbox" bind:checked={overwrite} /> Overwrite metadata</label>
      <label class="check" title="Skip the manual metadata review that runs before the AI review. Only does anything when upload.ai_review.enabled is set."><input type="checkbox" bind:checked={skipInitialReview} /> Skip initial review</label>
      <label class="check" title="Runs the AI metadata review without asking, applies its edits, and skips your manual check of what it changed. Needs upload.ai_review.enabled and an API key in config; does nothing otherwise."><input type="checkbox" bind:checked={applyAiSuggestions} /> Apply AI suggestions</label>
      <label class="check" title="Run everything — checks, spectrals, torrent creation — but do not post it. RED validates server-side; OPS builds locally only."><input type="checkbox" bind:checked={dryRun} /> Dry run (validate only)</label>
    </div>
    <Preflight
      {path}
      {source}
      {skips}
      trackers={tracker ? [tracker] : []}
      bind:cleared={preflightCleared}
      onUseSource={(s) => (source = s)}
    />
    <div style="margin-top: 0.8rem">
      <button class="btn" onclick={start} disabled={!path || !tracker || starting || gated}>
        {dryRun ? 'Start dry run' : 'Start upload'}
      </button>
      {#if gated}
        <span class="muted" style="margin-left: 0.6rem">Verify the album above before uploading.</span>
      {/if}
    </div>
    {#if error}<p class="muted">{error}</p>{/if}
  </div>
{/if}

{#if activeJob}
  <div class="card">
    <div class="row">
      <h2 class="grow" style="margin: 0">{activeJob.title}</h2>
      {#if activeJob.status === 'running'}
        <button class="btn small secondary" onclick={cancel}>Cancel</button>
      {/if}
    </div>
    <JobStatus job={activeJob} />

    <QuestionPanel job={activeJob} />
    <JobActivity job={activeJob} />
  </div>
{/if}

<style>
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(220px, 100%), 1fr));
    gap: 0.6rem 1rem;
    margin-top: 0.7rem;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    font-size: 0.85rem;
    color: var(--text-dim);
  }
  label.check {
    flex-direction: row;
    align-items: center;
    gap: 0.35rem;
    color: var(--text);
  }
        </style>
