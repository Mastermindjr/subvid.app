export const defaultLang = "es" as const

// Display names for the language switcher.
export const languages = {
  en: "English",
  es: "Español",
} as const

export type Lang = keyof typeof languages
