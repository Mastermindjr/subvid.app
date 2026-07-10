import { hasBuiltInTranslationSupport } from "@/scripts/builtInTranslate.ts";
import { createDownloadsController } from "@/scripts/downloads.ts";
import { createEditorHistory } from "@/scripts/editorHistory.ts";
import { createEditorSegmentsController } from "@/scripts/editorSegments.ts";
import { createExportModal } from "@/scripts/export/exportModal.ts";
import { createVideoExporter } from "@/scripts/export/videoExport.ts";
import { baseFileName, prettifyBytes } from "@/scripts/file.ts";
import { I18N, langName, tt } from "@/scripts/i18n.ts";
import { detectEngine } from "@/scripts/localEngine.ts";
import { createBatchPanel } from "@/scripts/stages/batchPanel.ts";
import { createStageManager } from "@/scripts/stageManager.ts";
import { createConfigStageController } from "@/scripts/stages/configStage.ts";
import { createEditorStageController } from "@/scripts/stages/editorStage.ts";
import { createUploadStageController } from "@/scripts/stages/uploadStage.ts";
import { createSubtitleStyleController } from "@/scripts/subtitleStyle.ts";
import { createTimelineController } from "@/scripts/timeline.ts";
import { createTransformersClient } from "@/scripts/transformersClient.ts";
import { createTranslationService } from "@/scripts/translation.ts";
import { ui } from "@/scripts/ui.ts";

type Segment = { start: number; end: number; text: string };
type SegmentsByLang = Record<string, Segment[]>;
type VisibleTrack = {
  lang: string;
  label: string;
  role: "default" | "transcription" | "subtitles";
  segments: Segment[];
  hidden?: boolean;
  locked?: boolean;
};
type TrackState = { hidden?: boolean; locked?: boolean };

const {
  downloads,
  renderDownloads,
  updateDownloadStatus,
  makeTransformersTracker,
  fetchWithProgress,
  refreshClearModelsUI,
  clearLocalModels,
} = createDownloadsController({
  ui,
  tt,
  prettifyBytes,
  hasBuiltInTranslationSupport,
});

// ── State ──
let selectedVideoFile: File | null = null
let videoObjectUrl = ""
let detectedLang = ""
let baseSegments: Segment[] = []
let segmentsByLang: SegmentsByLang = {}
let orderedLangs: string[] = []
let activeLang = ""
let dualTrackMode = false
let dualTrackLangs: string[] = []
let trackStates: Record<string, TrackState> = {}
let exporting = false

const { setStage } = createStageManager({ ui });
const asrTracker = makeTransformersTracker("asr");
const translationTracker = makeTransformersTracker("translation");
const transformersClient = createTransformersClient({
  onProgress(key, payload) {
    if (key === "asr") asrTracker(payload);
    else if (key === "translation") translationTracker(payload);
  },
});

let translationService: ReturnType<typeof createTranslationService>;
let historyController: ReturnType<typeof createEditorHistory<SegmentsByLang>>;
let editorStageController: ReturnType<typeof createEditorStageController>;
let subtitleStyleController: ReturnType<typeof createSubtitleStyleController>;

const translateSegments = (
  segments: Segment[],
  sourceLang: string,
  targetLang: string,
) => translationService.translateSegments(segments, sourceLang, targetLang);

const isTranslationReady = () => translationService.isTranslationReady()

function currentSegments(): Segment[] {
  return segmentsByLang[activeLang] || [];
}

function trackLabel(lang: string) {
  if (dualTrackMode && lang === detectedLang)
    return tt("tracks.transcription", { lang: langName(lang) });
  if (dualTrackMode && dualTrackLangs.includes(lang))
    return tt("tracks.subtitles", { lang: langName(lang) });
  return langName(lang);
}

function trackRole(lang: string): VisibleTrack["role"] {
  if (dualTrackMode && lang === detectedLang) return "transcription";
  if (dualTrackMode && dualTrackLangs.includes(lang)) return "subtitles";
  return "default";
}

function visibleTrackLangs() {
  const langs =
    dualTrackMode && dualTrackLangs.includes(activeLang)
      ? dualTrackLangs
      : [activeLang];
  return langs.filter((lang, index) => lang && langs.indexOf(lang) === index);
}

function trackState(lang: string) {
  return trackStates[lang] || {};
}

function visibleTracks(): VisibleTrack[] {
  return visibleTrackLangs()
    .map((lang) => ({
      lang,
      label: trackLabel(lang),
      role: trackRole(lang),
      segments: segmentsByLang[lang] || [],
      hidden: !!trackState(lang).hidden,
      locked: !!trackState(lang).locked,
    }))
    .filter((track) => track.segments.length);
}

function currentVideoSegments(): any[] {
  const tracks = visibleTracks().filter((track) => !track.hidden);
  return tracks.length ? tracks : [];
}

