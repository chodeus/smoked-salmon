import { apiGet, confirmLoggedIn, wsUrl } from './api'

export interface JobProgress {
  done: number
  total: number
  desc: string
}

export interface Question {
  id: string
  kind: 'confirm' | 'prompt' | 'edit'
  text: string
  default: unknown
  choices: string[] | null
  initial: string | null
}

export type JobStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled' | 'unknown_outcome'

export interface Job {
  id: string
  type: string
  title: string
  params: Record<string, unknown>
  status: JobStatus
  created_at: string
  finished_at: string | null
  progress: JobProgress | null
  result: any
  error: string | null
  question: Question | null
  log: string[]
  spectrals: string[] | null
}

const LOG_CAP = 1000

class JobStore {
  jobs = $state<Job[]>([])
  connected = $state(false)
  loadError = $state('')
  private ws: WebSocket | null = null
  private started = false
  private retry: ReturnType<typeof setTimeout> | null = null
  private resyncBuffer: any[] | null = null
  private resyncing = false
  private resyncPending = false
  // Work begun before a stop() drops its result once this has changed.
  private stops = 0

  init() {
    if (this.started) return
    this.started = true
    void this.connect()
  }

  /** On logging out: no more reconnecting. */
  stop() {
    this.stops++
    this.started = false
    if (this.retry) clearTimeout(this.retry)
    this.retry = null
    if (this.ws) {
      // Its close event comes later, and must not reconnect after a new login.
      this.ws.onclose = null
      this.ws.close()
    }
    this.ws = null
    this.connected = false
    this.jobs = []
  }

  get(id: string): Job | undefined {
    return this.jobs.find((j) => j.id === id)
  }

  /** Insert a job received outside the websocket (e.g. a POST response).
   *  Insert-only: once the ws delivered 'created', its stream is authoritative. */
  add(job: Job) {
    if (!this.get(job.id)) this.upsert(job)
  }

  /** Replace the store with a fresh snapshot; called on every (re)connect.
   *  Events arriving during the fetch are buffered and replayed on top. */
  private async resync() {
    if (!this.started) return
    // Single-flight: a reconnect/retry mustn't run two snapshots at once, or an older
    // snapshot could clobber a newer one and drop events. Queue one follow-up instead.
    if (this.resyncing) {
      this.resyncPending = true
      return
    }
    const stops = this.stops
    this.resyncing = true
    this.resyncBuffer = []
    try {
      const snapshot = await apiGet<Job[]>('/jobs')
      if (stops !== this.stops) return
      const buffered = this.resyncBuffer
      this.resyncBuffer = null
      this.jobs = snapshot.map((j) => ({ ...j, log: j.log ?? [] }))
      for (const event of buffered) this.apply(event)
      this.loadError = ''
    } catch (e) {
      if (stops !== this.stops) return
      // Replay events buffered during the failed snapshot instead of dropping them
      // (the socket may still be connected), and retry the snapshot shortly.
      const buffered = this.resyncBuffer ?? []
      this.resyncBuffer = null
      for (const event of buffered) this.apply(event)
      this.loadError = `Failed to load job list: ${e}`
      if (this.started) this.retry = setTimeout(() => this.resync(), 2000)
    } finally {
      this.resyncing = false
      if (this.resyncPending) {
        this.resyncPending = false
        void this.resync()
      }
    }
  }

  private async connect() {
    const stops = this.stops
    try {
      // A failed websocket says nothing of why: a lost login here sends the browser back to the login.
      await confirmLoggedIn()
    } catch {
      if (stops === this.stops) this.scheduleReconnect()
      return
    }
    if (!this.started || stops !== this.stops) return
    const ws = new WebSocket(wsUrl())
    this.ws = ws
    ws.onopen = () => {
      this.connected = true
      // Events that fired while we were disconnected are gone — resync.
      this.resync()
    }
    ws.onclose = () => {
      this.connected = false
      if (this.ws === ws) this.ws = null
      this.scheduleReconnect()
    }
    ws.onmessage = (msg) => {
      const event = JSON.parse(msg.data)
      if (this.resyncBuffer !== null) {
        this.resyncBuffer.push(event)
        return
      }
      this.apply(event)
    }
  }

  private scheduleReconnect() {
    if (!this.started) return
    if (this.retry) clearTimeout(this.retry)
    this.retry = setTimeout(() => void this.connect(), 2000)
  }

  private apply(event: any) {
    const job = event.job_id ? this.get(event.job_id) : undefined
    switch (event.event) {
        case 'created':
        case 'finished':
          this.upsert(event.job)
          break
        case 'progress':
          if (job) job.progress = event.progress
          break
        case 'status':
          if (job) job.status = event.status
          break
        case 'question':
          if (job) job.question = event.question
          break
        case 'question_answered':
          if (job) job.question = null
          break
        case 'log':
          if (job) {
            job.log.push(event.line)
            if (job.log.length > LOG_CAP) job.log.splice(0, job.log.length - LOG_CAP / 2)
          }
          break
        case 'spectrals_ready':
          if (job) job.spectrals = event.files
          break
    }
  }

  private upsert(job: Job) {
    job.log = job.log ?? []
    const idx = this.jobs.findIndex((j) => j.id === job.id)
    if (idx >= 0) {
      // Never let a snapshot shrink the live-streamed log.
      const existing = this.jobs[idx]
      if (existing.log.length > job.log.length) job.log = existing.log
      this.jobs[idx] = job
    } else {
      this.jobs.unshift(job)
    }
  }
}

export const jobStore = new JobStore()
