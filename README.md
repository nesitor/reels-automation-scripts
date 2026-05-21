# Aspectados Reels — Automation pipeline

Automation que toma un guion de 16 clips, genera la imagen base de la
protagonista en **Nano Banana 2** (Gemini Image — vía Google AI Studio,
proxy compatible OpenAI, o pay-as-you-go), genera la primera imagen de
cada clip con la cara fija, recorta la marca de agua, encola los 16 clips
a **ComfyUI** (LTX 2.3 v1.1, local o remoto vía HTTP) y monta un preview
al final.

Cada etapa es **idempotente** y guarda su estado en **SQLite**, así que cualquier
script se puede reanudar tras Ctrl+C, kernel panic, corte de luz, lo que sea.

> 📚 **Otros docs en este folder:**
> - [`CLAUDE.md`](CLAUDE.md) — contexto operacional para agentes (reglas duras, schema, gotchas).
> - [`SCRIPTS.md`](SCRIPTS.md) — referencia completa de cada script + ejemplos por comando.
> - [`docs/TELEGRAM_SETUP.md`](docs/TELEGRAM_SETUP.md) — bot setup paso a paso.
> - [`docs/COMFYUI_MCP_OPTIONAL.md`](docs/COMFYUI_MCP_OPTIONAL.md) — por qué la pipeline no usa MCP y cómo añadir uno opcional.

---

## Qué hay en esta carpeta

```
automation/
├── pipeline.py                Orquestador (chains stages 2→6)
├── requirements.txt           Deps Python
├── .env.example               Plantilla de credenciales (copiar a .env)
├── db/
│   └── schema.sql             Tablas SQLite (idempotente)
├── lib/                       Módulos compartidos (no se ejecutan directos)
│   ├── config.py · db.py · logger.py
│   ├── telegram.py · image_utils.py
│   ├── nano_banana.py         Google GenAI wrapper (con soporte de proxy)
│   └── comfyui_client.py      Cliente HTTP-only para ComfyUI
├── scripts/                   Stages + utilidades
│   ├── init_db.py · import_guion.py
│   ├── stage1_protagonist.py · stage2_scene_images.py
│   ├── stage3_crop_watermark.py · stage4_queue_videos.py
│   ├── stage5_poll_videos.py · stage6_compile.py
│   ├── status.py · adopt_assets.py · redo_clips.py
│   ├── inspect_workflow.py · test_proxy_image.py
├── workflows/
│   ├── ltx_2.3_v1.1.json      Workflow ComfyUI exportado en API format
│   ├── node_map.json          Mapeo de IDs (instalación-específico)
│   └── node_map.example.json  Plantilla documentada
├── guion/
│   └── video<N>.v<M>.json     Guiones versionados (uno por vídeo)
└── outputs/                   Artefactos generados (gitignored)
    └── <video_id>/v<N>_<profile>/
        ├── images/            Frames raw de Nano Banana
        ├── images_cropped/    Frames sin watermark
        └── videos/            MP4s descargados de ComfyUI
```

---

## Migrar a un repo nuevo (checklist)

Cuando muevas esta carpeta a su propio repo:

```bash
# 1. Copia la carpeta a tu nueva ubicación
cp -r /Users/andresdiazmolins/code/ai/aspectados/automation/ ~/code/aspectados-reels/
cd ~/code/aspectados-reels

# 2. Inicializa git
git init && git add . && git commit -m "Initial commit"

# 3. (Opcional) decide qué arrastrar del estado actual:
#    - db/state.db*       → estado actual de tus runs (mantener si quieres continuar)
#    - images/video_2/    → carpeta manual con tus PNGs (ya cubierta por outputs/)
#    - outputs/           → artefactos ya generados (ya está gitignored)
#    - stage5.log         → log local del poller (ya está gitignored)
```

### En el repo nuevo, primera vez