function resetEditorState() {
  detectedLang = "";
  baseSegments = [];
  segmentsByLang = {};
  orderedLangs = [];
  activeLang = "";
  dualTrackMode = false;
  dualTrackLangs = [];
  trackStates = {};
}

function snapshotSegments() {
  return historyController.snapshotSegments();
}

function pushHistory(snapshotBefore: string) {
  historyController.pushHistory(snapshotBefore);
}

function resetHistory() {
  historyController.resetHistory();
}

function enableExports(on: boolean) {
  editorStageController.enableExports(on);
}

function syncActiveCaptionStyle() {
  subtitleStyleController?.setActiveTrack(trackRole(activeLang), activeLang);
}

function enableWordAnimationForAll() {
  if (!ui.wordAnimation.checked) return;
  subtitleStyleController?.setWordHighlightForAll(true);
}

let editorSegmentsController: any;
const { renderTimeline, highlightSegment, updateCaption } =
  createTimelineController({
    ui,
    tt,
    currentSegments,
    visibleTracks,
    activeLang: () => activeLang,
    setActiveLang: (lang) => {
      activeLang = lang;
      syncActiveCaptionStyle();
    },
    renderTabs: () => editorSegmentsController.renderTabs(),
    renderCaptions: (tracks, time) =>
      subtitleStyleController?.renderCaptions(tracks, time),
    toggleTrackHidden: (lang) => {
      const before = snapshotSegments();
      trackStates[lang] = {
        ...trackState(lang),
        hidden: !trackState(lang).hidden,
      };
      pushHistory(before);
      renderTimeline();
      updateCaption();
    },
    toggleTrackLocked: (lang) => {
      const before = snapshotSegments();
      trackStates[lang] = {
        ...trackState(lang),
        locked: !trackState(lang).locked,
      };
      pushHistory(before);
      renderTimeline();
      updateCaption();
    },
    snapshotSegments,
    pushHistory,
    renderSegments: () => editorSegmentsController.renderSegments(),
    enableExports,
  });
editorSegmentsController = createEditorSegmentsController({
  ui,
  tt,
  langName,
  getState: () => ({
    detectedLang,
    baseSegments,
    segmentsByLang,
    orderedLangs,
    activeLang,
    dualTrackMode,
    dualTrackLangs,
    trackStates,
  }),
  setActiveLang: (lang) => {
    activeLang = lang;
    syncActiveCaptionStyle();
  },
  setOrderedLangs: (langs) => {
    orderedLangs = langs;
  },
  setSegmentsForLang: (lang, segments) => {
    segmentsByLang[lang] = segments;
    if (ui.wordAnimation.checked)
      subtitleStyleController?.setWordHighlightForAll(true);
  },
  trackLabel,
  translateSegments,
  isTranslationReady,
  snapshotSegments,
  pushHistory,
  renderTimeline,
  highlightSegment,
  updateCaption,
  enableExports,
});
const {
  addLanguage,
  buildLangSelects,
  populateAddLang,
  renderSegments,
  renderTabs,
  setLangAddStatus,
  wireSegmentEditor,
} = editorSegmentsController;
subtitleStyleController = createSubtitleStyleController({ ui, I18N });
const {
  applyCaptionStyle,
  renderPresets,
  syncStyleControls,
  wireStyleControls,
} = subtitleStyleController;
const exportModal = createExportModal({ ui, tt, isExporting: () => exporting });
const { closeExportModal } = exportModal;

editorStageController = createEditorStageController({
  ui,
  currentSegments,
  selectedVideoFile: () => selectedVideoFile,
  activeLang: () => activeLang,
  isExporting: () => exporting,
  setStage,
  undo: () => historyController.undo(),
  redo: () => historyController.redo(),
});

historyController = createEditorHistory<SegmentsByLang>({
  getState: () => ({
    segmentsByLang,
    orderedLangs,
    activeLang,
    dualTrackMode,
    dualTrackLangs,
    trackStates,
  }),
  restoreState: (state) => {
    segmentsByLang = state.segmentsByLang || {};
    orderedLangs = state.orderedLangs || Object.keys(segmentsByLang);
    activeLang = state.activeLang || orderedLangs[0] || "";
    if (!segmentsByLang[activeLang])
      activeLang = orderedLangs[0] || Object.keys(segmentsByLang)[0] || "";
    dualTrackMode = !!state.dualTrackMode;
    dualTrackLangs = state.dualTrackLangs || [];
    trackStates = state.trackStates || {};
  },
  refreshButtons: (canUndo, canRedo) => {
    if (ui.undoBtn) ui.undoBtn.disabled = !canUndo;
    if (ui.redoBtn) ui.redoBtn.disabled = !canRedo;
  },
  onRestore: () => {
    syncActiveCaptionStyle();
    renderTabs();
    renderSegments();
    enableExports(true);
    updateCaption();
  },
});

