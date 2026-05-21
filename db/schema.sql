-- ============================================================================
-- Aspectados Reels Automation — SQLite state schema
--
-- This DB is the single source of truth for pipeline state. All scripts are
-- idempotent against this state: re-running any stage skips what is already
-- `done` and only acts on `pending` / `failed` rows.
--
-- All file-path columns (json_path, output_dir, image_path,
-- image_cropped_path, video_path, reference_image_path) store paths RELATIVE
-- to the project root, so the folder can be moved without breaking. See
-- lib/paths.py: producers call rel() before writing, consumers call
-- resolve() after reading (resolve() also accepts legacy absolute paths).
--
-- WAL mode is enabled at runtime for safe concurrent reads (poller + status
-- CLI + manual sqlite3 sessions) alongside writer scripts.
-- ============================================================================

-- One row per (video_id, version) of a guion. Allows keeping older versions
-- around when the script is rewritten.
CREATE TABLE IF NOT EXISTS guiones (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id            TEXT    NOT NULL,
    version             INTEGER NOT NULL,
    title               TEXT,
    json_path           TEXT    NOT NULL,
    duration_target_s   INTEGER,
    notes               TEXT,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(video_id, version)
);

-- One "run" = one execution of the full pipeline against a specific guion
-- version, with a specific quality profile (preview / final). Multiple runs
-- per guion version are allowed (e.g. preview first, then final).
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guion_id        INTEGER NOT NULL,
    profile         TEXT    NOT NULL CHECK (profile IN ('preview', 'final')),
    status          TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','completed','failed','cancelled')),
    output_dir      TEXT    NOT NULL,
    notes           TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP,
    FOREIGN KEY(guion_id) REFERENCES guiones(id)
);

-- Protagonist generation. One guion can have N candidate variations; exactly
-- one is later flagged approved=1 and used as face reference for every clip.
CREATE TABLE IF NOT EXISTS protagonists (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    guion_id                INTEGER NOT NULL,
    variation_index         INTEGER NOT NULL,
    prompt                  TEXT    NOT NULL,
    reference_image_path    TEXT,
    seed                    INTEGER,
    status                  TEXT    NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','generating','done','failed')),
    approved                INTEGER NOT NULL DEFAULT 0,
    error                   TEXT,
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at            TIMESTAMP,
    approved_at             TIMESTAMP,
    FOREIGN KEY(guion_id) REFERENCES guiones(id),
    UNIQUE(guion_id, variation_index)
);

-- Per-clip state. Each row has independent image-gen state and video-gen state
-- so the two stages advance independently and can be retried independently.
CREATE TABLE IF NOT EXISTS clips (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                      INTEGER NOT NULL,
    clip_order                  INTEGER NOT NULL,
    section                     TEXT,
    beat                        TEXT,
    dialogue                    TEXT,
    scene_summary               TEXT,
    image_prompt                TEXT NOT NULL,
    video_prompt                TEXT NOT NULL,
    video_prompt_negative       TEXT,
    use_protagonist_reference   INTEGER NOT NULL DEFAULT 1,

    image_status                TEXT NOT NULL DEFAULT 'pending'
                                CHECK (image_status IN ('pending','generating','done','failed')),
    image_path                  TEXT,
    image_cropped_path          TEXT,
    image_seed                  INTEGER,
    image_attempts              INTEGER NOT NULL DEFAULT 0,
    image_error                 TEXT,
    image_completed_at          TIMESTAMP,

    video_status                TEXT NOT NULL DEFAULT 'pending'
                                CHECK (video_status IN ('pending','queued','running','done','failed')),
    video_path                  TEXT,
    comfyui_prompt_id           TEXT,
    video_attempts              INTEGER NOT NULL DEFAULT 0,
    video_started_at            TIMESTAMP,
    video_completed_at          TIMESTAMP,
    video_duration_real_s       REAL,
    video_error                 TEXT,

    FOREIGN KEY(run_id) REFERENCES runs(id),
    UNIQUE(run_id, clip_order)
);

-- Append-only audit log. Useful for debugging long async runs.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    run_id      INTEGER,
    clip_id     INTEGER,
    stage       TEXT,
    level       TEXT NOT NULL DEFAULT 'info'
                CHECK (level IN ('debug','info','warn','error')),
    message     TEXT NOT NULL,
    payload     TEXT
);

-- Outbound notifications log (deduplication + audit).
CREATE TABLE IF NOT EXISTS notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    event_key       TEXT NOT NULL UNIQUE,
    channel         TEXT NOT NULL,
    payload         TEXT,
    delivery_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (delivery_status IN ('pending','sent','failed')),
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_clips_run_status_image ON clips(run_id, image_status);
CREATE INDEX IF NOT EXISTS idx_clips_run_status_video ON clips(run_id, video_status);
CREATE INDEX IF NOT EXISTS idx_clips_comfyui_prompt   ON clips(comfyui_prompt_id);
CREATE INDEX IF NOT EXISTS idx_events_run             ON events(run_id, ts);