```bash
# 4. Venv + deps
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 5. Credenciales (NO commitees .env)
cp .env.example .env
$EDITOR .env

# 6. DB
python scripts/init_db.py

# 7. ComfyUI workflow
#    Exporta desde ComfyUI: Settings → Enable Dev mode → "Save (API Format)"
#    Pon el JSON en workflows/ltx_2.3_v1.1.json
python scripts/inspect_workflow.py workflows/ltx_2.3_v1.1.json
cp workflows/node_map.example.json workflows/node_map.json
$EDITOR workflows/node_map.json   # rellena los IDs reales

# 8. Smoke tests
python -c "from lib import comfyui_client; print('comfyui reachable:', comfyui_client.ping())"
python scripts/test_proxy_image.py "test"

# 9. Listo. A producir vídeos:
python scripts/import_guion.py guion/video2.v1.json --profile preview
```

### Qué se exporta y qué no

| Categoría | En git | Por qué |
|---|---|---|
| Todo `lib/`, `scripts/`, `pipeline.py` | ✅ | Código fuente |
| `db/schema.sql` | ✅ | Definición del estado |
| `guion/*.json` | ✅ | Los guiones son el contrato creativo del proyecto |
| `workflows/ltx_2.3_v1.1.json` | ✅ | Tu workflow real importa para reproducibilidad |
| `workflows/node_map.json` | ✅ | Las IDs son específicas de tu workflow |
| `.env.example` | ✅ | Plantilla pública |
| `.env` | ❌ (gitignored) | Credenciales reales |
| `db/state.db*` | ❌ (gitignored) | Estado en runtime, no semántico |
| `outputs/` | ❌ (gitignored excepto `.gitkeep`) | Binarios pesados, regenerables |
| `*.log`, `*.pid`, `test_proxy.png` | ❌ (gitignored) | Cruft de ejecución |
| `images/` (carpeta manual) | depende | Si la usas como source-of-truth de tus imágenes externas, sí; si es un staging temporal, no |

---

## Setup (una sola vez)

> Si ya seguiste la sección "Migrar a un repo nuevo" arriba, esto es lo mismo
> resumido. Si no, empieza aquí.

```bash
cd automation

# 1. Crea un venv y dependencias
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Configura variables de entorno
cp .env.example .env
# edita .env y rellena al menos:
#   GOOGLE_AI_STUDIO_API_KEY  -> https://aistudio.google.com/apikey
#                                  (o el token de tu proxy)
#   GEMINI_IMAGE_MODEL        -> ej. gemini-2.5-flash-image o
#                                  gemini/nano-banana-pro-preview
#   GOOGLE_AI_BASE_URL        -> (opcional) URL de tu proxy OpenAI-compat
#   COMFYUI_HOST              -> http://127.0.0.1:8188 o URL remota
#   TELEGRAM_BOT_TOKEN        -> ver docs/TELEGRAM_SETUP.md
#   TELEGRAM_CHAT_ID          -> ver docs/TELEGRAM_SETUP.md

# 3. Inicializa la DB de estado (idempotente)
python scripts/init_db.py

# 4. Exporta tu workflow desde ComfyUI en "API format"
#    (Settings → Enable Dev mode → "Save (API Format)")
#    Guárdalo como: workflows/ltx_2.3_v1.1.json
#
# 5. Inspecciona el workflow para encontrar los IDs de nodos
python scripts/inspect_workflow.py workflows/ltx_2.3_v1.1.json

# 6. Copia el ejemplo de node_map y rellena con los IDs reales
cp workflows/node_map.example.json workflows/node_map.json
# La pipeline patchea estos 5 puntos del workflow en cada submission:
#   - prompt_positive:  CLIPTextEncode con el prompt positivo del clip
#   - prompt_negative:  CLIPTextEncode con el negativo (opcional)
#   - input_image:      LoadImage / LTXVImageToVideo con el frame de referencia
#   - seed:             sampler — ojo: key "seed" o "noise_seed" según tu nodo
#   - output_filename:  SaveVideo / VHS_VideoCombine — para localizar el MP4
```

### Formato del node_map

Acepta dos formas. Usa la corta cuando el input key sea el por defecto:

```json
{
  "prompt_positive": "6",
  "prompt_negative": "7",
  "input_image":     "12",
  "seed":            { "node_id": "25", "input": "noise_seed" },
  "output_filename": { "node_id": "37", "input": "filename_prefix" }
}
```

