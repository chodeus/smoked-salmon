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
  let chosen = $state<string[]>([])
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

  let showHelp = $state(false)
  let preflightCleared = $state(false)
  const HELP: Record<string, string> = {
    tracker: "Which site to upload to. While upload.multi_tracker_upload is on (the default), the remaining trackers are offered after the first upload completes.",
    source: "Media the files came from. Leave as ask to be prompted during the upload.",
    lossy: "Whether the master itself is lossy-sourced. Check automatically prints the frequency analysis next to the spectrals and asks you; yes and no skip the question.",
    groupId: "Add to an existing torrent group instead of creating a new one. Accepts an ID or a torrents.php?id= URL.",
    request: "Fill a request when the upload completes. Accepts a request URL or ID.",
    sourceUrl: "For WEB uploads: the store or streaming URL the files came from, added to the release description.",
    encoding: "Required when the files are lossy; ignored for lossless. Leave as ask to be prompted.",
    spectrals: "Track numbers whose spectrals go into the release description, e.g. 1 4 7. Leave blank to choose during the upload.",
    autoRename: "Rename files and folders to salmon's templates without asking to confirm each one.",
    spectralsAfter: "Generate, review and report spectrals after the torrent is uploaded instead of before it.",
    compress: "Re-encode FLACs to the configured compression level before uploading. Slower, smaller files, audio unchanged.",
    scene: "Mark as a scene release: the folder and file names are left untouched, tags are not standardised and the cover is not compressed. Cannot be combined with essential-files-only.",
    skipUp: "Skip the upconversion check, which looks for 16-bit audio padded out to 24-bit.",
    skipMqa: "Skip the MQA marker check. Pre-flight tests every file; the upload itself only tests the first.",
    skipLogCheck: "Skip scoring CD rip logs and verifying their checksums against the audio.",
    skipIntegrityCheck: "Skip verifying that every audio file decodes cleanly (flac -wt / mp3val).",
    essentialOnly: "Upload only audio, logs, cues and artwork; strip nfo, sfv, md5, txt and other extras. Cannot be combined with scene.",
    overwrite: "Ignore the artists, year, label, catalogue number and genres already in the file tags and take them from the scraped sources instead.",
    skipInitialReview: "Skip the manual metadata review that runs before the AI review. Only does anything when upload.ai_review.enabled is set.",
    applyAiSuggestions: "Runs the AI metadata review without asking, applies its edits, and skips your manual check of what it changed. Needs upload.ai_review.enabled and an API key in config; does nothing otherwise.",
    dryRun: "Run everything — checks, spectrals, torrent creation — but build it locally and send nothing to the tracker.",
  }

  let activeJobId = $state<string | null>(null)
  let error = $state('')
  let starting = $state(false)

  const activeJob = $derived(activeJobId ? jobStore.get(activeJobId) : undefined)
  // Verify exactly what the upload will check, so the two cannot disagree.
  const checks = $derived([
    'provenance',
    ...(skipIntegrityCheck ? [] : ['integrity']),
    ...(skipUp ? [] : ['upconvert']),
    ...(skipMqa ? [] : ['mqa']),
    ...(skipLogCheck ? [] : ['log']),
  ])
  // Dry runs post nothing, so they are exempt from the pre-flight gate.
  const gated = $derived(!dryRun && !preflightCleared)

  function toggleTracker(t: string) {
    chosen = chosen.includes(t) ? chosen.filter((c) => c !== t) : [...trackers.filter((x) => chosen.includes(x) || x === t)]
  }

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
        if (!chosen.length && o.trackers.length) chosen = [o.trackers[0]]
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
        tracker: chosen[0],
        trackers: chosen,
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

