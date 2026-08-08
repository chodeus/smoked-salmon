import { apiGet, wsUrl } from './api'

export interface JobProgress {
  done: number
  total: number
  desc: string
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
}

class JobStore {
  jobs = $state<Job[]>([])
  connected = $state(false)
  loadError = $state('')
  private ws: WebSocket | null = null
  private started = false

  init() {
    if (this.started) return
    this.started = true
    this.connect()
  }

  get(id: string): Job | undefined {
    return this.jobs.find((j) => j.id === id)
  }

  /** Insert a job received outside the websocket (e.g. a POST response). */
  add(job: Job) {
    this.upsert(job)
  }

  /** Replace the store with a fresh snapshot; called on every (re)connect. */
  private async resync() {
    try {
      this.jobs = await apiGet<Job[]>('/jobs')
      this.loadError = ''
    } catch (e) {
      this.loadError = `Jobliste konnte nicht geladen werden: ${e}`
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
      if (event.event === 'created' || event.event === 'finished') {
        this.upsert(event.job)
      } else if (event.event === 'progress') {
        const job = this.get(event.job_id)
        if (job) job.progress = event.progress
      } else if (event.event === 'status') {
        const job = this.get(event.job_id)
        if (job) job.status = event.status
      }
    }
  }

  private upsert(job: Job) {
    const idx = this.jobs.findIndex((j) => j.id === job.id)
    if (idx >= 0) this.jobs[idx] = job
    else this.jobs.unshift(job)
  }
}

export const jobStore = new JobStore()
