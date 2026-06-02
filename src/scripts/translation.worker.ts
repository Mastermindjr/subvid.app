// Dedicated Web Worker that hosts local transformers.js translation pipelines.
// It is created lazily so Whisper can be ready at startup without also loading
// translation models into the same worker.
//
// Protocol (main -> worker):
//   -> { id, type: "ensure-translation", payload: { backend, model, webgpu, requireWebGPU } }
//   -> { id, type: "translate", payload: { backend, texts, src?, tgt? } }
//   <- { type: "progress", key, payload }   // streamed model-download progress
//   <- { id, type: "done", result? }         // request finished
//   <- { id, type: "error", error }          // request failed

import { env, pipeline } from "@huggingface/transformers"

env.allowLocalModels = false
env.useBrowserCache = true

let translator: any = null
let translatorModel = ""
let translatorBackend: "marian" | "nllb" | "" = ""
let translatorDevice: "webgpu" | "default" = "default"

const post = (msg: any) => (self as any).postMessage(msg)

const progressCallback = (p: any) =>
  post({ type: "progress", key: "translation", payload: p })

async function createTranslator(
  backend: "marian" | "nllb",
  model: string,
  preferWebGPU: boolean,
  requireWebGPU: boolean,
) {
  const baseOptions = { progress_callback: progressCallback }
  if (preferWebGPU) {
    const webgpuAttempts =
      backend === "marian"
        ? [{ ...baseOptions, device: "webgpu" }]
        : [{ ...baseOptions, device: "webgpu" }]

    for (const options of webgpuAttempts) {
      try {
        console.info(`[translate] loading ${backend} on WebGPU`)
        const instance = await pipeline("translation", model, options)
        translatorDevice = "webgpu"
        return instance
      } catch (error) {
        console.warn(`[translate] WebGPU unavailable for ${backend}`, error)
      }
    }

    if (requireWebGPU)
      throw new Error(`WebGPU is required for ${backend} translation`)
  }

  console.info(`[translate] loading ${backend} on default backend`)
  translatorDevice = "default"
  return pipeline("translation", model, baseOptions)
}

async function ensureTranslator(
  backend: "marian" | "nllb",
  model: string,
  preferWebGPU: boolean,
  requireWebGPU: boolean,
) {
  if (translator && translatorBackend === backend && translatorModel === model) return
  translator = await createTranslator(backend, model, preferWebGPU, requireWebGPU)
  translatorBackend = backend
  translatorModel = model
}

async function runTranslation(payload: any) {
  if (!translator) throw new Error("Translation model is not loaded")
  const options =
    payload.src && payload.tgt
      ? {
          src_lang: payload.src,
          tgt_lang: payload.tgt,
          ...(payload.generation || {}),
        }
      : payload.generation || undefined
  return options ? translator(payload.texts, options) : translator(payload.texts)
}

self.onmessage = async (event: MessageEvent) => {
  const { id, type, payload } = event.data || {}
  try {
    if (type === "ensure-translation") {
      await ensureTranslator(
        payload.backend || "nllb",
        payload.model,
        !!payload.webgpu,
        !!payload.requireWebGPU,
      )
      post({ id, type: "done" })
    } else if (type === "translate") {
      let result
      try {
        result = await runTranslation(payload)
      } catch (error) {
        if (
          translatorDevice !== "webgpu" ||
          translatorBackend !== "nllb" ||
          !translatorModel
        ) {
          throw error
        }
        console.warn("[translate] WebGPU NLLB failed, retrying on default backend", error)
        const model = translatorModel
        translator = null
        await ensureTranslator("nllb", model, false, false)
        result = await runTranslation(payload)
      }
      post({ id, type: "done", result })
    } else {
      post({ id, type: "error", error: `Unknown message type: ${type}` })
    }
  } catch (err: any) {
    post({ id, type: "error", error: String(err?.message || err) })
  }
}
