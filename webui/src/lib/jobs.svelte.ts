import { apiGet, wsUrl } from './api'

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

export interface Job {
  id: string
  type: string
  title: string
  params: Record<string, unknown>
  status: 'queued' | 'running' | 'done' | 'error' | 'cancelled'
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
  private resyncBuffer: any[] | null = null

  init() {
    if (this.started) return
    this.started = true
    this.connect()
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
    this.resyncBuffer = []
    try {
      const snapshot = await apiGet<Job[]>('/jobs')
      const buffered = this.resyncBuffer
      this.resyncBuffer = null
      this.jobs = snapshot.map((j) => ({ ...j, log: j.log ?? [] }))
      for (const event of buffered) this.apply(event)
      this.loadError = ''
    } catch (e) {
      // Replay events buffered during the failed snapshot instead of dropping them
      // (the socket may still be connected), and retry the snapshot shortly.
      const buffered = this.resyncBuffer ?? []
      this.resyncBuffer = null
      for (const event of buffered) this.apply(event)
      this.loadError = `Failed to load job list: ${e}`
      setTimeout(() => this.resync(), 2000)
    }
  }

  private connect() {
    const ws = new WebSocket(wsUrl())
    this.ws = ws
    ws.onopen = () => {
      this.connected = true
      // Events that fired while we were disconnected are gone — resync.
      this.resync()
    }
    ws.onclose = () => {
      this.connected = false
      this.ws = null
      setTimeout(() => this.connect(), 2000)
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
