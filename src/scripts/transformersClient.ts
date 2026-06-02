type PendingRequest = {
  resolve: (value: unknown) => void
  reject: (reason?: unknown) => void
  target: WorkerTarget
}

type TransformersClientOptions = {
  onProgress?: (key: string, payload: unknown) => void
}

type WorkerTarget = "asr" | "translation"

export function createTransformersClient(options: TransformersClientOptions = {}) {
  const asrWorker = new Worker(new URL("./transcriber.worker.ts", import.meta.url), {
    type: "module",
  })
  let translationWorker: Worker | null = null
  const pending = new Map<number, PendingRequest>()
  let reqId = 0
  let onChunk: (() => void) | null = null

  function rejectPendingForTarget(target: WorkerTarget, reason: unknown) {
    for (const [id, request] of pending) {
      if (request.target !== target) continue
      pending.delete(id)
      request.reject(reason)
    }
  }

  function wireWorker(worker: Worker, target: WorkerTarget) {
    worker.onmessage = (event) => {
      const { id, type } = event.data || {}
      if (type === "progress") {
        options.onProgress?.(event.data.key, event.data.payload)
        return
      }
      if (type === "chunk") {
        onChunk?.()
        return
      }
      const request = pending.get(id)
      if (!request) return
      pending.delete(id)
      if (type === "error") request.reject(new Error(event.data.error))
      else request.resolve(event.data.result)
    }

    worker.onerror = (event) => {
      rejectPendingForTarget(target, event.error || new Error(event.message))
      if (target === "translation") translationWorker = null
    }
  }

  function getTranslationWorker() {
    if (!translationWorker) {
      translationWorker = new Worker(
        new URL("./translation.worker.ts", import.meta.url),
        { type: "module" },
      )
      wireWorker(translationWorker, "translation")
    }
    return translationWorker
  }

  function workerForCall(type: string) {
    if (type === "ensure-asr" || type === "transcribe") {
      return { worker: asrWorker, target: "asr" as const }
    }
    if (type === "ensure-translation" || type === "translate") {
      return { worker: getTranslationWorker(), target: "translation" as const }
    }
    throw new Error(`Unknown transformers call type: ${type}`)
  }

  wireWorker(asrWorker, "asr")

  return {
    call(type: string, payload?: unknown, transfer: Transferable[] = []) {
      const { worker, target } = workerForCall(type)
      const id = ++reqId
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject, target })
        try {
          worker.postMessage({ id, type, payload }, transfer)
        } catch (error) {
          pending.delete(id)
          reject(error)
        }
      })
    },
    setChunkHandler(handler: (() => void) | null) {
      onChunk = handler
    },
  }
}
