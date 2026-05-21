# CLAUDE.md — AI Reels Pipeline

Context for AI coding agents (and humans) operating inside this folder. Read
this first before changing anything. Cross-reference [SCRIPTS.md](SCRIPTS.md)
for command-line usage examples.

---

## What this folder does

End-to-end automation for AI-generated vertical reels:

```
guion JSON  →  Nano Banana 2 (protagonist + per-clip first frames)
            →  watermark crop
            →  ComfyUI / LTX 2.3 v1.1 (5s vertical video + audio per clip)
            →  preview.mp4 via ffmpeg concat
```

Designed for **long-running jobs** (16 clips × ~45 min/clip in ComfyUI = ~12h
of GPU time per video) without babysitting. Everything is **idempotent** and
**resumable** — any stage can be killed and re-launched and it picks up where
it left off.

## High-level architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  guion/video<N>.v<M>.json                                       │
│  ├── protagonist.prompt          (face base reference)          │
│  ├── defaults.video_negative_prompt                             │
│  └── clips[]                                                    │
│      ├── image_prompt, video_prompt                             │
│      ├── use_protagonist_reference (per-clip face-ref opt-out)  │
│      └── …                                                      │
└──────────────┬──────────────────────────────────────────────────┘
               │ import_guion.py
               ▼
┌─────────────────────────────────────────────────────────────────┐
│  db/state.db (SQLite WAL)                                       │
│  ├── guiones  (video_id, version) UNIQUE                        │
│  ├── runs     (guion_id, profile=preview|final, status)         │
│  ├── protagonists  (variations, approved=1 on one)              │
│  ├── clips    (per-clip image_status + video_status)            │
│  ├── events   (append-only audit log)                           │
│  └── notifications (Telegram dedupe)                            │
└──────────────┬──────────────────────────────────────────────────┘
               │ stage1..6 (each reads/updates state)
               ▼
┌─────────────────────────────────────────────────────────────────┐
│  outputs/<video_id>/v<version>_<profile>/                       │
│  ├── images/clip_NN.png             (raw NB2 output)            │
│  ├── images_cropped/clip_NN_cropped.png  (watermark stripped)   │
│  ├── videos/clip_NN.mp4             (downloaded from ComfyUI)   │
│  └── preview.mp4                    (ffmpeg concat of all 16)   │
└─────────────────────────────────────────────────────────────────┘
```

## Folder layout

```
automation/
├── README.md                       Usage walkthrough (human-friendly)
├── CLAUDE.md                       This file
├── SCRIPTS.md                      Command reference
├── .env.example                    Template for credentials + paths
├── requirements.txt                Python deps
├── pipeline.py                     Orchestrator (chains stages)
├── db/
│   └── schema.sql                  Idempotent CREATE TABLE IF NOT EXISTS …
├── lib/                            Shared modules — never run directly
│   ├── config.py                   .env loader → CFG dataclass (frozen)
│   ├── paths.py                    rel()/resolve() — project-relative path I/O
│   ├── db.py                       connect() context manager, init_schema()
│   ├── logger.py                   rich logging + event() → events table
│   ├── telegram.py                 Bot wrapper, dedupes by event_key SHA1
│   ├── image_utils.py              crop_watermark() + rescale to 9:16
│   ├── nano_banana.py              Google GenAI SDK wrapper w/ proxy support
│   └── comfyui_client.py           HTTP-only client (upload/prompt/view)
├── scripts/                        Stage entrypoints, all idempotent
│   ├── init_db.py                  Run schema.sql against state.db
│   ├── import_guion.py             JSON → DB row in runs + clips
│   ├── stage1_protagonist.py       Generate N face variations, --approve one
│   ├── stage2_scene_images.py      Per-clip first frame with face-ref
│   ├── stage3_crop_watermark.py    Crop bottom 100px and rescale
│   ├── stage4_queue_videos.py      Submit to ComfyUI queue
│   ├── stage5_poll_videos.py       Long-running daemon: poll + download MP4s
│   ├── stage6_compile.py           ffmpeg concat → preview.mp4
│   ├── status.py                   Rich dashboard --watch
│   ├── adopt_assets.py             Register pre-existing PNGs/MP4s as done
│   ├── redo_clips.py               Reset state + re-chain stages for chosen clips
│   ├── inspect_workflow.py         Print ComfyUI workflow node IDs
│   └── test_proxy_image.py         Smoke-test an OpenAI-compat proxy
├── workflows/
│   ├── ltx_2.3_v1.1.json           Exported ComfyUI workflow (API format)
│   ├── node_map.json               Maps logical names → node IDs
│   └── node_map.example.json       Documented template for node_map.json
├── guion/
│   ├── video2.v1.json              First reel — single redhead protagonist
│   └── video3.v1.json              Second reel — mixed speakers + b-roll
├── outputs/                        gitignored (only .gitkeep tracked)
├── imported_assets/                gitignored — adopt_assets.py copies
│                                   external assets here, by run_id
└── docs/
    ├── TELEGRAM_SETUP.md           Bot + chat_id walkthrough
    └── COMFYUI_MCP_OPTIONAL.md     Why MCP isn't in the pipeline + how to add it
