import { handle } from "@astrojs/cloudflare/handler"
import { defaultLang, languages } from "@/i18n/locales"

const LOCALES = Object.keys(languages)
const LOCALE_COOKIE = "locale"
const VARY = "Accept-Language, Cookie"

function validLocale(value: string | undefined) {
  return value && LOCALES.includes(value) ? value : undefined
}

function cookieValue(request: Request, name: string) {
  const cookie = request.headers.get("Cookie")
  if (!cookie) return undefined

  const prefix = `${name}=`
  const match = cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix))

  if (!match) return undefined

  try {
    return decodeURIComponent(match.slice(prefix.length))
  } catch {
    return match.slice(prefix.length)
  }
}

function preferredLocale(request: Request) {
  const header = request.headers.get("Accept-Language")
  if (!header) return undefined

  return header
    .split(",")
    .map((entry) => {
      const [range = "", qValue] = entry.trim().split(";q=")
      const locale = range.toLowerCase().split("-")[0]
      const quality = qValue ? Number.parseFloat(qValue) : 1

      return {
        locale: validLocale(locale),
        quality: Number.isFinite(quality) ? quality : 0,
      }
    })
    .filter((entry) => entry.locale && entry.quality > 0)
    .sort((a, b) => b.quality - a.quality)[0]?.locale
}

function localeRedirect(request: Request) {
  const chosen = validLocale(cookieValue(request, LOCALE_COOKIE))
  const target = chosen ?? preferredLocale(request)

  if (!target || target === defaultLang) return undefined

  const url = new URL(request.url)
  url.pathname = `/${target}/`
  url.search = ""

  return new Response(null, {
    status: 302,
    headers: {
      Location: url.toString(),
      "Cache-Control": "private, no-store",
      Vary: VARY,
      "X-Content-Type-Options": "nosniff",
      "Referrer-Policy": "strict-origin-when-cross-origin",
      "X-Frame-Options": "DENY",
      "Permissions-Policy":
        "camera=(), microphone=(), geolocation=(), payment=()",
    },
  })
}

export default {
  fetch(request, env, context) {
    const { pathname } = new URL(request.url)

    // Only redirect real page navigations. In-page fetches (e.g. the
    // client-side language switcher downloading a locale's HTML) must get
    // the page they asked for, regardless of the locale cookie.
    const fetchMode = request.headers.get("Sec-Fetch-Mode") ?? "navigate"
    if (pathname === "/" && fetchMode === "navigate") {
      const redirect = localeRedirect(request)
      if (redirect) return redirect
    }

    return handle(request, env, context)
  },
} satisfies ExportedHandler<Env>
