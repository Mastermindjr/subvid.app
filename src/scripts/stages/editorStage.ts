import { baseFileName } from "@/scripts/file.ts"
import type { Stage } from "@/scripts/stageManager.ts"
import { buildSrt } from "@/scripts/subtitles.ts"
import type { ui as appUi } from "@/scripts/ui.ts"

type Segment = { start: number; end: number; text: string }

type EditorStageOptions = {
  ui: typeof appUi
  currentSegments: () => Segment[]
  activeLang: () => string
  selectedVideoFile: () => File | null
  isExporting: () => boolean
  setStage: (stage: Stage) => void
  undo: () => void
  redo: () => void
}

function isTextInputTarget(target: EventTarget | null) {
  return (
    target instanceof HTMLElement &&
    (target.tagName === "INPUT" ||
      target.tagName === "TEXTAREA" ||
      target.isContentEditable)
  )
}

export function createEditorStageController({
  ui,
  currentSegments,
  activeLang,
  selectedVideoFile,
  isExporting,
  setStage,
  undo,
  redo,
}: EditorStageOptions) {
  function enableExports(on: boolean) {
    const ready = on && currentSegments().length > 0
    ui.downloadSrtBtn.disabled = !ready
    ui.downloadVideoBtn.disabled = !ready
    ui.exportFormat.disabled = !ready
    ui.exportQuality.disabled = !ready
  }

  function backToConfig() {
    if (isExporting()) return
    ui.video.pause()
    setStage("config")
  }

  function downloadSrt() {
    const segments = currentSegments()
    if (!segments.length) return

    const blob = new Blob([buildSrt(segments)], {
      type: "text/plain;charset=utf-8",
    })
    const url = URL.createObjectURL(blob)
    const link = document.createElement("a")
    link.href = url
    link.download = `${baseFileName(selectedVideoFile())}.${activeLang()}.srt`
    link.click()
    URL.revokeObjectURL(url)
  }

  function handleKeyboardShortcut(event: KeyboardEvent) {
    if (!ui.stageEditor.hidden && ui.exportModal.hidden) {
      const key = event.key.toLowerCase()

      if ((event.metaKey || event.ctrlKey) && (key === "z" || key === "y")) {
        if (isTextInputTarget(event.target)) return

        const wantsRedo = key === "y" || (key === "z" && event.shiftKey)
        event.preventDefault()
        if (wantsRedo) redo()
        else undo()
        return
      }

      if (event.key === " " && !isTextInputTarget(event.target)) {
        event.preventDefault()
        if (ui.video.paused) ui.video.play().catch(() => {})
        else ui.video.pause()
        return
      }

      if (
        (event.key === "ArrowLeft" || event.key === "ArrowRight") &&
        !isTextInputTarget(event.target)
      ) {
        const duration = ui.video.duration
        if (!Number.isFinite(duration)) return
        event.preventDefault()
        const step = event.shiftKey ? 5 : 1
        const delta = event.key === "ArrowRight" ? step : -step
        ui.video.currentTime = Math.max(
          0,
          Math.min(duration, (ui.video.currentTime || 0) + delta),
        )
      }
    }
  }

  function wireEditorStage() {
    ui.backBtn.addEventListener("click", backToConfig)
    ui.undoBtn?.addEventListener("click", undo)
    ui.redoBtn?.addEventListener("click", redo)
    ui.downloadSrtBtn.addEventListener("click", downloadSrt)
    document.addEventListener("keydown", handleKeyboardShortcut)
  }

  return {
    enableExports,
    backToConfig,
    downloadSrt,
    wireEditorStage,
  }
}
