// Client for the optional local engine (cli/subvid_server.py).
// When the server is running on the user's machine, the web app can offload
// transcription to it (GPU, faster-whisper) and drive server-side batch jobs.

export type EngineInfo = { device: string; model: string; auth: boolean }
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

// Where the engine might be, tried in order by detectEngine():
//   1. the page's own origin — the engine serves dist/client itself, both on
//      the LAN (http://192.168.x.x:8787) and behind an https reverse proxy
//   2. the page's host on the default engine port (astro dev over LAN)
//   3. classic loopback (astro dev / the hosted https site + local engine)
const CANDIDATE_BASES: string[] = (() => {
  const bases: string[] = []
  if (typeof location !== "undefined" && /^https?:$/.test(location.protocol)) {
    bases.push(location.origin)
    if (
      location.protocol === "http:" &&
      !["localhost", "127.0.0.1"].includes(location.hostname)
    )
      bases.push(`http://${location.hostname}:8787`)
  }
  bases.push("http://127.0.0.1:8787")
  return [...new Set(bases)]
})()

let BASE = CANDIDATE_BASES[CANDIDATE_BASES.length - 1]

// Access token for "server mode" (browsing the engine machine's folders) on
// engines exposed beyond loopback. Accepted once via the ?engineToken=… URL
// param (then stripped from the address bar and kept in localStorage) or
// typed in when the user enables server mode in the batch panel.
const TOKEN_KEY = "subvid:engineToken"

function readToken(): string {
  if (typeof location === "undefined") return ""
  try {
    const params = new URLSearchParams(location.search)
    const fromUrl = params.get("engineToken")
    if (fromUrl) {
      // A bookmarked ?engineToken=… link implies "remember this device".
      localStorage.setItem(TOKEN_KEY, fromUrl)
      params.delete("engineToken")
      const qs = params.toString()
      history.replaceState(
        null,
        "",
        location.pathname + (qs ? `?${qs}` : "") + location.hash,
      )
    }
    // Session-only tokens (user declined "remember this device") win over
    // any previously remembered one.
    return sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY) || ""
  } catch {
    return ""
  }
}

let token = readToken()

function authHeaders(): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {}
}

export function hasStoredToken() {
  return !!token
}

// remember=true keeps the token on this device (localStorage); false keeps
// it for this browser session only.
export function setEngineToken(value: string, remember = false) {
  token = value.trim()
  try {
    sessionStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(TOKEN_KEY)
    if (token) (remember ? localStorage : sessionStorage).setItem(TOKEN_KEY, token)
  } catch {}
}

// Persist the current (already verified) token on this device.
export function rememberEngineToken() {
  if (token) setEngineToken(token, true)
}

// Whether this engine requires a token for server mode (set when exposed
// beyond loopback; false for a purely local engine).
export function engineNeedsToken() {
  return !!engineInfo?.auth
}

// True when the stored token (or the lack of one, on a local engine) grants
// access to the server-filesystem endpoints.
export async function verifyServerAccess(): Promise<boolean> {
  try {
    const res = await fetch(`${BASE}/api/browse?path=`, {
      headers: authHeaders(),
      signal: AbortSignal.timeout(4000),
    })
    return res.ok
  } catch {
    return false
  }
}

let engineInfo: EngineInfo | null = null

// The "use local engine" checkbox: when off, transcription AND translation
// must both run in the browser even though the engine is reachable.
let engineOptIn = true

export function setEngineOptIn(value: boolean) {
  engineOptIn = value
}

export function getEngineInfo() {
  return engineInfo
}

export function engineAvailable() {
  return !!engineInfo
}

// Engine reachable AND the user wants processing offloaded to it.
export function engineEnabled() {
  return !!engineInfo && engineOptIn
}

async function api(path: string, init?: RequestInit) {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { ...(init?.headers as Record<string, string>), ...authHeaders() },
  })
  if (!res.ok) {
    let detail = `${res.status}`
    try {
      detail = (await res.json())?.detail || detail
    } catch {}
    throw new Error(detail)
  }
  return res.json()
}

async function probeHealth(base: string): Promise<Response> {
  return fetch(`${base}/api/health`, {
    headers: authHeaders(),
    signal: AbortSignal.timeout(1500),
  })
}

export async function detectEngine(): Promise<EngineInfo | null> {
  engineInfo = null
  for (const base of CANDIDATE_BASES) {
    try {
      const res = await probeHealth(base)
      if (!res.ok) continue
      const data = await res.json()
      if (data?.ok) {
        BASE = base
        engineInfo = { device: data.device, model: data.model, auth: !!data.auth }
        return engineInfo
      }
    } catch {}
  }
  return null
}

export function browsePath(path: string): Promise<BrowseResult> {
  return api(`/api/browse?path=${encodeURIComponent(path)}`)
}

// Opens a native folder-picker dialog on the machine that runs the engine
// and resolves with the chosen path ("" if the user cancelled).
export async function pickFolder(): Promise<string> {
  const data = await api("/api/pick-folder", { method: "POST" })
  return data?.path || ""
}

// Server-side NLLB translation (GPU, no browser model download). Japanese
// output is transliterated to romaji by the engine.
export async function translateWithEngine(
  texts: string[],
  src: string,
  tgt: string,
): Promise<string[]> {
  const data = await api("/api/translate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ texts, src, tgt }),
  })
  if (!Array.isArray(data?.texts) || data.texts.length !== texts.length)
    throw new Error("engine translation returned an unexpected payload")
  return data.texts
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

// Client-side batch: upload the user's own files for processing; outputs are
// listed and downloaded from the job when it finishes. No token needed.
export async function startUploadJob(
  files: File[],
  options: Record<string, unknown>,
): Promise<string> {
  const form = new FormData()
  for (const file of files) form.append("files", file)
  form.append("options", JSON.stringify(options))
  const data = await api("/api/jobs/upload", { method: "POST", body: form })
  return data.id
}

export type JobOutputFile = { index: number; name: string; size: number }

export async function listJobFiles(id: string): Promise<JobOutputFile[]> {
  const data = await api(`/api/jobs/${id}/files`)
  return Array.isArray(data?.files) ? data.files : []
}

export function jobFileUrl(id: string, index: number): string {
  return `${BASE}/api/jobs/${id}/files/${index}`
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
