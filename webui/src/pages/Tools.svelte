<script lang="ts">
  import { apiGet, apiPost } from '../lib/api'
  import FolderPicker from '../lib/FolderPicker.svelte'
  import JobStatus from '../lib/JobStatus.svelte'
  import { jobStore, type Job } from '../lib/jobs.svelte'

  let options = $state<{
    trackers: string[]
    sources: string[]
    encodings: string[]
    image_hosts: string[]
    transcodes: string[]
  } | null>(null)

  let jobIds = $state<string[]>([])
  const jobs = $derived(jobIds.map((id) => jobStore.get(id)).filter(Boolean) as Job[])

  // descgen
  let descUrls = $state('')
  let description = $state('')
  let descBusy = $state(false)
  let descError = $state('')

  // images
  let imagePaths = $state('')
  let imageHost = $state('')
  let imageUrls = $state<string[]>([])
  let imageError = $state('')

  // tag
  let tagPath = $state('')
  let tagSource = $state('')
  let tagEncoding = $state('')
  let tagOverwrite = $state(false)
  let tagAutoRename = $state(true)
  let tagError = $state('')

  // cross-upload
  let xPath = $state('')
  let xSource = $state('')
  let xTarget = $state('')
  let xDownconvert = $state(false)
  let xAllFormats = $state(false)
  let xGroupId = $state('')
  let xTranscodes = $state<string[]>([])
  let xError = $state('')

  $effect(() => {
    apiGet<typeof options>('/tools/options')
      .then((o) => {
        options = o
        if (o && !imageHost && o.image_hosts.length) imageHost = o.image_hosts[0]
        if (o && !xSource && o.trackers.length) xSource = o.trackers[0]
        if (o && !xTarget && o.trackers.length > 1) xTarget = o.trackers[1]
      })
      .catch(() => {})
  })

  function track(job: Job) {
    jobStore.add(job)
    jobIds = [job.id, ...jobIds]
  }

  function lines(value: string): string[] {
    return value
      .split(/[\s,]+/)
      .map((v) => v.trim())
      .filter(Boolean)
  }

  async function generateDescription() {
    descError = ''
    description = ''
    descBusy = true
    try {
      const res = await apiPost<{ description: string }>('/descgen', { urls: lines(descUrls) })
      description = res.description
    } catch (e) {
      descError = String(e)
    } finally {
      descBusy = false
    }
  }

  async function uploadImages() {
    imageError = ''
    imageUrls = []
    try {
      const res = await apiPost<{ urls: string[] }>('/images/upload', {
        paths: lines(imagePaths),
        host: imageHost || null,
      })
      imageUrls = res.urls
    } catch (e) {
      imageError = String(e)
    }
  }

  async function startTag() {
    tagError = ''
    try {
      track(
        await apiPost<Job>('/tag', {
          path: tagPath,
          source: tagSource,
          encoding: tagEncoding || null,
          overwrite: tagOverwrite,
          auto_rename: tagAutoRename,
        }),
      )
    } catch (e) {
      tagError = String(e)
    }
  }

  async function startCrossUpload() {
    xError = ''
    const groupId = xGroupId.trim()
    if (groupId && !/^\d+$/.test(groupId)) {
      xError = 'Target group ID must be a number.'
      return
    }
    try {
      track(
        await apiPost<Job>('/cross-upload', {
          path: xPath,
          source: xSource,
          target: xTarget,
          downconvert: xDownconvert,
          all_formats: xAllFormats,
          target_group_id: groupId ? Number(groupId) : null,
          transcodes: xTranscodes,
        }),
      )
    } catch (e) {
      xError = String(e)
    }
  }
</script>

<h1>Tools</h1>

