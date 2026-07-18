// @ts-check
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'astro/config';

import tailwindcss from '@tailwindcss/vite';
import cloudflare from '@astrojs/cloudflare';
import sitemap from '@astrojs/sitemap';

// https://astro.build/config
export default defineConfig({
  // Production URL — required for absolute canonical/hreflang/OG URLs and the
  // sitemap. Update this if the site is served from a different domain.
  site: 'https://subvid.app',
  output: 'server',
  // The Cloudflare adapter targets the Workers runtime for edge middleware,
  // while the public pages are emitted as prerendered static assets.
  adapter: cloudflare(),
  i18n: {
    locales: ['en', 'es'],
    defaultLocale: 'es',
    routing: {
      prefixDefaultLocale: false
    }
  },
  integrations: [
    sitemap({
      i18n: {
        defaultLocale: 'es',
        locales: { en: 'en', es: 'es' }
      }
    })
  ],
  vite: {
    plugins: [tailwindcss()],
    optimizeDeps: {
      exclude: ['mediabunny']
    },
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url))
      }
    },
    worker: {
      format: 'es'
    }
  }
});