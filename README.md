<div align="center">

# subtitulos

**Genera subtítulos con IA y añádelos a tus vídeos como pista activable — desde el navegador o por lotes con la línea de comandos.**

Fork de [subvid.app](https://github.com/midudev/subvid.app) (de midudev) orientado a bibliotecas multimedia
(Jellyfin, Plex…): sin quemar subtítulos, sin recodificar, sin subir archivos a ningún servidor.

</div>

## Dos formas de usarlo

| | 🌐 Web (interfaz gráfica) | ⚡ CLI (`cli/subvid_batch.py`) |
| --- | --- | --- |
| **Ideal para** | Un vídeo puntual, editar/traducir/estilizar subtítulos | Procesar carpetas enteras para tu biblioteca |
| **Transcripción** | Whisper *base* en el navegador (WASM/CPU) | faster-whisper hasta *large-v3* con **GPU NVIDIA** (CUDA) o CPU |
| **Velocidad** | Lenta (≈ tiempo real o menos) | 10–60× tiempo real con GPU |
| **Lotes / paralelo** | No (un archivo por sesión) | Sí (`-j N` archivos en paralelo, escaneo recursivo) |
| **Editor visual** | Sí: timeline, undo/redo, estilos, multipista | No |
| **Traducción** | Sí (NLLB-200 / Marian, en el navegador) | Solo a inglés (`--task translate`) |
| **Salidas** | `.srt` · **MKV con pista activable** · MP4/WebM quemados | `.srt` sidecar · **MKV/MP4 con pista activable** |
| **Privacidad** | Todo local (los modelos se descargan al navegador) | Todo local |

En ambos casos la opción de "pista activable" (*soft subs*) usa un remux de ffmpeg con *stream copy*:
el vídeo se copia bit a bit **sin recodificar** — tarda segundos, no pierde calidad y los subtítulos
se activan/desactivan desde el reproductor.

> **✨ Modo integrado (recomendado):** arranca el **motor local** (`python cli/subvid_server.py`)
> y abre la web. La web lo detecta automáticamente y combina lo mejor de ambos: la interfaz y el
> editor del navegador con la velocidad GPU de faster-whisper, más un panel de **procesado por
> lotes** con progreso por archivo. Ver [Motor local](#-motor-local-ui-integrada).

---

## 🌐 Aplicación web

SPA estática (Astro) que corre 100 % en tu navegador: transcribe con Whisper vía
[transformers.js](https://huggingface.co/docs/transformers.js), traduce con NLLB-200 y te deja
editar los subtítulos en un timeline antes de exportar.

### Arranque

```sh
pnpm install     # (o npx pnpm install)
pnpm dev         # http://localhost:4321
```

Requisitos: Node.js ≥ 22.12 y un navegador Chromium moderno (o Firefox). Sin variables de
entorno ni servicios externos; los modelos (~300 MB la primera vez) se cachean en IndexedDB.

### Flujo

1. **Sube** un vídeo o audio (MP4, MOV, WebM, MKV, MP3, WAV, OGG) — no sale de tu equipo.
2. **Configura** idioma de audio (o autodetección) y de subtítulos.
3. **Genera** — Whisper transcribe; NLLB traduce si hace falta.
4. **Edita** texto, tiempos y estilo en el timeline (undo/redo, multipista).
5. **Exporta**:
   - `.srt` — instantáneo. Nómbralo `pelicula.es.srt` junto al vídeo y Jellyfin lo detecta solo.
   - **MKV · subtítulos seleccionables** — remux sin recodificar, en segundos. Incluye todas las
     pistas de idioma visibles como streams con su etiqueta de idioma. *(Recomendado)*
   - MP4/WebM con subtítulos **quemados** — re-codifica el vídeo completo (lento); solo si
     necesitas que el texto forme parte de la imagen.

### Comandos

| Comando | Descripción |
| --- | --- |
| `pnpm dev` | Servidor de desarrollo en `localhost:4321` |
| `pnpm build` | Build de producción en `./dist/` |
| `pnpm preview` | Previsualizar el build |
| `pnpm deploy` | Build + deploy a Cloudflare Workers |

---

## ✨ Motor local (UI integrada)

`cli/subvid_server.py` es un servidor ligero (FastAPI, solo en `127.0.0.1`) que expone el motor
faster-whisper a la web. Con él corriendo, la web deja de ser "la opción lenta":

```sh
cd cli
.venv\Scripts\activate
python subvid_server.py        # http://127.0.0.1:8787
```

Después abre la web (`pnpm dev`, o directamente `http://127.0.0.1:8787` si has hecho
`pnpm build`, porque el servidor también sirve la web compilada). La web detecta el motor y:

- **Flujo de un archivo**: en la pantalla de configuración aparece "Usar motor local (GPU)"
  (activado por defecto). La transcripción corre en tu GPU con `large-v3-turbo` en vez del
  Whisper *base* del navegador — mucho más rápida y precisa — y el editor, la traducción a
  cualquier idioma (NLLB en el navegador) y las exportaciones siguen funcionando igual.
- **Procesado por lotes**: botón "Procesar en lote con el motor local" en la pantalla inicial.
  Se abre un panel donde navegas por las carpetas *del servidor* (sin subir nada), montas una
  cola de carpetas y/o archivos, eliges modo/modelo/idioma/VAD… y ves **una barra de progreso
  por archivo** (transcribiendo → muxeando → hecho), con botón de cancelar que detiene el
  trabajo a mitad de transcripción.
- Si el motor no está corriendo, la web funciona como siempre (todo en el navegador).

---

## ⚡ CLI por lotes

`cli/subvid_batch.py` transcribe con [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
(CUDA si tienes GPU NVIDIA; CPU int8 si no) y añade los subtítulos como pista seleccionable
mediante remux sin recodificar. Pensado para pasarle carpetas enteras de tu biblioteca.

### Instalación

```sh
cd cli
python -m venv .venv
.venv\Scripts\activate          # Windows (en Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
```

Requisitos: Python ≥ 3.10 y [ffmpeg](https://ffmpeg.org) en el PATH (`winget install Gyan.FFmpeg`).
Las ruedas CUDA (cuBLAS/cuDNN) vienen en `requirements.txt`; si no hay GPU se usa CPU automáticamente.

### Uso

```sh
# Carpeta completa (recursivo): genera un .srt sidecar por vídeo
# (modo "sidecar" por defecto; Jellyfin los detecta solo y no se
# reescribe ningún vídeo — importante en discos SMR)
python subvid_batch.py "D:\Series\MiSerie"

# Además del .srt, crear la versión con pista de subtítulos incrustada
python subvid_batch.py "D:\Pelis" --mode both

# Idioma forzado, 3 archivos en paralelo, salida a otra carpeta
python subvid_batch.py "D:\Pelis" -l es -j 3 -o "D:\Salida"

# Máxima precisión (más lento): modelo large-v3
python subvid_batch.py video.mkv -m large-v3
```

### Opciones

| Opción | Por defecto | Descripción |
| --- | --- | --- |
| `--mode mux\|sidecar\|both` | `sidecar` | `.srt` externo (recomendado para Jellyfin), pista incrustada, o ambos |
| `--to LANGS` | — | Idiomas extra en una sola pasada (`--to es,en`): traducción **local** con NLLB-200 en GPU; cada idioma sale como pista propia + `.srt`. La pista del idioma original siempre se incluye |
| `--audio-track N` | `0` | Qué pista de audio transcribir si el vídeo tiene varias (doblajes). Por defecto la primera |
| `-m, --model` | `large-v3-turbo` | Modelo Whisper: `tiny`/`base`/`small`/`medium`/`large-v3`/`large-v3-turbo` |
| `-l, --language` | autodetección | Idioma del audio (`es`, `en`, …) |
| `-j, --jobs` | `1` | Archivos procesados en paralelo (con una sola GPU, más de 1 no acelera: comparten la misma cola CUDA y congelan el escritorio) |
| `--container auto\|mkv\|mp4` | `auto` | `auto` conserva el contenedor original (MP4/MOV → MP4 con `mov_text`; el resto → MKV con SRT) |
| `--device auto\|cuda\|cpu` | `auto` | Dispositivo de inferencia |
| `--task transcribe\|translate` | `transcribe` | `translate` genera los subtítulos en inglés |
| `--default-track` | desactivado | Marca la pista como activa por defecto al reproducir |
| `--no-vad` | — | Fuerza el filtro de voz apagado. El VAD viene **desactivado por defecto** (`config.toml`): incluso con alta sensibilidad se saltaba voz sobre música; actívalo en `[vad]` si aparecen subtítulos "fantasma" en silencios |
| `--overwrite` | desactivado | Regenera aunque la salida exista (si no, se salta lo ya procesado) |
| `--no-recursive` | desactivado | No escanear subcarpetas |

### Comportamiento

- **Nunca toca el original**: si la salida coincidiría con el archivo de entrada, se llama
  `<nombre>.subs.<ext>`.
- **Re-ejecutable**: los archivos ya procesados (salida existente, sidecar presente o vídeo que
  ya lleva pista de subtítulos) se saltan; usa `--overwrite` para forzar.
- **Subtítulos con timing por palabra**: las líneas se cortan en los silencios y nunca se quedan
  colgadas en pantalla durante pausas largas.
- La pista muxeada lleva metadatos de idioma (`spa`, `eng`…) y va **desactivada por defecto**:
  se enciende desde Jellyfin/VLC (o usa `--default-track`).

### Primera ejecución y avisos

- La primera vez se descarga el modelo de Hugging Face (~1,6 GB para `large-v3-turbo`) con
  barras de progreso; queda cacheado en `~/.cache/huggingface` y las siguientes ejecuciones
  arrancan al instante.
- El aviso `You are sending unauthenticated requests to the HF Hub` es **informativo** y solo
  aparece durante descargas: sin token hay límites de velocidad más bajos. Si quieres, crea un
  token gratuito en [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) y
  expórtalo como `HF_TOKEN`.

### Subtítulos en varios idiomas (una sola pasada)

```sh
# Pista original + español + inglés, todo en el mismo MKV/MP4 y como .srt
python subvid_batch.py "D:\Series\MiSerie" --to es,en
```

La traducción corre **en local y en GPU** con NLLB-200 sobre CTranslate2 (el mismo runtime
que Whisper; sin PyTorch). El modelo se descarga la primera vez y se configura en
`config.toml → [translation]`:

| Modelo NLLB (CTranslate2) | Descarga | Calidad |
| --- | --- | --- |
| `…distilled-600M-ct2-float16` | ~1,2 GB | Buena, muy rápida |
| `…distilled-1.3B-ct2-float16` | ~2,6 GB | Muy buena — **por defecto** |
| `…3.3B-ct2-float16` | ~6,7 GB | La mejor local |

30 idiomas soportados (es, en, fr, de, pt, it, ja, zh, …). Nota: `--to en` da mejor
traducción al inglés que `--task translate` (la traducción integrada de Whisper está
degradada en el modelo *turbo*).

### Vídeos con varias pistas de audio

Whisper solo "oye" **una** pista: por defecto la primera del contenedor. Si un vídeo
tiene varios doblajes (p. ej. japonés + castellano), los subtítulos saldrán del idioma
de esa primera pista. Con `--audio-track 1` (2, …) se transcribe otra pista (se extrae
con ffmpeg antes de transcribir). El remux final conserva siempre **todas** las pistas
de audio del original.

### ¿Qué modelo elegir?

| Modelo | VRAM aprox. | Velocidad (RTX 3080 Ti) | Calidad |
| --- | --- | --- | --- |
| `large-v3` | ~10 GB (fp16) | ~8–15× tiempo real | La mejor disponible |
| `large-v3-turbo` | ~6 GB | ~20–40× tiempo real | ≈ large-v3 (mínima pérdida) — **recomendado** |
| `medium` / `small` | 2–5 GB | Más rápido | Buena para audio limpio |
| `tiny` / `base` | <1 GB | Muy rápido | Solo pruebas |

---

## Stack técnico

| Capa | Tecnología |
| --- | --- |
| Web | [Astro 6](https://astro.build) + Tailwind CSS 4, deploy en Cloudflare Workers |
| ASR (web) | Whisper base vía [@huggingface/transformers](https://www.npmjs.com/package/@huggingface/transformers) en un Web Worker |
| Traducción (web) | NLLB-200 / Opus-MT (transformers.js) |
| Audio/mux (web) | [@ffmpeg/ffmpeg](https://ffmpegwasm.netlify.app) (WASM) |
| Export quemado (web) | [mediabunny](https://www.npmjs.com/package/mediabunny) + WebCodecs |
| ASR (CLI) | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2, CUDA/CPU) |
| Mux (CLI) | ffmpeg nativo (*stream copy*) |

## Estructura

```text
cli/subvid_batch.py   # CLI por lotes (transcripción GPU + mux)
cli/subvid_server.py  # Motor local: integra el pipeline con la web (batch + GPU)
cli/subvid_gui.py     # GUI de escritorio alternativa (tkinter)
cli/config.toml       # Configuración central (modelo, VAD, líneas, …)
src/
├── components/       # UI Astro (upload, config, editor, modal de exportación…)
├── i18n/ui.ts        # Traducciones (en, es)
├── pages/            # Rutas: / (en), /es/
├── scripts/
│   ├── app.ts                 # Lógica principal del cliente
│   ├── transcriber.worker.ts  # Web Worker de Whisper
│   ├── translation.worker.ts  # Web Worker de traducción
│   ├── media/audio.ts         # ffmpeg.wasm: extracción de audio y mux
│   └── export/                # Exportación de vídeo (mux / WebCodecs / recorder)
└── styles/
```

## Créditos y licencia

Proyecto original: [subvid.app](https://github.com/midudev/subvid.app) de [midudev](https://midu.dev).
Licencia PolyForm-Noncommercial-1.0.0 (ver `LICENSE`).