const configStageController = createConfigStageController({
  ui,
  tt,
  downloads,
  fetchWithProgress,
  updateDownloadStatus,
  transformersClient,
  translateSegments,
  selectedVideoFile: () => selectedVideoFile,
  isExporting: () => exporting,
  setGeneratedState: (state) => {
    detectedLang = state.detectedLang;
    baseSegments = state.baseSegments;
    segmentsByLang = state.segmentsByLang;
    orderedLangs = state.orderedLangs;
    activeLang = state.activeLang;
    dualTrackMode = state.dualTrackMode;
    dualTrackLangs = state.dualTrackLangs;
    trackStates = {};
    enableWordAnimationForAll();
    syncActiveCaptionStyle();
  },
  renderTabs,
  renderSegments,
  enableExports,
  resetHistory,
  updateCaption,
  setStage,
});

translationService = createTranslationService({
  downloads,
  renderDownloads,
  updateDownloadStatus,
  transformersClient,
  tt,
  langName,
  setStatus: configStageController.setStatus,
});

const uploadStageController = createUploadStageController({
  ui,
  tt,
  setStage,
  setStatus: configStageController.setStatus,
  setProgress: configStageController.setProgress,
  isExporting: () => exporting,
  getVideoObjectUrl: () => videoObjectUrl,
  setVideoObjectUrl: (url) => {
    videoObjectUrl = url;
  },
  setSelectedVideoFile: (file) => {
    selectedVideoFile = file;
  },
  resetEditorState,
  setLangAddStatus,
  populateAddLang,
  renderSegments,
  enableExports,
  resetHistory,
  startEarlyTranscription: (file) =>
    configStageController.startEarlyTranscription(file),
  resetTranscriptionCache: () =>
    configStageController.resetTranscriptionCache(),
});

const { downloadVideo } = createVideoExporter({
  ui,
  tt,
  currentSegments: currentVideoSegments,
  selectedVideoFile: () => selectedVideoFile,
  activeLang: () => activeLang,
  baseFileName: () => baseFileName(selectedVideoFile),
  isExporting: () => exporting,
  setExporting: (value) => {
    exporting = value;
  },
  enableExports,
  setStatus: configStageController.setStatus,
  modal: exportModal,
  remuxAudioToAacLc: configStageController.remuxAudioToAacLc,
  muxSubtitleTracks: configStageController.muxSubtitleTracks,
});

// ── Init ──
buildLangSelects();
renderDownloads();
renderPresets();
syncStyleControls();
applyCaptionStyle();
wireStyleControls();
wireSegmentEditor();
// Note: models are intentionally NOT preloaded here. Downloading ffmpeg WASM +
// Whisper (~330 MB) on every page load overloaded reloads and language
// switches; the pipeline loads them lazily the first time an in-browser
// transcription actually runs (never, when the local engine handles it).
setStage("upload");
uploadStageController.wireUploadStage();
configStageController.wireConfigStage();
editorStageController.wireEditorStage();
ui.langAddSelect?.addEventListener("change", () => {
  const target = ui.langAddSelect.value;
  if (target) addLanguage(target);
});
ui.downloadVideoBtn.addEventListener("click", downloadVideo);
ui.exportClose.addEventListener("click", closeExportModal);
ui.exportBackdrop.addEventListener("click", closeExportModal);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !ui.exportModal.hidden) closeExportModal();
});
ui.downloadsToggle.addEventListener("click", () => {
  const opening = ui.downloadsPanel.hidden;
  ui.downloadsPanel.hidden = !opening;
  // The panel header already shows the status, so drop the dock label while open.
  ui.statusDock?.classList.toggle("panel-open", opening);
  if (opening) refreshClearModelsUI();
});
ui.clearModelsBtn?.addEventListener("click", clearLocalModels);

// Local engine (cli/subvid_server.py): when it is running, transcription can
// run on the user's GPU and whole folders can be processed in batch.
const batchPanel = createBatchPanel({ ui, tt, langName });
batchPanel.wire();
const baseLocalEngineHint = ui.localEngineHint?.textContent || "";
function applyEngineState(info: { device: string; model: string } | null) {
  ui.engineOn.hidden = !info;
  ui.engineOff.hidden = !!info;
  ui.localEngineField.hidden = !info;
  if (!info) return;
  const badge = tt("engine.detected", {
    device: info.device.toUpperCase(),
    model: info.model,
  });
  ui.engineBadge.textContent = badge;
  if (ui.localEngineHint) {
    ui.localEngineHint.textContent = `${baseLocalEngineHint} (${badge})`;
  }
}
detectEngine().then(applyEngineState);
ui.engineRetry?.addEventListener("click", async () => {
  ui.engineRetry.disabled = true;
  try {
    applyEngineState(await detectEngine());
  } finally {
    ui.engineRetry.disabled = false;
  }
});
