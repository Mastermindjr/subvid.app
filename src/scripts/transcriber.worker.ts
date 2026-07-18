// Dedicated Web Worker that hosts the Whisper ASR transformers.js pipeline.
// Loading and running inference is heavy CPU/WASM work that would otherwise
// freeze the main thread (the UI, progress bars, etc.).
//
// Protocol (main ⇄ worker):
//   → { id, type: "ensure-asr", payload: { model, webgpu } }
//   → { id, type: "transcribe", payload: { audio, language, wordTimestamps } }
//                                                             // audio buffer transferred
//   ← { type: "progress", key, payload }   // streamed model-download progress
//   ← { type: "chunk" }                     // streamed per-chunk ASR progress
//   ← { id, type: "done", result? }         // request finished
//   ← { id, type: "error", error }          // request failed

import { env, pipeline } from "@huggingface/transformers";

env.allowLocalModels = false;
// The Cache API only exists in secure contexts (https / localhost). When the
// page is served over plain http on a LAN address, `caches` is undefined and
// transformers.js would throw "Browser cache is not available…" — fall back
// to no caching (the model re-downloads per session) instead of failing.
env.useBrowserCache = typeof caches !== "undefined";

let recognizer: any = null;
let recognizerDevice: "webgpu" | "wasm" = "wasm";
let recognizerModel: string = "";

const post = (msg: any, transfer: Transferable[] = []) =>
  (self as any).postMessage(msg, transfer);

const progressCallback = (p: any) =>
  post({ type: "progress", key: "asr", payload: p });

async function loadRecognizer(model: string, device: "webgpu" | "wasm") {
  console.info(`[ASR] loading Whisper model on ${device.toUpperCase()}`);
  const options: any = {
    progress_callback: progressCallback,
    dtype: "fp32", // Force non-quantized model for compatibility
  };
  if (device === "webgpu") options.device = "webgpu";
  recognizer = await pipeline("automatic-speech-recognition", model, options);
  recognizerDevice = device;
  recognizerModel = model;
  console.info(`[ASR] Whisper model loaded successfully (${device})`);
}

async function ensureRecognizer(model: string, preferWebGPU: boolean) {
  if (recognizer && recognizerModel === model) return;
  recognizer = null;
  // WebGPU first (uses the GPU on NVIDIA/AMD/Intel/Apple alike); WASM (CPU)
  // as fallback. Some WebGPU stacks only fail at inference time — that case
  // is handled with a WASM retry in the transcribe handler below.
  if (preferWebGPU && (navigator as any).gpu) {
    try {
      await loadRecognizer(model, "webgpu");
      return;
    } catch (error) {
      console.warn("[ASR] WebGPU unavailable, falling back to WASM", error);
      recognizer = null;
    }
  }
  await loadRecognizer(model, "wasm");
}

async function runTranscription(payload: any) {
  return recognizer(payload.audio, {
    chunk_length_s: 30,
    stride_length_s: 5,
    return_timestamps: payload.wordTimestamps ? "word" : true,
    language: payload.language || null,
    chunk_callback: () => post({ type: "chunk" }),
  });
}

self.onmessage = async (event: MessageEvent) => {
  const { id, type, payload } = event.data || {};
  try {
    if (type === "ensure-asr") {
      await ensureRecognizer(payload.model, !!payload.webgpu);
      post({ id, type: "done" });
    } else if (type === "transcribe") {
      let output: any;
      try {
        output = await runTranscription(payload);
      } catch (error) {
        // WebGPU pipelines can load fine and still fail during inference
        // (driver/shader issues); rebuild on WASM and retry once.
        if (recognizerDevice !== "webgpu") throw error;
        console.warn("[ASR] WebGPU inference failed, retrying on WASM", error);
        await loadRecognizer(recognizerModel, "wasm");
        output = await runTranscription(payload);
      }
      post({ id, type: "done", result: output });
    } else {
      post({ id, type: "error", error: `Unknown message type: ${type}` });
    }
  } catch (err: any) {
    post({ id, type: "error", error: String(err?.message || err) });
  }
};
