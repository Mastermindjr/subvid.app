import { LANGS } from "@/scripts/languages.ts"
import {
  browsePath,
  cancelJob,
  getJob,
  startBatchJob,
  type BrowseResult,
} from "@/scripts/localEngine.ts"
import type { ui as appUi } from "@/scripts/ui.ts"

type BatchPanelOptions = {
  ui: typeof appUi & Record<string, any>
  tt: (path: string, vars?: Record<string, unknown>) => string
  langName: (code: string) => string
}

const POLL_MS = 700

export function createBatchPanel({ ui, tt, langName }: BatchPanelOptions) {
  let currentPath = ""
  let currentListing: BrowseResult | null = null
  const selected = new Map<string, string>() // path -> display name
  let jobId = ""
  let pollTimer = 0

  function setStatus(text: string) {
    ui.batchStatus.textContent = text
  }

  function open() {
    ui.batchPanel.hidden = false
    if (!currentListing) navigate("")
  }

  function close() {
    ui.batchPanel.hidden = true
  }

  async function navigate(path: string) {
    try {
      const listing = await browsePath(path)
      currentPath = listing.path
      currentListing = listing
      renderListing()
    } catch (e) {
      setStatus(tt("batch.browseFailed", { error: String((e as Error).message || e) }))
    }
  }

  function renderListing() {
    const listing = currentListing
    if (!listing) return
    ui.batchPath.textContent = listing.path || tt("batch.roots")
    ui.batchUp.disabled = listing.parent == null && !listing.path
    ui.batchListing.innerHTML = ""
    for (const dir of listing.dirs) {
      const li = document.createElement("li")
      li.textContent = `📁 ${dir.name}`
      li.addEventListener("click", () => navigate(dir.path))
      ui.batchListing.appendChild(li)
    }
    for (const file of listing.files) {
      const li = document.createElement("li")
      li.classList.toggle("is-selected", selected.has(file.path))
      const label = document.createElement("span")
      label.textContent = `🎬 ${file.name}`
      const size = document.createElement("span")
      size.className = "size"
      size.textContent = file.size ? prettySize(file.size) : ""
      li.append(label, size)
      li.addEventListener("click", () => {
        toggleSelection(file.path, file.name)
        li.classList.toggle("is-selected", selected.has(file.path))
      })
      ui.batchListing.appendChild(li)
    }
  }

  function prettySize(bytes: number) {
    if (bytes > 1 << 30) return `${(bytes / (1 << 30)).toFixed(1)} GB`
    if (bytes > 1 << 20) return `${Math.round(bytes / (1 << 20))} MB`
    return `${Math.max(1, Math.round(bytes / 1024))} KB`
  }

  function toggleSelection(path: string, name: string) {
    if (selected.has(path)) selected.delete(path)
    else selected.set(path, name)
    renderSelected()
  }

  function renderSelected() {
    ui.batchSelected.innerHTML = ""
    if (!selected.size) {
      const li = document.createElement("li")
      li.className = "batch-empty"
      li.textContent = tt("batch.selectedEmpty")
      ui.batchSelected.appendChild(li)
      return
    }
    for (const [path, name] of selected) {
      const li = document.createElement("li")
      const label = document.createElement("span")
      label.textContent = name
      label.title = path
      const remove = document.createElement("button")
      remove.type = "button"
      remove.textContent = "✕"
      remove.setAttribute("aria-label", tt("batch.remove"))
      remove.addEventListener("click", () => {
        selected.delete(path)
        renderSelected()
        renderListing()
      })
      li.append(label, remove)
      ui.batchSelected.appendChild(li)
    }
  }

  function populateLanguages() {
    ui.batchLang.innerHTML = ""
    const auto = document.createElement("option")
    auto.value = ""
    auto.textContent = tt("batch.auto")
    ui.batchLang.appendChild(auto)
    ui.batchToLangs.innerHTML = ""
    for (const code of Object.keys(LANGS)) {
      const option = document.createElement("option")
      option.value = code
      option.textContent = langName(code)
      ui.batchLang.appendChild(option)
      ui.batchToLangs.appendChild(option.cloneNode(true))
    }
  }

  function collectOptions() {
    const options: Record<string, unknown> = {
      mode: ui.batchMode.value,
      model: ui.batchModel.value,
      task: ui.batchTask.value,
      recursive: ui.batchRecursive.checked,
    }
    if (ui.batchLang.value) options.language = ui.batchLang.value
    const toLangs = [...ui.batchToLangs.selectedOptions].map((o) => o.value)
    if (toLangs.length) options.to_langs = toLangs.join(",")
    if (ui.batchOutputDir.value.trim()) options.output_dir = ui.batchOutputDir.value.trim()
    if (!ui.batchVad.checked) options.no_vad = true
    else options.vad_threshold = Number(ui.batchVadThreshold.value)
    return options
  }

  async function start() {
    if (jobId) return
    if (!selected.size) {
      setStatus(tt("batch.selectedEmpty"))
      return
    }
    ui.batchStart.disabled = true
    setStatus(tt("batch.running"))
    ui.batchFiles.innerHTML = ""
    try {
      jobId = await startBatchJob([...selected.keys()], collectOptions())
    } catch (e) {
      ui.batchStart.disabled = false
      setStatus(tt("batch.startFailed", { error: String((e as Error).message || e) }))
      return
    }
    ui.batchCancel.hidden = false
    poll()
  }

  async function poll() {
    if (!jobId) return
    let job: any
    try {
      job = await getJob(jobId)
    } catch {
      pollTimer = window.setTimeout(poll, POLL_MS * 3)
      return
    }
    renderJob(job)
    if (job.status === "running") {
      pollTimer = window.setTimeout(poll, POLL_MS)
    } else {
      finishJob(job)
    }
  }

  function stateLabel(state: string) {
    const label = tt(`batch.states.${state}`)
    return label.startsWith("batch.") ? state : label
  }

  function renderJob(job: any) {
    const rows = ui.batchFiles
    const files: any[] = job.files || []
    while (rows.children.length < files.length) {
      const row = document.createElement("div")
      row.className = "batch-file-row"
      row.innerHTML =
        '<span class="batch-file-name"></span>' +
        '<span class="batch-file-state"></span>' +
        '<span class="batch-file-bar"><i></i></span>' +
        '<span class="batch-file-detail"></span>'
      rows.appendChild(row)
    }
    files.forEach((file, index) => {
      const row = rows.children[index] as HTMLElement
      const [name, state, bar, detail] = [
        row.children[0],
        row.children[1] as HTMLElement,
        row.children[2].firstElementChild as HTMLElement,
        row.children[3],
      ]
      name.textContent = file.name
      ;(name as HTMLElement).title = file.path
      state.textContent = stateLabel(file.status)
      state.dataset.state = file.status
      bar.style.width = `${Math.round(file.pct || 0)}%`
      detail.textContent = file.detail || ""
    })
    const done = files.filter((f) =>
      ["done", "skipped", "error", "cancelled"].includes(f.status),
    ).length
    setStatus(tt("batch.progressSummary", { done, total: files.length }))
  }

  function finishJob(job: any) {
    const failed = (job.files || []).filter((f: any) => f.status === "error").length
    setStatus(
      job.status === "cancelled"
        ? tt("batch.cancelledMsg")
        : failed
          ? tt("batch.doneWithErrors", { failed })
          : tt("batch.doneAll"),
    )
    jobId = ""
    ui.batchStart.disabled = false
    ui.batchCancel.hidden = true
  }

  async function cancel() {
    if (!jobId) return
    try {
      await cancelJob(jobId)
      setStatus(tt("batch.cancelling"))
    } catch {}
  }

  function wire() {
    populateLanguages()
    renderSelected()
    ui.batchOpen?.addEventListener("click", open)
    ui.batchClose.addEventListener("click", close)
    ui.batchBackdrop.addEventListener("click", () => {
      if (!jobId) close()
    })
    ui.batchUp.addEventListener("click", () => {
      if (!currentListing) return
      navigate(currentListing.parent ?? "")
    })
    ui.batchAddFolder.addEventListener("click", () => {
      if (!currentPath) return
      toggleSelection(currentPath, `📁 ${currentPath}`)
      renderListing()
    })
    ui.batchVadThreshold.addEventListener("input", () => {
      ui.batchVadValue.textContent = Number(ui.batchVadThreshold.value).toFixed(2)
    })
    ui.batchVad.addEventListener("change", () => {
      ui.batchVadThreshold.disabled = !ui.batchVad.checked
    })
    ui.batchStart.addEventListener("click", start)
    ui.batchCancel.addEventListener("click", cancel)
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !ui.batchPanel.hidden && !jobId) close()
    })
  }

  return { wire, open, close }
}
