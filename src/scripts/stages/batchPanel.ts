import { LANGS, orderedSubtitleLangs } from "@/scripts/languages.ts"
import {
  browsePath,
  cancelJob,
  getJob,
  jobFileUrl,
  listJobFiles,
  pickFolder,
  startBatchJob,
  startUploadJob,
  type BrowseResult,
} from "@/scripts/localEngine.ts"
import type { ui as appUi } from "@/scripts/ui.ts"

type BatchPanelOptions = {
  ui: typeof appUi & Record<string, any>
  tt: (path: string, vars?: Record<string, unknown>) => string
  langName: (code: string) => string
}

type QueueItem = { path: string; name: string; isDir: boolean; file?: File }

const POLL_MS = 700
const LAST_PATH_KEY = "subvid.batch.lastPath"

export function createBatchPanel({ ui, tt, langName }: BatchPanelOptions) {
  let currentPath = ""
  let currentListing: BrowseResult | null = null
  let filterText = ""
  let queue: QueueItem[] = []
  let jobId = ""
  let jobIsUpload = false
  let pollTimer = 0
  let dragIndex = -1
  // false = client mode (pick files on THIS device, upload them);
  // true = server mode (browse the engine machine's folders — needs auth).
  let serverMode = false

  // The native folder dialog opens on the ENGINE machine, so it only makes
  // sense when the browser runs there too (loopback page).
  const browserIsOnEngine =
    typeof location !== "undefined" &&
    ["localhost", "127.0.0.1"].includes(location.hostname)

  function setStatus(text: string) {
    ui.batchStatus.textContent = text
  }

  function clock(epochSeconds: number) {
    return new Date(epochSeconds * 1000).toLocaleTimeString()
  }

  function prettyDuration(seconds: number) {
    const s = Math.max(0, Math.round(seconds))
    const h = Math.floor(s / 3600)
    const m = Math.floor((s % 3600) / 60)
    if (h) return `${h}h ${String(m).padStart(2, "0")}m`
    if (m) return `${m}m ${String(s % 60).padStart(2, "0")}s`
    return `${s}s`
  }

  function applyMode() {
    ui.batchClientPicker.hidden = serverMode
    ui.batchServerBrowser.hidden = !serverMode
    // Output folder / subfolders only mean something for server-side paths;
    // uploads always download their results instead.
    ui.batchOutputDirField.hidden = !serverMode
    ui.batchRecursiveField.hidden = !serverMode
    ui.batchPickFolder.hidden = !browserIsOnEngine
    ui.batchModeBadge.textContent = tt(
      serverMode ? "batch.modeServerBadge" : "batch.modeClientBadge",
    )
    ui.batchModeBadge.classList.toggle("is-server", serverMode)
  }

  // The app-wide Local/Server switch (footer) drives this; authentication
  // already happened there.
  function setServerMode(value: boolean) {
    if (value === serverMode) return
    if (jobId) return // don't switch under a running job
    serverMode = value
    queue = [] // items from the other mode don't apply here
    renderQueue()
    renderListing()
    applyMode()
  }

  function open() {
    applyMode()
    ui.batchPanel.hidden = false
    if (serverMode && !currentListing) {
      let last = ""
      try {
        last = localStorage.getItem(LAST_PATH_KEY) || ""
      } catch {}
      navigate(last)
    }
  }

  function close() {
    ui.batchPanel.hidden = true
  }

  // ── Browser ──

  // Inline tree expansion: expanded dir path -> its listing (null while loading).
  const expandedDirs = new Map<string, BrowseResult | null>()

  async function navigate(path: string) {
    try {
      const listing = await browsePath(path)
      currentPath = listing.path
      currentListing = listing
      expandedDirs.clear()
      filterText = ""
      ui.batchFilter.value = ""
      try {
        if (listing.path) localStorage.setItem(LAST_PATH_KEY, listing.path)
      } catch {}
      renderCrumbs()
      renderListing()
    } catch (e) {
      // Fall back to the roots view if a remembered path no longer exists.
      if (path) return navigate("")
      setStatus(tt("batch.browseFailed", { error: String((e as Error).message || e) }))
    }
  }

  function renderCrumbs() {
    ui.batchCrumbs.innerHTML = ""
    const rootBtn = document.createElement("button")
    rootBtn.type = "button"
    rootBtn.textContent = tt("batch.roots")
    rootBtn.addEventListener("click", () => navigate(""))
    ui.batchCrumbs.appendChild(rootBtn)
    if (!currentPath) return

    const parts = currentPath.split(/[\\/]+/).filter(Boolean)
    let accumulated = ""
    parts.forEach((part, index) => {
      accumulated = index === 0 ? `${part}\\` : `${accumulated}${part}\\`
      const target = accumulated
      const sep = document.createElement("span")
      sep.className = "crumb-sep"
      sep.textContent = "›"
      const btn = document.createElement("button")
      btn.type = "button"
      btn.textContent = part
      btn.title = target
      btn.addEventListener("click", () => navigate(target))
      ui.batchCrumbs.append(sep, btn)
    })
  }

  function inQueue(path: string) {
    return queue.some((item) => item.path === path)
  }

  async function toggleExpand(path: string) {
    if (expandedDirs.has(path)) {
      expandedDirs.delete(path)
      renderListing()
      return
    }
    expandedDirs.set(path, null) // show the row as loading
    renderListing()
    try {
      expandedDirs.set(path, await browsePath(path))
    } catch {
      expandedDirs.delete(path)
    }
    renderListing()
  }

  function renderListing() {
    const listing = currentListing
    if (!listing) return
    ui.batchListing.innerHTML = ""
    const filter = filterText.toLowerCase()
    const matches = (name: string) => !filter || name.toLowerCase().includes(filter)

    const addButton = (item: QueueItem) => {
      const btn = document.createElement("button")
      btn.type = "button"
      btn.className = "row-add"
      btn.textContent = inQueue(item.path) ? "✓" : "＋"
      btn.title = tt("batch.addItem")
      btn.addEventListener("click", (e) => {
        e.stopPropagation()
        toggleQueueItem(item)
        renderListing()
      })
      return btn
    }

    const indent = (li: HTMLElement, depth: number) => {
      if (depth) li.style.paddingLeft = `${0.5 + depth * 1.1}rem`
    }

    const renderEntries = (entries: BrowseResult, depth: number) => {
      for (const dir of entries.dirs.filter((d) => depth > 0 || matches(d.name))) {
        const li = document.createElement("li")
        li.className = "is-dir"
        indent(li, depth)

        const expanded = expandedDirs.has(dir.path)
        const loading = expanded && expandedDirs.get(dir.path) === null
        const chevron = document.createElement("button")
        chevron.type = "button"
        chevron.className = "row-expand"
        chevron.textContent = loading ? "…" : expanded ? "▾" : "▸"
        chevron.title = tt(expanded ? "batch.collapseDir" : "batch.expandDir")
        chevron.setAttribute("aria-expanded", expanded ? "true" : "false")
        chevron.addEventListener("click", (e) => {
          e.stopPropagation()
          toggleExpand(dir.path)
        })

        const label = document.createElement("span")
        label.className = "row-label"
        label.textContent = `📁 ${dir.name}`
        li.append(chevron, label, addButton({ path: dir.path, name: dir.name, isDir: true }))
        li.classList.toggle("is-selected", inQueue(dir.path))
        li.addEventListener("click", () => navigate(dir.path))
        ui.batchListing.appendChild(li)

        const children = expandedDirs.get(dir.path)
        if (children) renderEntries(children, depth + 1)
      }
      for (const file of entries.files.filter((f) => depth > 0 || matches(f.name))) {
        const item: QueueItem = { path: file.path, name: file.name, isDir: false }
        const li = document.createElement("li")
        li.classList.toggle("is-selected", inQueue(file.path))
        indent(li, depth)
        const spacer = document.createElement("span")
        spacer.className = "row-expand row-expand--spacer"
        const label = document.createElement("span")
        label.className = "row-label"
        label.textContent = `🎬 ${file.name}`
        const size = document.createElement("span")
        size.className = "size"
        size.textContent = file.size ? prettySize(file.size) : ""
        li.append(spacer, label, size, addButton(item))
        li.addEventListener("click", () => {
          toggleQueueItem(item)
          renderListing()
        })
        ui.batchListing.appendChild(li)
      }
    }

    renderEntries(listing, 0)
  }

  function prettySize(bytes: number) {
    if (bytes > 1 << 30) return `${(bytes / (1 << 30)).toFixed(1)} GB`
    if (bytes > 1 << 20) return `${Math.round(bytes / (1 << 20))} MB`
    return `${Math.max(1, Math.round(bytes / 1024))} KB`
  }

  // ── Client-side files (uploaded to the engine) ──

  function addClientFiles(files: FileList | null) {
    if (!files?.length) return
    for (const file of Array.from(files)) {
      const key = `${file.name}:${file.size}`
      if (!inQueue(key)) {
        queue.push({ path: key, name: file.name, isDir: false, file })
      }
    }
    renderQueue()
  }

  // ── Queue (ordered, drag & drop + arrow reordering) ──

  function toggleQueueItem(item: QueueItem) {
    const index = queue.findIndex((q) => q.path === item.path)
    if (index >= 0) queue.splice(index, 1)
    else queue.push(item)
    renderQueue()
  }

  function moveQueueItem(from: number, to: number) {
    if (to < 0 || to >= queue.length || from === to) return
    const [item] = queue.splice(from, 1)
    queue.splice(to, 0, item)
    renderQueue()
  }

  function renderQueue() {
    ui.batchQueue.innerHTML = ""
    if (!queue.length) {
      const li = document.createElement("li")
      li.className = "batch-empty"
      li.textContent = tt("batch.selectedEmpty")
      ui.batchQueue.appendChild(li)
      return
    }
    queue.forEach((item, index) => {
      const li = document.createElement("li")
      li.draggable = true
      li.dataset.index = String(index)

      const handle = document.createElement("span")
      handle.className = "q-handle"
      handle.textContent = "⠿"
      const label = document.createElement("span")
      label.className = "q-label"
      label.textContent = `${item.isDir ? "📁" : "🎬"} ${item.name}`
      label.title = item.path

      const up = document.createElement("button")
      up.type = "button"
      up.textContent = "▲"
      up.disabled = index === 0
      up.setAttribute("aria-label", tt("batch.moveUp"))
      up.addEventListener("click", () => moveQueueItem(index, index - 1))
      const down = document.createElement("button")
      down.type = "button"
      down.textContent = "▼"
      down.disabled = index === queue.length - 1
      down.setAttribute("aria-label", tt("batch.moveDown"))
      down.addEventListener("click", () => moveQueueItem(index, index + 1))
      const remove = document.createElement("button")
      remove.type = "button"
      remove.textContent = "✕"
      remove.setAttribute("aria-label", tt("batch.remove"))
      remove.addEventListener("click", () => {
        queue.splice(index, 1)
        renderQueue()
        renderListing()
      })

      li.addEventListener("dragstart", (e) => {
        dragIndex = index
        e.dataTransfer?.setData("text/plain", String(index))
        if (e.dataTransfer) e.dataTransfer.effectAllowed = "move"
      })
      li.addEventListener("dragover", (e) => {
        e.preventDefault()
        li.classList.add("drag-over")
      })
      li.addEventListener("dragleave", () => li.classList.remove("drag-over"))
      li.addEventListener("drop", (e) => {
        e.preventDefault()
        li.classList.remove("drag-over")
        if (dragIndex >= 0) moveQueueItem(dragIndex, index)
        dragIndex = -1
      })

      li.append(handle, label, up, down, remove)
      ui.batchQueue.appendChild(li)
    })
  }

  // ── Unified subtitle-language chips ──

  function renderLanguageChips() {
    ui.batchLangs.innerHTML = ""
    const original = document.createElement("button")
    original.type = "button"
    original.className = "is-fixed"
    original.textContent = tt("batch.originalChip")
    original.title = tt("batch.originalChipHint")
    ui.batchLangs.appendChild(original)
    const { primary, others } = orderedSubtitleLangs()
    for (const code of primary) {
      const chip = document.createElement("button")
      chip.type = "button"
      chip.dataset.lang = code
      chip.textContent = langName(code)
      chip.addEventListener("click", () => chip.classList.toggle("is-on"))
      ui.batchLangs.appendChild(chip)
    }
    const othersToggle = document.createElement("button")
    othersToggle.type = "button"
    othersToggle.textContent = tt("othersGroup")
    othersToggle.addEventListener("click", () => {
      othersToggle.remove()
      for (const code of others) {
        const chip = document.createElement("button")
        chip.type = "button"
        chip.dataset.lang = code
        chip.textContent = langName(code)
        chip.addEventListener("click", () => chip.classList.toggle("is-on"))
        ui.batchLangs.appendChild(chip)
      }
    })
    ui.batchLangs.appendChild(othersToggle)
  }

  function selectedExtraLangs(): string[] {
    return [...ui.batchLangs.querySelectorAll("button.is-on")].map(
      (chip: any) => chip.dataset.lang,
    )
  }

  // ── Job control ──

  function populateAudioLanguages() {
    ui.batchLang.innerHTML = ""
    const auto = document.createElement("option")
    auto.value = ""
    auto.textContent = tt("batch.auto")
    ui.batchLang.appendChild(auto)
    for (const code of Object.keys(LANGS)) {
      const option = document.createElement("option")
      option.value = code
      option.textContent = langName(code)
      ui.batchLang.appendChild(option)
    }
  }

  // ── Style options (apply to every video of the batch) ──
  // Controls carry data-style with the CLI option suffix. Selects use "" as
  // "keep the preset/config default"; other inputs only count once touched,
  // so an untouched panel doesn't override the chosen preset.
  const styleTouched = new Set<string>()

  function wireStyle() {
    for (const el of ui.batchStyle?.querySelectorAll<HTMLElement>("[data-style]") || []) {
      const key = el.dataset.style || ""
      el.addEventListener("change", () => styleTouched.add(key))
    }
    const size = document.getElementById("batch-style-size") as HTMLInputElement | null
    size?.addEventListener("input", () => {
      styleTouched.add("size")
      ui.batchStyleSizeValue.textContent = Number(size.value).toFixed(2)
    })
    const bgOpacity = document.getElementById(
      "batch-style-bg-opacity",
    ) as HTMLInputElement | null
    bgOpacity?.addEventListener("input", () => {
      styleTouched.add("bg_opacity")
      ui.batchStyleBgOpacityValue.textContent = Number(bgOpacity.value).toFixed(2)
    })
  }

  function collectStyleOptions(options: Record<string, unknown>) {
    const controls = ui.batchStyle?.querySelectorAll<HTMLInputElement | HTMLSelectElement>(
      "[data-style]",
    )
    for (const el of controls || []) {
      const key = el.dataset.style || ""
      if (key === "word_animation") continue // sent with the main options
      if (key === "preset") {
        if (el.value) options.style = el.value
        continue
      }
      if (el instanceof HTMLSelectElement) {
        if (el.value) options[`sub_${key}`] = el.value
        continue
      }
      if (!styleTouched.has(key)) continue
      options[`sub_${key}`] =
        el.type === "checkbox"
          ? el.checked
          : el.type === "range"
            ? Number(el.value)
            : el.value
    }
  }

  function collectOptions() {
    const options: Record<string, unknown> = {
      mode: ui.batchMode.value,
      model: ui.batchModel.value,
      word_animation: ui.batchWordAnim?.checked ?? false,
    }
    collectStyleOptions(options)
    if (serverMode) {
      options.recursive = ui.batchRecursive.checked
      if (ui.batchOutputDir.value.trim())
        options.output_dir = ui.batchOutputDir.value.trim()
    }
    if (ui.batchLang.value) options.language = ui.batchLang.value
    const extraLangs = selectedExtraLangs()
    if (extraLangs.length) options.to_langs = extraLangs.join(",")
    if (!ui.batchVad.checked) options.no_vad = true
    else options.vad_threshold = Number(ui.batchVadThreshold.value)
    return options
  }

  async function start() {
    if (jobId) return
    if (!queue.length) {
      setStatus(tt("batch.selectedEmpty"))
      return
    }
    ui.batchStart.disabled = true
    setStatus(tt(serverMode ? "batch.running" : "batch.uploading"))
    ui.batchTimes.textContent = ""
    ui.batchFiles.innerHTML = ""
    ui.batchDownloads.hidden = true
    ui.batchDownloads.innerHTML = ""
    try {
      if (serverMode) {
        jobIsUpload = false
        jobId = await startBatchJob(queue.map((item) => item.path), collectOptions())
      } else {
        jobIsUpload = true
        const files = queue.map((item) => item.file).filter(Boolean) as File[]
        jobId = await startUploadJob(files, collectOptions())
        setStatus(tt("batch.running"))
      }
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

  function renderTimes(job: any) {
    if (!job.startedAt) return
    if (job.finishedAt) {
      ui.batchTimes.textContent = tt("batch.timesFinished", {
        start: clock(job.startedAt),
        end: clock(job.finishedAt),
        duration: prettyDuration(job.finishedAt - job.startedAt),
      })
    } else {
      ui.batchTimes.textContent = tt("batch.timesRunning", {
        start: clock(job.startedAt),
        elapsed: prettyDuration(Date.now() / 1000 - job.startedAt),
      })
    }
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
    renderTimes(job)
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
    renderTimes(job)
    const finishedId = jobId
    jobId = ""
    ui.batchStart.disabled = false
    ui.batchCancel.hidden = true
    if (jobIsUpload && finishedId) renderDownloads(finishedId)
  }

  async function renderDownloads(id: string) {
    let files: Awaited<ReturnType<typeof listJobFiles>>
    try {
      files = await listJobFiles(id)
    } catch {
      return
    }
    if (!files.length) return
    ui.batchDownloads.innerHTML = ""
    const title = document.createElement("h3")
    title.textContent = tt("batch.downloadsTitle")
    ui.batchDownloads.appendChild(title)
    for (const file of files) {
      const link = document.createElement("a")
      link.href = jobFileUrl(id, file.index)
      link.download = file.name
      const label = document.createElement("span")
      label.textContent = `⬇ ${file.name}`
      const size = document.createElement("span")
      size.className = "dl-size"
      size.textContent = prettySize(file.size)
      link.append(label, size)
      ui.batchDownloads.appendChild(link)
    }
    ui.batchDownloads.hidden = false
  }

  async function cancel() {
    if (!jobId) return
    try {
      await cancelJob(jobId)
      setStatus(tt("batch.cancelling"))
    } catch {}
  }

  function wire() {
    populateAudioLanguages()
    renderLanguageChips()
    renderQueue()
    ui.batchOpen?.addEventListener("click", () => open())
    wireStyle()
    ui.batchAddFiles?.addEventListener("click", () => ui.batchFileInput.click())
    ui.batchFileInput?.addEventListener("change", () => {
      addClientFiles(ui.batchFileInput.files)
      ui.batchFileInput.value = ""
    })
    applyMode()
    ui.batchClose.addEventListener("click", close)
    ui.batchBackdrop.addEventListener("click", () => {
      if (!jobId) close()
    })
    ui.batchFilter.addEventListener("input", () => {
      filterText = ui.batchFilter.value
      renderListing()
    })
    ui.batchPickFolder?.addEventListener("click", async () => {
      // Native folder dialog on the engine machine; browse to whatever the
      // user picked there.
      ui.batchPickFolder.disabled = true
      try {
        const path = await pickFolder()
        if (path) await navigate(path)
      } catch (e) {
        setStatus(tt("batch.browseFailed", { error: String((e as Error).message || e) }))
      } finally {
        ui.batchPickFolder.disabled = false
      }
    })
    ui.batchVadThreshold.addEventListener("input", () => {
      ui.batchVadValue.textContent = Number(ui.batchVadThreshold.value).toFixed(2)
    })
    ui.batchVad.addEventListener("change", () => {
      ui.batchVadThreshold.disabled = !ui.batchVad.checked
    })
    // Initial sync: the slider must match the checkbox from the start (the
    // browser may also restore a checked state across reloads).
    ui.batchVadThreshold.disabled = !ui.batchVad.checked
    ui.batchStart.addEventListener("click", start)
    ui.batchCancel.addEventListener("click", cancel)
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !ui.batchPanel.hidden && !jobId) close()
    })
  }

  // Re-render every JS-generated text after an in-place locale switch,
  // preserving the user's current selections.
  function refreshTexts() {
    const langValue = ui.batchLang.value
    populateAudioLanguages()
    ui.batchLang.value = langValue
    const activeChips = selectedExtraLangs()
    renderLanguageChips()
    for (const chip of ui.batchLangs.querySelectorAll("button[data-lang]")) {
      if (activeChips.includes((chip as HTMLElement).dataset.lang || ""))
        chip.classList.add("is-on")
    }
    renderQueue()
    applyMode()
    if (currentListing) {
      renderCrumbs()
      renderListing()
    }
  }

  return { wire, open, close, refreshTexts, setServerMode }
}
