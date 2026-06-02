export const ASR_MODEL = "Xenova/whisper-base"
export const TRANSLATION_MODEL = "Xenova/nllb-200-distilled-600M"

export const MARIAN_TRANSLATION_MODELS: Record<string, string> = {
  "en:es": "Xenova/opus-mt-en-es",
  "es:en": "Xenova/opus-mt-es-en",
  "en:fr": "Xenova/opus-mt-en-fr",
  "fr:en": "Xenova/opus-mt-fr-en",
  "en:de": "Xenova/opus-mt-en-de",
  "de:en": "Xenova/opus-mt-de-en",
  "en:pt": "Xenova/opus-mt-en-pt",
  "pt:en": "Xenova/opus-mt-pt-en",
  "en:it": "Xenova/opus-mt-en-it",
  "it:en": "Xenova/opus-mt-it-en",
  "en:nl": "Xenova/opus-mt-en-nl",
  "nl:en": "Xenova/opus-mt-nl-en",
  "en:ru": "Xenova/opus-mt-en-ru",
  "ru:en": "Xenova/opus-mt-ru-en",
}

export const LANGS = {
  en: { label: "English", nllb: "eng_Latn" },
  es: { label: "Spanish", nllb: "spa_Latn" },
  fr: { label: "French", nllb: "fra_Latn" },
  de: { label: "German", nllb: "deu_Latn" },
  pt: { label: "Portuguese", nllb: "por_Latn" },
  it: { label: "Italian", nllb: "ita_Latn" },
  nl: { label: "Dutch", nllb: "nld_Latn" },
  ru: { label: "Russian", nllb: "rus_Cyrl" },
  ja: { label: "Japanese", nllb: "jpn_Jpan" },
  ko: { label: "Korean", nllb: "kor_Hang" },
  zh: { label: "Chinese", nllb: "zho_Hans" },
  ar: { label: "Arabic", nllb: "arb_Arab" },
  hi: { label: "Hindi", nllb: "hin_Deva" },
  pl: { label: "Polish", nllb: "pol_Latn" },
  tr: { label: "Turkish", nllb: "tur_Latn" },
}