Defaults: `prompt_positive→text`, `prompt_negative→text`, `input_image→image`,
`seed→seed`, `output_filename→filename_prefix`. Si tu nodo usa otra key,
declara el objeto explícito.

Si NO quieres que la pipeline toque el negativo (porque tu workflow ya tiene
uno fijo que te gusta), borra `prompt_negative` del `node_map.json` y se
queda como esté en tu workflow.

---

## Flujo de uso para un vídeo (camino feliz)

Suponiendo que tu guion vive en `guion/video2.v1.json`:

```bash
# === Stage 1 — protagonista (manual, una vez por guion) ===
# Genera 6 variaciones de la cara base:
python scripts/stage1_protagonist.py \
       --video-id video2_decisiones_sin_conocerte \
       --variations 6

# Mira los PNG en outputs/protagonist/guion_*/ y elige el mejor índice (0..5).
# Aprueba esa variación:
python scripts/stage1_protagonist.py \
       --video-id video2_decisiones_sin_conocerte \
       --approve 3

# === Stages 2 → 4 — orquestados ===
# Importa el guion como run, genera imágenes, recorta watermark, encola vídeos:
python pipeline.py --guion guion/video2.v1.json --profile preview --notify

# El comando imprime el `run_id` (apúntalo).

# === Stage 5 — poller (largo, déjalo en background) ===
# 16 clips × 45 min = ~12h. Lánzalo así y vete a dormir:
nohup python scripts/stage5_poll_videos.py --run-id 1 --notify \
      &> stage5.log &

# Mientras corre puedes ver el estado en otra terminal:
python scripts/status.py --watch

# === Stage 6 — preview montado ===
# Cuando Telegram te avise que el run está completo:
python scripts/stage6_compile.py --run-id 1 --notify
```

---

## Ejecutar stages individuales / reanudar con assets pre-existentes

Tres formas de saltar trabajo ya hecho:

### A) Tienes imágenes o clips ya generados → "adóptalos" en la DB

`adopt_assets.py` registra archivos existentes como `done` para que los stages
siguientes los salten. La convención de nombres es flexible: el script busca
`clip` + número en cualquier parte del nombre (p. ej. `clip_01.png`,
`clip-3.mp4`, `Aspectados_clip07_v2.png`).

```bash
# Importa el guion primero para tener un run_id
python scripts/import_guion.py guion/video2.v1.json --profile preview
# → run_id=2

# Adopta imágenes ya generadas en otra sesión (todavía sin watermark cropping)
python scripts/adopt_assets.py --run-id 2 \
       --images-raw /Users/andres/old-images/video2/

# O imágenes ya recortadas (saltan el stage 3 también)
python scripts/adopt_assets.py --run-id 2 \
       --images-cropped /Users/andres/old-images-cropped/

# Adopta los 2-3 clips MP4 que ya tienes renderizados
python scripts/adopt_assets.py --run-id 2 \
       --videos /Users/andres/old-mp4s/

# Si los pones bajo outputs/<video>/v1_preview/{images,images_cropped,videos}/
# usa --auto y los pilla todos:
python scripts/adopt_assets.py --run-id 2 --auto

# Vista previa de lo que adoptaría, sin tocar la DB:
python scripts/adopt_assets.py --run-id 2 --auto --dry-run
```

Después, los stages restantes solo procesan lo que falta:

```bash
python pipeline.py --run-id 2 --watch --notify
```

### B) Ejecutar solo un subconjunto de stages

`pipeline.py` admite tres formas de elegir qué stages corren:

```bash
# Solo stages 3 y 4 sobre un run existente
python pipeline.py --run-id 2 --stages 3,4 --notify

# Desde el stage 4 hasta el final (4, 5, 6) sobre run existente, con polling
python pipeline.py --run-id 2 --from-stage 4 --watch --compile --notify

# Saltar el stage 2 (porque ya adoptaste imágenes)
python pipeline.py --run-id 2 --skip-stages 2 --notify
```

Recordatorio de stages:

| # | Script | Qué hace |
|---|---|---|
| 2 | `stage2_scene_images.py` | Genera primer frame por clip con Nano Banana 2 |
| 3 | `stage3_crop_watermark.py` | Recorta watermark y rescala a 9:16 |
| 4 | `stage4_queue_videos.py` | Encola los clips en ComfyUI (rápido) |
| 5 | `stage5_poll_videos.py` | Daemon — polling 12h hasta que terminan |
| 6 | `stage6_compile.py` | `ffmpeg concat` → preview.mp4 |

Por defecto `pipeline.py` corre **2, 3, 4** y se desconecta. Añade `--watch`
para incluir el 5, y `--compile` para el 6.

### C) Lanzar un stage suelto

Cada stage es ejecutable directamente y es idempotente:

```bash
python scripts/stage3_crop_watermark.py --run-id 2
python scripts/stage4_queue_videos.py --run-id 2 --retry --notify
python scripts/stage5_poll_videos.py --run-id 2 --once  # un solo poll y salir
```

---

## Diseño y por qué es idempotente

| Estado guardado | Dónde | Significado |
|---|---|---|
| Versión del guion | `guiones` (SQLite) | (video_id, version) único |
| Una ejecución | `runs` | una corrida = un (guion, profile) |
| Protagonista | `protagonists` | N variaciones, 1 aprobada |
| Por clip | `clips` | image_status + video_status independientes |
| Auditoría | `events` | append-only, debugging |
| Notifs Telegram | `notifications` | dedupe por event_key |

Cada script:
1. Lee el estado de SQLite.
2. Filtra solo lo que está `pending` o `failed` (y bajo el límite de intentos).
3. Actualiza el estado al final.

**Reanudar = relanzar el mismo comando.** Cualquier clip ya `done` se salta.

---

## Idempotencia + asíncrono: cómo encaja

- `stage4_queue_videos.py` **no espera** — envía los 16 jobs a ComfyUI y
  acaba en segundos. ComfyUI los procesa serie en su cola FIFO.
- `stage5_poll_videos.py` corre como **daemon independiente**, no necesita
  Claude Code ni tu terminal interactiva. Sobrevive a `nohup` o `screen`.
- Si el daemon muere, **vuelve a lanzarlo**: reconciliará el estado de la cola
  de ComfyUI con SQLite y seguirá donde lo dejó.
- Cada clip emite un ping de Telegram en cada transición terminal
  (`done`/`failed`) y hay un ping final cuando ya no queda nada en cola.

---

## Versionado del guion

Cada cambio mayor crea un nuevo archivo `guion/<video_id>.vN.json`. Como la
clave `(video_id, version)` es única en SQLite, importar v2 no machaca v1:
puedes mantener tomas antiguas en disco y comparar.

```
guion/
  ├── video2.v1.json    ← 16 clips, primera versión
  ├── video2.v2.json    ← reescrita con feedback A/B
  └── video3.v1.json    ← otro vídeo
```

---

## Recovery rápido

| Síntoma | Solución |
|---|---|
| Un clip de imagen falló | Vuelve a lanzar `stage2_scene_images.py --run-id N`. Solo reintenta los `failed` con `attempts < IMAGE_MAX_ATTEMPTS`. |
| El daemon de poll murió | `nohup python scripts/stage5_poll_videos.py --run-id N --notify &` |
| Un clip de vídeo falló en ComfyUI | `python scripts/stage4_queue_videos.py --run-id N --retry` lo reencola. |
| Quiero ver dónde estoy | `python scripts/status.py --run-id N --watch` |
| ComfyUI evictó un job (status=unknown) | Marca el clip a `pending` en SQLite y relanza stage4. |

---

## Documentos asociados

- [`CLAUDE.md`](CLAUDE.md) — contexto operacional para agentes (reglas duras, schema invariants, gotchas baked-in).
- [`SCRIPTS.md`](SCRIPTS.md) — referencia completa de **todos** los scripts con cada flag + ejemplos.
- [`docs/TELEGRAM_SETUP.md`](docs/TELEGRAM_SETUP.md) — cómo crear el bot y obtener `chat_id`.
- [`docs/COMFYUI_MCP_OPTIONAL.md`](docs/COMFYUI_MCP_OPTIONAL.md) — MCP opcional para inspección ad-hoc (la pipeline NO depende de él).