```

---

## Hard rules — DO NOT violate these

1. **State lives in SQLite only.** Never store pipeline state in flat files
   the scripts depend on. The single source of truth is `db/state.db`. Files
   on disk (PNGs, MP4s) are derived artefacts whose paths are *recorded* in
   the DB, but the DB drives behaviour. Those paths are stored **relative to
   the project root** (see `lib/paths.py`) so the whole folder can be moved
   without breaking — producers call `paths.rel()` before writing a path,
   consumers call `paths.resolve()` after reading one.

2. **Every stage is idempotent.** When adding new stages or modifying
   existing ones, the contract is: `WHERE status='pending'` (or `failed` if
   `--retry`), `UPDATE … 'done'` on success, `UPDATE … 'failed'` on hard
   error. Re-running must skip what's done and only act on the rest.

3. **No filesystem assumption about ComfyUI.** `lib/comfyui_client.py` talks
   to ComfyUI over HTTP only (`POST /upload/image`, `POST /prompt`,
   `GET /queue`, `GET /history`, `GET /view`). This is deliberate — ComfyUI
   can be on a different machine. **Never reintroduce `COMFYUI_INPUT_DIR` or
   `COMFYUI_OUTPUT_DIR` filesystem paths.**

4. **The poller daemon (`stage5_poll_videos.py`) must survive the agent.**
   It runs for ~12h with `nohup`. Never make it depend on an MCP, Claude Code
   session, or interactive process. SIGINT/SIGTERM clean shutdown via the
   `_should_stop` flag is the contract.

5. **Telegram notifications dedupe by `event_key`.** Idempotent re-runs must
   not spam the user. The `notifications` table holds a SHA1 hash of the
   logical event identity. New notification points must follow the
   `telegram.send((parts,...), text)` pattern, never bypass it.

---

## DB schema invariants

| Table | Key | Invariant |
|---|---|---|
| `guiones` | `(video_id, version)` UNIQUE | Reimport of the same JSON is a no-op on this row |
| `runs` | `(guion_id, profile)` not unique | One guion can have many runs (preview, final, retomas) |
| `protagonists` | `approved=1` on **at most one** row per `guion_id` | Enforced by `stage1_protagonist.py::_approve` which zeroes others first |
| `clips` | `(run_id, clip_order)` UNIQUE | A run always has the same N clips, in order |
| `events` | append-only | Never UPDATE or DELETE rows here |
| `notifications` | `event_key` UNIQUE | One delivery per logical event, ever |

### Per-clip state machine

```
image_status:  pending → generating → done    (failed on hard error)
video_status:  pending → queued → running → done    (failed on hard error)
```

`done` is terminal unless explicitly reset (see `scripts/redo_clips.py`).
`stage5_poll_videos.py` may transition `queued → running` mid-flight when it
sees ComfyUI started the job, and it may transition `done → running` back if
the history says done but the MP4 isn't downloadable yet — that's by design
(retries the download next poll cycle).

---

## node_map.json — three accepted forms

`workflows/node_map.json` maps **logical names** the pipeline understands
(`prompt_positive`, `prompt_negative`, `input_image`, `seed`,
`output_filename`) to **physical node IDs** in your exported ComfyUI
workflow. Three forms:

```json
{
  "prompt_positive": "2483",                                ← shorthand
  "input_image": { "node_id": "2004", "input": "image" },   ← explicit input key
  "seed": [                                                  ← list (all get same value)
    { "node_id": "4832", "input": "noise_seed" },
    { "node_id": "4967", "input": "noise_seed" }
  ]
}
```

- Shorthand uses the default input key per logical name
  (`text`/`text`/`image`/`seed`/`filename_prefix`).
- Explicit form overrides the input key (use this when your sampler uses
  `noise_seed` instead of `seed`).
- List form patches the same value into multiple nodes — necessary for LTX
  2.3 v1.1 which has TWO RandomNoise nodes (first-pass + upscale-pass).

`prompt_negative` is OPTIONAL. Remove it from `node_map.json` if you want
the workflow's hardcoded negative to win.

---

## Guion JSON structure (canonical)

```json
{
  "video_id": "videoN_short_slug",
  "version": 1,
  "title": "…",
  "duration_target_s": 60,
  "notes": "…",

  "defaults": {
    "video_negative_prompt": "…"     ← falls back here if a clip omits it
  },

  "protagonist": {
    "id": "…",
    "prompt": "…"                    ← used for the base reference image
  },

  "clips": [
    {
      "order": 1,                    ← matches clip_order in DB
      "section": "HOOK",
      "beat": "1/4",
      "dialogue": "…",
      "scene_summary": "…",
      "use_protagonist_reference": true,   ← false for b-roll / non-protagonist speakers
      "image_prompt": "…",
      "video_prompt": "…",
      "video_prompt_negative": "…"   ← optional per-clip override
    },
    …16 total
  ]
}
```

Convention: 16 clips × 5s = ~80s of raw material, edited down to ~60s in
post. Filename versioning: `<video_id>.v<N>.json`. Increment `version` on
material rewrites; cosmetic edits stay on the same version.

---

## Environment variables (.env)

See `.env.example` for the full template. Categories:

| Var | What |
|---|---|
| `GOOGLE_AI_STUDIO_API_KEY` | Gemini key OR whatever your proxy expects |
| `GEMINI_IMAGE_MODEL` | e.g. `gemini-2.5-flash-image`, `gemini/nano-banana-pro-preview` |
| `GOOGLE_AI_BASE_URL` | (optional) Custom endpoint — e.g. an OpenAI-compat proxy mirror |
| `GOOGLE_AI_EXTRA_HEADERS` | (optional) JSON-object string with extra headers |
| `COMFYUI_HOST` | `http://127.0.0.1:8188` (local) or remote URL |
| `COMFYUI_CLIENT_ID` | Arbitrary string; ComfyUI uses it for queue grouping |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ENABLED` | Notifications |
| `WATERMARK_CROP_PX` | Default 100. Pixels stripped from bottom before rescale |
| `IMAGE_MAX_ATTEMPTS` | Default 3. Bound on stage2 retries per clip |
| `VIDEO_MAX_ATTEMPTS` | Default 2. Bound on stage4 retries per clip |
| `POLL_INTERVAL_S` | Default 60. stage5 cadence |

Removed / deprecated:
- ~~`COMFYUI_INPUT_DIR`~~ — no longer used (we POST via `/upload/image`).
- ~~`COMFYUI_OUTPUT_DIR`~~ — no longer used (we GET via `/view`).

If older `.env` files still have those, they're inert — config loader
ignores them.

---

## How to add a new pipeline stage

If you're tempted to add `stage7_…`:

1. Add it to `scripts/` as a self-contained module that does
   `sys.path.insert(0, parent)` and imports `lib.*`.
2. Its main entrypoint must:
   - Take `--run-id` (and any clip filters).
   - Query SQLite for items in `pending` (or `failed` if `--retry`).
   - Update rows on success/failure.
   - Emit `event(stage_name, message, run_id=, clip_id=)` for every meaningful
     transition.
   - Accept `--notify` and call `telegram.send(...)` on summary points.
3. Register it in `pipeline.py`'s `STAGES` dict so `--stages` / `--from-stage`
   pick it up.
4. Add docs to `SCRIPTS.md`.

If the new state needs persistence: add a column to `clips` (or a new table)
in `db/schema.sql`. SQLite migrations: just add `ALTER TABLE … ADD COLUMN …`
notes in this file or a `db/migrations/` folder. Be defensive — the schema
file is meant to be `IF NOT EXISTS` clean for greenfield, plus manual ALTER
commands for existing DBs.

---

## Common gotchas

1. **ComfyUI's `LoadImage` does NOT accept absolute paths.** It looks up
   filenames in `<ComfyUI>/input/`. We sidestep this by POSTing the cropped
   PNG to `/upload/image` and patching the node with the bare filename
   returned by ComfyUI. Don't try to be clever and pass `image=/path/...`.

2. **LTX 2.3 v1.1 has TWO `RandomNoise` nodes** (first-pass + upscale-pass).
   Both need the seed. node_map.json uses the **list form** for `seed`.

3. **Nano Banana 2 free tier is gone.** `gemini-3.x-flash-image-preview` has
   `limit: 0` on the free tier. Options: paid GA, a proxy via
   `GOOGLE_AI_BASE_URL`, or fallback to local ComfyUI image gen with FLUX +
   PuLID (not currently wired).

4. **Watermark crop preserves 9:16 by rescaling.** Cropping alone would
   change aspect ratio and confuse LTX. `lib/image_utils.py::crop_watermark`
   crops bottom N px then resizes back to the original WxH. The tiny vertical
   compression is invisible at video resolution.

5. **stage4 only enqueues `pending`/`failed` clips, never `done`.** To
   regenerate a clip that's already done, use `scripts/redo_clips.py` — it
   resets state and re-chains stages for you. Don't `UPDATE` the DB by hand
   unless you know what you're doing.

6. **The protagonist face reference can bleed into non-protagonist clips.**
   That's why every clip has `use_protagonist_reference: true|false`. B-roll
   and "other speaker" clips set it to false so `stage2_scene_images.py`
   skips passing the face image as multimodal context.

7. **`status_str` from ComfyUI's `/history` can be `error` even on partial
   success.** When it is, we trust it: mark `video_status='failed'`, log the
   error. The poller never tries to be smarter than ComfyUI on this.

8. **WAL mode means there are sidecar files.** `db/state.db`, `state.db-wal`,
   `state.db-shm`. All three need to be backed up together if you want a
   consistent snapshot of an in-flight run. `.gitignore` covers them.

9. **All paths in the DB are project-relative.** `runs.output_dir`,
   `clips.image_path` / `image_cropped_path` / `video_path`,
   `protagonists.reference_image_path` and `guiones.json_path` are stored
   relative to the project root. Never write a raw absolute path into those
   columns — go through `lib/paths.py::rel()`. When reading them back, go
   through `paths.resolve()` (it passes legacy absolute paths through
   untouched, so old rows keep working). This is what lets the whole folder
   be moved without a manual DB fix. `adopt_assets.py` copies any asset that
   lives *outside* the project into `imported_assets/<run_id>/` first, so it
   too gets a stable relative path.

---

## Bug fixes baked in (so you don't repeat them)

- `lib/db.py::connect()` uses `conn.in_transaction` before COMMIT/ROLLBACK.
  `executescript()` auto-commits any open transaction, so a naive COMMIT at
  end of context manager raised `cannot commit - no transaction is active`.
  The check makes the context manager safe for mixed-mode use.

- `scripts/adopt_assets.py::_CLIP_RE = r"clip[\s_\-]*(\d+)"`. Matches names
  like `clip 4.png` (with space), `clip_01.png`, `clip-3.mp4`,
  `MyVideo_clip07_v2.png`. The first version forgot whitespace.

- `lib/comfyui_client.py::collect_video_path` was renamed to
  `find_output_in_history` + separate `download_output()` when we moved from
  filesystem-shared to HTTP-only. If you see legacy code referencing
  `collect_video_path`, it's stale.

- `scripts/redo_clips.py::_backup_videos` inspects the **disk**, not just the
  DB `video_path` column. `_reset_video` nulls `video_path` on reset, so a
  second redo of the same clip would otherwise back up nothing while the
  previous MP4 still sits on disk waiting to be overwritten. `_find_clip_video`
  falls back to the conventional `<output_dir>/videos/clip_NN.<ext>` location.

---

## Running fresh in a new repo

```bash
# 1. clone, cd in
cd ai-reels-pipeline/

# 2. venv + deps
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 3. fill credentials
cp .env.example .env
$EDITOR .env

# 4. init DB
python scripts/init_db.py

# 5. drop your ComfyUI workflow + node_map
#    (export from ComfyUI: Settings → Dev mode → Save API Format)
mv ~/Downloads/workflow_api.json workflows/ltx_2.3_v1.1.json
python scripts/inspect_workflow.py workflows/ltx_2.3_v1.1.json
cp workflows/node_map.example.json workflows/node_map.json
$EDITOR workflows/node_map.json

# 6. smoke-test
python scripts/test_proxy_image.py "una manzana en una mesa"
python -c "from lib import comfyui_client; print('comfyui reachable:', comfyui_client.ping())"

# 7. import a guion + start
python scripts/import_guion.py guion/video2.v1.json --profile preview
# → run_id=1
python scripts/stage1_protagonist.py --video-id video2_decisiones_sin_conocerte --variations 8
# … pick variation, --approve N
python pipeline.py --run-id 1 --notify
```

Full command reference: [SCRIPTS.md](SCRIPTS.md).