{#if !activeJob || (activeJob.status !== 'queued' && activeJob.status !== 'running')}
  <div class="card">
    <FolderPicker bind:value={path} />
    <div class="grid">
      <div class="field" title={HELP.tracker}>
        <span class="field-label">Trackers</span>
        <div class="row" style="flex-wrap: wrap; gap: 0.6rem">
          {#each trackers as t}
            <label class="check">
              <input type="checkbox" checked={chosen.includes(t)} onchange={() => toggleTracker(t)} />
              {t}
            </label>
          {/each}
        </div>
        {#if chosen.length > 1}
          <small class="hint">
            Verified against all {chosen.length}. The upload starts on {chosen[0]}; when it finishes you are asked
            whether to continue, and only {chosen.slice(1).join(' and ')} will be offered.
          </small>
        {:else if showHelp}
          <small class="hint">{HELP.tracker}</small>
        {/if}
      </div>
      <label title={HELP.source}>
        Source
        <select bind:value={source}>
          <option value="">— ask —</option>
          {#each sources as s}<option value={s}>{s}</option>{/each}
        </select>
      {#if showHelp}<small class="hint">{HELP.source}</small>{/if}
      </label>
      <label title={HELP.lossy}>
        Lossy Master
        <select bind:value={lossy}>
          <option value="auto">check automatically</option>
          <option value="yes">yes</option>
          <option value="no">no</option>
        </select>
      {#if showHelp}<small class="hint">{HELP.lossy}</small>{/if}
      </label>
      <label title={HELP.groupId}>
        Group-ID (optional)
        <input type="text" bind:value={groupId} placeholder="existing group" />
      {#if showHelp}<small class="hint">{HELP.groupId}</small>{/if}
      </label>
      <label title={HELP.request}>
        Request (optional)
        <input type="text" bind:value={request} placeholder="Request URL or ID" />
      {#if showHelp}<small class="hint">{HELP.request}</small>{/if}
      </label>
      <label title={HELP.sourceUrl}>
        Source-URL (optional, WEB)
        <input type="text" bind:value={sourceUrl} placeholder="https://…" />
      {#if showHelp}<small class="hint">{HELP.sourceUrl}</small>{/if}
      </label>
      <label title={HELP.encoding}>
        Encoding (lossy sources)
        <select bind:value={encoding}>
          <option value="">— ask —</option>
          {#each encodings as e}<option value={e}>{e}</option>{/each}
        </select>
      {#if showHelp}<small class="hint">{HELP.encoding}</small>{/if}
      </label>
      <label title={HELP.spectrals}>
        Spectral tracks (optional)
        <input type="text" bind:value={spectrals} placeholder="e.g. 1 4 7" />
      {#if showHelp}<small class="hint">{HELP.spectrals}</small>{/if}
      </label>
    </div>
    <div class="row" style="margin-top: 0.9rem">
      <label class="check">
        <input type="checkbox" bind:checked={showHelp} /> Explain these options
      </label>
    </div>
    <div class="row opts" style="flex-wrap: wrap; margin-top: 0.6rem">
      <div class="opt"><label class="check" title={HELP.autoRename}><input type="checkbox" bind:checked={autoRename} /> Auto-Rename</label>{#if showHelp}<small class="hint">{HELP.autoRename}</small>{/if}</div>
      <div class="opt"><label class="check" title={HELP.spectralsAfter}><input type="checkbox" bind:checked={spectralsAfter} /> Spectrals after upload</label>{#if showHelp}<small class="hint">{HELP.spectralsAfter}</small>{/if}</div>
      <div class="opt"><label class="check" title={HELP.compress}><input type="checkbox" bind:checked={compress} /> Recompress FLACs</label>{#if showHelp}<small class="hint">{HELP.compress}</small>{/if}</div>
      <div class="opt"><label class="check" title={HELP.scene}><input type="checkbox" bind:checked={scene} /> Scene-Release</label>{#if showHelp}<small class="hint">{HELP.scene}</small>{/if}</div>
      <div class="opt"><label class="check" title={HELP.skipUp}><input type="checkbox" bind:checked={skipUp} /> Skip upconvert check</label>{#if showHelp}<small class="hint">{HELP.skipUp}</small>{/if}</div>
      <div class="opt"><label class="check" title={HELP.skipMqa}><input type="checkbox" bind:checked={skipMqa} /> Skip MQA check</label>{#if showHelp}<small class="hint">{HELP.skipMqa}</small>{/if}</div>
      <div class="opt"><label class="check" title={HELP.skipLogCheck}><input type="checkbox" bind:checked={skipLogCheck} /> Skip log check</label>{#if showHelp}<small class="hint">{HELP.skipLogCheck}</small>{/if}</div>
      <div class="opt"><label class="check" title={HELP.skipIntegrityCheck}><input type="checkbox" bind:checked={skipIntegrityCheck} /> Skip integrity check</label>{#if showHelp}<small class="hint">{HELP.skipIntegrityCheck}</small>{/if}</div>
      <div class="opt"><label class="check" title={HELP.essentialOnly}><input type="checkbox" bind:checked={essentialOnly} /> Essential files only</label>{#if showHelp}<small class="hint">{HELP.essentialOnly}</small>{/if}</div>
      <div class="opt"><label class="check" title={HELP.overwrite}><input type="checkbox" bind:checked={overwrite} /> Overwrite metadata</label>{#if showHelp}<small class="hint">{HELP.overwrite}</small>{/if}</div>
      <div class="opt"><label class="check" title={HELP.skipInitialReview}><input type="checkbox" bind:checked={skipInitialReview} /> Skip initial review</label>{#if showHelp}<small class="hint">{HELP.skipInitialReview}</small>{/if}</div>
      <div class="opt"><label class="check" title={HELP.applyAiSuggestions}><input type="checkbox" bind:checked={applyAiSuggestions} /> Apply AI suggestions</label>{#if showHelp}<small class="hint">{HELP.applyAiSuggestions}</small>{/if}</div>
      <div class="opt"><label class="check" title={HELP.dryRun}><input type="checkbox" bind:checked={dryRun} /> Dry run (validate only)</label>{#if showHelp}<small class="hint">{HELP.dryRun}</small>{/if}</div>
    </div>
    <Preflight
      {path}
      {source}
      {checks}
      trackers={chosen}
      bind:cleared={preflightCleared}
      onUseSource={(s) => (source = s)}
    />
    <div style="margin-top: 0.8rem">
      <button class="btn" onclick={start} disabled={!path || !chosen.length || starting || gated}>
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
  .field {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    font-size: 0.85rem;
    color: var(--text-dim);
  }
  .field-label {
    color: var(--text-dim);
  }
  label.check {
    flex-direction: row;
    align-items: center;
    gap: 0.35rem;
    color: var(--text);
  }
  .hint {
    display: block;
    color: var(--text-dim);
    font-size: 0.78rem;
    line-height: 1.35;
    margin-top: 0.15rem;
    max-width: 46ch;
  }
  .opts {
    align-items: start;
  }
  .opts .opt {
    flex: 0 1 auto;
  }
  /* With help shown each option needs room for its paragraph. */
  .opts:has(.hint) {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(260px, 100%), 1fr));
    gap: 0.6rem 1.2rem;
  }
        </style>
