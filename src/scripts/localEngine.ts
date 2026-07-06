// Client for the optional local engine (cli/subvid_server.py).
// When the server is running on the user's machine, the web app can offload
// transcription to it (GPU, faster-whisper) and drive server-side batch jobs.

export type EngineInfo = { device: string; model: string }
export type EngineWord = { start: number; end: number; text: string }
export type EngineSegment = {
  start: number
  end: number
  text: string
  words?: EngineWord[]
}
export type EngineTranscription = { language: string; segments: EngineSegment[] }
export type BrowseEntry = { name: string; path: string; size?: number }
export type BrowseResult = {
  path: string
  parent: string | null
  dirs: BrowseEntry[]
  files: BrowseEntry[]
}
export type BatchFileState = {
  name: string
  path: string
  status: string
  pct: number
  detail: string
}
export type BatchJobState = {
  id: string
  status: string
  files: BatchFileState[]
}

const BASE = "http://127.0.0.1:8787"

let engineInfo: EngineInfo | null = null

export function getEngineInfo() {
  return engineInfo
}

export function engineAvailable() {
  return !!engineInfo
}

async function api(path: string, init?: RequestInit) {
  const res = await fetch(`${BASE}${path}`, init)
  if (!res.ok) {
    let detail = `${res.status}`
    try {
      detail = (await res.json())?.detail || detail
    } catch {}
    throw new Error(detail)
  }
  return res.json()
}

export async function detectEngine(): Promise<EngineInfo | null> {
  try {
    const res = await fetch(`${BASE}/api/health`, {
      signal: AbortSignal.timeout(1500),
    })
    const data = res.ok ? await res.json() : null
    engineInfo = data?.ok ? { device: data.device, model: data.model } : null
  } catch {
    engineInfo = null
  }
  return engineInfo
}

export function browsePath(path: string): Promise<BrowseResult> {
  return api(`/api/browse?path=${encodeURIComponent(path)}`)
}

export async function startBatchJob(
  paths: string[],
  options: Record<string, unknown>,
): Promise<string> {
  const data = await api("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paths, options }),
  })
  return data.id
}

export function getJob(id: string): Promise<any> {
  return api(`/api/jobs/${id}`)
}

export function cancelJob(id: string): Promise<any> {
  return api(`/api/jobs/${id}/cancel`, { method: "POST" })
}

export async function transcribeWithEngine(
  file: File,
  language: string,
  onProgress?: (pct: number, detail: string) => void,
): Promise<EngineTranscription> {
  const form = new FormData()
  form.append("file", file)
  if (language) form.append("language", language)
  const { id } = await api("/api/transcribe", { method: "POST", body: form })

  for (;;) {
    await new Promise((r) => setTimeout(r, 600))
    const job = await getJob(id)
    if (job.status === "done") return job.result
    if (job.status === "error") throw new Error(job.error || "engine error")
    if (job.status === "cancelled") throw new Error("cancelled")
    onProgress?.(job.pct || 0, job.detail || "")
  }
}
