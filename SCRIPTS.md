# SCRIPTS.md — Command reference

Every executable script + every CLI flag + worked examples. For architecture
context read [CLAUDE.md](CLAUDE.md). For a guided tour read [README.md](README.md).

> All commands assume you're in `automation/` with the venv active.

## Table of contents

- [pipeline.py](#pipelinepy) — top-level orchestrator
- [scripts/init_db.py](#scriptsinit_dbpy)
- [scripts/import_guion.py](#scriptsimport_guionpy)
- [scripts/stage1_protagonist.py](#scriptsstage1_protagonistpy)
- [scripts/stage2_scene_images.py](#scriptsstage2_scene_imagespy)
- [scripts/stage3_crop_watermark.py](#scriptsstage3_crop_watermarkpy)
- [scripts/stage4_queue_videos.py](#scriptsstage4_queue_videospy)
- [scripts/stage5_poll_videos.py](#scriptsstage5_poll_videospy)
- [scripts/stage6_compile.py](#scriptsstage6_compilepy)
- [scripts/status.py](#scriptsstatuspy)
- [scripts/adopt_assets.py](#scriptsadopt_assetspy)
- [scripts/redo_clips.py](#scriptsredo_clipspy)
- [scripts/inspect_workflow.py](#scriptsinspect_workflowpy)
- [scripts/test_proxy_image.py](#scriptstest_proxy_imagepy)

---

## pipeline.py

Top-level orchestrator. Chains a chosen subset of stages 2 → 6 in order,
either against a fresh guion JSON or an existing `run_id`.

| Flag | Type | Default | Description |
|---|---|---|---|
| `--guion PATH` | path | — | Import this guion as a **new** run. Mutually exclusive with `--run-id`. |
| `--run-id N` | int | — | Reuse an **existing** run, skipping import. Mutually exclusive with `--guion`. |
| `--profile {preview,final}` | choice | `preview` | Run profile (only used when importing). |
| `--notes TEXT` | str | — | Free-form note saved on the new run row. |
| `--stages X,Y,Z` | csv | — | Exact set of stage numbers to run. Highest priority — overrides `--from-stage`/`--to-stage`. |
| `--from-stage N` | int | 2 | Start at this stage (inclusive). |
| `--to-stage N` | int | 4 (or 6 if `--watch`/`--compile`) | Stop at this stage (inclusive). |
| `--skip-stages X,Y` | csv | — | Subtract these from the final selection. |
| `--watch` | flag | off | Include stage 5 (long-running poller). |
| `--compile` | flag | off | Include stage 6 (ffmpeg concat). |
| `--notify` | flag | off | Pass `--notify` through to every stage that supports it. |

### Examples

```bash
# Submit-and-detach: import + stages 2, 3, 4 (default), with Telegram pings
python pipeline.py --guion guion/video2.v1.json --profile preview --notify

# Resume an existing run, only stages 3 and 4
python pipeline.py --run-id 1 --stages 3,4 --notify

# Resume and run from stage 4 through 6 (stay attached + compile preview)
python pipeline.py --run-id 1 --from-stage 4 --watch --compile --notify

# Skip stage 2 because you already adopted images manually
python pipeline.py --run-id 1 --skip-stages 2 --notify
```

---

## scripts/init_db.py

Initialise / migrate `db/state.db` against `db/schema.sql`. Idempotent —
every `CREATE TABLE` is `IF NOT EXISTS`.

No arguments.

```bash
python scripts/init_db.py
```

Re-run any time after editing `db/schema.sql` to apply new `CREATE` /
`CREATE INDEX` statements. For non-idempotent migrations (e.g. `ALTER TABLE
ADD COLUMN`), run those manually with `sqlite3 db/state.db`.

---

## scripts/import_guion.py

Read a guion JSON, upsert it into `guiones`, create a new `runs` row, and
seed all clip rows in `pending` state.

| Flag | Type | Default | Description |
|---|---|---|---|
| `guion_json` | positional path | — | Path to the guion JSON. |
| `--profile {preview,final}` | choice | `preview` | The run profile to create. |
| `--notes TEXT` | str | — | Free-form note saved on the run row. |

Prints `run_id=N` on the last stdout line — `pipeline.py` parses this.

### Examples

```bash
python scripts/import_guion.py guion/video2.v1.json --profile preview
# → run_id=1

python scripts/import_guion.py guion/video3.v1.json --profile final \
       --notes "final pass after preview QA"
# → run_id=4
```

---

## scripts/stage1_protagonist.py

Two modes selected by flags:

1. **Generate**: produce N face variations of the protagonist prompt (no
   reference image), each with a different random seed. Output PNGs go to
   `outputs/protagonist/guion_<id>/variation_NN_seed<X>.png`.
2. **Approve**: mark one variation `approved=1` so stage2 uses it as face
   reference. Zeroes any previously-approved variation in the same guion.

| Flag | Type | Default | Description |
|---|---|---|---|
| `--video-id ID` | str | — | Resolves to the latest version of that video_id. Use this OR `--guion-id`. |
| `--guion-id N` | int | — | Direct numeric guion_id. |
| `--variations N` | int | 6 | Generate N new variations. Skips indices already `done`. |
| `--approve K` | int | — | Mark variation_index=K as the canonical face reference. |

### Examples

```bash
# Generate 8 candidate faces (idempotent: only new indices are generated)
python scripts/stage1_protagonist.py \
       --video-id video2_decisiones_sin_conocerte --variations 8

# After inspecting outputs/protagonist/guion_*/variation_*.png, approve one
python scripts/stage1_protagonist.py \
       --video-id video2_decisiones_sin_conocerte --approve 3

# Re-approve a different one (zeroes the previous approval automatically)
python scripts/stage1_protagonist.py \
       --video-id video2_decisiones_sin_conocerte --approve 5
```

To regenerate variation indices that were already done, manually delete or
reset them first:

```bash
sqlite3 db/state.db "DELETE FROM protagonists WHERE guion_id=1 AND variation_index >= 3;"
```

---

## scripts/stage2_scene_images.py

Generate the first frame of each clip with Nano Banana 2. Passes the
approved protagonist image as multimodal context **only** when the clip's
`use_protagonist_reference` is true.

| Flag | Type | Default | Description |
|---|---|---|---|
| `--run-id N` | int | **required** | Which run to process. |
| `--notify` | flag | off | Send a Telegram summary after the batch. |

Processes clips where `image_status IN ('pending','failed')` AND
`image_attempts < IMAGE_MAX_ATTEMPTS` (default 3). Done clips are skipped.

### Examples

```bash
# Process all pending images for run 1
python scripts/stage2_scene_images.py --run-id 1 --notify

# Reset a specific clip and regenerate it
sqlite3 db/state.db "UPDATE clips SET image_status='pending', image_attempts=0 \
                     WHERE run_id=1 AND clip_order=7;"
python scripts/stage2_scene_images.py --run-id 1
```

---

## scripts/stage3_crop_watermark.py

Strip the Nano Banana 2 watermark from each clip image: crop the bottom N px
and rescale back to original dimensions (preserves 9:16). Writes the result
to `outputs/<…>/images_cropped/clip_NN_cropped.png` and stores the path in
`image_cropped_path`.

| Flag | Type | Default | Description |
|---|---|---|---|
| `--run-id N` | int | **required** | |
| `--crop-px N` | int | from `.env` (100) | Override `WATERMARK_CROP_PX` for this run. |

Only processes clips with `image_status='done'` AND empty
`image_cropped_path`. Re-running is a no-op once done.

### Examples

```bash
python scripts/stage3_crop_watermark.py --run-id 1

# Use a larger crop because the watermark moved
python scripts/stage3_crop_watermark.py --run-id 1 --crop-px 140
```

---

## scripts/stage4_queue_videos.py

Submit the cropped images + video prompts to ComfyUI's queue. Returns
quickly — does not wait for generation. `stage5_poll_videos.py` handles
completion.

| Flag | Type | Default | Description |
|---|---|---|---|
| `--run-id N` | int | **required** | |
| `--retry` | flag | off | Also re-enqueue clips previously marked `failed`. |
| `--force` | flag | off | Also re-enqueue clips stuck in `queued`/`running` (e.g. after cancelling the ComfyUI queue). Ignores the `VIDEO_MAX_ATTEMPTS` cap. `done` clips are still skipped. |
| `--clips SPEC` | str | — | Filter by clip_order. Syntax: `7` / `3,7,11` / `5-10` / `7-` / `-3`. |
| `--notify` | flag | off | Send a Telegram summary after submission. |

Processes clips where `image_cropped_path IS NOT NULL` AND
`video_status IN ('pending','failed')` AND `video_attempts < VIDEO_MAX_ATTEMPTS`
(default 2), optionally filtered by `--clips`. With `--force` the status set
widens to also include `queued`/`running` and the `video_attempts` cap is
ignored — meant for recovering a run after the ComfyUI queue was cancelled.

### Examples

```bash
# Enqueue all pending clips
python scripts/stage4_queue_videos.py --run-id 1 --notify

# Enqueue only one clip as a smoke test
python scripts/stage4_queue_videos.py --run-id 1 --clips 7 --notify

# Enqueue clips 7 through 16 (the rest of the reel after a successful smoke test)
python scripts/stage4_queue_videos.py --run-id 1 --clips 7- --notify

# Cherry-pick clips
python scripts/stage4_queue_videos.py --run-id 1 --clips 3,7,11 --notify

# Re-enqueue clips that previously failed
python scripts/stage4_queue_videos.py --run-id 1 --retry --notify

# Recover a run after cancelling the ComfyUI queue: re-enqueue clips
# left stranded in 'queued'/'running'
python scripts/stage4_queue_videos.py --run-id 1 --force --notify
```

---

## scripts/stage5_poll_videos.py

Long-running daemon. Polls ComfyUI's `/queue` and `/history` endpoints,
downloads finished MP4s via `/view` into `outputs/<…>/videos/`, marks each
clip `done` or `failed` accordingly. Sends per-clip Telegram pings on
terminal transitions.

| Flag | Type | Default | Description |
|---|---|---|---|
| `--run-id N` | int | **required** | |
| `--interval N` | int | `POLL_INTERVAL_S` (60) | Seconds between poll cycles. |
| `--once` | flag | off | Single reconciliation pass, then exit (useful for cron). |
| `--notify` | flag | off | Telegram pings per clip + final run summary. |

Survives SIGINT / SIGTERM cleanly — finishes the current cycle then exits.
Restarting picks up where it left off because state is in SQLite.

### Examples

```bash
# Foreground (Ctrl+C to stop)
python scripts/stage5_poll_videos.py --run-id 1 --notify

# Background daemon (the usual pattern for 12-hour reels)
nohup python scripts/stage5_poll_videos.py --run-id 1 --notify &> stage5.log &
echo $! > stage5.pid

# Stop the daemon cleanly
kill $(cat stage5.pid)

# One-shot pass for cron
* * * * * cd /path/to/automation && \
  ./venv/bin/python scripts/stage5_poll_videos.py --run-id 1 --once --notify
```

---

## scripts/stage6_compile.py

Concatenate finished clip MP4s into `preview.mp4` using `ffmpeg -f concat`.
Requires `ffmpeg` in `PATH` (`brew install ffmpeg`).

| Flag | Type | Default | Description |
|---|---|---|---|
| `--run-id N` | int | **required** | |
| `--output PATH` | path | `<run_output_dir>/preview.mp4` | Override output path. |
| `--allow-partial` | flag | off | Concatenate even if some clips are not yet done. |
| `--notify` | flag | off | Telegram ping with output path. |

Fails fast if any clip is not done (and `--allow-partial` not set).

### Examples

```bash
# Standard usage when stage5 has finished
python scripts/stage6_compile.py --run-id 1 --notify

# Compile a preview-so-far while some clips are still rendering
python scripts/stage6_compile.py --run-id 1 --allow-partial \
       --output outputs/preview_partial.mp4
```

---

## scripts/status.py

Rich-printed dashboard showing per-clip state for a given run. Read-only.

| Flag | Type | Default | Description |
|---|---|---|---|
| `--run-id N` | int | latest run | Which run to display. |
| `--watch` | flag | off | Auto-refresh every 5s. |

### Examples

```bash
# Snapshot of the latest run
python scripts/status.py

# Watch a specific run live (great in a second terminal during stage5)
python scripts/status.py --run-id 1 --watch
```

Sample output (truncated):
```
Run 1 — video2_decisiones_sin_conocerte v1 (preview)
┃ #  ┃ section ┃ beat ┃ image    ┃ video    ┃ prompt_id        ┃
┃ 01 ┃ HOOK    ┃ 1/4  ┃ done (1) ┃ done (1) ┃ a1b2c3d4-…       ┃
┃ 02 ┃ HOOK    ┃ 2/4  ┃ done (1) ┃ running  ┃ e5f6g7h8-…       ┃
…
```

---

## scripts/adopt_assets.py

Register pre-existing PNG / MP4 files in the DB as `done` so subsequent
stages skip them. Useful when you generated some assets manually outside the
pipeline (web UI, prior session, etc.).

Where files land: assets already inside the project tree are registered in
place; assets from **outside** the project are first copied into
`imported_assets/<run_id>/{images,images_cropped,videos}/` so the DB only
ever stores project-relative paths (and a folder move never breaks them).
The original external file is left untouched.

Filename detection: each file must contain `clip` + a number (case-insensitive,
optional whitespace/underscore/hyphen separator).
Examples that match: `clip_01.png`, `clip 4.png`, `Aspectados_clip07_v2.png`.

| Flag | Type | Default | Description |
|---|---|---|---|
| `--run-id N` | int | **required** | |
| `--images-raw DIR` | path | — | Raw NB2 images (still need stage3 cropping). |
| `--images-cropped DIR` | path | — | Already-cropped images (stage3 will skip them). |
| `--videos DIR` | path | — | Finished MP4 clips (stages 4 and 5 will skip them). |
| `--auto` | flag | off | Auto-discover inside the run's `output_dir/{images,images_cropped,videos}/`. |
| `--dry-run` | flag | off | Show matches without touching the DB. |

### Examples

```bash
# Adopt raw images from an external location
python scripts/adopt_assets.py --run-id 2 \
       --images-raw /Users/andres/Downloads/old_images/

# Adopt all three directories under the run's output_dir
python scripts/adopt_assets.py --run-id 2 --auto

# Dry-run first (always recommended before adopting)
python scripts/adopt_assets.py --run-id 2 --auto --dry-run

# Adopt videos + already-cropped images in one go
python scripts/adopt_assets.py --run-id 2 \
       --images-cropped outputs/.../images_cropped/ \
       --videos outputs/.../videos/
```

---

## scripts/redo_clips.py

Reset chosen clips' state and (optionally) re-chain stage2 → stage3 → stage4
to regenerate them.

| Flag | Type | Default | Description |
|---|---|---|---|
| `--run-id N` | int | **required** | |
| `--clips SPEC` | str | **required** | Same syntax as stage4: `7` / `3,7,11` / `5-10` / `7-` / `-3`. |
| `--scope X[,Y]` | csv | `video` | What to reset: `video` (keeps image), `image,video`, or `all` (shorthand). |
| `--backup` | flag | off | Copy existing MP4s to `_backup/<timestamp>/` before reset. |
| `--no-requeue` | flag | off | Only reset state; don't run stage2/3/4. |
| `--notify` | flag | off | Pass `--notify` to chained stages. |
| `--dry-run` | flag | off | Preview without changes. |

### Examples

```bash
# Re-render the video for clips 3, 7, 11 (keeping their existing images)
python scripts/redo_clips.py --run-id 1 --clips 3,7,11 --backup --notify

# Re-render image AND video for clip 7 (e.g. bad first frame)
python scripts/redo_clips.py --run-id 1 --clips 7 --scope image,video --notify

# Reset clips 5-10 without enqueueing yet (you'll run stage4 manually later)
python scripts/redo_clips.py --run-id 1 --clips 5-10 --no-requeue

# Dry-run to see what would happen
python scripts/redo_clips.py --run-id 1 --clips 3,7,11 --backup --dry-run
```

The chained stage4 call uses the same `--clips` filter — only the resetted
clips get re-queued, not the entire run.

---

## scripts/inspect_workflow.py

Print every node in an exported ComfyUI API-format workflow. Use this when
filling in `workflows/node_map.json` from a new workflow.

| Flag | Type | Default | Description |
|---|---|---|---|
| `workflow` | positional path | — | Path to the workflow JSON. |
| `--full` | flag | off | Show every input of every node (default: only the interesting keys). |
| `--filter STRING` | str | — | Only show nodes whose class_type contains this substring. |

### Examples

```bash
# Quick overview
python scripts/inspect_workflow.py workflows/ltx_2.3_v1.1.json

# Find the positive vs negative CLIPTextEncode
python scripts/inspect_workflow.py workflows/ltx_2.3_v1.1.json --filter CLIPTextEncode

# Find the seed node (could be KSampler, RandomNoise, …)
python scripts/inspect_workflow.py workflows/ltx_2.3_v1.1.json --filter Sampler
python scripts/inspect_workflow.py workflows/ltx_2.3_v1.1.json --filter RandomNoise

# Find the SaveVideo / VHS_VideoCombine for the filename_prefix
python scripts/inspect_workflow.py workflows/ltx_2.3_v1.1.json --filter Save
python scripts/inspect_workflow.py workflows/ltx_2.3_v1.1.json --filter VHS

# Everything, all inputs
python scripts/inspect_workflow.py workflows/ltx_2.3_v1.1.json --full
```

The output table shows `id`, `class_type`, `title`, and the most important
input keys. For each row, note:
- If the input you need is keyed `seed` → use shorthand in node_map.json.
- If it's keyed `noise_seed` → use the explicit form: `{"node_id":"X","input":"noise_seed"}`.
- Two RandomNoise nodes for LTX 2.3 → list form (both get the same seed).

---

## scripts/test_proxy_image.py

Standalone smoke-test for an OpenAI-compatible image generation proxy. Does
not import the pipeline modules; runs in isolation.

| Flag | Type | Default | Description |
|---|---|---|---|
| `prompt` | positional str | "apple on a table" | Prompt to send. |
| `--method {chat-stream,chat-nostream,images,all}` | choice | `all` | Which endpoint(s) to try. |
| `--timeout N` | float | 300 | Read timeout in seconds. |
| `--connect-timeout N` | float | 10 | Connect timeout in seconds. |
| `--out PATH` | path | `test_proxy.png` | Output file. |

Reads credentials from `.env` (`GOOGLE_AI_BASE_URL`,
`GOOGLE_AI_STUDIO_API_KEY`, `GEMINI_IMAGE_MODEL`) or `PROXY_*` env overrides.

### Methods

| Method | Endpoint | When useful |
|---|---|---|
| `chat-stream` | `POST /chat/completions` with `stream:true` (SSE parsing) | Nano Banana via Chat Completions — the typical free-Gemini-on-a-proxy path. |
| `chat-nostream` | `POST /chat/completions` with `stream:false` | Older proxies that return one JSON body. |
| `images` | `POST /images/generations` | DALL-E / Imagen / gpt-image-style proxies. |
| `all` (default) | All three in order, stops at first success | Auto-discovery. |

### Examples

```bash
# Try every endpoint until one works
python scripts/test_proxy_image.py "una manzana roja en una mesa"

# Just streaming chat completions (most likely for Nano Banana)
python scripts/test_proxy_image.py "una manzana" --method chat-stream --timeout 60

# Just the Images API (Imagen / gpt-image)
PROXY_IMAGE_MODEL=gemini/imagen-4.0-fast-generate-001 \
  python scripts/test_proxy_image.py "una manzana" --method images --timeout 60

# Override credentials without touching .env
PROXY_BASE_URL=http://localhost:20130/v1 \
PROXY_API_KEY=$SOME_OTHER_KEY \
PROXY_IMAGE_MODEL=gemini/nano-banana-pro-preview \
  python scripts/test_proxy_image.py "test"

# Short timeout while debugging a hang
python scripts/test_proxy_image.py "test" --method chat-stream --timeout 15
```

Output saved to `test_proxy.png` on success. On failure prints the
content-type, status code, and a body preview so you can diagnose.

---

## Common workflows

### Bring up a fresh video from a guion JSON

```bash
# 1. Import
python scripts/import_guion.py guion/video2.v1.json --profile preview
# → run_id=1

# 2. Generate + approve protagonist
python scripts/stage1_protagonist.py --video-id video2_decisiones_sin_conocerte --variations 8
# … look at outputs/protagonist/guion_1/, pick a good one
python scripts/stage1_protagonist.py --video-id video2_decisiones_sin_conocerte --approve 3

# 3. Stages 2-4 (submit-and-detach), then daemon
python pipeline.py --run-id 1 --notify
nohup python scripts/stage5_poll_videos.py --run-id 1 --notify &> stage5.log &

# 4. Watch
python scripts/status.py --run-id 1 --watch

# 5. When all clips done
python scripts/stage6_compile.py --run-id 1 --notify
```

### Resume mid-pipeline after adopting external assets

```bash
python scripts/import_guion.py guion/video3.v1.json --profile preview
# → run_id=2

# Bring in PNGs you generated by hand in another tool
python scripts/adopt_assets.py --run-id 2 --images-raw ~/Downloads/v3_images/ --dry-run
python scripts/adopt_assets.py --run-id 2 --images-raw ~/Downloads/v3_images/

# Skip stage 2; run cropping + queue + watch + compile
python pipeline.py --run-id 2 --skip-stages 2 --watch --compile --notify
```

### Redo a few clips after QA pass

```bash
# Reset video state for clips 3, 7, 11; backup old MP4s; re-enqueue
python scripts/redo_clips.py --run-id 1 --clips 3,7,11 --backup --notify

# Make sure the poller is still running (it should be)
ps aux | grep stage5_poll_videos | grep -v grep
```

### Probe a remote ComfyUI without running anything else

```bash
python3 -c "from lib import comfyui_client as c; print('reachable:', c.ping())"
curl -sS $COMFYUI_HOST/queue | python3 -m json.tool
curl -sS $COMFYUI_HOST/history | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print(f'{len(d)} prompts in history')"
```
