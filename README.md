# AI Reels Pipeline

Pipeline de automatización **end-to-end** para producir reels verticales (9:16)
generados con IA. A partir de un guion en JSON, genera la imagen base de la
protagonista, la primera imagen de cada clip con la cara fija, encola la
generación de vídeo en ComfyUI y monta un preview final.

Está pensado para **trabajos largos sin supervisión**: un vídeo de 16 clips
equivale a unas ~12 h de GPU en ComfyUI. Por eso cada etapa es **idempotente**
y **reanudable** — se puede matar y relanzar cualquier script y retoma
exactamente donde lo dejó.

---

## Tabla de contenidos

1. [Características](#características)
2. [Cómo funciona](#cómo-funciona)
3. [Estructura del repositorio](#estructura-del-repositorio)
4. [Requisitos previos](#requisitos-previos)
5. [Instalación y configuración](#instalación-y-configuración)
6. [Configurar el `node_map`](#configurar-el-node_map)
7. [Guía de uso — producir un vídeo](#guía-de-uso--producir-un-vídeo)
8. [Reanudar y reutilizar trabajo previo](#reanudar-y-reutilizar-trabajo-previo)
9. [Arquitectura e idempotencia](#arquitectura-e-idempotencia)
10. [Versionado de guiones](#versionado-de-guiones)
11. [Solución de problemas](#solución-de-problemas)
12. [Qué se versiona y qué no](#qué-se-versiona-y-qué-no)
13. [Documentación relacionada](#documentación-relacionada)

---

## Características

- **Idempotente y reanudable** — el estado vive en SQLite; relanzar un comando
  equivale a continuar. Lo que ya está hecho se salta.
- **Tolerante a fallos** — sobrevive a `Ctrl+C`, kernel panics o cortes de luz
  sin perder progreso.
- **ComfyUI por HTTP** — la instancia de ComfyUI puede ser local o remota; no
  se asume un sistema de archivos compartido.
- **Daemon de larga duración** — el poller de vídeo sobrevive al cierre de la
  terminal (`nohup` / `screen`).
- **Notificaciones por Telegram** — un ping en cada transición relevante, con
  deduplicación para no spamear al relanzar.
- **Generación de imágenes flexible** — Google AI Studio, un proxy compatible
  con OpenAI o el modo pay-as-you-go de Gemini.

---

## Cómo funciona

El pipeline encadena seis etapas. La primera es manual (eliges la cara de la
protagonista); el resto se orquestan con `pipeline.py`.

```
  guion JSON  ──import_guion.py──►  SQLite (db/state.db)
                                         │  estado único e idempotente
                                         ▼
   Stage 1 · Protagonista   Genera N variaciones de cara → apruebas 1
   Stage 2 · Imágenes       Primer frame de cada clip con la cara fija
   Stage 3 · Watermark      Recorta la marca de agua y rescala a 9:16
   Stage 4 · Encolar        Envía los 16 clips a la cola de ComfyUI
   Stage 5 · Poller         Daemon: espera y descarga los MP4 (~12 h)
   Stage 6 · Compilar       ffmpeg concat → preview.mp4
```

Modelos por defecto: **Nano Banana 2 / Gemini Image** para imágenes y
**LTX 2.3 v1.1** (vía ComfyUI) para el vídeo de cada clip.

---

## Estructura del repositorio

```
.
├── pipeline.py                Orquestador (encadena los stages 2→6)
├── requirements.txt           Dependencias de Python
├── .env.example               Plantilla de credenciales (copiar a .env)
├── db/
│   └── schema.sql             Tablas SQLite (CREATE … IF NOT EXISTS)
├── lib/                       Módulos compartidos (no se ejecutan directos)
│   ├── config.py · db.py · logger.py · paths.py
│   ├── telegram.py · image_utils.py
│   ├── nano_banana.py         Wrapper de Google GenAI (soporta proxy)
│   └── comfyui_client.py      Cliente HTTP-only para ComfyUI
├── scripts/                   Stages + utilidades (todos idempotentes)
│   ├── init_db.py · import_guion.py
│   ├── stage1_protagonist.py … stage6_compile.py
│   ├── status.py · adopt_assets.py · redo_clips.py
│   └── inspect_workflow.py · test_proxy_image.py
├── workflows/
│   ├── ltx_2.3_v1.1.json      Workflow de ComfyUI exportado en API format
│   ├── node_map.json          Mapeo de IDs de nodos (específico de tu setup)
│   └── node_map.example.json  Plantilla documentada
├── guion/                     Guiones en JSON (gitignored, contenido propio)
├── outputs/                   Artefactos generados (gitignored)
│   └── <video_id>/v<N>_<profile>/
│       ├── images/            Frames raw del generador de imágenes
│       ├── images_cropped/    Frames sin marca de agua
│       └── videos/            MP4 descargados de ComfyUI
└── docs/                      Guías complementarias
```

---

## Requisitos previos

| Requisito | Detalle |
|---|---|
| **Python 3.10+** | Con `venv` disponible. |
| **ffmpeg** | En el `PATH` (necesario para el stage 6). |
| **ComfyUI** | Una instancia accesible por HTTP (local o remota) con el workflow LTX 2.3 v1.1 cargado. |
| **Credenciales de imagen** | Una clave de Google AI Studio o el token de un proxy compatible. |
| **Bot de Telegram** | _Opcional._ Para recibir notificaciones — ver [`docs/TELEGRAM_SETUP.md`](docs/TELEGRAM_SETUP.md). |

---

## Instalación y configuración

Se hace **una sola vez** por máquina.

```bash
# 1. Entorno virtual y dependencias
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Credenciales — copia la plantilla y rellénala (NO commitees .env)
cp .env.example .env
$EDITOR .env

# 3. Inicializa la base de datos de estado (idempotente)
python scripts/init_db.py

# 4. Coloca tu workflow de ComfyUI exportado en "API format"
#    (en ComfyUI: Settings → Enable Dev mode → "Save (API Format)")
#    Guárdalo como: workflows/ltx_2.3_v1.1.json

# 5. Inspecciona el workflow para localizar los IDs de los nodos
python scripts/inspect_workflow.py workflows/ltx_2.3_v1.1.json

# 6. Crea tu node_map a partir de la plantilla y rellénalo
cp workflows/node_map.example.json workflows/node_map.json
$EDITOR workflows/node_map.json

# 7. Smoke tests
python -c "from lib import comfyui_client; print('comfyui reachable:', comfyui_client.ping())"
python scripts/test_proxy_image.py "una manzana sobre una mesa"
```

### Variables de `.env` mínimas

| Variable | Para qué sirve |
|---|---|
| `GOOGLE_AI_STUDIO_API_KEY` | Clave de Gemini ([aistudio.google.com/apikey](https://aistudio.google.com/apikey)) o el token de tu proxy. |
| `GEMINI_IMAGE_MODEL` | Modelo de imagen, p. ej. `gemini-2.5-flash-image`. |
| `GOOGLE_AI_BASE_URL` | _Opcional._ URL de un proxy compatible con OpenAI. |
| `COMFYUI_HOST` | `http://127.0.0.1:8188` (local) o la URL remota. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | _Opcional._ Notificaciones. |

> La plantilla [`.env.example`](.env.example) documenta **todas** las variables,
> sus valores por defecto y cuáles son opcionales.

---

## Configurar el `node_map`

El `node_map.json` traduce los **nombres lógicos** que el pipeline entiende a
los **IDs físicos** de los nodos de tu workflow exportado. El pipeline parchea
estos cinco puntos en cada envío a ComfyUI:

| Nombre lógico | Qué parchea |
|---|---|
| `prompt_positive` | `CLIPTextEncode` con el prompt positivo del clip. |
| `prompt_negative` | `CLIPTextEncode` con el negativo _(opcional)_. |
| `input_image` | `LoadImage` / `LTXVImageToVideo` con el frame de referencia. |
| `seed` | El sampler — la clave puede ser `seed` o `noise_seed`. |
| `output_filename` | `SaveVideo` / `VHS_VideoCombine` — para localizar el MP4. |

Acepta dos formas. Usa la corta cuando la clave de input sea la por defecto;
usa la explícita cuando tu nodo use otra clave:

```json
{
  "prompt_positive": "6",
  "prompt_negative": "7",
  "input_image":     "12",
  "seed":            { "node_id": "25", "input": "noise_seed" },
  "output_filename": { "node_id": "37", "input": "filename_prefix" }
}
```

Claves por defecto de la forma corta: `prompt_positive→text`,
`prompt_negative→text`, `input_image→image`, `seed→seed`,
`output_filename→filename_prefix`.

> Si **no** quieres que el pipeline toque el prompt negativo (porque tu
> workflow ya tiene uno fijo), elimina `prompt_negative` del `node_map.json` y
> se respetará el del workflow.

---

## Guía de uso — producir un vídeo

Suponiendo que tu guion está en `guion/video2.v1.json`:

### Stage 1 — Protagonista (manual, una vez por guion)

```bash
# Genera 6 variaciones de la cara base
python scripts/stage1_protagonist.py \
       --video-id video2_decisiones_sin_conocerte \
       --variations 6

# Revisa los PNG en outputs/protagonist/guion_*/ , elige el mejor (índice 0..5)
# y apruébalo:
python scripts/stage1_protagonist.py \
       --video-id video2_decisiones_sin_conocerte \
       --approve 3
```

### Stages 2 → 4 — Orquestados

```bash
# Importa el guion como run, genera imágenes, recorta watermark y encola vídeos
python pipeline.py --guion guion/video2.v1.json --profile preview --notify
```

El comando imprime el `run_id` — **apúntalo**, lo necesitas para el resto.

### Stage 5 — Poller (largo, en segundo plano)

16 clips × ~45 min ≈ ~12 h. Lánzalo como daemon y olvídate:

```bash
nohup python scripts/stage5_poll_videos.py --run-id 1 --notify &> stage5.log &
```

Mientras corre, consulta el estado en otra terminal:

```bash
python scripts/status.py --watch
```

### Stage 6 — Compilar el preview

Cuando Telegram avise de que el run está completo:

```bash
python scripts/stage6_compile.py --run-id 1 --notify
```

### Referencia de stages

| # | Script | Qué hace |
|---|---|---|
| 1 | `stage1_protagonist.py` | Genera variaciones de la cara base; apruebas una. |
| 2 | `stage2_scene_images.py` | Primer frame de cada clip con la cara fija. |
| 3 | `stage3_crop_watermark.py` | Recorta la marca de agua y rescala a 9:16. |
| 4 | `stage4_queue_videos.py` | Encola los clips en ComfyUI (rápido, no espera). |
| 5 | `stage5_poll_videos.py` | Daemon: poll de la cola y descarga de MP4 (~12 h). |
| 6 | `stage6_compile.py` | `ffmpeg concat` → `preview.mp4`. |

Por defecto `pipeline.py` ejecuta los stages **2, 3 y 4** y se desconecta.
Añade `--watch` para incluir el 5 y `--compile` para el 6.

---

## Reanudar y reutilizar trabajo previo

Hay tres formas de saltar trabajo ya hecho.

### A) Adoptar imágenes o clips ya generados

`adopt_assets.py` registra archivos existentes como `done` para que los stages
siguientes los salten. La detección de nombres es flexible: busca `clip` + un
número en cualquier parte del nombre (p. ej. `clip_01.png`, `clip-3.mp4`,
`MyVideo_clip07_v2.png`).

```bash
# Primero importa el guion para obtener un run_id
python scripts/import_guion.py guion/video2.v1.json --profile preview
# → run_id=2

# Imágenes raw generadas en otra sesión (aún sin recortar watermark)
python scripts/adopt_assets.py --run-id 2 --images-raw ~/old-images/video2/

# Imágenes ya recortadas (también se salta el stage 3)
python scripts/adopt_assets.py --run-id 2 --images-cropped ~/old-images-cropped/

# Clips MP4 ya renderizados
python scripts/adopt_assets.py --run-id 2 --videos ~/old-mp4s/

# Si los assets ya están bajo outputs/<video>/v1_preview/{images,...}/ usa --auto
python scripts/adopt_assets.py --run-id 2 --auto

# Vista previa de qué adoptaría, sin tocar la DB
python scripts/adopt_assets.py --run-id 2 --auto --dry-run
```

Después, los stages restantes solo procesan lo que falta:

```bash
python pipeline.py --run-id 2 --watch --notify
```

### B) Ejecutar solo un subconjunto de stages

```bash
# Solo los stages 3 y 4 sobre un run existente
python pipeline.py --run-id 2 --stages 3,4 --notify

# Desde el stage 4 hasta el final, con polling y compilado
python pipeline.py --run-id 2 --from-stage 4 --watch --compile --notify

# Saltar el stage 2 (porque ya adoptaste las imágenes)
python pipeline.py --run-id 2 --skip-stages 2 --notify
```

### C) Lanzar un stage suelto

Cada stage es ejecutable directamente y es idempotente:

```bash
python scripts/stage3_crop_watermark.py --run-id 2
python scripts/stage4_queue_videos.py --run-id 2 --retry --notify
python scripts/stage5_poll_videos.py --run-id 2 --once   # un solo poll y salir
```

> Para regenerar un clip que ya está `done`, usa `scripts/redo_clips.py`: resetea
> su estado y reencadena los stages. No edites la DB a mano.

---

## Arquitectura e idempotencia

El estado vive **únicamente en SQLite** (`db/state.db`). Los archivos en disco
(PNG, MP4) son artefactos derivados cuyas rutas se *registran* en la DB, pero la
DB es la que manda.

| Estado | Tabla | Significado |
|---|---|---|
| Versión del guion | `guiones` | `(video_id, version)` único. |
| Una ejecución | `runs` | Un run = un `(guion, profile)`. |
| Protagonista | `protagonists` | N variaciones, exactamente 1 aprobada. |
| Estado por clip | `clips` | `image_status` + `video_status` independientes. |
| Auditoría | `events` | Log append-only para depuración. |
| Notificaciones | `notifications` | Dedupe por `event_key`. |

Cada script sigue el mismo contrato:

1. Lee el estado desde SQLite.
2. Filtra solo lo que está `pending` o `failed` (y por debajo del límite de
   intentos).
3. Actualiza el estado al terminar (`done` / `failed`).

**Reanudar es relanzar el mismo comando.** Cualquier clip ya `done` se salta.

### Idempotencia + asíncrono

- `stage4_queue_videos.py` **no espera**: envía los 16 jobs a ComfyUI y termina
  en segundos. ComfyUI los procesa en serie en su cola FIFO.
- `stage5_poll_videos.py` corre como **daemon independiente** — no necesita esta
  terminal ni ninguna sesión interactiva. Sobrevive a `nohup` / `screen`.
- Si el daemon muere, **vuélvelo a lanzar**: reconcilia el estado de la cola de
  ComfyUI con SQLite y sigue donde lo dejó.

---

## Versionado de guiones

Cada cambio mayor crea un archivo nuevo `guion/<video_id>.v<N>.json`. Como la
clave `(video_id, version)` es única en SQLite, importar la v2 no machaca la v1:

```
guion/
  ├── video2.v1.json    ← 16 clips, primera versión
  ├── video2.v2.json    ← reescrita tras feedback
  └── video3.v1.json    ← otro vídeo
```

Convención: incrementa `version` en reescrituras de fondo; los retoques
cosméticos se quedan en la misma versión.

---

## Solución de problemas

| Síntoma | Solución |
|---|---|
| Una imagen de un clip falló | Relanza `stage2_scene_images.py --run-id N`. Solo reintenta los `failed` con `attempts < IMAGE_MAX_ATTEMPTS`. |
| El daemon del poller murió | `nohup python scripts/stage5_poll_videos.py --run-id N --notify &` |
| Un clip de vídeo falló en ComfyUI | `python scripts/stage4_queue_videos.py --run-id N --retry` lo reencola. |
| ¿Dónde voy? | `python scripts/status.py --run-id N --watch` |
| ComfyUI desalojó un job (`status=unknown`) | Marca el clip como `pending` y relanza el stage 4. |
| Regenerar un clip ya `done` | `python scripts/redo_clips.py` — resetea y reencadena. |

---

## Qué se versiona y qué no

| Categoría | En git | Por qué |
|---|---|---|
| `lib/`, `scripts/`, `pipeline.py` | ✅ | Código fuente. |
| `db/schema.sql` | ✅ | Definición del estado. |
| `workflows/ltx_2.3_v1.1.json` | ✅ | El workflow real importa para reproducibilidad. |
| `.env.example` | ✅ | Plantilla pública. |
| `.env` | ❌ | Credenciales reales. |
| `workflows/node_map.json` | ❌ | Los IDs son específicos de cada setup. |
| `guion/*.json` | ❌ | Contenido creativo propio; puede llevar datos sensibles. |
| `db/state.db*` | ❌ | Estado de runtime, no semántico. |
| `outputs/`, `imported_assets/` | ❌ | Binarios pesados y regenerables. |
| `*.log`, `*.pid`, `test_proxy.png` | ❌ | Artefactos de ejecución. |

> Lo marcado con ❌ está cubierto por [`.gitignore`](.gitignore).

---

## Documentación relacionada

- [`CLAUDE.md`](CLAUDE.md) — contexto operativo para agentes de IA: reglas duras,
  invariantes del schema y *gotchas* conocidos.
- [`SCRIPTS.md`](SCRIPTS.md) — referencia completa de **todos** los scripts, con
  cada flag y ejemplos por comando.
- [`docs/TELEGRAM_SETUP.md`](docs/TELEGRAM_SETUP.md) — cómo crear el bot y obtener
  el `chat_id`, paso a paso.
- [`docs/COMFYUI_MCP_OPTIONAL.md`](docs/COMFYUI_MCP_OPTIONAL.md) — por qué el
  pipeline no depende de MCP y cómo añadir uno opcional para inspección ad-hoc.