<div class="card">
  <h2>Description generator</h2>
  <p class="muted">Build a tracklist description from one or more metadata URLs.</p>
  <input type="text" class="mono" bind:value={descUrls} placeholder="https://… https://…" />
  <div class="row" style="margin-top: 0.6rem">
    <button class="btn" onclick={generateDescription} disabled={!descUrls.trim() || descBusy}>
      {descBusy ? 'Generating …' : 'Generate'}
    </button>
  </div>
  {#if descError}<p class="muted">{descError}</p>{/if}
  {#if description}<pre class="log mono">{description}</pre>{/if}
</div>

<div class="card">
  <h2>Image upload</h2>
  <p class="muted">Upload image files to a host. Paths must sit inside salmon's configured directories.</p>
  <input type="text" class="mono" bind:value={imagePaths} placeholder="/data/…/cover.jpg" />
  <div class="row" style="margin-top: 0.6rem">
    <select bind:value={imageHost} style="width: auto">
      {#each options?.image_hosts ?? [] as h}<option value={h}>{h}</option>{/each}
    </select>
    <button class="btn" onclick={uploadImages} disabled={!imagePaths.trim()}>Upload</button>
  </div>
  {#if imageError}<p class="muted">{imageError}</p>{/if}
  {#each imageUrls as url}<p class="mono muted">{url}</p>{/each}
</div>

<div class="card">
  <h2>Tag an album</h2>
  <p class="muted">Retag and rename without uploading. Prompts appear here as questions.</p>
  <FolderPicker bind:value={tagPath} />
  <div class="row" style="margin-top: 0.6rem; flex-wrap: wrap">
    <select bind:value={tagSource} style="width: auto">
      <option value="">— source —</option>
      {#each options?.sources ?? [] as s}<option value={s}>{s}</option>{/each}
    </select>
    <select bind:value={tagEncoding} style="width: auto">
      <option value="">— encoding —</option>
      {#each options?.encodings ?? [] as e}<option value={e}>{e}</option>{/each}
    </select>
    <label class="check"><input type="checkbox" bind:checked={tagAutoRename} /> Auto-Rename</label>
    <label class="check"><input type="checkbox" bind:checked={tagOverwrite} /> Overwrite metadata</label>
    <button class="btn" onclick={startTag} disabled={!tagPath || !tagSource}>Tag</button>
  </div>
  {#if tagError}<p class="muted">{tagError}</p>{/if}
</div>

<div class="card">
  <h2>Cross-upload</h2>
  <p class="muted">Copy an existing upload from one tracker to another.</p>
  <FolderPicker bind:value={xPath} />
  <div class="row" style="margin-top: 0.6rem; flex-wrap: wrap">
    <select bind:value={xSource} style="width: auto">
      {#each options?.trackers ?? [] as t}<option value={t}>{t}</option>{/each}
    </select>
    <span class="muted">→</span>
    <select bind:value={xTarget} style="width: auto">
      {#each options?.trackers ?? [] as t}<option value={t}>{t}</option>{/each}
    </select>
    <input type="text" bind:value={xGroupId} placeholder="target group ID (optional)" style="width: auto" />
    <label class="check"><input type="checkbox" bind:checked={xDownconvert} /> Also 16-bit</label>
    <label class="check"><input type="checkbox" bind:checked={xAllFormats} /> All formats</label>
    {#each options?.transcodes ?? [] as t}
      <label class="check">
        <input
          type="checkbox"
          checked={xTranscodes.includes(t)}
          onchange={(e) =>
            (xTranscodes = e.currentTarget.checked
              ? [...xTranscodes, t]
              : xTranscodes.filter((v) => v !== t))}
        /> MP3 {t}
      </label>
    {/each}
    <button class="btn" onclick={startCrossUpload} disabled={!xPath || !xSource || !xTarget}>
      Cross-upload
    </button>
  </div>
  {#if xError}<p class="muted">{xError}</p>{/if}
</div>

{#each jobs as job (job.id)}
  <div class="card">
    <h2>{job.title}</h2>
    <JobStatus {job} />
  </div>
{/each}
