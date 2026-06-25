import { $ } from "@/scripts/dom.ts"

export function createExportModal({ ui, tt, isExporting }: any) {
  const EXPORT_STEPS = [
    { id: "prepare", label: tt("exportSteps.prepare") },
    { id: "render", label: tt("exportSteps.render") },
    { id: "encode", label: tt("exportSteps.encode") },
    { id: "done", label: tt("exportSteps.done") },
  ]

  function openExportModal() {
    ui.exportSteps.innerHTML = EXPORT_STEPS.map(
      (s) =>
        `<li class="export-step" data-id="${s.id}" data-state="pending"><span class="export-step-dot"></span><span class="export-step-label">${s.label}</span></li>`,
    ).join("")
    ui.exportError.hidden = true
    ui.exportError.textContent = ""
    ui.exportClose.hidden = true
    ui.exportTitle.textContent = tt("exportStages.exporting")
    ui.exportHint.hidden = false
    setExportStep("prepare", "active")
    setExportStage(tt("exportStages.preparing"), "busy")
    setExportProgress(0)
    ui.exportModal.hidden = false
  }

  function closeExportModal() {
    if (isExporting()) return
    ui.exportModal.hidden = true
  }

  function setExportStage(text: string, kind = "busy") {
    ui.exportStage.textContent = text
    ui.exportStage.dataset.kind = kind
  }

  function setExportProgress(percent: number) {
    const clamped = Math.max(0, Math.min(100, percent))
    const rounded = Math.round(clamped)
    ui.exportFill.style.width = `${clamped}%`
    ui.exportPct.textContent = `${rounded}%`
    ui.exportTrack?.setAttribute("aria-valuenow", String(rounded))
  }

  function setExportStep(id: string, state: string) {
    const el = $(`[data-id="${id}"]`, ui.exportSteps)
    if (el) el.dataset.state = state
  }

  function setExportNotice(message: string) {
    ui.exportError.textContent = message
    ui.exportError.hidden = !message
  }

  function failExport(message: string) {
    setExportStage(tt("exportStages.failed"), "error")
    ui.exportError.textContent = message
    ui.exportError.hidden = false
    ui.exportHint.hidden = true
    ui.exportClose.hidden = false
  }

  return {
    openExportModal,
    closeExportModal,
    setExportStage,
    setExportProgress,
    setExportStep,
    setExportNotice,
    failExport,
  }
}
