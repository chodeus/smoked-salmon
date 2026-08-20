<script lang="ts">
  import { CHIP, MARK, type Row } from './verdicts'

  let {
    rows,
    acked = [],
    onToggleAck,
  }: {
    rows: Row[]
    acked?: string[]
    onToggleAck?: (id: string) => void
  } = $props()
</script>

<ul class="rows">
  {#each rows as r}
    <li class="verdict-{r.verdict}">
      <span class="mark chip {CHIP[r.verdict]}">{MARK[r.verdict]}</span>
      <span class="label">{r.label}</span>
      <span class="detail">{r.detail}</span>
      {#if r.verdict === 'warn' && onToggleAck}
        <label class="ack">
          <input type="checkbox" checked={acked.includes(r.id)} onchange={() => onToggleAck(r.id)} />
          Acknowledge
        </label>
      {/if}
    </li>
  {/each}
</ul>

<style>
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
