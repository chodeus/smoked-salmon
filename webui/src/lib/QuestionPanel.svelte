<script lang="ts">
  import { apiPost } from './api'
  import type { Job } from './jobs.svelte'

  let { job }: { job: Job } = $props()

  let text = $state('')
  let editText = $state('')
  let error = $state('')
  let sending = $state(false)
  let lastQuestionId = ''

  $effect(() => {
    const q = job.question
    if (q && q.id !== lastQuestionId) {
      lastQuestionId = q.id
      text = q.default != null && typeof q.default !== 'boolean' ? String(q.default) : ''
      editText = q.initial ?? ''
      error = ''
    }
  })

  async function send(value: unknown) {
    if (!job.question || sending) return
    sending = true
    error = ''
    try {
      await apiPost(`/jobs/${job.id}/answer`, { question_id: job.question.id, value })
    } catch (e) {
      error = String(e)
    } finally {
      sending = false
    }
  }
</script>

{#if job.question}
  <div class="question card">
    <pre class="qtext">{job.question.text}</pre>

    {#if job.question.kind === 'confirm'}
      <div class="row">
        <button class="btn" disabled={sending} onclick={() => send(true)}>
          Yes{job.question.default === true ? ' (Default)' : ''}
        </button>
        <button class="btn secondary" disabled={sending} onclick={() => send(false)}>
          No{job.question.default === false ? ' (Default)' : ''}
        </button>
      </div>
    {:else if job.question.kind === 'edit'}
      <textarea bind:value={editText} rows={Math.min(20, Math.max(6, editText.split('\n').length + 1))}></textarea>
      <div class="row" style="margin-top: 0.5rem">
        <button class="btn" disabled={sending} onclick={() => send(editText)}>Save</button>
        <button class="btn secondary" disabled={sending} onclick={() => send(null)}>Leave unchanged</button>
      </div>
    {:else}
      {#if job.question.choices}
        <div class="row" style="flex-wrap: wrap; margin-bottom: 0.5rem">
          {#each job.question.choices as choice}
            <button class="btn secondary" disabled={sending} onclick={() => send(choice)}>{choice}</button>
          {/each}
        </div>
      {/if}
      <form
        class="row"
        onsubmit={(e) => {
          e.preventDefault()
          send(text)
        }}
      >
        <input type="text" class="grow mono" bind:value={text} placeholder="Answer …" />
        <button class="btn" disabled={sending}>Send</button>
      </form>
    {/if}

    {#if error}<p class="muted">{error}</p>{/if}
  </div>
{/if}

<style>
  .question {
    border-color: var(--accent-dim);
    background: #221a1c;
  }
  .qtext {
    white-space: pre-wrap;
    font-family: inherit;
    margin: 0 0 0.7rem;
  }
  textarea {
    width: 100%;
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.5rem 0.7rem;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.85rem;
  }
</style>
